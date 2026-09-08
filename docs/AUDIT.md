# Repository audit — Smart Money Concepts

**Audit date:** 2026-09-08

**Scope:** source, tests, packaging/automation, and trading-signal integrity in the checked-out `1b62fd6c` baseline. This is a code and methodology review, **not** an assurance of profitability or investment advice.

## Executive summary

The package is compact, readable in places, MIT-licensed, and its current regression suite passes when installed with its declared dependencies. It is a useful research starting point, but it is **not safe to treat as a live-signal or backtest-ready trading engine without remediation**.

The most consequential finding is temporal leakage: several outputs write a result on an earlier bar only after using later bars to determine it. That makes charts and naïve historical backtests look more timely than the information actually was. In addition, `previous_high_low()` returns the high/low from **two** completed resample periods back, despite its name and documentation promising the immediately previous period.

The companion TradingView study at [`scripts/SmartMoneyConcepts_v6.pine`](../scripts/SmartMoneyConcepts_v6.pine) therefore deliberately uses confirmation-first, bar-close semantics rather than attempting a line-for-line port of retrospective Python arrays.

## What was verified

| Check | Result |
| --- | --- |
| Clean working tree before deliverables | Pass |
| Editable install from `setup.py` | Pass |
| Regression suite | **10/10 passing** in 5.07 seconds |
| Test command | `.venv/bin/python -m unittest tests/unit_tests.py -v` |
| Environment used | Python 3.11.2, pandas 3.0.5, NumPy 2.4.6, Numba 0.67.0 |
| Static check | `git diff --check` was clean before deliverables |

The test run confirms that the checked-in CSV snapshots still match current implementation behavior. It does **not** establish that the behavior conforms to a canonical SMC/ICT definition, is free of look-ahead bias, or will be profitable.

## Findings and recommended remediation

### Critical — historical outputs contain look-ahead information

