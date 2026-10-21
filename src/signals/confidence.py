"""Confidence score combining model calibration quality, data freshness
and news/order-flow confirmation (section 26).

Roadmap: PHASE 11. The Phase 1 stub signature was `compute_confidence(signal:
dict) -> float`; kept exactly as-is for the required positional argument,
with everything else added as keyword-only parameters with documented
defaults (matching how every phase since Phase 8 has extended a Phase 1
stub signature rather than silently redefining it) — `now` in particular,
since "how fresh is this data" needs a reference clock, and this codebase's
established convention (`news/pipeline.py: apply_decay(..., now=...)`,
`FeatureEngine.build_rows(..., start_ts_ns, end_ts_ns)`) is to make "now"
an explicit, injectable parameter rather than an internal
`datetime.now()` call, so this function stays deterministic and testable.

`signal` is expected to be shaped like `signal_engine.py`'s own output
(`SignalEngine.build_signals()`) — `signal`, `probability_up`,
`probability_down`, `probability_neutral`, `market_regime`,
`features_snapshot`, `timestamp`, and an optional `model_calibration`
dict (see below) — but this function only ever reads keys with `.get()`
and degrades to a neutral 0.5 sub-score when a key is missing, so it
tolerates a partially-populated signal dict rather than requiring the
full shape.

## Three components, each independently missing-tolerant

1. **Calibration quality** — how trustworthy is *this model's*
   `predict_proba()` output in general, not this specific prediction?
   Read from `signal.get("model_calibration", {}).get("roc_auc_macro")`
   (the diagnostic `models/calibration.py: calibrate()` produces,
   expected to have been attached to the signal by whichever code built
   it — e.g. copied from the model's current `ModelRegistry.current_promoted()`
   record). Mapped from `[0.5, 1.0]` (0.5 = no better than chance, 1.0 =
   perfect separation) onto `[0.0, 1.0]`; missing or `None` (a model with
   no recorded calibration diagnostic yet) defaults to a **neutral 0.5**,
   not a penalty-implying `0.0` — an unmeasured model isn't evidence of a
   bad one.
2. **Data freshness** — `now - signal["timestamp"]` against
   `max_data_age_s` (default matches `strategy.yaml:
   entry_filter.max_data_age_s = 3`), linearly decaying from `1.0` at
   `age=0` to `0.0` at `age >= max_data_age_s`. Missing `timestamp` or no
   `now` given in an offline/backtest context defaults to neutral `0.5`.
3. **News/order-flow confirmation** — of the model's chosen direction and
   up to three independent signals available in
   `signal["features_snapshot"]` (`obi_10`, `trade_imbalance`,
   `news_score` — the same canonical OBI level `baseline_model.py`
   defaults to), what fraction agree in sign? A directional call
   confirmed by all three independent reads is worth more than one where
   order flow and news actively disagree with the model. Neutral `0.5`
   when the signal itself is neutral (`direction=0`, nothing to confirm)
   or when none of the three columns are available.

The three components are combined by a plain weighted average — not
empirically tuned, a documented starting point per section 26, same
spirit as `strategy.yaml`'s own `probability_up_min=0.62` being flagged
"starting point only, must be re-optimized".
"""
from __future__ import annotations

import datetime as dt

DEFAULT_MAX_DATA_AGE_S = 3.0  # strategy.yaml: entry_filter.max_data_age_s
DEFAULT_WEIGHTS = {"calibration": 0.4, "freshness": 0.2, "confirmation": 0.4}
CONFIRMATION_COLUMNS = ("obi_10", "trade_imbalance", "news_score")
NEUTRAL_SCORE = 0.5


def _calibration_component(signal: dict) -> float:
    calibration = signal.get("model_calibration") or {}
    roc_auc = calibration.get("roc_auc_macro")
    if roc_auc is None:
        return NEUTRAL_SCORE
    # roc_auc_macro is defined on [0, 1] with 0.5 = chance; clamp before
    # rescaling in case a caller passes a value outside the usual range.
    roc_auc = max(0.0, min(1.0, roc_auc))
    return max(0.0, (roc_auc - 0.5) / 0.5)


def _freshness_component(signal: dict, *, now: "dt.datetime | None", max_data_age_s: float) -> float:
    timestamp = signal.get("timestamp")
    if timestamp is None or now is None:
        return NEUTRAL_SCORE
    age_s = (now - timestamp).total_seconds()
    if age_s <= 0:
        return 1.0
    if age_s >= max_data_age_s:
        return 0.0
    return 1.0 - age_s / max_data_age_s


def _confirmation_component(signal: dict) -> float:
    direction = signal.get("signal", 0)
    if direction == 0:
        return NEUTRAL_SCORE
    features = signal.get("features_snapshot") or {}
    votes = []
    for column in CONFIRMATION_COLUMNS:
        value = features.get(column)
        if value is None or value == 0:
            continue
        votes.append(1.0 if (value > 0) == (direction > 0) else 0.0)
    if not votes:
        return NEUTRAL_SCORE
    return sum(votes) / len(votes)


def compute_confidence(
    signal: dict,
    *,
    now: "dt.datetime | None" = None,
    max_data_age_s: float = DEFAULT_MAX_DATA_AGE_S,
    weights: "dict | None" = None,
) -> float:
    """See module docstring. Always returns a float in `[0.0, 1.0]`
    (never `None` — with every component tolerant of missing data via its
    own neutral default, there's always *some* answer, even if it's an
    uninformative 0.5 for a mostly-empty `signal`)."""
    w = weights or DEFAULT_WEIGHTS
    total_weight = sum(w.values())
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive number")

    calibration = _calibration_component(signal)
    freshness = _freshness_component(signal, now=now, max_data_age_s=max_data_age_s)
    confirmation = _confirmation_component(signal)

    score = (
        w.get("calibration", 0.0) * calibration
        + w.get("freshness", 0.0) * freshness
        + w.get("confirmation", 0.0) * confirmation
    ) / total_weight
    return max(0.0, min(1.0, score))
