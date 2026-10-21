"""Exit logic candidates to test: fixed TP/SL, volatility-based SL,
probability reversal, order-flow reversal, liquidity reversal, time
stop, across 10s-300s horizons (section 39).

Roadmap: PHASE 11. `should_exit(position, features) -> str|None` checks
every `config/strategy.yaml: exit_candidates` entry **in the order
listed there** and returns the name of the first one that fires (or
`None` if none do) — a position closes for exactly one reason, and which
reason wins when several would fire simultaneously is a real, if usually
rare, methodological choice, made explicit here as "config order" rather
than left as an accident of dict iteration.

`position` is expected to carry at least `side` (`"buy"`/`"sell"`) and
`entry_price`; the reversal/liquidity checks additionally read optional
*entry-time snapshot* keys the caller should populate when opening the
position (`entry_obi`, `entry_trade_imbalance`, `entry_liquidity_imbalance`)
— each check degrades to "unavailable, skip" (never "trigger by default")
when its snapshot key is missing, same convention as every other
optional-data check in this codebase. `opened_at` is required for
`time_stop`.

`features` is a current-moment Phase 7 feature row — `price`,
`timestamp`, `volatility`, `market_regime`, `obi_10`, `trade_imbalance`,
`liquidity_imbalance` if available. `features["timestamp"]` doubles as
"now" for `time_stop`, deliberately not a separate wall-clock read — a
feature row already represents "what does the market look like right
now," and reusing it keeps this function as testable/deterministic as
everything upstream of it. **`probability_reversal` needs a fresh model
score**, which a bare feature row doesn't carry on its own
(`predict_proba()` is a model operation, not a feature-engine one) — the
caller can still make this candidate available by merging
`probability_up`/`probability_down` keys (e.g. from a fresh
`SignalEngine.build_signal()` call) into `features` before calling
`should_exit()`; if they're absent, this one candidate is simply skipped,
not an error.
"""
from __future__ import annotations

from src.models.direction import direction_from_probabilities

# Defaults are illustrative starting points (section 39: "all tested,
# none assumed best") — not empirically validated, same spirit as every
# other un-tuned threshold in this codebase.
DEFAULT_TAKE_PROFIT_BPS = 15.0
DEFAULT_STOP_LOSS_BPS = 15.0
DEFAULT_VOLATILITY_STOP_MULTIPLIER = 3.0
DEFAULT_REVERSAL_PROBABILITY_MIN = 0.55
DEFAULT_ORDER_FLOW_REVERSAL_MIN_ABS = 0.3
DEFAULT_LIQUIDITY_REVERSAL_MIN_ABS = 0.3
DEFAULT_TIME_STOP_S = 300  # strategy.yaml: time_stop_candidates_s max

EXIT_FIXED_TP = "fixed_tp"
EXIT_FIXED_SL = "fixed_sl"
EXIT_VOLATILITY_SL = "volatility_based_sl"
EXIT_PROBABILITY_REVERSAL = "probability_reversal"
EXIT_ORDER_FLOW_REVERSAL = "order_flow_reversal"
EXIT_LIQUIDITY_REVERSAL = "liquidity_reversal"
EXIT_TIME_STOP = "time_stop"

# Same order as config/strategy.yaml: exit_candidates.
CANDIDATE_ORDER = (
    EXIT_FIXED_TP,
    EXIT_FIXED_SL,
    EXIT_VOLATILITY_SL,
    EXIT_PROBABILITY_REVERSAL,
    EXIT_ORDER_FLOW_REVERSAL,
    EXIT_LIQUIDITY_REVERSAL,
    EXIT_TIME_STOP,
)


def _position_side_sign(position: dict) -> int:
    return 1 if position.get("side") == "buy" else -1


def _unrealized_bps(position: dict, features: dict) -> "float | None":
    entry_price = position.get("entry_price")
    current_price = features.get("price")
    if entry_price is None or current_price is None or entry_price == 0:
        return None
    raw_bps = (current_price - entry_price) / entry_price * 10_000
    return raw_bps * _position_side_sign(position)  # positive == in the position's favor


def _check_fixed_tp(position: dict, features: dict, *, take_profit_bps: float) -> bool:
    unrealized_bps = _unrealized_bps(position, features)
    return unrealized_bps is not None and unrealized_bps >= take_profit_bps


def _check_fixed_sl(position: dict, features: dict, *, stop_loss_bps: float) -> bool:
    unrealized_bps = _unrealized_bps(position, features)
    return unrealized_bps is not None and unrealized_bps <= -stop_loss_bps


