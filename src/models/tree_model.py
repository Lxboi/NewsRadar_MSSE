"""Gradient boosting / random forest models, tried only after the
logistic baseline is understood (section 30) — this file's job is
exactly the same contract as `logistic_model.py`'s
(`fit(rows)`/`predict_proba(rows)`, one instance per
`(asset, horizon_s, threshold_bps)`), so the two can be compared on equal
footing: same feature selection (`feature_matrix.py`), same label
extraction, same output shape.

Roadmap: PHASE 9. Uses `HistGradientBoostingClassifier` rather than
plain `GradientBoostingClassifier`/`RandomForestClassifier`: it natively
tolerates missing values in numeric columns (no imputation step, unlike
`logistic_model.py` — trees can split around a "missing" branch, a linear
model can't), and it's the lighter, histogram-binned implementation
scikit-learn recommends for anything beyond toy data sizes — worth
defaulting to given section 63's standing "keep heavy compute off the
Pi's real-time path" concern, even though model *training* happens
offline/research-side, not on the Pi.
"""
from __future__ import annotations

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from src.models.feature_matrix import CATEGORICAL_COLUMNS, extract_labels, feature_columns, rows_to_frame

DEFAULT_MAX_ITER = 100
DEFAULT_MAX_DEPTH = None
DEFAULT_LEARNING_RATE = 0.1


class TreeDirectionModel:
    """See module docstring. Same `fit(rows)`/`predict_proba(rows)`
    contract as `LogisticDirectionModel` by design — the two are meant to
    be dropped into the same evaluation code interchangeably."""

    def __init__(
        self,
        horizon_s: int,
        threshold_bps: float,
        *,
        max_iter: int = DEFAULT_MAX_ITER,
        max_depth: "int | None" = DEFAULT_MAX_DEPTH,
        learning_rate: float = DEFAULT_LEARNING_RATE,
        random_state: "int | None" = None,
    ) -> None:
        self.horizon_s = horizon_s
        self.threshold_bps = threshold_bps
        self.max_iter = max_iter
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.random_state = random_state
        self.feature_columns_: "list[str] | None" = None
        self.pipeline: "Pipeline | None" = None

    def fit(self, rows: "list[dict]") -> "TreeDirectionModel":
        columns = feature_columns(rows)
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

    def predict_proba(self, rows: "list[dict]") -> "list[dict]":
        """Same output shape as `LogisticDirectionModel.predict_proba()` —
        see that docstring for why it matches `storage/database.py:
        Signal`'s column names."""
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
        # No imputation, no scaling for the numeric side: trees split on
        # raw thresholds (scale-invariant) and HistGradientBoostingClassifier
        # natively routes NaN down a learned default branch at each split
        # — imputing first would just be throwing away information the
        # model can use directly (a `None` feature can itself be
        # informative, e.g. no relevant news yet).
        preprocessor = ColumnTransformer(
            [
                ("numeric", "passthrough", numeric),
                ("categorical", OneHotEncoder(handle_unknown="ignore"), categorical),
            ]
        )
        model = HistGradientBoostingClassifier(
            max_iter=self.max_iter,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            random_state=self.random_state,
        )
        return Pipeline([("preprocess", preprocessor), ("model", model)])
