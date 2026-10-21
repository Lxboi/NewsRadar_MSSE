"""Phase 11 tests: signals/scoring.py — probability-only signal quality
classification (A/B/C/NO_TRADE), deliberately independent of
signals/confidence.py's separate confidence score.
"""
from __future__ import annotations

import pytest

from src.signals.scoring import (
    DEFAULT_QUALITY_A_MIN_PROBABILITY,
    DEFAULT_QUALITY_B_MIN_PROBABILITY,
    DEFAULT_QUALITY_C_MIN_PROBABILITY,
    NO_TRADE,
    QUALITY_A,
    QUALITY_B,
    QUALITY_C,
    classify_signal_quality,
)


def _proba(p_up=0.0, p_down=0.0, p_neutral=0.0):
    return {"probability_up": p_up, "probability_down": p_down, "probability_neutral": p_neutral}


def test_high_conviction_up_grades_a():
    signal = _proba(p_up=0.90, p_down=0.05, p_neutral=0.05)
    assert classify_signal_quality(signal) == QUALITY_A


def test_high_conviction_down_grades_a():
    signal = _proba(p_up=0.05, p_down=0.90, p_neutral=0.05)
    assert classify_signal_quality(signal) == QUALITY_A


def test_mid_conviction_grades_b():
    signal = _proba(p_up=0.60, p_down=0.15, p_neutral=0.25)
    assert classify_signal_quality(signal) == QUALITY_B


def test_weak_conviction_grades_c():
    signal = _proba(p_up=0.53, p_down=0.20, p_neutral=0.27)
    assert classify_signal_quality(signal) == QUALITY_C


def test_below_c_threshold_is_no_trade():
    signal = _proba(p_up=0.40, p_down=0.30, p_neutral=0.30)
    assert classify_signal_quality(signal) == NO_TRADE


def test_neutral_argmax_is_no_trade_even_with_high_neutral_probability():
    # A signal whose model is very confident *nothing will happen* must
    # never be graded "C" just because its winning probability is high --
    # quality is about directional conviction, not confidence in inaction.
    signal = _proba(p_up=0.05, p_down=0.05, p_neutral=0.90)
    assert classify_signal_quality(signal) == NO_TRADE


@pytest.mark.parametrize(
    "probability,expected",
    [
        (DEFAULT_QUALITY_A_MIN_PROBABILITY, QUALITY_A),
        (DEFAULT_QUALITY_A_MIN_PROBABILITY - 1e-9, QUALITY_B),
        (DEFAULT_QUALITY_B_MIN_PROBABILITY, QUALITY_B),
        (DEFAULT_QUALITY_B_MIN_PROBABILITY - 1e-9, QUALITY_C),
        (DEFAULT_QUALITY_C_MIN_PROBABILITY, QUALITY_C),
        (DEFAULT_QUALITY_C_MIN_PROBABILITY - 1e-9, NO_TRADE),
    ],
)
def test_boundaries_are_inclusive_on_the_lower_bound(probability, expected):
    signal = _proba(p_up=probability, p_down=0.0, p_neutral=1 - probability)
    assert classify_signal_quality(signal) == expected


def test_custom_thresholds_override_defaults():
    signal = _proba(p_up=0.51, p_down=0.10, p_neutral=0.39)
    assert classify_signal_quality(signal) == NO_TRADE
    assert (
        classify_signal_quality(
            signal,
            quality_a_min_probability=0.9,
            quality_b_min_probability=0.6,
            quality_c_min_probability=0.5,
        )
        == QUALITY_C
    )


def test_works_directly_on_a_raw_predict_proba_row_not_just_signal_engine_output():
    # classify_signal_quality only reads the three probability_* keys, so
    # it should tolerate a bare predict_proba() row with no other keys.
    assert classify_signal_quality({"probability_up": 0.99, "probability_down": 0.005, "probability_neutral": 0.005}) == QUALITY_A
