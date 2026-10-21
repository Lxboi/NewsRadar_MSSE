"""First statistical model: calibrated logistic regression predicting
P(up)/P(down)/P(neutral) for a given horizon/threshold pair.

Roadmap: PHASE 9. "Calibrated" per the module's original docstring means
the *raw* probabilities from `predict_proba()` — logistic regression's
own likelihood-based outputs are a reasonable starting point but not
guaranteed well-calibrated on this data; `calibration.py`'s
`fit_calibrated()` is what actually verifies/corrects that with a
held-out validation set, applied to any fitted model (this one or
`tree_model.py`'s), not baked in here.

One instance per `(asset, horizon_s, threshold_bps)` — mirrors the rest
of the codebase's one-instance-per-asset convention (`OrderBookEngine`,
`FeatureEngine`, `RegimeDetector`): a model trained for a 30s/10bps
question isn't the same model as one trained for a 300s/50bps question,
so the pair is part of the model's identity, not a call-time parameter.
"""
from __future__ import annotations

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from src.models.feature_matrix import CATEGORICAL_COLUMNS, extract_labels, feature_columns, rows_to_frame

DEFAULT_C = 1.0
DEFAULT_MAX_ITER = 1000


class LogisticDirectionModel:
    """`fit()`/`predict_proba()` both take Phase 7 feature+target rows
    directly (not a pre-built X/y matrix), so this composes with
    `scripts/build_feature_table.py`'s output and
    `models/evaluation.py`'s row-based conventions without a separate
    data-prep step at the call site."""

    def __init__(
        self,
        horizon_s: int,
        threshold_bps: float,
        *,
        C: float = DEFAULT_C,
        max_iter: int = DEFAULT_MAX_ITER,
        random_state: "int | None" = None,
    ) -> None:
        self.horizon_s = horizon_s
        self.threshold_bps = threshold_bps
        self.C = C
        self.max_iter = max_iter
        self.random_state = random_state
        self.feature_columns_: "list[str] | None" = None
        self.pipeline: "Pipeline | None" = None

    def fit(self, rows: "list[dict]") -> "LogisticDirectionModel":
        columns = self._feature_columns(rows)
        labels = extract_labels(rows, self.horizon_s, self.threshold_bps)
        keep = [i for i, y in enumerate(labels) if y is not None]
        if not keep:
            raise ValueError(
                f"no rows with a resolved target for horizon_s={self.horizon_s}, "
                f"threshold_bps={self.threshold_bps} — nothing to train on"
            )

        X = rows_to_frame([rows[i] for i in keep], columns)
        y = np.array([labels[i] for i in keep])

        self.feature_columns_ = columns
        self.pipeline = self._build_pipeline(columns)
        self.pipeline.fit(X, y)
        return self

    def _feature_columns(self, rows: "list[dict]") -> "list[str]":
        """Which columns `fit()` trains on. Factored out (Phase 15
        addendum 7) so a subclass can restrict the feature set without
        duplicating the rest of `fit()` -- `models/news_model.py:
        NewsDirectionModel` is the first such subclass, restricting to
        just the news-derived columns. Default behavior (every
        non-identifier, non-target column) is unchanged from before this
        method existed."""
        return feature_columns(rows)

    def predict_proba(self, rows: "list[dict]") -> "list[dict]":
        """Returns one `{"probability_down", "probability_neutral",
        "probability_up"}` dict per row, matching
        `storage/database.py: Signal`'s column names directly — a later
        phase wiring model output into a `Signal` row shouldn't need a
        translation step."""
        if self.pipeline is None or self.feature_columns_ is None:
            raise RuntimeError("call fit() before predict_proba()")
        X = rows_to_frame(rows, self.feature_columns_)
        proba = self.pipeline.predict_proba(X)
        class_order = list(self.pipeline.classes_)
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

    def _build_pipeline(self, columns: "list[str]") -> Pipeline:
        categorical = [c for c in columns if c in CATEGORICAL_COLUMNS]
        numeric = [c for c in columns if c not in CATEGORICAL_COLUMNS]
        # Logistic regression can't handle NaN or raw strings — median
        # imputation (robust to the outliers a microstructure feature
        # table is prone to, unlike a mean) + standardization (logistic
        # regression's coefficients/regularization are scale-sensitive,
        # and this table mixes OBI in [-1,1] with return fractions two
        # orders of magnitude smaller) ahead of one-hot for the regime.
        preprocessor = ColumnTransformer(
            [
                (
                    "numeric",
                    Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]),
                    numeric,
                ),
                ("categorical", OneHotEncoder(handle_unknown="ignore"), categorical),
            ]
        )
        model = LogisticRegression(C=self.C, max_iter=self.max_iter, random_state=self.random_state)
        return Pipeline([("preprocess", preprocessor), ("model", model)])
