"""Phase 9 tests: models/calibration.py. Covers the diagnostics path
(calibrate()), the actual recalibration path (fit_calibrated()), and the
sklearn-1.8-specific roc_auc_score regression this module works around
(_safe_roc_auc_macro() — found via smoke-testing, see module docstring):
scikit-learn no longer raises ValueError when a class is entirely absent
from the validation labels, it silently returns nan with a warning, and
calibrate() must still report None in that case, not nan.
"""
import math

import numpy as np
import pytest

from src.models.calibration import (
    CLASS_ORDER,
    CLASS_PROBABILITY_KEYS,
    CalibratedDirectionModel,
    _multiclass_brier_score,
    _safe_roc_auc_macro,
    calibrate,
    calibration_curves,
    diagnostics_from_arrays,
    fit_calibrated,
    labels_and_proba,
)
from src.models.logistic_model import LogisticDirectionModel


def _row(obi, regime="NORMAL_VOL", *, up, down):
    return {
        "asset": "BTC",
        "timestamp": 0,
        "price": 100.0,
        "obi": obi,
        "market_regime": regime,
        "target_30s_up_10bps": 1 if up else 0,
        "target_30s_down_10bps": 1 if down else 0,
    }


def _balanced_rows(n=300, seed=0):
    # obi in [-1, 1] with a clear-but-noisy linear relationship to
    # direction, banded to also produce a nontrivial neutral class —
    # gives calibrate()/fit_calibrated() a real (not degenerate) 3-class
    # problem to work with.
    rng = np.random.RandomState(seed)
    rows = []
    for i in range(n):
        obi = float(rng.uniform(-1, 1))
        score = 0.7 * obi + rng.normal(0, 0.3)
        up, down = score > 0.15, score < -0.15
        rows.append(_row(obi, up=up, down=down))
    return rows


def test_multiclass_brier_score_is_zero_for_a_perfect_confident_forecast():
    y = np.array([1, -1, 0])
    proba = np.array(
        [
            [0.0, 0.0, 1.0],  # class order (-1, 0, 1); predicts 1 perfectly
            [1.0, 0.0, 0.0],  # predicts -1 perfectly
            [0.0, 1.0, 0.0],  # predicts 0 perfectly
        ]
    )
    assert _multiclass_brier_score(y, proba) == pytest.approx(0.0)


def test_multiclass_brier_score_is_positive_for_a_wrong_confident_forecast():
    y = np.array([1])
    proba = np.array([[1.0, 0.0, 0.0]])  # confidently predicts -1, actual is 1
    score = _multiclass_brier_score(y, proba)
    assert score > 1.0  # (1-0)^2 + (0-0)^2 + (0-1)^2 = 2.0


def test_calibration_curves_returns_one_curve_per_class_with_documented_keys():
    y = np.array([1, -1, 0, 1, -1, 0, 1, -1, 0, 1])
    proba = np.tile([0.3, 0.3, 0.4], (10, 1))
    curves = calibration_curves(y, proba, n_bins=2)
    assert set(curves.keys()) == set(CLASS_PROBABILITY_KEYS.values())
    for curve in curves.values():
        assert "mean_predicted" in curve
        assert "fraction_positive" in curve


def test_safe_roc_auc_macro_returns_none_when_a_class_is_entirely_absent():
    # Only classes -1 and 1 present in y; class 0 never occurs.
    y = np.array([1, 1, 1, -1, -1, -1])
    rng = np.random.RandomState(0)
    proba = rng.dirichlet([1, 1, 1], size=6)
    assert _safe_roc_auc_macro(y, proba) is None


def test_safe_roc_auc_macro_returns_a_float_in_zero_one_when_all_classes_present():
    y = np.array([1, 0, -1] * 20)
    rng = np.random.RandomState(0)
    proba = rng.dirichlet([1, 1, 1], size=60)
    auc = _safe_roc_auc_macro(y, proba)
    assert auc is not None
    assert not math.isnan(auc)
    assert 0.0 <= auc <= 1.0


def test_calibrate_diagnostics_shape_and_bounds_on_a_real_fitted_model():
    rows = _balanced_rows(300)
    train, val = rows[:200], rows[200:]
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(train)
    diag = calibrate(model, val, horizon_s=30, threshold_bps=10)
    assert diag["n_scored"] == len(val)
    assert diag["brier_score"] >= 0.0
    assert diag["log_loss"] >= 0.0
    assert diag["roc_auc_macro"] is None or 0.0 <= diag["roc_auc_macro"] <= 1.0
    assert set(diag["calibration_curve"].keys()) == set(CLASS_PROBABILITY_KEYS.values())


def test_calibrate_is_exactly_labels_and_proba_then_diagnostics_from_arrays():
    # Phase 15 addendum 6: calibrate() was refactored into two public
    # halves (labels_and_proba() + diagnostics_from_arrays()) so
    # scripts/train_model.py's purged k-fold CV can pool (y, proba)
    # across several folds' models before computing diagnostics once --
    # this pins down that the refactor didn't change calibrate()'s own
    # single-model behavior at all.
    rows = _balanced_rows(300)
    train, val = rows[:200], rows[200:]
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(train)

    via_calibrate = calibrate(model, val, horizon_s=30, threshold_bps=10)
    y, proba = labels_and_proba(model, val, horizon_s=30, threshold_bps=10)
    via_parts = diagnostics_from_arrays(y, proba)

    assert via_calibrate == via_parts


