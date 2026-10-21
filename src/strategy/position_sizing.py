"""Position sizing from account_equity, risk_per_trade, stop_distance,
volatility, signal_confidence, expected_value, max_position_size
(section 40). Extremely conservative sizing in paper/live.

Roadmap: PHASE 12. `size_position(risk_inputs: dict) -> float` — a pure
function (no state, no I/O), same convention as
`models/evaluation.py: actual_direction()` or
`models/direction.py: direction_from_probabilities()`: given the same
inputs, always the same answer, trivially testable in isolation.

## The formula: risk-based sizing, then only ever scaled *down*

1. **Base size** — the classic fixed-fractional risk formula: risk a
   fixed `risk_per_trade_pct` of `account_equity` against the position's
   stop distance. `risk_amount = account_equity * risk_per_trade_pct /
   100`; `raw_size = risk_amount / (stop_distance_bps / 10_000)` — the
   quote-currency notional whose loss, if stopped out exactly at
   `stop_distance_bps`, equals `risk_amount`. Without a stop distance
   there is nothing to size a risk-based position against — this
   returns `0.0` rather than guessing a default notional, matching
   section 40's "extremely conservative" instruction literally: fail
   safe to no position, never to an arbitrary one.
2. **Conservative multipliers** — `signal_confidence`
   (`signals/confidence.py`'s `compute_confidence()` output, `[0, 1]`),
   `volatility`, and `expected_value_bps` (`signals/signal_engine.py`'s
   `expected_value`) each scale the base size *down*, never up: a
   missing factor defaults to a neutral multiplier of `1.0` (no
   opinion, not a boost), matching the "missing data is not a
   violation" convention `strategy/entry.py`/`strategy/exit.py` already
   use — but note the asymmetry versus those modules: there, a missing
   check never blocks; here, a missing factor never *inflates* the
   size. Both are the same underlying principle (partial information
   should never look more favorable than no information), applied to
   whichever direction "more favorable" points in context.
3. **Hard ceiling** — the final size is clamped to
   `account_equity * max_position_size_pct_equity / 100`, the one limit
   this function shares by name with `strategy/risk_manager.py`'s own
   `max_position_size_pct_equity` (not cross-imported — both simply
   default to the same `strategy.yaml: risk` value, kept independent
   since a caller could legitimately want to size against a *different*
   effective ceiling than the account-wide risk manager's).

The volatility/expected-value multiplier curves below are illustrative
starting points (section 40 gives the *inputs* to size from, not a
formula) — un-tuned, same spirit as every other un-tuned default in this
codebase, flagged here rather than silently presented as calibrated.
"""
from __future__ import annotations

DEFAULT_RISK_PER_TRADE_PCT = 0.1               # strategy.yaml: risk.max_risk_per_trade_pct
DEFAULT_MAX_POSITION_SIZE_PCT_EQUITY = 5.0      # strategy.yaml: risk.max_position_size_pct_equity

# Illustrative reference points for the volatility/EV multiplier curves —
# not empirically tuned. A position sized against volatility at or below
# this reference gets no volatility haircut; above it, size shrinks
# proportionally. 0.002 = 20bps, a "typical calm" 1s realized volatility
# reading for the assets this system targets (BTC/ETH/SOL).
DEFAULT_REFERENCE_VOLATILITY = 0.002
# expected_value_bps at or above this gets no EV haircut; below it, size
# shrinks proportionally; at or below 0 the size collapses to 0 (matches
# entry.py/EventBacktester's "EV must be positive after costs" gate --
# sizing a trade the entry filter would reject anyway is never useful).
DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS = 20.0


def _confidence_multiplier(signal_confidence: "float | None") -> float:
    if signal_confidence is None:
        return 1.0
    return max(0.0, min(1.0, signal_confidence))


def _volatility_multiplier(volatility: "float | None", *, reference_volatility: float) -> float:
    if volatility is None or volatility <= 0:
        return 1.0
    return max(0.0, min(1.0, reference_volatility / volatility))


def _expected_value_multiplier(expected_value_bps: "float | None", *, full_size_expected_value_bps: float) -> float:
    if expected_value_bps is None:
        return 1.0
    if expected_value_bps <= 0:
        return 0.0
    return max(0.0, min(1.0, expected_value_bps / full_size_expected_value_bps))


def size_position(
    risk_inputs: dict,
    *,
    reference_volatility: float = DEFAULT_REFERENCE_VOLATILITY,
    full_size_expected_value_bps: float = DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS,
) -> float:
    """`risk_inputs` keys (all but `account_equity` and `stop_distance_bps`
    optional, degrading per the module docstring when missing):
    `account_equity`, `risk_per_trade_pct` (default
    `DEFAULT_RISK_PER_TRADE_PCT`), `stop_distance_bps`, `volatility`,
    `signal_confidence`, `expected_value_bps`,
    `max_position_size_pct_equity` (default
    `DEFAULT_MAX_POSITION_SIZE_PCT_EQUITY`). Returns the position notional
    in quote currency — the same unit `strategy_engine.py:
    StrategyEngine.position_size_quote` uses — never negative, never
    above the equity ceiling.
    """
    account_equity = risk_inputs.get("account_equity", 0.0) or 0.0
    if account_equity <= 0:
        return 0.0

    stop_distance_bps = risk_inputs.get("stop_distance_bps")
    if not stop_distance_bps or stop_distance_bps <= 0:
        return 0.0

    risk_per_trade_pct = risk_inputs.get("risk_per_trade_pct", DEFAULT_RISK_PER_TRADE_PCT)
    risk_amount = account_equity * (risk_per_trade_pct / 100.0)
    raw_size = risk_amount / (stop_distance_bps / 10_000)

    confidence_multiplier = _confidence_multiplier(risk_inputs.get("signal_confidence"))
    volatility_multiplier = _volatility_multiplier(risk_inputs.get("volatility"), reference_volatility=reference_volatility)
    ev_multiplier = _expected_value_multiplier(
        risk_inputs.get("expected_value_bps"), full_size_expected_value_bps=full_size_expected_value_bps
    )

    size = raw_size * confidence_multiplier * volatility_multiplier * ev_multiplier

    max_position_size_pct_equity = risk_inputs.get("max_position_size_pct_equity", DEFAULT_MAX_POSITION_SIZE_PCT_EQUITY)
    max_size = account_equity * (max_position_size_pct_equity / 100.0)

    return max(0.0, min(size, max_size))
