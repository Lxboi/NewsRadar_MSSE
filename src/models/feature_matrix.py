"""Turns Phase 7 feature+target rows (`list[dict]`, as produced by
`features/feature_engine.py` + `compute_targets()`) into the shapes
scikit-learn expects. Shared by `logistic_model.py` and `tree_model.py`
so both models select features and extract labels *identically* — any
difference there would make a "logistic vs. tree" comparison (the whole
point of section 30's model progression) unfair before either model even
starts training.

Roadmap: PHASE 9.
"""
from __future__ import annotations

import pandas as pd

from src.models.evaluation import actual_direction

# `price` is excluded deliberately, not an oversight: it's the one
# feature-table column that's non-stationary (its scale is whatever the
# asset's price level happened to be during the training window) — a
# coefficient/split learned on raw price wouldn't generalize to a
# different price regime. `asset`/`timestamp` are identifiers, not
# signal. All three are still present in the row dicts (a model's
# caller may want them for bookkeeping), just never fed to the
# estimator.
NON_FEATURE_COLUMNS = frozenset({"asset", "timestamp", "price"})

# The only categorical column docs/06_features_v1.md defines
# (`market_regime`, section 19's LOW/NORMAL/HIGH/EXTREME_VOL labels).
# Listed explicitly rather than dtype-sniffed: row dicts come from
# Parquet/JSON round-trips where "is this a string or a category" isn't
# reliably distinguishable, and an explicit, documented set is safer than
# guessing.
CATEGORICAL_COLUMNS = frozenset({"market_regime"})


def feature_columns(rows: "list[dict]") -> "list[str]":
    """Every column present across `rows` except identifiers/non-stationary
    columns (see `NON_FEATURE_COLUMNS`) and any `target_*` label column —
    excluded *by prefix*, not by an explicit name list, since which
    horizon/threshold combinations exist depends on what
    `compute_targets()` was called with. Getting this exclusion wrong
    would let a label leak into the feature set, which is a far more
    dangerous bug than a merely missing feature — sorted for a
    deterministic column order across calls."""
    columns: "set[str]" = set()
    for row in rows:
        columns.update(row.keys())
    return sorted(
        c for c in columns
        if c not in NON_FEATURE_COLUMNS and not c.startswith("target_")
    )


def rows_to_frame(rows: "list[dict]", columns: "list[str]") -> pd.DataFrame:
    """A `DataFrame` with exactly `columns`, in that order, one row per
    input row. Missing keys become `NaN` (via `.get(c)` -> `None` ->
    pandas' float-column NaN coercion) rather than raising — a model's
    `fit()`/`predict_proba()` decide how to handle missing values
    (imputation, native NaN support, etc.), not this function."""
    return pd.DataFrame([{c: row.get(c) for c in columns} for row in rows], columns=list(columns))


def extract_labels(rows: "list[dict]", horizon_s: int, threshold_bps: float) -> "list[int | None]":
    """`actual_direction()` (models/evaluation.py) applied to every row —
    +1/-1/0/`None` per row. Re-exported here (not reimplemented) so
    label extraction for baseline evaluation and for model training can
    never quietly diverge."""
    return [actual_direction(row, horizon_s, threshold_bps) for row in rows]