def _check_volatility_sl(position: dict, features: dict, *, volatility_stop_multiplier: float) -> bool:
    unrealized_bps = _unrealized_bps(position, features)
    volatility = features.get("volatility")
    if unrealized_bps is None or volatility is None:
        return False
    stop_bps = volatility_stop_multiplier * volatility * 10_000
    return unrealized_bps <= -stop_bps


def _check_probability_reversal(position: dict, features: dict, *, reversal_probability_min: float) -> bool:
    p_up = features.get("probability_up")
    p_down = features.get("probability_down")
    p_neutral = features.get("probability_neutral")
    if p_up is None or p_down is None or p_neutral is None:
        return False
    direction, probability = direction_from_probabilities(
        {"probability_up": p_up, "probability_down": p_down, "probability_neutral": p_neutral}
    )
    position_side = _position_side_sign(position)
    return direction != 0 and direction != position_side and probability >= reversal_probability_min


def _check_order_flow_reversal(position: dict, features: dict, *, min_abs: float) -> bool:
    entry_obi = position.get("entry_obi")
    current_obi = features.get("obi_10")
    if entry_obi is None or current_obi is None:
        return False
    position_side = _position_side_sign(position)
    return (current_obi > 0) != (position_side > 0) and abs(current_obi) >= min_abs


def _check_liquidity_reversal(position: dict, features: dict, *, min_abs: float) -> bool:
    entry_liquidity = position.get("entry_liquidity_imbalance")
    current_liquidity = features.get("liquidity_imbalance")
    if entry_liquidity is None or current_liquidity is None:
        return False
    position_side = _position_side_sign(position)
    return (current_liquidity > 0) != (position_side > 0) and abs(current_liquidity) >= min_abs


def _check_time_stop(position: dict, features: dict, *, time_stop_s: float) -> bool:
    opened_at = position.get("opened_at")
    now = features.get("timestamp")
    if opened_at is None or now is None:
        return False
    return (now - opened_at).total_seconds() >= time_stop_s


_CHECKS = {
    EXIT_FIXED_TP: lambda position, features, cfg: _check_fixed_tp(position, features, take_profit_bps=cfg["take_profit_bps"]),
    EXIT_FIXED_SL: lambda position, features, cfg: _check_fixed_sl(position, features, stop_loss_bps=cfg["stop_loss_bps"]),
    EXIT_VOLATILITY_SL: lambda position, features, cfg: _check_volatility_sl(
        position, features, volatility_stop_multiplier=cfg["volatility_stop_multiplier"]
    ),
    EXIT_PROBABILITY_REVERSAL: lambda position, features, cfg: _check_probability_reversal(
        position, features, reversal_probability_min=cfg["reversal_probability_min"]
    ),
    EXIT_ORDER_FLOW_REVERSAL: lambda position, features, cfg: _check_order_flow_reversal(
        position, features, min_abs=cfg["order_flow_reversal_min_abs"]
    ),
    EXIT_LIQUIDITY_REVERSAL: lambda position, features, cfg: _check_liquidity_reversal(
        position, features, min_abs=cfg["liquidity_reversal_min_abs"]
    ),
    EXIT_TIME_STOP: lambda position, features, cfg: _check_time_stop(position, features, time_stop_s=cfg["time_stop_s"]),
}


def should_exit(
    position: dict,
    features: dict,
    *,
    take_profit_bps: float = DEFAULT_TAKE_PROFIT_BPS,
    stop_loss_bps: float = DEFAULT_STOP_LOSS_BPS,
    volatility_stop_multiplier: float = DEFAULT_VOLATILITY_STOP_MULTIPLIER,
    reversal_probability_min: float = DEFAULT_REVERSAL_PROBABILITY_MIN,
    order_flow_reversal_min_abs: float = DEFAULT_ORDER_FLOW_REVERSAL_MIN_ABS,
    liquidity_reversal_min_abs: float = DEFAULT_LIQUIDITY_REVERSAL_MIN_ABS,
    time_stop_s: float = DEFAULT_TIME_STOP_S,
) -> "str | None":
    """See module docstring. Returns the exit-candidate name (one of
    `CANDIDATE_ORDER`) of the first check that fires, checked in
    `config/strategy.yaml: exit_candidates`'s order, or `None` if the
    position should stay open."""
    cfg = {
        "take_profit_bps": take_profit_bps,
        "stop_loss_bps": stop_loss_bps,
        "volatility_stop_multiplier": volatility_stop_multiplier,
        "reversal_probability_min": reversal_probability_min,
        "order_flow_reversal_min_abs": order_flow_reversal_min_abs,
        "liquidity_reversal_min_abs": liquidity_reversal_min_abs,
        "time_stop_s": time_stop_s,
    }
    for candidate in CANDIDATE_ORDER:
        if _CHECKS[candidate](position, features, cfg):
            return candidate
    return None
