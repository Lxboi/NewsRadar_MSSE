"""Phase 9 tests: models/feature_matrix.py — feature/label extraction
shared by logistic_model.py and tree_model.py. Pure computation over
hand-built row dicts, no I/O.
"""
import math

import pandas as pd
import pytest

from src.models.feature_matrix import (
    CATEGORICAL_COLUMNS,
    NON_FEATURE_COLUMNS,
    extract_labels,
    feature_columns,
    rows_to_frame,
)


def test_feature_columns_excludes_identifiers_and_price():
    rows = [{"asset": "BTC", "timestamp": 1, "price": 100.0, "obi": 0.1, "momentum_30s": 0.2}]
    cols = feature_columns(rows)
    assert "asset" not in cols
    assert "timestamp" not in cols
    assert "price" not in cols
    assert cols == sorted(["obi", "momentum_30s"])


def test_feature_columns_excludes_any_target_prefixed_column():
    rows = [
        {
            "obi": 0.1,
            "target_30s_up_10bps": 1,
            "target_30s_down_10bps": 0,
            "target_300s_up_50bps": 0,
            "target_300s_down_50bps": 1,
        }
    ]
    cols = feature_columns(rows)
    assert cols == ["obi"]
    assert all(not c.startswith("target_") for c in cols)


def test_feature_columns_is_union_across_rows_not_just_first_row():
    # Row 2 has a column row 1 lacks (e.g. news_score only present once
    # news actually fired) — feature_columns() must still surface it, or
    # rows_to_frame() would silently drop a real feature.
    rows = [{"obi": 0.1}, {"obi": 0.2, "news_score": 0.5}]
    assert feature_columns(rows) == ["news_score", "obi"]


def test_feature_columns_sorted_deterministic():
    rows = [{"zeta": 1, "alpha": 2, "mu": 3}]
    assert feature_columns(rows) == ["alpha", "mu", "zeta"]


def test_feature_columns_empty_rows_returns_empty_list():
    assert feature_columns([]) == []


def test_non_feature_and_categorical_columns_match_documented_sets():
    assert NON_FEATURE_COLUMNS == frozenset({"asset", "timestamp", "price"})
    assert CATEGORICAL_COLUMNS == frozenset({"market_regime"})


def test_rows_to_frame_preserves_column_order_and_values():
    rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    df = rows_to_frame(rows, ["b", "a"])
    assert list(df.columns) == ["b", "a"]
    assert df["b"].tolist() == [2, 4]
    assert df["a"].tolist() == [1, 3]


def test_rows_to_frame_missing_key_becomes_nan_not_a_raise():
    rows = [{"a": 1}, {"a": 2, "b": 5}]
    df = rows_to_frame(rows, ["a", "b"])
    assert math.isnan(df.loc[0, "b"])
    assert df.loc[1, "b"] == 5


def test_rows_to_frame_empty_rows_yields_empty_frame_with_columns():
    df = rows_to_frame([], ["a", "b"])
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["a", "b"]
    assert len(df) == 0


def test_extract_labels_matches_actual_direction_semantics():
    rows = [
        {"target_30s_up_10bps": 1, "target_30s_down_10bps": 0},  # up
        {"target_30s_up_10bps": 0, "target_30s_down_10bps": 1},  # down
        {"target_30s_up_10bps": 0, "target_30s_down_10bps": 0},  # flat
        {},  # unresolved -> None
    ]
    assert extract_labels(rows, horizon_s=30, threshold_bps=10) == [1, -1, 0, None]


def test_extract_labels_reuses_evaluation_actual_direction_not_a_reimplementation():
    # Regression guard for the Phase 9 refactor: feature_matrix.py must
    # import and call evaluation.actual_direction(), not carry its own
    # copy that could silently drift from the baseline evaluation's
    # definition of up/down/flat.
    import src.models.feature_matrix as fm

    assert fm.extract_labels.__module__ == "src.models.feature_matrix"
    import inspect

    src = inspect.getsource(fm)
    assert "actual_direction" in src
    assert "from src.models.evaluation import actual_direction" in src


def test_extract_labels_raises_on_row_with_both_up_and_down_set():
    # actual_direction() raises ValueError on this inconsistent input —
    # extract_labels() must propagate it, not swallow it, since a row
    # with both flags set indicates a corrupt/buggy target table.
    rows = [{"target_30s_up_10bps": 1, "target_30s_down_10bps": 1}]
    with pytest.raises(ValueError):
        extract_labels(rows, horizon_s=30, threshold_bps=10)