| Area | Evidence | Why it matters | Recommended remediation |
| --- | --- | --- | --- |
| Swings | `swing_highs_lows()` compares each candidate to forward values via `shift(-(swing_length // 2))` at [`smc.py:155`](../smartmoneyconcepts/smc.py#L155). | A pivot at bar *t* is only knowable after `swing_length` later bars, yet the returned marker is written at *t*. A strategy that acts at that returned index gets impossible fills. | Return both `PivotIndex` and `ConfirmedIndex`, or emit the signal on the confirmation bar. Document the lag explicitly. |
| FVG | `fvg()` needs `low.shift(-1)` / `high.shift(-1)` at [`smc.py:74-100`](../smartmoneyconcepts/smc.py#L74). | A gap is marked on the middle candle while the confirming third candle is still in the future. | Emit an event on the third candle; retain the middle bar separately as zone origin metadata. |
| BOS / CHoCH | Structure is written at `last_positions[-2]` at [`smc.py:258`](../smartmoneyconcepts/smc.py#L258) and its later break is separately stored in `BrokenIndex`. Older events can be removed after subsequent outcomes at [`smc.py:348-352`](../smartmoneyconcepts/smc.py#L348). | The displayed event index is not the time the break became known, and history can be revised. This is unsafe as a direct feature for backtesting. | Model events as append-only records: `setup_index`, `confirmation_index`, `break_index`, `direction`, and `status`. Backtest only at/after `confirmation_index` or `break_index`. |
| Liquidity | Tolerance is `(ohlc.high.max() - ohlc.low.min()) * range_percent` at [`smc.py:595`](../smartmoneyconcepts/smc.py#L595). | The threshold depends on the entire supplied dataset, including future candles. Adding future data changes historical liquidity classifications. | Use a causal rolling range or ATR at each candidate swing (for example, `ATR(14) * multiplier`) and freeze that threshold for the level's lifetime. |
| Order blocks | An order block is written at `obIndex`, an older candle, only after a later swing break at [`smc.py:450-484`](../smartmoneyconcepts/smc.py#L450). | This backfills a zone into history before the setup was known. The final array also omits the creation/confirmation bar. | Keep `OriginIndex`, `CreatedIndex`, `MitigatedIndex`, and `InvalidatedIndex`; expose the block only from `CreatedIndex` onward in a live/backtest view. |

**Trading impact:** Any backtest using rows where `FVG`, `HighLow`, `BOS`, `CHOCH`, `OB`, or `Liquidity` is non-null as an immediate entry condition is likely optimistic. For research, shift such features forward to when they become knowable, then include spreads, commission, slippage, session liquidity, and next-bar execution assumptions.

### High — `previous_high_low()` is one period too old

`previous_high_low()` calculates `prev_period_idx = periods_before - 2` at [`smc.py:747`](../smartmoneyconcepts/smc.py#L747), then uses that index for `PreviousHigh` and `PreviousLow`. At the first bar of the third 4-hour period, it selects period 1, not period 2. A direct check using the committed EURUSD data found **0/1,529** 4-hour period starts whose `PreviousHigh` matched the immediately prior resampled high.

This contradicts the README contract (“previous high and low”) and changes both the reference levels and `BrokenHigh` / `BrokenLow` state.

**Recommendation:** change the lookup to the immediately preceding completed period (`periods_before - 1` for the current resampling convention), rewrite the grouping/reset logic around the target-period boundary, then regenerate expected fixtures from an independently reviewed oracle. This is a behavior change and should be released as a versioned breaking/fix release, not silently hidden by rewriting snapshots.

### High — public input requirements and indexes are unreliable

* `ob()` directly accesses `ohlc["volume"]` at [`smc.py:403`](../smartmoneyconcepts/smc.py#L403), but the class-wide validator only requests `"ohlc"`. A caller without volume gets a generic `KeyError` instead of a useful validation error.
* `previous_high_low()` resamples a `volume` column at [`smc.py:719-726`](../smartmoneyconcepts/smc.py#L719), although the README does not list volume as required for that function.
* All result frames are built from fresh `pd.Series(...)` without the input index (for example [`smc.py:104-111`](../smartmoneyconcepts/smc.py#L104)). A datetime-indexed input becomes a `RangeIndex` output, making joins error-prone and obscuring signal timing.
* `inputvalidator()` assumes a positional dataframe argument (`args[0]` or `args[1]`) at [`smc.py:12`](../smartmoneyconcepts/smc.py#L12). Keyword-only calls such as `smc.fvg(ohlc=df)` are not handled safely.

**Recommendation:** validate per method (`ohlc`, `ohlcv`, or datetime-indexed `ohlc` as appropriate); preserve `ohlc.index` on every returned frame; accept and validate keyword arguments; and add explicit tests for every required/optional input combination.

### Medium — numerical and definition risks

* Several internal price arrays use `np.float32`, including BOS level storage at [`smc.py:248`](../smartmoneyconcepts/smc.py#L248) and order-block bounds at [`smc.py:409-414`](../smartmoneyconcepts/smc.py#L409). This can discard meaningful precision on high-priced instruments or fine tick sizes. Retain `float64` internally and round only in presentation.
* The order-block fallback on a bullish break initializes `obBtm` to the previous **high** and `obTop` to the previous **low** at [`smc.py:454-457`](../smartmoneyconcepts/smc.py#L454), which inverts the range whenever no intervening segment is found. Initialize `top = high` and `bottom = low`, then assert `top >= bottom` before output.
* The session timezone conversion maps strings like `UTC+2` to `Etc/GMT+2` at [`smc.py:860-863`](../smartmoneyconcepts/smc.py#L860). The `Etc/GMT` convention reverses signs, so this can shift a session the wrong way. It also does not handle daylight-saving rules. Prefer named IANA zones (e.g. `America/New_York`, `Europe/London`) and test DST transitions.
* A custom session whose start equals end is continuously active under the current comparison logic and therefore does not reset daily. Session state should explicitly reset on session entry and be keyed by a session/date identifier.
* The order-block and liquidity definitions are implementation-specific. Their descriptions imply market-order/volume analysis, but the logic mainly uses price structure and a simple three-bar volume ratio. Define the methodology precisely and avoid claims that cannot be inferred from OHLCV candles.

### Medium — test quality and reproducibility

* The suite has one EURUSD 15-minute fixture and snapshot CSVs. Snapshot agreement is regression coverage, not independent correctness validation: expected outputs can preserve an original defect.
* There are no tests that prove causal availability, preserve datetime indexes, test keyword invocation, missing volume, malformed timestamps, DST, short/empty frames, equal highs/lows, or the intended immediate previous high/low.
* No property/invariant checks are present (for example: every zone must have `Top >= Bottom`; `BrokenIndex > setup index`; event timestamps must not predate confirmation).

**Recommendation:** use small hand-constructed OHLC fixtures plus property tests, maintain an independent reference implementation for the core definitions, and make “known-at” timestamps part of test assertions. Include a realistic execution layer in any strategy validation rather than judging raw indicator hit rates.

### Low — packaging and maintenance

* `numba>=0.58.1` is declared in [`setup.py:21`](../setup.py#L21) but there is no project source import or use of Numba. It adds a large native dependency surface and slower installs without a functional benefit. Remove it or actually use it with benchmarks.
* Dependencies are lower-bounded but unbounded above, and there is no lockfile or tested version matrix. The audit install selected pandas 3.0.5 and NumPy 2.4.6; future releases can change behavior.
* The workflow uses `actions/checkout@v2`, `actions/setup-python@v2`, and Python 3.8, all dated choices. Add maintained action versions, `python_requires`, a supported Python matrix, and dependency constraints.
* Importing the package prints an ANSI-colored promotional message from [`__init__.py`](../smartmoneyconcepts/__init__.py). Libraries should avoid stdout side effects during import; use documentation/logging only when explicitly requested.

## Pine Script companion: intentional behavioral differences

[`scripts/SmartMoneyConcepts_v6.pine`](../scripts/SmartMoneyConcepts_v6.pine) is a TradingView **Pine Script v6 indicator**, not a claim of byte-for-byte parity with the retrospective arrays.

| Python behavior | Pine v6 companion behavior |
| --- | --- |
| Swings/FVGs are stored at an earlier source bar after later data is known. | The visual event label and alert occur only on the confirmation bar close. Zones may be drawn from their historical origin for context, but are created only when known. |
| Liquidity tolerance uses full-series range. | Equal highs/lows use an ATR-based tolerance available at confirmation, then expire on a sweep. |
| BOS/CHoCH relies on a four-swing pattern and writes setup indexes. | A confirmed close/wick through the latest confirmed swing emits BOS; a break against the previous confirmed bias emits CHoCH. |
| Order block range/mitigation can be backfilled and subsequently removed from arrays. | The latest opposing candle before a confirmed structure break becomes a zone at the break bar; the zone is removed on first configured mitigation. |
| Previous high/low is currently two periods old. | The script plots the immediately preceding completed higher-timeframe high/low using stable `request.security(..., high[1]/low[1], lookahead_on)` semantics. |
| Fixed UTC-offset parsing. | Uses IANA timezone options for the optional session, retaining DST behavior. |

## Practical use checklist

1. Add the Pine file to a new TradingView chart as an **indicator**, not a strategy.
2. Start with a liquid market and use a swing length that matches the chart timeframe; do not compare settings across instruments without retuning/testing.
3. Use `Close` break confirmation for a conservative baseline. Treat wick mode as a sweep-sensitive alternative, not automatically as confirmation.
4. Define the trade plan outside the indicator: entry trigger, invalidation, position size, stop, target, maximum daily loss, and news/session filters.
5. Validate out of sample with realistic spread, commission, slippage, and next-bar/intrabar fill assumptions. Monitor results by market regime and do not optimize a single EURUSD sample.
6. For Python research, remediate the critical findings before creating features or training models from the current outputs.

## Suggested implementation order

1. Fix and test immediate prior-period levels; preserve indexes and fix input validation.
2. Introduce causal event records with creation/confirmation timestamps for swings, FVGs, BOS/CHoCH, liquidity, and OBs.
3. Replace global liquidity range with frozen causal ATR/rolling tolerance.
4. Add invariant/property and edge-case tests, then version the behavioral changes.
5. Modernize packaging and CI, remove unused Numba, and test a supported dependency/Python matrix.
6. Only then conduct a separate, documented strategy study with risk and execution assumptions.