def test_diagnostics_from_arrays_pools_predictions_from_different_models():
    # The actual CV use case: two different fitted models, each scored
    # on its own row-set, pooled into one diagnostics call -- rather
    # than two separate diagnostics dicts averaged together (which would
    # over-weight whichever model happened to score fewer rows).
    rows = _balanced_rows(300)
    model_a = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows[:150])
    model_b = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=1).fit(rows[150:250])

    y_a, proba_a = labels_and_proba(model_a, rows[150:220], horizon_s=30, threshold_bps=10)
    y_b, proba_b = labels_and_proba(model_b, rows[250:], horizon_s=30, threshold_bps=10)

    pooled_y = np.concatenate([y_a, y_b])
    pooled_proba = np.concatenate([proba_a, proba_b])
    diag = diagnostics_from_arrays(pooled_y, pooled_proba)

    assert diag["n_scored"] == len(y_a) + len(y_b)
    assert diag["brier_score"] >= 0.0


def test_calibrate_roc_auc_macro_is_none_not_nan_when_validation_set_misses_a_class():
    # Train on a full 3-class distribution so the model itself can be
    # fit, but evaluate against a validation slice engineered to never
    # produce a "down" target — reproduces the exact scenario found via
    # smoke-testing where scikit-learn 1.8's roc_auc_score silently
    # returns nan instead of raising.
    rng = np.random.RandomState(3)
    train = []
    for i in range(300):
        obi = float(rng.uniform(-1, 1))
        up, down = obi > 0.3, obi < -0.3
        train.append(_row(obi, up=up, down=down))
    val = []
    for i in range(100):
        obi = float(rng.uniform(-0.3, 1.0))  # never below -0.3 -> never "down"
        up = obi > 0.3
        val.append(_row(obi, up=up, down=False))

    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(train)
    diag = calibrate(model, val, horizon_s=30, threshold_bps=10)
    assert diag["roc_auc_macro"] is None


def test_calibrate_raises_on_empty_resolvable_validation_set():
    rows = _balanced_rows(50)
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    unresolved = [{"obi": 0.1, "market_regime": "NORMAL_VOL"}]  # no target_* columns
    with pytest.raises(ValueError):
        calibrate(model, unresolved, horizon_s=30, threshold_bps=10)


def test_fit_calibrated_requires_a_fitted_model():
    unfitted = LogisticDirectionModel(horizon_s=30, threshold_bps=10)
    rows = _balanced_rows(50)
    with pytest.raises(RuntimeError):
        fit_calibrated(unfitted, rows, horizon_s=30, threshold_bps=10)


def test_fit_calibrated_returns_calibrated_model_with_matching_predict_proba_contract():
    rows = _balanced_rows(300)
    train, val, test = rows[:150], rows[150:250], rows[250:]
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(train)
    calibrated = fit_calibrated(model, val, horizon_s=30, threshold_bps=10, method="sigmoid")
    assert isinstance(calibrated, CalibratedDirectionModel)
    proba = calibrated.predict_proba(test[:5])
    assert len(proba) == 5
    for d in proba:
        assert set(d.keys()) == {"probability_down", "probability_neutral", "probability_up"}
        assert d["probability_down"] + d["probability_neutral"] + d["probability_up"] == pytest.approx(1.0)


def test_fit_calibrated_isotonic_method_also_works():
    rows = _balanced_rows(300, seed=7)
    train, val, test = rows[:150], rows[150:250], rows[250:]
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(train)
    calibrated = fit_calibrated(model, val, horizon_s=30, threshold_bps=10, method="isotonic")
    proba = calibrated.predict_proba(test[:3])
    assert len(proba) == 3


def test_calibrated_model_output_can_itself_be_fed_back_into_calibrate():
    # CalibratedDirectionModel exposes the same predict_proba(rows)
    # contract, so calibrate() should accept it as `model` too.
    rows = _balanced_rows(300, seed=11)
    train, val, test = rows[:150], rows[150:250], rows[250:]
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(train)
    calibrated = fit_calibrated(model, val, horizon_s=30, threshold_bps=10)
    diag = calibrate(calibrated, test, horizon_s=30, threshold_bps=10)
    assert diag["n_scored"] == len(test)


def test_fit_calibrated_raises_on_validation_set_with_no_resolved_targets():
    rows = _balanced_rows(50)
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    unresolved = [{"obi": 0.1, "market_regime": "NORMAL_VOL"}]
    with pytest.raises(ValueError):
        fit_calibrated(model, unresolved, horizon_s=30, threshold_bps=10)


def test_class_order_and_probability_keys_are_consistent():
    assert CLASS_ORDER == (-1, 0, 1)
    assert CLASS_PROBABILITY_KEYS == {
        -1: "probability_down",
        0: "probability_neutral",
        1: "probability_up",
    }
