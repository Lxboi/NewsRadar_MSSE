"""Entry filter: EV > minimum edge, spread/slippage acceptable, data
fresh, regime acceptable, risk acceptable, probability threshold
optimized out-of-sample (not hardcoded at 50%) (section 38).

Roadmap: PHASE 11. The live counterpart to
`backtest/event_backtester.py: EventBacktester._passes_entry_filter()` —
same checks (probability minimum, expected-value-net-of-costs minimum,
spread ceiling, allowed regimes), same `config/strategy.yaml:
entry_filter` defaults, applied to `signal_engine.py`'s richer signal
payload instead of a backtest decision dict, plus one check the
backtester structurally can't perform on historical data: freshness
(`max_data_age_s`), since a backtest replay has no live wall clock to be
stale relative to.

Deliberately **not** a method on `SignalEngine` or folded into
`strategy_engine.py`: `should_enter()` matches its Phase 1 stub's plain-
function signature (`should_enter(signal, risk_state) -> bool`) exactly,
so it stays independently testable and independently callable — a
research notebook checking "would this specific signal have passed the
entry filter" shouldn't need to construct a whole `StrategyEngine`.

## `risk_state`'s shape — a documented placeholder

Phase 12's `strategy/risk_manager.py` doesn't exist yet, so `risk_state`
here is a minimal, explicitly documented placeholder contract rather
than a real `RiskManager` object: `{"blocked": bool, "reason": str |
None}`. `should_enter()` only ever reads `risk_state.get("blocked",
False)` — a caller with no real risk manager yet can pass `{}` (treated
as "not blocked") without raising. When Phase 12 implements
`RiskManager`, the natural integration point is a `RiskManager.state()`
method returning exactly this shape (extended with whatever additional
diagnostic keys are useful) so this function never has to change.
"""
from __future__ import annotations

from src.models.direction import direction_from_probabilities

# Defaults mirror config/strategy.yaml's own values — same convention
# backtest/event_backtester.py and signals/signal_engine.py already use.
DEFAULT_PROBABILITY_MIN = 0.62         # strategy.yaml: entry_filter.probability_up_min
DEFAULT_MIN_EXPECTED_VALUE = 0.0       # strategy.yaml: entry_filter.min_expected_value
DEFAULT_MAX_SPREAD_BPS = 5             # strategy.yaml: entry_filter.max_spread_bps
DEFAULT_MAX_DATA_AGE_S = 3             # strategy.yaml: entry_filter.max_data_age_s
DEFAULT_ALLOWED_REGIMES = ("LOW_VOL", "NORMAL_VOL")  # strategy.yaml: entry_filter.allowed_regimes


def should_enter(
    signal: dict,
    risk_state: dict,
    *,
    probability_min: "float | None" = DEFAULT_PROBABILITY_MIN,
    min_expected_value: "float | None" = DEFAULT_MIN_EXPECTED_VALUE,
    max_spread_bps: "float | None" = DEFAULT_MAX_SPREAD_BPS,
    max_data_age_s: "float | None" = DEFAULT_MAX_DATA_AGE_S,
    allowed_regimes: "tuple[str, ...] | None" = DEFAULT_ALLOWED_REGIMES,
    now=None,
) -> bool:
    """`signal` is `signal_engine.py`'s output shape (or anything with
    the same keys). Returns `False` for a neutral signal
    (`signal["signal"] == 0`) before checking anything else — there is
    no direction to enter. Every other check only applies when the data
    needed for it is actually present (`None`/missing never fails a
    check by itself, matching every other "missing data is not a
    violation" convention in this codebase) — `max_data_age_s` in
    particular is skipped entirely when `now` isn't given, since without
    a reference clock "is this stale" is unanswerable, not automatically
    false."""
    direction, probability = direction_from_probabilities(signal)
    if direction == 0:
        return False

    if risk_state.get("blocked", False):
        return False

    if probability_min is not None and probability < probability_min:
        return False

    expected_value = signal.get("expected_value")
    if expected_value is not None and min_expected_value is not None and expected_value < min_expected_value:
        return False

    features = signal.get("features_snapshot") or {}
    spread = features.get("spread")
    if spread is not None and max_spread_bps is not None and spread > max_spread_bps:
        return False

    regime = signal.get("market_regime")
    if regime is not None and allowed_regimes is not None and regime not in allowed_regimes:
        return False

    timestamp = signal.get("timestamp")
    if now is not None and timestamp is not None and max_data_age_s is not None:
        age_s = (now - timestamp).total_seconds()
        if age_s > max_data_age_s:
            return False

    return True
