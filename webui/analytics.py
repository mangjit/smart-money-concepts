"""Causal, deterministic technical analysis and risk-controlled signal generation.

The functions here intentionally do not call an LLM. Price levels, risk/reward and
signal action remain auditable and reproducible even if a language model is offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import fmean
from typing import Iterable

from .models import Candle, Market, SignalAction, SignalResponse, TechnicalSnapshot


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float


def _ema(values: list[float], length: int) -> float:
    """Return a stable EMA value; requires at least ``length`` observations."""
    if len(values) < length:
        raise ValueError(f"need at least {length} candles to calculate EMA")
    multiplier = 2.0 / (length + 1)
    value = fmean(values[:length])
    for price in values[length:]:
        value = (price - value) * multiplier + value
    return value


def _atr(candles: list[Candle], length: int = 14) -> float:
    """Wilder ATR calculated only from supplied, closed OHLC candles."""
    if len(candles) < length + 1:
        raise ValueError(f"need at least {length + 1} candles to calculate ATR")
    true_ranges: list[float] = []
    for index in range(1, len(candles)):
        current, previous = candles[index], candles[index - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr = fmean(true_ranges[:length])
    for true_range in true_ranges[length:]:
        atr = ((atr * (length - 1)) + true_range) / length
    return atr


def _rsi(closes: list[float], length: int = 14) -> float:
    if len(closes) < length + 1:
        raise ValueError(f"need at least {length + 1} candles to calculate RSI")
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = fmean(gains[:length])
    average_loss = fmean(losses[:length])
    for gain, loss in zip(gains[length:], losses[length:]):
        average_gain = ((average_gain * (length - 1)) + gain) / length
        average_loss = ((average_loss * (length - 1)) + loss) / length
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _confirmed_pivots(candles: list[Candle], side: str, length: int = 5) -> list[Pivot]:
    """Return pivots known after ``length`` right-side candles.

    The most recent ``length`` candles are deliberately excluded because their
    pivot status is not confirmed. This avoids the source repository's backfill
    problem when the output is used in a live or replay workflow.
    """
    if len(candles) < 2 * length + 1:
        return []
    pivots: list[Pivot] = []
    for index in range(length, len(candles) - length):
        window = candles[index - length : index + length + 1]
        value = candles[index].high if side == "high" else candles[index].low
        comparison = [candle.high if side == "high" else candle.low for candle in window]
        if side == "high" and value == max(comparison):
            pivots.append(Pivot(index=index, price=value))
        elif side == "low" and value == min(comparison):
            pivots.append(Pivot(index=index, price=value))
    return pivots


def _price_places(value: float) -> int:
    magnitude = abs(value)
    if magnitude >= 1000:
        return 2
    if magnitude >= 1:
        return 5
    return 8


def _round_price(value: float) -> float:
    """Keep API levels readable without claiming an exchange-specific tick size."""
    return round(value, _price_places(value))


def _round_outward(value: float, *, direction: str) -> float:
    """Round a target away from entry so displayed reward never falls below 2R."""
    import math

    multiplier = 10 ** _price_places(value)
    if direction == "up":
        return math.ceil(value * multiplier) / multiplier
    return math.floor(value * multiplier) / multiplier


def _format_price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.8g}"


def _current_fvg(candles: list[Candle]) -> tuple[bool, bool]:
    """Return bullish/bearish FVG states for the latest fully supplied three bars."""
    if len(candles) < 3:
        return False, False
    first, middle, current = candles[-3], candles[-2], candles[-1]
    bullish = current.low > first.high and middle.close > middle.open
    bearish = current.high < first.low and middle.close < middle.open
    return bullish, bearish


def _volume_ratio(candles: list[Candle], window: int = 20) -> float | None:
    volumes = [candle.volume for candle in candles[-(window + 1) :]]
    if len(volumes) < window + 1 or not any(volumes):
        return None
    baseline = fmean(volumes[:-1])
    return volumes[-1] / baseline if baseline > 0 else None


def _reasoning(
    *,
    action: SignalAction,
    snapshot: TechnicalSnapshot,
    entry: float | None,
    stop: float | None,
    target: float | None,
    risk_reward: float | None,
    bullish_score: int,
    bearish_score: int,
) -> list[str]:
    """Generate 11 short, factual explanations from computed data only."""
    volume_text = "unavailable" if snapshot.volume_ratio is None else f"{snapshot.volume_ratio:.2f}x its 20-candle average"
    high_text = _format_price(snapshot.last_swing_high)
    low_text = _format_price(snapshot.last_swing_low)
    trend_text = "above" if snapshot.ema_fast > snapshot.ema_slow else "below"
    structure_text = (
        "a bullish confirmed-swing break is present"
        if snapshot.bullish_structure_break
        else "a bearish confirmed-swing break is present"
        if snapshot.bearish_structure_break
        else "no fresh confirmed-swing break is present"
    )
    fvg_text = "bullish" if snapshot.bullish_fvg else "bearish" if snapshot.bearish_fvg else "no current"
    decision_text = {
        SignalAction.BUY: "The deterministic rule set permits a long setup, subject to the stated invalidation.",
        SignalAction.SELL: "The deterministic rule set permits a short setup, subject to the stated invalidation.",
        SignalAction.NO_SIGNAL: "The rule set blocks a trade because alignment is incomplete; waiting is the intended action.",
    }[action]
    levels_text = (
        f"Reference entry is {_format_price(entry)}, stop is {_format_price(stop)}, and target is {_format_price(target)}."
        if entry is not None and stop is not None and target is not None
        else "No executable reference levels are published while the setup is blocked."
    )
    rr_text = f"The planned reward-to-risk is {risk_reward:.2f}:1, meeting the 2.00:1 minimum." if risk_reward else "The 2.00:1 minimum reward-to-risk gate is not evaluated without a trade."

    return [
        f"Last close is {_format_price(snapshot.close)} and Wilder ATR(14) is {_format_price(snapshot.atr)}.",
        f"EMA(20) is {_format_price(snapshot.ema_fast)} and EMA(50) is {_format_price(snapshot.ema_slow)}; fast trend is {trend_text} the slow trend.",
        f"RSI(14) is {snapshot.rsi:.1f}, which is assessed with a neutral-to-momentum band rather than as a standalone trigger.",
        f"Latest confirmed swing high is {high_text} and latest confirmed swing low is {low_text}.",
        f"Structure check: {structure_text}.",
        f"The latest three-candle imbalance check reports {fvg_text} fair value gap.",
        f"Current volume is {volume_text}.",
        f"Bullish alignment score is {bullish_score}/4 and bearish alignment score is {bearish_score}/4.",
        decision_text,
        levels_text,
        rr_text,
    ]


def build_signal(
    candles: Iterable[Candle],
    *,
    symbol: str,
    market: Market,
    timeframe: str,
    data_source: str,
) -> SignalResponse:
    """Create a repeatable BUY/SELL/NO_SIGNAL response from closed candles.

    A tradable action requires: EMA alignment, non-extreme momentum, a fresh
    confirmed structure break, and acceptable participation. Stops are exactly
    1.5 ATR from the reference close and targets are exactly 2.0R. The function
    deliberately uses the last supplied close as a reference price; it does not
    submit orders and cannot promise a fill at that price.
    """
    candles = list(candles)
    if len(candles) < 80:
        raise ValueError("at least 80 closed candles are required for a signal")

    closes = [candle.close for candle in candles]
    atr = _atr(candles)
    ema_fast = _ema(closes, 20)
    ema_slow = _ema(closes, 50)
    rsi = _rsi(closes)
    highs = _confirmed_pivots(candles, "high")
    lows = _confirmed_pivots(candles, "low")
    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None
    current = candles[-1]
    previous = candles[-2]
    bullish_break = bool(last_high and previous.close <= last_high.price < current.close)
    bearish_break = bool(last_low and previous.close >= last_low.price > current.close)
    bullish_fvg, bearish_fvg = _current_fvg(candles)
    volume_ratio = _volume_ratio(candles)

    snapshot = TechnicalSnapshot(
        close=_round_price(current.close),
        atr=_round_price(atr),
        ema_fast=_round_price(ema_fast),
        ema_slow=_round_price(ema_slow),
        rsi=round(rsi, 2),
        volume_ratio=round(volume_ratio, 2) if volume_ratio is not None else None,
        last_swing_high=_round_price(last_high.price) if last_high else None,
        last_swing_low=_round_price(last_low.price) if last_low else None,
        bullish_fvg=bullish_fvg,
        bearish_fvg=bearish_fvg,
        bullish_structure_break=bullish_break,
        bearish_structure_break=bearish_break,
    )

    bullish_score = sum(
        [
            current.close > ema_fast > ema_slow,
            52 <= rsi <= 72,
            bullish_break,
            volume_ratio is None or volume_ratio >= 0.80,
        ]
    )
    bearish_score = sum(
        [
            current.close < ema_fast < ema_slow,
            28 <= rsi <= 48,
            bearish_break,
            volume_ratio is None or volume_ratio >= 0.80,
        ]
    )

    # A fresh structural break is mandatory; this avoids issuing late momentum entries.
    if bullish_score == 4:
        action = SignalAction.BUY
    elif bearish_score == 4:
        action = SignalAction.SELL
    else:
        action = SignalAction.NO_SIGNAL

    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    if action is SignalAction.BUY:
        entry = _round_price(current.close)
        stop = _round_price(entry - 1.5 * atr)
        risk = entry - stop
        # Round the displayed target away from entry; this preserves the >=2R invariant.
        target = _round_outward(entry + 2.0 * risk, direction="up")
        risk_reward = round((target - entry) / risk, 2)
    elif action is SignalAction.SELL:
        entry = _round_price(current.close)
        stop = _round_price(entry + 1.5 * atr)
        risk = stop - entry
        target = _round_outward(entry - 2.0 * risk, direction="down")
        risk_reward = round((entry - target) / risk, 2)

    confidence = 0
    if action is SignalAction.BUY:
        confidence = min(82, 55 + bullish_score * 6 + (4 if bullish_fvg else 0))
    elif action is SignalAction.SELL:
        confidence = min(82, 55 + bearish_score * 6 + (4 if bearish_fvg else 0))
    else:
        confidence = min(49, max(bullish_score, bearish_score) * 10 + 5)

    reasoning = _reasoning(
        action=action,
        snapshot=snapshot,
        entry=entry,
        stop=stop,
        target=target,
        risk_reward=risk_reward,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
    )
    exit_plan = (
        [
            "Invalidate the long thesis if a closed candle reaches the stop-loss level; do not widen the stop.",
            "At 1R, consider reducing risk according to a pre-written plan rather than moving the target impulsively.",
            "Take the remaining position at the published 2R target or exit sooner if the planned market structure invalidates.",
        ]
        if action is SignalAction.BUY
        else [
            "Invalidate the short thesis if a closed candle reaches the stop-loss level; do not widen the stop.",
            "At 1R, consider reducing risk according to a pre-written plan rather than moving the target impulsively.",
            "Take the remaining position at the published 2R target or exit sooner if the planned market structure invalidates.",
        ]
        if action is SignalAction.SELL
        else [
            "No order is planned; wait for a fresh confirmed structure break with EMA and RSI alignment.",
            "Recalculate from newly closed candles rather than anticipating a pivot or entering between updates.",
        ]
    )

    return SignalResponse(
        action=action,
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        generated_at=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        risk_reward=risk_reward,
        confidence=confidence,
        technicals=snapshot,
        reasoning=reasoning,
        exit_plan=exit_plan,
        risk_notice=(
            "Educational paper-trading output only. It is not investment advice, does not account for your capital, "
            "spread, slippage, leverage, news risk, or suitability, and it cannot guarantee a fill or outcome."
        ),
        data_source=data_source,
    )
