"""Server-side public market-data adapters.

No browser API keys are required. Crypto uses Binance's public kline endpoint; forex
uses Yahoo Finance's public chart endpoint. Both providers can be unavailable or have
symbol/interval limitations, so callers receive an explicit error instead of invented
or synthetic price data.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import time
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


@dataclass(frozen=True)
class MarketDataResult:
    candles: list[Candle]
    source: str


class MarketDataService:
    def __init__(self, settings: Settings):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            headers={"User-Agent": "smart-money-concepts-webui/0.1 (research dashboard)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def candles(self, *, symbol: str, market: Market, timeframe: str, limit: int) -> MarketDataResult:
        if market is Market.CRYPTO:
            return await self._binance_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        return await self._yahoo_forex_candles(symbol=symbol, timeframe=timeframe, limit=limit)

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
