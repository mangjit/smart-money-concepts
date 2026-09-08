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

        if self._settings.twelve_data_api_key:
            result = await self._twelve_data_candles(symbol=symbol, market=market, timeframe=timeframe, limit=limit)
        elif market is Market.CRYPTO:
            result = await self._binance_candles(symbol=symbol, timeframe=timeframe, limit=limit)
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
        except (httpx.HTTPError, ValueError) as error:
            raise MarketDataUnavailable(f"Binance candles are unavailable for {symbol}: {error}") from error

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
        # The final kline is normally in progress. Never create a signal from it.
        return self._closed_result(candles, source="Binance public market data", limit=limit)

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
