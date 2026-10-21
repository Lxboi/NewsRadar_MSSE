"""Phase 11 tests: signals/confidence.py — three independently
missing-tolerant components (calibration, freshness, confirmation)
combined by a weighted average.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.signals.confidence import (
    DEFAULT_MAX_DATA_AGE_S,
    DEFAULT_WEIGHTS,
    NEUTRAL_SCORE,
    compute_confidence,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def test_fully_empty_signal_returns_neutral():
    # Every component degrades to 0.5, weighted average of all-0.5 is 0.5.
    assert compute_confidence({}) == pytest.approx(NEUTRAL_SCORE)


def test_perfect_calibration_freshness_and_full_confirmation_scores_near_one():
    signal = {
        "signal": 1,
        "timestamp": BASE,
        "model_calibration": {"roc_auc_macro": 1.0},
        "features_snapshot": {"obi_10": 0.5, "trade_imbalance": 0.3, "news_score": 0.8},
    }
    score = compute_confidence(signal, now=BASE, max_data_age_s=3.0)
    assert score == pytest.approx(1.0)


def test_chance_level_calibration_scores_zero_for_that_component():
    signal_chance = {"model_calibration": {"roc_auc_macro": 0.5}}
    signal_missing = {}
    # roc_auc_macro=0.5 (chance) -> calibration component 0.0, strictly
    # below the neutral 0.5 a *missing* diagnostic gets — an unmeasured
    # model is not penalized, but a *measured-as-chance* model is.
    assert compute_confidence(signal_chance) < compute_confidence(signal_missing)


def test_calibration_component_clamped_for_out_of_range_input():
    # roc_auc_macro slightly above 1.0 (e.g. a rounding artifact upstream)
    # must not push the overall score above 1.0 or raise.
    signal = {"model_calibration": {"roc_auc_macro": 1.2}}
    score = compute_confidence(signal)
    assert 0.0 <= score <= 1.0


def test_freshness_decays_linearly_with_age():
    signal = {"timestamp": BASE}
    score_fresh = compute_confidence(signal, now=BASE, max_data_age_s=10.0, weights={"freshness": 1.0})
    score_half = compute_confidence(
        signal, now=BASE + dt.timedelta(seconds=5), max_data_age_s=10.0, weights={"freshness": 1.0}
    )
    score_stale = compute_confidence(
        signal, now=BASE + dt.timedelta(seconds=20), max_data_age_s=10.0, weights={"freshness": 1.0}
    )
    assert score_fresh == pytest.approx(1.0)
    assert score_half == pytest.approx(0.5)
    assert score_stale == pytest.approx(0.0)


def test_freshness_defaults_to_neutral_when_now_not_given():
    signal = {"timestamp": BASE}
    assert compute_confidence(signal, weights={"freshness": 1.0}) == pytest.approx(NEUTRAL_SCORE)


def test_freshness_defaults_to_neutral_when_timestamp_missing():
    assert compute_confidence({}, now=BASE, weights={"freshness": 1.0}) == pytest.approx(NEUTRAL_SCORE)


def test_confirmation_neutral_when_signal_is_neutral():
    signal = {"signal": 0, "features_snapshot": {"obi_10": 0.9, "trade_imbalance": 0.9, "news_score": 0.9}}
    assert compute_confidence(signal, weights={"confirmation": 1.0}) == pytest.approx(NEUTRAL_SCORE)


def test_confirmation_neutral_when_no_columns_available():
    signal = {"signal": 1, "features_snapshot": {}}
    assert compute_confidence(signal, weights={"confirmation": 1.0}) == pytest.approx(NEUTRAL_SCORE)


def test_confirmation_partial_agreement_is_fractional():
    # direction=1 (up); obi_10 agrees (positive), trade_imbalance disagrees
    # (negative), news_score missing -> 1 of 2 available votes agree.
    signal = {
        "signal": 1,
        "features_snapshot": {"obi_10": 0.4, "trade_imbalance": -0.2},
    }
    assert compute_confidence(signal, weights={"confirmation": 1.0}) == pytest.approx(0.5)


def test_confirmation_full_disagreement_scores_zero():
    signal = {"signal": 1, "features_snapshot": {"obi_10": -0.5}}
    assert compute_confidence(signal, weights={"confirmation": 1.0}) == pytest.approx(0.0)


def test_zero_value_confirmation_column_treated_as_no_vote():
    # value == 0 is neither confirming nor disconfirming -- must be
    # excluded from the vote, not treated as a "disagree" (sign comparison
    # against 0 would be ambiguous either way).
    signal = {"signal": 1, "features_snapshot": {"obi_10": 0.0, "trade_imbalance": 0.6}}
    assert compute_confidence(signal, weights={"confirmation": 1.0}) == pytest.approx(1.0)


def test_default_weights_sum_used_when_none_given():
    signal = {
        "signal": 1,
        "timestamp": BASE,
        "model_calibration": {"roc_auc_macro": 0.75},
        "features_snapshot": {"obi_10": 0.5},
    }
    default_score = compute_confidence(signal, now=BASE)
    explicit_score = compute_confidence(signal, now=BASE, weights=DEFAULT_WEIGHTS)
    assert default_score == pytest.approx(explicit_score)


def test_weights_need_not_sum_to_one_normalized_by_total():
    signal = {"model_calibration": {"roc_auc_macro": 1.0}}
    # weight of 2.0 (not 1.0) on the only active component should behave
    # identically to weight 1.0, since the function normalizes by the sum.
    score = compute_confidence(signal, weights={"calibration": 2.0})
    assert score == pytest.approx(1.0)


def test_zero_or_negative_total_weight_raises():
    with pytest.raises(ValueError):
        compute_confidence({}, weights={"calibration": 0.0, "freshness": 0.0, "confirmation": 0.0})


def test_result_always_within_unit_interval_and_never_none():
    signal = {
        "signal": -1,
        "timestamp": BASE,
        "model_calibration": {"roc_auc_macro": 0.9},
        "features_snapshot": {"obi_10": -0.3, "trade_imbalance": -0.1, "news_score": 0.2},
    }
    score = compute_confidence(signal, now=BASE + dt.timedelta(seconds=1), max_data_age_s=DEFAULT_MAX_DATA_AGE_S)
    assert score is not None
    assert 0.0 <= score <= 1.0
