"""Server-side market-data adapters with a Twelve Data primary feed.

When TWELVE_DATA_API_KEY is configured, Twelve Data supplies chart/analysis candles
for Forex and Crypto. The fallback public feeds remain available only for local setups
without a Twelve Data key. The browser never receives a provider key, and an unavailable
provider raises an explicit error rather than returning synthetic price data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic, time
from typing import Final

import httpx

from .config import Settings
from .models import Candle, Market


class MarketDataUnavailable(RuntimeError):
    """Raised when a public provider cannot return trustworthy closed candles."""


_BINANCE_INTERVALS: Final[dict[str, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}
_YAHOO_INTERVALS: Final[dict[str, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "60m",
    "4h": "1h",  # Yahoo has no universal 4h endpoint; client aggregates 1h bars.
    "1d": "1d",
}
_TWELVE_DATA_INTERVALS: Final[dict[str, str]] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day",
}
_COMMON_CRYPTO_QUOTES: Final[tuple[str, ...]] = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH")
_BYBIT_INTERVALS: Final[dict[str, str]] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}
_KRAKEN_INTERVALS: Final[dict[str, int]] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}
_COINBASE_GRANULARITIES: Final[dict[str, int]] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


@dataclass(frozen=True)
class MarketDataResult:
    candles: list[Candle]
    source: str


class MarketDataService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            headers={"User-Agent": "smart-money-concepts-webui/0.1 (research dashboard)"},
        )
        self._cache: dict[tuple[str, Market, str, int], tuple[float, MarketDataResult]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def candles(self, *, symbol: str, market: Market, timeframe: str, limit: int) -> MarketDataResult:
        key = (symbol, market, timeframe, limit)
        cached = self._cache.get(key)
        if cached and monotonic() - cached[0] < self._settings.market_cache_seconds:
            return cached[1]

        if market is Market.FUTURES:
            # Crypto perpetual futures use Bybit's public linear-market endpoint. Twelve
            # Data spot pair syntax is intentionally not treated as a futures contract.
            result = await self._bybit_candles(symbol=symbol, timeframe=timeframe, limit=limit, category="linear")
        elif self._settings.twelve_data_api_key:
            result = await self._twelve_data_candles(symbol=symbol, market=market, timeframe=timeframe, limit=limit)
        elif market is Market.CRYPTO:
            result = await self._crypto_spot_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        else:
            result = await self._yahoo_forex_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        self._cache[key] = (monotonic(), result)
        return result

    async def _twelve_data_candles(self, *, symbol: str, market: Market, timeframe: str, limit: int) -> MarketDataResult:
        interval = _TWELVE_DATA_INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataUnavailable(f"Unsupported Twelve Data timeframe: {timeframe}")
        twelve_symbol = self._normalize_twelve_data_symbol(symbol, market)
        try:
            response = await self._client.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol": twelve_symbol,
                    "interval": interval,
                    "outputsize": min(limit + 1, 5000),
                    "timezone": "UTC",
                    "order": "asc",
                    "apikey": self._settings.twelve_data_api_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            # HTTPX includes the full request URL in its exception text; that URL has the API key.
            raise MarketDataUnavailable(f"Twelve Data returned HTTP {error.response.status_code} for {twelve_symbol}") from error
        except httpx.HTTPError as error:
            raise MarketDataUnavailable(f"Twelve Data request could not be completed for {twelve_symbol}") from error
        except ValueError as error:
            raise MarketDataUnavailable(f"Twelve Data did not return valid JSON for {twelve_symbol}") from error

        if not isinstance(payload, dict):
            raise MarketDataUnavailable(f"Twelve Data returned an invalid response for {twelve_symbol}")
        if payload.get("status") == "error" or payload.get("code"):
            message = payload.get("message") or payload.get("status") or "unknown Twelve Data error"
            raise MarketDataUnavailable(f"Twelve Data rejected {twelve_symbol}: {message}")
        values = payload.get("values")
        if not isinstance(values, list):
            raise MarketDataUnavailable(f"Twelve Data returned no candle values for {twelve_symbol}")
        try:
            candles = sorted(
                [
                    Candle(
                        timestamp=self._twelve_timestamp(item["datetime"]),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=float(item.get("volume") or 0),
                    )
                    for item in values
                ],
                key=lambda candle: candle.timestamp,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MarketDataUnavailable(f"Twelve Data returned malformed candles for {twelve_symbol}") from error
        return self._closed_result(candles, source="Twelve Data market data", limit=limit)

    async def _crypto_spot_candles(self, *, symbol: str, timeframe: str, limit: int) -> MarketDataResult:
        """Use multiple public exchanges only when Twelve Data is not configured.

        Kraken is tried first because Binance can return HTTP 451 on restricted
        networks. Coinbase, Bybit, and Binance remain best-effort fallbacks for pair
        coverage. The selected provider is always named in the response; no cross-pair
        price is substituted and no synthetic bar is created.
        """
        failures: list[str] = []
        for provider in (self._kraken_candles, self._coinbase_candles, self._bybit_spot_candles, self._binance_candles):
            try:
                return await provider(symbol=symbol, timeframe=timeframe, limit=limit)
            except MarketDataUnavailable as error:
                failures.append(str(error))
        raise MarketDataUnavailable(
            f"Crypto candles are unavailable from Kraken, Coinbase, Bybit, and Binance for {symbol}. "
            f"{' '.join(failures)} Configure TWELVE_DATA_API_KEY for the primary multi-market feed."
        )

    async def _bybit_spot_candles(self, *, symbol: str, timeframe: str, limit: int) -> MarketDataResult:
        return await self._bybit_candles(symbol=symbol, timeframe=timeframe, limit=limit, category="spot")

    async def _kraken_candles(self, *, symbol: str, timeframe: str, limit: int) -> MarketDataResult:
        interval = _KRAKEN_INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataUnavailable(f"Unsupported Kraken timeframe: {timeframe}")
        kraken_pair = self._normalize_kraken_symbol(symbol)
        try:
            response = await self._client.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": kraken_pair, "interval": interval},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise MarketDataUnavailable(f"Kraken returned HTTP {error.response.status_code} for {symbol}") from error
        except httpx.HTTPError as error:
            raise MarketDataUnavailable(f"Kraken request could not be completed for {symbol}") from error
        except ValueError as error:
            raise MarketDataUnavailable(f"Kraken did not return valid JSON for {symbol}") from error

        errors = payload.get("error") if isinstance(payload, dict) else None
        result = payload.get("result") if isinstance(payload, dict) else None
        if errors or not isinstance(result, dict):
            detail = "; ".join(str(item) for item in errors) if isinstance(errors, list) else "invalid response"
            raise MarketDataUnavailable(f"Kraken rejected {symbol}: {detail}")
        rows = next((value for key, value in result.items() if key != "last" and isinstance(value, list)), None)
        if not isinstance(rows, list):
            raise MarketDataUnavailable(f"Kraken returned no candles for {symbol}")
        try:
            candles = [
                Candle(
                    timestamp=int(row[0]) * 1000,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[6]),
                )
                for row in rows
            ]
        except (IndexError, TypeError, ValueError) as error:
            raise MarketDataUnavailable(f"Kraken returned malformed candles for {symbol}") from error
        return self._closed_result(candles, source="Kraken public spot market data", limit=limit)

    async def _coinbase_candles(self, *, symbol: str, timeframe: str, limit: int) -> MarketDataResult:
        granularity = _COINBASE_GRANULARITIES.get(timeframe)
        if granularity is None:
            raise MarketDataUnavailable(f"Unsupported Coinbase timeframe: {timeframe}")
        product = self._normalize_coinbase_symbol(symbol)
        try:
            response = await self._client.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={"granularity": granularity},
            )
            response.raise_for_status()
            rows = response.json()
        except httpx.HTTPStatusError as error:
            raise MarketDataUnavailable(f"Coinbase returned HTTP {error.response.status_code} for {symbol}") from error
        except httpx.HTTPError as error:
            raise MarketDataUnavailable(f"Coinbase request could not be completed for {symbol}") from error
        except ValueError as error:
            raise MarketDataUnavailable(f"Coinbase did not return valid candles for {symbol}") from error

        if not isinstance(rows, list):
            raise MarketDataUnavailable(f"Coinbase returned an invalid candle response for {symbol}")
        try:
            candles = sorted(
                [
                    Candle(
                        timestamp=int(row[0]) * 1000,
                        low=float(row[1]),
                        high=float(row[2]),
                        open=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                    for row in rows
                ],
                key=lambda candle: candle.timestamp,
            )
        except (IndexError, TypeError, ValueError) as error:
            raise MarketDataUnavailable(f"Coinbase returned malformed candles for {symbol}") from error
        return self._closed_result(candles, source="Coinbase public spot market data", limit=limit)

    async def _binance_candles(self, *, symbol: str, timeframe: str, limit: int) -> MarketDataResult:
        interval = _BINANCE_INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataUnavailable(f"Unsupported crypto timeframe: {timeframe}")
        try:
            response = await self._client.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": min(limit + 1, 1000)},
            )
            response.raise_for_status()
            rows = response.json()
        except httpx.HTTPStatusError as error:
            raise MarketDataUnavailable(f"Binance returned HTTP {error.response.status_code} for {symbol}") from error
        except httpx.HTTPError as error:
            raise MarketDataUnavailable(f"Binance request could not be completed for {symbol}") from error
        except ValueError as error:
            raise MarketDataUnavailable(f"Binance did not return valid candles for {symbol}") from error

        if not isinstance(rows, list):
            raise MarketDataUnavailable(f"Binance returned an invalid candle response for {symbol}")
        try:
            candles = [
                Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
                for row in rows
            ]
        except (IndexError, TypeError, ValueError) as error:
            raise MarketDataUnavailable(f"Binance returned malformed candles for {symbol}") from error
        # The final kline is normally in progress. Never create a signal from it.
        return self._closed_result(candles, source="Binance public market data", limit=limit)

    async def _bybit_candles(self, *, symbol: str, timeframe: str, limit: int, category: str) -> MarketDataResult:
        interval = _BYBIT_INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataUnavailable(f"Unsupported Bybit timeframe: {timeframe}")
        normalized_symbol = symbol.upper().replace("/", "").replace("-", "").replace("_", "").removesuffix(".P")
        if not normalized_symbol.isalnum() or len(normalized_symbol) < 5:
            raise MarketDataUnavailable("Bybit symbols must look like BTCUSDT or ETHUSDT")
        try:
            response = await self._client.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": category, "symbol": normalized_symbol, "interval": interval, "limit": min(limit + 1, 1000)},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise MarketDataUnavailable(f"Bybit returned HTTP {error.response.status_code} for {normalized_symbol}") from error
        except httpx.HTTPError as error:
            raise MarketDataUnavailable(f"Bybit request could not be completed for {normalized_symbol}") from error
        except ValueError as error:
            raise MarketDataUnavailable(f"Bybit did not return valid JSON for {normalized_symbol}") from error

        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list) or payload.get("retCode") not in {0, "0", None}:
            message = payload.get("retMsg") if isinstance(payload, dict) else "invalid response"
            raise MarketDataUnavailable(f"Bybit rejected {normalized_symbol}: {message}")
        try:
            candles = sorted(
                [
                    Candle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                    for row in rows
                ],
                key=lambda candle: candle.timestamp,
            )
        except (IndexError, TypeError, ValueError) as error:
            raise MarketDataUnavailable(f"Bybit returned malformed candles for {normalized_symbol}") from error
        market_name = "Bybit public futures data" if category == "linear" else "Bybit public spot market data"
        return self._closed_result(candles, source=market_name, limit=limit)

    async def _yahoo_forex_candles(self, *, symbol: str, timeframe: str, limit: int) -> MarketDataResult:
        interval = _YAHOO_INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataUnavailable(f"Unsupported forex timeframe: {timeframe}")
        yahoo_symbol = self._normalize_yahoo_forex_symbol(symbol)
        # Yahoo limits 1-minute history most aggressively. These ranges leave room for
        # the closed-bar drop while staying within typical public-endpoint limits.
        range_value = "7d" if timeframe == "1m" else "60d" if timeframe in {"5m", "15m", "1h", "4h"} else "2y"
        try:
            response = await self._client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}",
                params={"interval": interval, "range": range_value, "includePrePost": "false"},
            )
            response.raise_for_status()
            payload = response.json()["chart"]["result"][0]
            quote = payload["indicators"]["quote"][0]
            timestamps = payload["timestamp"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise MarketDataUnavailable(f"Yahoo Finance candles are unavailable for {symbol}: {error}") from error

        raw = [
            Candle(
                timestamp=int(timestamp) * 1000,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume or 0),
            )
            for timestamp, open_, high, low, close, volume in zip(
                timestamps,
                quote["open"],
                quote["high"],
                quote["low"],
                quote["close"],
                quote.get("volume", [0] * len(timestamps)),
            )
            if None not in (open_, high, low, close)
        ]
        if timeframe == "4h":
            raw = self._aggregate_four_hours(raw)
        return self._closed_result(raw, source="Yahoo Finance public market data", limit=limit)

    @staticmethod
    def _twelve_timestamp(value: str) -> int:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)

    @staticmethod
    def _normalize_twelve_data_symbol(symbol: str, market: Market) -> str:
        cleaned = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        if market is Market.FOREX:
            if len(cleaned) == 6 and cleaned.isalpha():
                return f"{cleaned[:3]}/{cleaned[3:]}"
            raise MarketDataUnavailable("Twelve Data Forex symbols must be a six-letter pair, for example EURUSD")
        for quote in _COMMON_CRYPTO_QUOTES:
            if cleaned.endswith(quote) and len(cleaned) > len(quote):
                return f"{cleaned[:-len(quote)]}/{quote}"
        raise MarketDataUnavailable("Twelve Data Crypto symbols must include a supported quote, for example BTCUSDT or ETHUSD")

    @staticmethod
    def _normalize_kraken_symbol(symbol: str) -> str:
        cleaned = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        for quote in _COMMON_CRYPTO_QUOTES:
            if cleaned.endswith(quote) and len(cleaned) > len(quote):
                base = cleaned[: -len(quote)]
                # Kraken's REST pair names use XBT and XDG for these legacy symbols.
                base = {"BTC": "XBT", "DOGE": "XDG"}.get(base, base)
                return f"{base}{quote}"
        raise MarketDataUnavailable("Kraken symbols must include a supported quote, for example BTCUSDT or ETHUSD")

    @staticmethod
    def _normalize_coinbase_symbol(symbol: str) -> str:
        cleaned = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        for quote in _COMMON_CRYPTO_QUOTES:
            if cleaned.endswith(quote) and len(cleaned) > len(quote):
                return f"{cleaned[: -len(quote)]}-{quote}"
        raise MarketDataUnavailable("Coinbase symbols must include a supported quote, for example BTCUSD or ETHUSDT")

    @staticmethod
    def _normalize_yahoo_forex_symbol(symbol: str) -> str:
        cleaned = symbol.upper().replace("=X", "")
        if len(cleaned) == 6 and cleaned.isalpha():
            return f"{cleaned}=X"
        raise MarketDataUnavailable("Forex symbols must be a six-letter pair, for example EURUSD or GBPJPY")

    @staticmethod
    def _aggregate_four_hours(candles: list[Candle]) -> list[Candle]:
        buckets: dict[int, list[Candle]] = {}
        four_hours_ms = 4 * 60 * 60 * 1000
        for candle in candles:
            bucket = candle.timestamp - (candle.timestamp % four_hours_ms)
            buckets.setdefault(bucket, []).append(candle)
        return [
            Candle(
                timestamp=bucket,
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                volume=sum(candle.volume for candle in group),
            )
            for bucket, group in sorted(buckets.items())
        ]

    @staticmethod
    def _closed_result(candles: list[Candle], *, source: str, limit: int) -> MarketDataResult:
        if len(candles) < 2:
            raise MarketDataUnavailable("Provider returned too few candles")
        # Remove the newest provider candle even if it appears complete. This conservative
        # choice makes the dashboard and Telegram bot bar-close only.
        closed = candles[:-1][-limit:]
        if len(closed) < 80:
            raise MarketDataUnavailable("Provider returned fewer than 80 closed candles")
        if closed[-1].timestamp > int(time() * 1000):
            raise MarketDataUnavailable("Provider returned an invalid future candle")
        return MarketDataResult(candles=closed, source=source)
