"""Pydantic contracts shared by the WebUI, Telegram adapter, and services."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Market(str, Enum):
    CRYPTO = "crypto"
    FOREX = "forex"


class OandaEnvironment(str, Enum):
    PRACTICE = "practice"
    LIVE = "live"


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_SIGNAL = "NO_SIGNAL"


class Candle(BaseModel):
    timestamp: int = Field(description="Unix time in milliseconds (UTC)")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @field_validator("high")
    @classmethod
    def high_must_cover_open(cls, high: float, info):
        values = info.data
        if "open" in values and high < values["open"]:
            raise ValueError("high cannot be lower than open")
        return high


class CandleResponse(BaseModel):
    symbol: str
    market: Market
    timeframe: str
    source: str
    candles: list[Candle]


class SignalRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=24, examples=["BTCUSDT"])
    market: Market = Market.CRYPTO
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1h"
    limit: int = Field(default=300, ge=80, le=1000)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, symbol: str) -> str:
        return symbol.strip().upper().replace("/", "").replace("-", "")


class TechnicalSnapshot(BaseModel):
    close: float
    atr: float
    ema_fast: float
    ema_slow: float
    rsi: float
    volume_ratio: float | None = None
    last_swing_high: float | None = None
    last_swing_low: float | None = None
    bullish_fvg: bool = False
    bearish_fvg: bool = False
    bullish_structure_break: bool = False
    bearish_structure_break: bool = False


class SignalResponse(BaseModel):
    action: SignalAction
    symbol: str
    market: Market
    timeframe: str
    generated_at: int = Field(description="Unix time in milliseconds (UTC)")
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    confidence: int = Field(ge=0, le=100)
    technicals: TechnicalSnapshot
    reasoning: list[str] = Field(min_length=10, max_length=15)
    exit_plan: list[str] = Field(min_length=2, max_length=4)
    risk_notice: str
    data_source: str


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    model_id: str = Field(default="rule-based", max_length=64)
    symbol: str | None = Field(default=None, max_length=24)
    market: Market = Market.CRYPTO
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1h"

    @field_validator("symbol")
    @classmethod
    def normalize_optional_symbol(cls, symbol: str | None) -> str | None:
        return symbol.strip().upper().replace("/", "").replace("-", "") if symbol else None


class ChatResponse(BaseModel):
    session_id: str
    model_id: str
    reply: str
    memory_enabled: bool
    signal: SignalResponse | None = None


class ModelStatus(BaseModel):
    id: str
    label: str
    configured: bool
    capability: str


class OandaQuote(BaseModel):
    instrument: str
    timestamp: str
    bid: float
    ask: float
    closeout_bid: float | None = None
    closeout_ask: float | None = None


class OandaPosition(BaseModel):
    instrument: str
    long_units: float
    short_units: float
    unrealized_pl: float


class OandaTrade(BaseModel):
    id: str
    instrument: str
    current_units: float
    price: float
    unrealized_pl: float


class OandaPendingOrder(BaseModel):
    id: str
    instrument: str
    order_type: str
    units: float
    price: float | None = None


class OandaAccountSnapshot(BaseModel):
    environment: OandaEnvironment
    currency: str
    balance: float
    nav: float
    margin_available: float
    margin_used: float
    unrealized_pl: float
    open_trade_count: int
    open_position_count: int
    pending_order_count: int
    positions: list[OandaPosition]
    open_trades: list[OandaTrade]
    pending_orders: list[OandaPendingOrder]
    quote: OandaQuote | None = None
    read_only: bool = True


class OandaConfiguration(BaseModel):
    practice_configured: bool
    live_configured: bool
    access_protected: bool
    read_only: bool = True


class HealthResponse(BaseModel):
    status: str
    memory: Literal["mongo", "in-memory"]
    telegram_configured: bool
    twelve_data_configured: bool
    oanda: OandaConfiguration
