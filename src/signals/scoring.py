"""Signal quality classification: A (high conviction) / B (interesting) /
C (weak) / NO TRADE (section 33).

Roadmap: PHASE 11. Thresholds default to `config/strategy.yaml:
signal_engine`'s own values (`quality_A_min_probability=0.68`,
`quality_B_min_probability=0.58`, `quality_C_min_probability=0.52`) —
that config block is literally named and scoped for this classification,
so this function is deliberately probability-only, not folding in
`confidence.py`'s separate confidence score. Keeping them apart is a
choice, not an oversight: probability is "how sure is the model", while
`compute_confidence()` layers in calibration quality/freshness/order-flow
confirmation on top. A caller (`SignalEngine`) can combine both however
it wants; this function answers exactly the question its config keys are
named for.

`signal` is expected to have `probability_up`/`probability_down`/
`probability_neutral` keys (as `signal_engine.py`'s own output, or any
raw `predict_proba()` row, provides) — this function reads exactly those
three, nothing else.
"""
from __future__ import annotations

from src.models.direction import direction_from_probabilities

DEFAULT_QUALITY_A_MIN_PROBABILITY = 0.68
DEFAULT_QUALITY_B_MIN_PROBABILITY = 0.58
DEFAULT_QUALITY_C_MIN_PROBABILITY = 0.52

QUALITY_A = "A"
QUALITY_B = "B"
QUALITY_C = "C"
NO_TRADE = "NO_TRADE"


def classify_signal_quality(
    signal: dict,
    *,
    quality_a_min_probability: float = DEFAULT_QUALITY_A_MIN_PROBABILITY,
    quality_b_min_probability: float = DEFAULT_QUALITY_B_MIN_PROBABILITY,
    quality_c_min_probability: float = DEFAULT_QUALITY_C_MIN_PROBABILITY,
) -> str:
    """`"A"`/`"B"`/`"C"`/`"NO_TRADE"`. `NO_TRADE` covers two distinct
    cases, both meaning "nothing to grade a directional call on": the
    model's own argmax class is neutral (`direction_from_probabilities()`
    returns `0`), or the winning directional probability doesn't even
    clear the lowest bar (`quality_c_min_probability`). A signal that's
    neutral by construction is never silently graded `"C"` just because
    its neutral probability happens to be a high number — quality grades
    are about conviction in a *direction*, not about how confident the
    model is that nothing will happen."""
    direction, probability = direction_from_probabilities(signal)
    if direction == 0:
        return NO_TRADE
    if probability >= quality_a_min_probability:
        return QUALITY_A
    if probability >= quality_b_min_probability:
        return QUALITY_B
    if probability >= quality_c_min_probability:
        return QUALITY_C
    return NO_TRADE
