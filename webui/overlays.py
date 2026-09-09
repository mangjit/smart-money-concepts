"""Convert repository SMC calculations into bounded, closed-candle chart overlays.

The indicator package returns pandas frames indexed by candle position. This adapter
keeps that implementation server-side, converts results into JSON-safe UI contracts,
and deliberately limits visual clutter. It does not invent levels or use broker data.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import pandas as pd

from smartmoneyconcepts.smc import smc

from .models import Candle, Market, OverlayLevel, OverlayMarker, OverlaySummary, OverlayZone, SmcOverlayResponse

_SWING_LENGTH = 5
_MAX_MARKERS = 32
_MAX_ZONES_PER_KIND = 4
_MAX_LEVELS = 20
_SESSION_CHOICES = frozenset(
    {
        "Sydney",
        "Tokyo",
        "London",
        "New York",
        "Asian kill zone",
        "London open kill zone",
        "New York kill zone",
        "london close kill zone",
    }
)


class OverlayCalculationError(ValueError):
    """Raised for invalid overlay inputs that should be reported to the API client."""


def build_smc_overlays(
    *,
    candles: Iterable[Candle],
    symbol: str,
    market: Market,
    timeframe: str,
    source: str,
    session: str = "London",
) -> SmcOverlayResponse:
    """Calculate chart annotations from a single immutable list of closed candles.

    The core library's swing/FVG definitions need later candles for confirmation. The
    adapter therefore hides unconfirmed tail annotations from the display. Historical
    liquidity and order-block markings remain descriptive/research overlays, not an
    instruction to trade or a guarantee that a level remains actionable.
    """
    candle_list = list(candles)
    if len(candle_list) < 80:
        raise OverlayCalculationError("at least 80 closed candles are required for SMC overlays")
    if session not in _SESSION_CHOICES:
        allowed = ", ".join(sorted(_SESSION_CHOICES))
        raise OverlayCalculationError(f"Unsupported session. Choose one of: {allowed}")

    frame = pd.DataFrame(
        {
            "open": [candle.open for candle in candle_list],
            "high": [candle.high for candle in candle_list],
            "low": [candle.low for candle in candle_list],
            "close": [candle.close for candle in candle_list],
            "volume": [candle.volume for candle in candle_list],
        }
    )
    time_indexed = frame.copy()
    time_indexed.index = pd.to_datetime([candle.timestamp for candle in candle_list], unit="ms", utc=True)

    warnings: list[str] = []
    try:
        swing = smc.swing_highs_lows(frame, swing_length=_SWING_LENGTH)
        fair_value_gaps = smc.fvg(frame, join_consecutive=True)
        structure = smc.bos_choch(frame, swing, close_break=True)
        order_blocks = smc.ob(frame, swing, close_mitigation=False)
        liquidity = smc.liquidity(frame, swing, range_percent=0.01)
        retracement = smc.retracements(frame, swing)
    except (ArithmeticError, IndexError, KeyError, LookupError, TypeError, ValueError) as error:
        raise OverlayCalculationError(f"Unable to calculate SMC overlays: {error}") from error

    previous_timeframe = _previous_timeframe(timeframe)
    try:
        previous = smc.previous_high_low(time_indexed.copy(), time_frame=previous_timeframe)
        session_data = smc.sessions(time_indexed.copy(), session=session, time_zone="UTC")
    except (ArithmeticError, IndexError, KeyError, LookupError, TypeError, ValueError) as error:
        # These two overlays depend on a time-indexed series and can be unavailable on
        # short intraday history. Keep the remaining SMC studies visible.
        previous = None
        session_data = None
        warnings.append(f"Previous-level/session overlay unavailable: {error}")

    markers = _swing_markers(swing, candle_list)
    levels: list[OverlayLevel] = []
    zones = _zones_from_fvg(fair_value_gaps, candle_list)
    zones.extend(_zones_from_order_blocks(order_blocks, candle_list))
    structure_markers, structure_levels = _structure_annotations(structure, candle_list)
    liquidity_markers, liquidity_levels = _liquidity_annotations(liquidity, candle_list)
    markers.extend(structure_markers)
    markers.extend(liquidity_markers)
    levels.extend(structure_levels)
    levels.extend(liquidity_levels)

    previous_summary: list[OverlaySummary] = []
    if previous is not None:
        previous_levels, previous_summary = _previous_annotations(previous, candle_list, previous_timeframe)
        levels.extend(previous_levels)
    session_summary: list[OverlaySummary] = []
    if session_data is not None:
        session_levels, session_markers, session_summary = _session_annotations(session_data, candle_list, session)
        levels.extend(session_levels)
        markers.extend(session_markers)

    retracement_summary, retracement_marker = _retracement_annotation(retracement, candle_list)
    if retracement_marker is not None:
        markers.append(retracement_marker)
    summary = [
        OverlaySummary(label="FVG", value=f"{len([zone for zone in zones if zone.kind == 'fvg'])} recent zones", tone="neutral"),
        OverlaySummary(label="Order blocks", value=f"{len([zone for zone in zones if zone.kind == 'order_block'])} recent zones", tone="neutral"),
        OverlaySummary(label="Swings", value=f"{len(_swing_markers(swing, candle_list))} confirmed labels", tone="neutral"),
        *previous_summary,
        *session_summary,
        retracement_summary,
    ]

    # Most recent annotations win. Timestamps sort markers deterministically for
    # Lightweight Charts and the limit prevents a dense chart becoming unreadable.
    markers = sorted(markers, key=lambda marker: marker.timestamp)[-_MAX_MARKERS:]
    levels = levels[-_MAX_LEVELS:]
    return SmcOverlayResponse(
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        source=source,
        markers=markers,
        levels=levels,
        zones=zones,
        summary=summary,
        warnings=warnings,
    )


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _index_from_value(value: object, length: int) -> int | None:
    number = _finite(value)
    if number is None:
        return None
    index = int(number)
    return index if 0 <= index < length else None


def _previous_timeframe(timeframe: str) -> str:
    # Use a completed higher timeframe whenever the available history makes that
    # meaningful. The underlying package returns an explicit empty result otherwise.
    return "1H" if timeframe in {"1m", "5m", "15m"} else "1D" if timeframe in {"1h", "4h"} else "1W"


def _swing_markers(swing: pd.DataFrame, candles: list[Candle]) -> list[OverlayMarker]:
    markers: list[OverlayMarker] = []
    # The package appends an endpoint swing. Do not show the final look-ahead tail.
    latest_confirmed = len(candles) - _SWING_LENGTH - 1
    for index, direction in enumerate(swing["HighLow"]):
        if index > latest_confirmed:
            continue
        direction_number = _finite(direction)
        if direction_number == 1:
            markers.append(OverlayMarker(timestamp=candles[index].timestamp, position="aboveBar", color="#ff9aa7", shape="arrowDown", text="SH"))
        elif direction_number == -1:
            markers.append(OverlayMarker(timestamp=candles[index].timestamp, position="belowBar", color="#69d8ff", shape="arrowUp", text="SL"))
    return markers[-16:]


def _zones_from_fvg(fair_value_gaps: pd.DataFrame, candles: list[Candle]) -> list[OverlayZone]:
    zones: list[OverlayZone] = []
    # FVG needs candle i+1, so the final source index cannot be confirmed/displayed.
    for index, direction in enumerate(fair_value_gaps["FVG"]):
        direction_number = _finite(direction)
        if direction_number not in {1, -1} or index >= len(candles) - 1:
            continue
        top = _finite(fair_value_gaps["Top"].iloc[index])
        bottom = _finite(fair_value_gaps["Bottom"].iloc[index])
        if top is None or bottom is None:
            continue
        mitigated = _index_from_value(fair_value_gaps["MitigatedIndex"].iloc[index], len(candles))
        active = mitigated in {None, 0}
        zones.append(
            OverlayZone(
                label="Bullish FVG" if direction_number == 1 else "Bearish FVG",
                kind="fvg",
                direction="bullish" if direction_number == 1 else "bearish",
                top=max(top, bottom),
                bottom=min(top, bottom),
                start_timestamp=candles[index].timestamp,
                end_timestamp=candles[-1].timestamp if active else candles[mitigated].timestamp,
                active=active,
            )
        )
    return zones[-_MAX_ZONES_PER_KIND:]


def _zones_from_order_blocks(order_blocks: pd.DataFrame, candles: list[Candle]) -> list[OverlayZone]:
    zones: list[OverlayZone] = []
    for index, direction in enumerate(order_blocks["OB"]):
        direction_number = _finite(direction)
        if direction_number not in {1, -1}:
            continue
        top = _finite(order_blocks["Top"].iloc[index])
        bottom = _finite(order_blocks["Bottom"].iloc[index])
        if top is None or bottom is None:
            continue
        mitigated = _index_from_value(order_blocks["MitigatedIndex"].iloc[index], len(candles))
        active = mitigated in {None, 0}
        zones.append(
            OverlayZone(
                label="Bullish OB" if direction_number == 1 else "Bearish OB",
                kind="order_block",
                direction="bullish" if direction_number == 1 else "bearish",
                top=max(top, bottom),
                bottom=min(top, bottom),
                start_timestamp=candles[index].timestamp,
                end_timestamp=candles[-1].timestamp if active else candles[mitigated].timestamp,
                active=active,
            )
        )
    return zones[-_MAX_ZONES_PER_KIND:]


def _structure_annotations(structure: pd.DataFrame, candles: list[Candle]) -> tuple[list[OverlayMarker], list[OverlayLevel]]:
    markers: list[OverlayMarker] = []
    levels: list[OverlayLevel] = []
    for index, row in structure.iterrows():
        level = _finite(row["Level"])
        broken_index = _index_from_value(row["BrokenIndex"], len(candles))
        if level is None or broken_index is None:
            continue
        for key, prefix in (("BOS", "BOS"), ("CHOCH", "CHoCH")):
            direction = _finite(row[key])
            if direction not in {1, -1}:
                continue
            bullish = direction == 1
            markers.append(
                OverlayMarker(
                    timestamp=candles[broken_index].timestamp,
                    position="belowBar" if bullish else "aboveBar",
                    color="#32d296" if bullish else "#ff6f7d",
                    shape="arrowUp" if bullish else "arrowDown",
                    text=f"{prefix} {'↑' if bullish else '↓'}",
                )
            )
            levels.append(
                OverlayLevel(
                    label=f"{prefix} {'bull' if bullish else 'bear'}",
                    price=level,
                    color="#32d296" if bullish else "#ff6f7d",
                    style="dashed",
                )
            )
    return markers[-10:], levels[-10:]


def _liquidity_annotations(liquidity: pd.DataFrame, candles: list[Candle]) -> tuple[list[OverlayMarker], list[OverlayLevel]]:
    markers: list[OverlayMarker] = []
    levels: list[OverlayLevel] = []
    for index, row in liquidity.iterrows():
        direction = _finite(row["Liquidity"])
        level = _finite(row["Level"])
        if direction not in {1, -1} or level is None:
            continue
        buy_side = direction == 1
        label = "BSL" if buy_side else "SSL"
        markers.append(
            OverlayMarker(
                timestamp=candles[index].timestamp,
                position="aboveBar" if buy_side else "belowBar",
                color="#e6b85c",
                shape="circle",
                text=label,
            )
        )
        levels.append(OverlayLevel(label=f"{label} liquidity", price=level, color="#e6b85c", style="dotted"))
    return markers[-8:], levels[-8:]


def _previous_annotations(previous: pd.DataFrame, candles: list[Candle], reference: str) -> tuple[list[OverlayLevel], list[OverlaySummary]]:
    high: float | None = None
    low: float | None = None
    for _, row in previous.iloc[::-1].iterrows():
        high = high if high is not None else _finite(row["PreviousHigh"])
        low = low if low is not None else _finite(row["PreviousLow"])
        if high is not None and low is not None:
            break
    levels: list[OverlayLevel] = []
    summary: list[OverlaySummary] = []
    if high is not None:
        levels.append(OverlayLevel(label=f"Previous {reference} high", price=high, color="#8f9cff", style="dashed"))
        summary.append(OverlaySummary(label=f"Prev {reference} high", value=f"{high:.8g}", tone="neutral"))
    if low is not None:
        levels.append(OverlayLevel(label=f"Previous {reference} low", price=low, color="#8f9cff", style="dashed"))
        summary.append(OverlaySummary(label=f"Prev {reference} low", value=f"{low:.8g}", tone="neutral"))
    return levels, summary


def _session_annotations(session_data: pd.DataFrame, candles: list[Candle], session: str) -> tuple[list[OverlayLevel], list[OverlayMarker], list[OverlaySummary]]:
    active_indices = [index for index, active in enumerate(session_data["Active"]) if _finite(active) == 1]
    if not active_indices:
        return [], [], [OverlaySummary(label=session, value="No active session bars in loaded history", tone="neutral")]
    # Use the latest active run; historical runs would otherwise crowd the chart.
    end = active_indices[-1]
    start = end
    while start > 0 and _finite(session_data["Active"].iloc[start - 1]) == 1:
        start -= 1
    high = _finite(session_data["High"].iloc[end])
    low = _finite(session_data["Low"].iloc[end])
    levels: list[OverlayLevel] = []
    if high is not None:
        levels.append(OverlayLevel(label=f"{session} high", price=high, color="#d597ff", style="dotted"))
    if low is not None:
        levels.append(OverlayLevel(label=f"{session} low", price=low, color="#d597ff", style="dotted"))
    marker = OverlayMarker(timestamp=candles[start].timestamp, position="inBar", color="#d597ff", shape="square", text=session)
    summary = [
        OverlaySummary(
            label=session,
            value=f"{candles[start].timestamp}–{candles[end].timestamp} UTC",
            tone="neutral",
        )
    ]
    return levels, [marker], summary


def _retracement_annotation(retracement: pd.DataFrame, candles: list[Candle]) -> tuple[OverlaySummary, OverlayMarker | None]:
    for _, row in retracement.iloc[::-1].iterrows():
        direction = _finite(row["Direction"])
        if direction not in {1, -1}:
            continue
        current = _finite(row["CurrentRetracement%"])
        deepest = _finite(row["DeepestRetracement%"])
        if current is None or deepest is None:
            continue
        trend = "up-leg" if direction == 1 else "down-leg"
        bullish = direction == 1
        return (
            OverlaySummary(
                label="Retracement",
                value=f"{trend}: {current:.1f}% current / {deepest:.1f}% deepest",
                tone="positive" if bullish else "negative",
            ),
            OverlayMarker(
                timestamp=candles[-1].timestamp,
                position="belowBar" if bullish else "aboveBar",
                color="#4dd5a1" if bullish else "#ff8491",
                shape="circle",
                text=f"R {current:.1f}%",
            ),
        )
    return OverlaySummary(label="Retracement", value="Insufficient confirmed swing structure", tone="neutral"), None
