"""Phase 9 tests: models/tree_model.py. Mirrors test_logistic_model.py's
structure (same fit(rows)/predict_proba(rows) contract, same synthetic
data shape) but adds the one behavior specific to
HistGradientBoostingClassifier: native NaN tolerance with no imputation
step, unlike the logistic model.
"""
import pytest

from src.models.tree_model import TreeDirectionModel


def _row(obi, regime="NORMAL_VOL", news_score=None, *, up, down):
    return {
        "asset": "BTC",
        "timestamp": 0,
        "price": 100.0,
        "obi": obi,
        "market_regime": regime,
        "news_score": news_score,
        "target_30s_up_10bps": 1 if up else 0,
        "target_30s_down_10bps": 1 if down else 0,
    }


def _separable_rows(n=200):
    rows = []
    for i in range(n):
        obi = 0.5 if i % 2 == 0 else -0.5
        rows.append(_row(obi, up=(obi > 0), down=(obi < 0)))
    return rows


def test_fit_raises_when_no_row_has_a_resolved_target():
    rows = [{"obi": 0.1, "market_regime": "NORMAL_VOL"}]
    with pytest.raises(ValueError):
        TreeDirectionModel(horizon_s=30, threshold_bps=10).fit(rows)


def test_predict_proba_before_fit_raises_runtime_error():
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10)
    with pytest.raises(RuntimeError):
        model.predict_proba([_row(0.1, up=True, down=False)])


def test_fit_then_predict_proba_learns_a_trivially_separable_relationship():
    rows = _separable_rows(200)
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba([_row(0.5, up=True, down=False), _row(-0.5, up=False, down=True)])
    assert proba[0]["probability_up"] > proba[0]["probability_down"]
    assert proba[1]["probability_down"] > proba[1]["probability_up"]


def test_predict_proba_output_shape_and_keys_match_signal_columns():
    rows = _separable_rows(200)
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba(rows[:5])
    assert len(proba) == 5
    for d in proba:
        assert set(d.keys()) == {"probability_down", "probability_neutral", "probability_up"}
        assert all(0.0 <= v <= 1.0 for v in d.values())
        assert d["probability_down"] + d["probability_neutral"] + d["probability_up"] == pytest.approx(1.0)


def test_handles_missing_numeric_feature_natively_without_imputation():
    # Unlike logistic_model.py, tree_model.py's numeric columns are
    # "passthrough" (no SimpleImputer) — this must not raise, and must
    # not silently drop the row either.
    rows = _separable_rows(200)
    rows[0]["obi"] = None
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba([{"obi": None, "market_regime": "NORMAL_VOL", "news_score": None}])
    assert len(proba) == 1


def test_handles_categorical_market_regime_column():
    rows = []
    for i in range(100):
        regime = "LOW_VOL" if i % 2 == 0 else "HIGH_VOL"
        rows.append(_row(0.1, regime=regime, up=True, down=False))
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba([_row(0.1, regime="EXTREME_VOL", up=True, down=False)])
    assert len(proba) == 1


def test_numeric_preprocessor_step_is_passthrough_not_imputed_or_scaled():
    rows = _separable_rows(50)
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    preprocessor = model.pipeline.named_steps["preprocess"]
    by_name = {name: transformer for name, transformer, _columns in preprocessor.transformers}
    assert by_name["numeric"] == "passthrough"


def test_feature_columns_recorded_on_the_model_exclude_target_and_identifier_columns():
    rows = _separable_rows(50)
    model = TreeDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    assert "asset" not in model.feature_columns_
    assert "timestamp" not in model.feature_columns_
    assert "price" not in model.feature_columns_
    assert all(not c.startswith("target_") for c in model.feature_columns_)
