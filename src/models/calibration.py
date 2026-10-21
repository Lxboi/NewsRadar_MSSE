"""Probability calibration (Platt/isotonic) and calibration diagnostics:
Brier score, log loss, ROC-AUC, calibration curve (section 30).

Roadmap: PHASE 9. The original Phase-1 stub signature was
`calibrate(model, X_val, y_val)` — adapted here to
`calibrate(model, rows, horizon_s, threshold_bps)` to match the row-based
convention every Phase 7-9 module already uses
(`FeatureEngine.build_rows()`, `compute_targets()`,
`models/evaluation.py`, `logistic_model.py`/`tree_model.py`'s
`fit(rows)`/`predict_proba(rows)`) — extracting `(X, y)` from feature+
target rows the same way everywhere means calibration can never silently
disagree with training/evaluation about what a row's label is.

Two separate concerns, both from the module's original docstring:

- **Diagnostics** (`calibrate()`): does this model's `predict_proba()`
  already produce trustworthy probabilities? Answered without changing
  the model at all — multiclass Brier score, log loss, macro-averaged
  one-vs-rest ROC-AUC, and a per-class reliability (calibration) curve.
- **Actual recalibration** (`fit_calibrated()`): wraps an already-fitted
  model with `sklearn.calibration.CalibratedClassifierCV` (Platt/sigmoid
  or isotonic), fit on a *held-out* validation set the base model never
  trained on, returning a new model with the same row-based
  `predict_proba()` contract. Uses `sklearn.frozen.FrozenEstimator` —
  the current (scikit-learn >= 1.6) replacement for the old
  `CalibratedClassifierCV(estimator, cv="prefit")` pattern, which was
  removed (confirmed against the installed scikit-learn 1.8: passing
  `cv="prefit"` now raises `InvalidParameterError`). `requirements.txt`
  is pinned to `scikit-learn>=1.6` specifically because of this.
"""
from __future__ import annotations

import warnings

import numpy as np
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.exceptions import UndefinedMetricWarning
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import log_loss, roc_auc_score

from src.models.feature_matrix import extract_labels, rows_to_frame

# Fixed class order used throughout this module — must match
# logistic_model.py/tree_model.py's predict_proba() key mapping.
CLASS_ORDER = (-1, 0, 1)
CLASS_PROBABILITY_KEYS = {-1: "probability_down", 0: "probability_neutral", 1: "probability_up"}
DEFAULT_CALIBRATION_BINS = 10


def _labels_and_proba_matrix(model, rows: "list[dict]", horizon_s: int, threshold_bps: float):
    labels = extract_labels(rows, horizon_s, threshold_bps)
    keep = [i for i, y in enumerate(labels) if y is not None]
    if not keep:
        raise ValueError("no rows with a resolved target to evaluate against")
    eval_rows = [rows[i] for i in keep]
    y = np.array([labels[i] for i in keep])
    proba_dicts = model.predict_proba(eval_rows)
    proba = np.array(
        [[d["probability_down"], d["probability_neutral"], d["probability_up"]] for d in proba_dicts]
    )
    return y, proba


def _multiclass_brier_score(y: np.ndarray, proba: np.ndarray) -> float:
    """scikit-learn's `brier_score_loss` is binary-only. The standard
    multiclass generalization: mean over samples of the sum of squared
    errors between predicted probability and the one-hot actual class,
    across all classes. 0.0 is a perfect, fully-confident-and-correct
    forecast; higher is worse."""
    onehot = np.zeros_like(proba)
    for k, cls in enumerate(CLASS_ORDER):
        onehot[:, k] = (y == cls).astype(float)
    return float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))


