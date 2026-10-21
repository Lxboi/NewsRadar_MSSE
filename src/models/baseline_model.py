"""Non-ML baselines from section 31: momentum only, OBI only, trade
imbalance only, news score only, volatility regime, order flow +
momentum, news + order flow. Any ML model must beat these — per section
4, a complex model that doesn't beat a one-line reference isn't worth
deploying, so these have to exist and be well-understood before Phase 9's
statistical models are worth building at all (docs/08_backtesting_strategy.md's
"order of operations": baselines -> strategy hypotheses -> statistical
models -> walk-forward -> statistical validation).

Roadmap: PHASE 8. Each baseline is one line of arithmetic over Phase 7's
feature columns (docs/06_features_v1.md) — deliberately: the whole point
of a baseline is to be transparent enough that nobody has to trust it,
they can just read it. See docs/13_baseline_strategies.md for why each
formula was chosen, and in particular why the two *combination* baselines
(order_flow_momentum, news_order_flow) use plain unweighted addition
rather than any kind of tuned/normalized blend.

Every baseline returns a plain `float`: positive = expects an up-move,
negative = expects a down-move, magnitude = relative conviction (not
comparable *across* different kinds — a momentum baseline's magnitude is
a return fraction, an OBI baseline's is already in [-1, 1]; only the
*sign* is meaningful for the cross-baseline evaluation in
`evaluation.py`). `0.0` covers both "genuinely neutral" and "the input
feature was `None`" — the two are indistinguishable to a directional
baseline anyway, and `evaluation.py` excludes zero-signal rows from
accuracy scoring either way, so collapsing them costs nothing.
"""
from __future__ import annotations

BASELINE_KINDS = (
    "momentum",
    "obi",
    "trade_imbalance",
    "news",
    "volatility_regime",
    "order_flow_momentum",
    "news_order_flow",
)

# strategy.yaml's `strategies_under_test` only benchmarks a subset of
# these (5 of 7 — no standalone `baseline_volatility_regime`/
# `baseline_order_flow_momentum` entries) in the eventual Phase 10
# comparison sweep. Both are still implemented here because
# `baseline_model.py`'s own Phase-1 docstring commits to all 7 as the
# section-31 reference set — see docs/13_baseline_strategies.md for the
# discrepancy and why it's left as-is rather than silently resolved
# either direction.

DEFAULT_MOMENTUM_HORIZON_S = 30
DEFAULT_OBI_LEVEL = 10

_HIGH_VOL_REGIMES = ("HIGH_VOL", "EXTREME_VOL")


def _momentum(features: dict, momentum_horizon_s: int) -> float:
    value = features.get(f"returns_{momentum_horizon_s}s")
    return value if value is not None else 0.0


def _obi(features: dict, obi_level: int) -> float:
    value = features.get(f"obi_{obi_level}")
    return value if value is not None else 0.0


def _trade_imbalance(features: dict) -> float:
    value = features.get("trade_imbalance")
    return value if value is not None else 0.0


def _news(features: dict) -> float:
    value = features.get("news_score")
    return value if value is not None else 0.0


def _volatility_regime(features: dict, momentum_horizon_s: int) -> float:
    """Regime-conditional momentum: trend-following in a LOW/NORMAL_VOL
    market, mean-reversion (sign-flipped) in a HIGH/EXTREME_VOL one — the
    standard microstructure heuristic that trend persistence and mean
    reversion trade places as realized volatility rises. `market_regime`
    itself carries no direction on its own (LOW_VOL says nothing about
    up vs. down), so it's used here to condition momentum rather than as
    a standalone signal."""
    momentum = _momentum(features, momentum_horizon_s)
    if features.get("market_regime") in _HIGH_VOL_REGIMES:
        return -momentum
    return momentum


def _order_flow_signal(features: dict, obi_level: int) -> float:
    """Shared building block for both combination baselines: the average
    of the book-side imbalance (OBI) and the executed-flow imbalance
    (trade_imbalance) — both already scaled to [-1, 1], so a plain
    average needs no normalization."""
    return (_obi(features, obi_level) + _trade_imbalance(features)) / 2


def _order_flow_momentum(features: dict, momentum_horizon_s: int, obi_level: int) -> float:
    """Order flow + momentum: plain unweighted addition, not a tuned
    blend — see the module docstring and docs/13 for why."""
    return _order_flow_signal(features, obi_level) + _momentum(features, momentum_horizon_s)


def _news_order_flow(features: dict, obi_level: int) -> float:
    """News + order flow: same plain-addition philosophy."""
    return _order_flow_signal(features, obi_level) + _news(features)


def baseline_signal(
    features: dict,
    kind: str,
    *,
    momentum_horizon_s: int = DEFAULT_MOMENTUM_HORIZON_S,
    obi_level: int = DEFAULT_OBI_LEVEL,
) -> float:
    """`features` is one row shaped like `feature_engine.py`'s output
    (a `dict` with at least the keys the requested `kind` needs — missing
    keys are treated as `None`, see the module docstring). `kind` must be
    one of `BASELINE_KINDS`.

    `momentum_horizon_s` must match one of Phase 7's computed
    `returns_*s` columns (default 30s, matching
    `config/strategy.yaml: horizons_s`'s inclusion of 30). `obi_level`
    must be one of `1/5/10/25` (default 10, matching
    `feature_engine.py: DEFAULT_TRADE_FLOW_WINDOW_S`'s 10s window, so the
    order-flow side of the combination baselines is measuring roughly the
    same lookback as the trade-imbalance side)."""
    if kind == "momentum":
        return _momentum(features, momentum_horizon_s)
    if kind == "obi":
        return _obi(features, obi_level)
    if kind == "trade_imbalance":
        return _trade_imbalance(features)
    if kind == "news":
        return _news(features)
    if kind == "volatility_regime":
        return _volatility_regime(features, momentum_horizon_s)
    if kind == "order_flow_momentum":
        return _order_flow_momentum(features, momentum_horizon_s, obi_level)
    if kind == "news_order_flow":
        return _news_order_flow(features, obi_level)
    raise ValueError(f"unknown baseline kind {kind!r}; must be one of {BASELINE_KINDS}")