def _safe_roc_auc_macro(y: np.ndarray, proba: np.ndarray) -> "float | None":
    """`roc_auc_score(..., multi_class="ovr")` is undefined when a class
    is entirely absent from `y` (a validation window with, say, no
    genuine down-moves at all). Older scikit-learn raised `ValueError`
    for this, which is what this function originally just let propagate
    up as a caught exception; the installed scikit-learn (1.8) instead
    emits an `UndefinedMetricWarning` and silently returns `nan` for
    that class's contribution, so the macro average comes back `nan`
    without ever raising — confirmed empirically via smoke test, not
    assumed. A `nan` masquerading as a normal float is worse than an
    exception: `if roc_auc_macro is None` (the documented "undefined"
    check every caller of `calibrate()` should use) silently passes it
    through, and `nan` isn't valid strict JSON. So: check class presence
    *before* calling `roc_auc_score` and return `None` directly when it's
    not all there, rather than trusting the exception (or lack of one)
    scikit-learn happens to raise this version."""
    if set(np.unique(y).tolist()) != set(CLASS_ORDER):
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UndefinedMetricWarning)
            auc = roc_auc_score(y, proba, labels=list(CLASS_ORDER), multi_class="ovr", average="macro")
    except ValueError:
        return None
    return None if np.isnan(auc) else float(auc)


def calibration_curves(y: np.ndarray, proba: np.ndarray, n_bins: int = DEFAULT_CALIBRATION_BINS) -> dict:
    """One reliability curve per class, one-vs-rest (`calibration_curve()`
    itself is binary-only): for class `c`, bins samples by predicted
    `P(c)` and reports the actual fraction that were really class `c` in
    each bin. A well-calibrated model has `fraction_positive ≈
    mean_predicted` in every bin — this function only measures that, it
    doesn't fix it (`fit_calibrated()` is for fixing it)."""
    curves = {}
    for k, cls in enumerate(CLASS_ORDER):
        y_binary = (y == cls).astype(int)
        try:
            frac_pos, mean_pred = calibration_curve(y_binary, proba[:, k], n_bins=n_bins, strategy="quantile")
        except ValueError:
            # Too few distinct probability values (or too few examples of
            # this class) for `n_bins` quantile bins — degrade to a
            # coarser uniform binning rather than raising, since "not
            # enough data for a fine-grained curve" is a normal outcome
            # on a small validation set, not an error.
            frac_pos, mean_pred = calibration_curve(y_binary, proba[:, k], n_bins=2, strategy="uniform")
        curves[CLASS_PROBABILITY_KEYS[cls]] = {
            "mean_predicted": mean_pred.tolist(),
            "fraction_positive": frac_pos.tolist(),
        }
    return curves


def labels_and_proba(model, rows: "list[dict]", horizon_s: int, threshold_bps: float):
    """Public wrapper around `_labels_and_proba_matrix()` — for callers
    that need to *pool* `(y, proba)` across several models/row-sets
    before computing diagnostics, rather than get one model's
    single-call diagnostics dict from `calibrate()` directly. Added
    Phase 15 addendum 6 for `scripts/train_model.py`'s purged k-fold CV:
    each fold fits its own model and scores its own held-out rows, and
    the honest way to report one pooled ROC-AUC/Brier/log-loss/
    calibration-curve across all folds is to concatenate every fold's
    `(y, proba)` arrays and run the diagnostics math once — not average
    each fold's own diagnostics, which double-counts folds with more
    resolved rows the same as folds with fewer."""
    return _labels_and_proba_matrix(model, rows, horizon_s, threshold_bps)


def diagnostics_from_arrays(y: np.ndarray, proba: np.ndarray, *, n_bins: int = DEFAULT_CALIBRATION_BINS) -> dict:
    """The shared tail end of `calibrate()` — brier/log-loss/roc-auc/
    calibration-curve computed directly from already-built `(y, proba)`
    arrays, factored out (Phase 15 addendum 6) so pooled cross-
    validation evaluation (see `labels_and_proba()`) can reuse the exact
    same math `calibrate()` uses for a single model, instead of a
    second implementation that could silently drift out of sync."""
    auc = _safe_roc_auc_macro(y, proba)
    return {
        "n_scored": len(y),
        "brier_score": _multiclass_brier_score(y, proba),
        "log_loss": float(log_loss(y, proba, labels=list(CLASS_ORDER))),
        "roc_auc_macro": auc,
        "calibration_curve": calibration_curves(y, proba, n_bins=n_bins),
    }


def calibrate(
    model,
    rows: "list[dict]",
    horizon_s: int,
    threshold_bps: float,
    *,
    n_bins: int = DEFAULT_CALIBRATION_BINS,
) -> dict:
    """Diagnostics only — never modifies `model`. `rows` should be a
    validation set (ideally one `model` never saw during `fit()`;
    running this against the training set itself will overstate how
    calibrated the model really is, same as any train/validation
    hygiene). Returns `n_scored`, `brier_score`, `log_loss`,
    `roc_auc_macro` (`None` if a class is entirely absent from `rows`,
    since ROC-AUC is undefined against a validation set with no negative
    or no positive examples of that class — see `_safe_roc_auc_macro()`
    for why this is checked explicitly rather than caught as an
    exception), and `calibration_curve` (per-class reliability curves)."""
    y, proba = labels_and_proba(model, rows, horizon_s, threshold_bps)
    return diagnostics_from_arrays(y, proba, n_bins=n_bins)


class CalibratedDirectionModel:
    """Returned by `fit_calibrated()`. Wraps a fitted
    `CalibratedClassifierCV` behind the same row-based `predict_proba()`
    contract `LogisticDirectionModel`/`TreeDirectionModel` expose, so
    calibrated and uncalibrated models are interchangeable to any caller
    (`calibrate()` itself included — you can re-run diagnostics on a
    `CalibratedDirectionModel` the same way)."""

    def __init__(self, calibrated_pipeline: CalibratedClassifierCV, feature_columns: "list[str]") -> None:
        self._calibrated_pipeline = calibrated_pipeline
        self.feature_columns_ = feature_columns

    def predict_proba(self, rows: "list[dict]") -> "list[dict]":
        X = rows_to_frame(rows, self.feature_columns_)
        proba = self._calibrated_pipeline.predict_proba(X)
        class_order = list(self._calibrated_pipeline.classes_)
        out = []
        for row_proba in proba:
            by_class = dict(zip(class_order, row_proba))
            out.append(
                {
                    "probability_down": float(by_class.get(-1, 0.0)),
                    "probability_neutral": float(by_class.get(0, 0.0)),
                    "probability_up": float(by_class.get(1, 0.0)),
                }
            )
        return out


def fit_calibrated(
    model,
    rows: "list[dict]",
    horizon_s: int,
    threshold_bps: float,
    *,
    method: str = "sigmoid",
) -> CalibratedDirectionModel:
    """`model` must already be fit()'d (a `LogisticDirectionModel` or
    `TreeDirectionModel`). `rows` must be a held-out validation set
    disjoint from whatever `model.fit()` trained on — this function has
    no way to enforce that split itself, same as
    `docs/08_backtesting_strategy.md`'s train/validation/out-of-sample
    discipline generally; the caller owns it. `method`: `"sigmoid"`
    (Platt scaling) or `"isotonic"` — per scikit-learn's own guidance,
    prefer `"sigmoid"` when the validation set is small (isotonic
    regression overfits easily below roughly 1000 samples)."""
    if model.pipeline is None or model.feature_columns_ is None:
        raise RuntimeError("model must be fit() before calibration")

    labels = extract_labels(rows, horizon_s, threshold_bps)
    keep = [i for i, y in enumerate(labels) if y is not None]
    if not keep:
        raise ValueError("no rows with a resolved target to calibrate against")
    X = rows_to_frame([rows[i] for i in keep], model.feature_columns_)
    y = np.array([labels[i] for i in keep])

    calibrated_pipeline = CalibratedClassifierCV(FrozenEstimator(model.pipeline), method=method)
    calibrated_pipeline.fit(X, y)
    return CalibratedDirectionModel(calibrated_pipeline, model.feature_columns_)
