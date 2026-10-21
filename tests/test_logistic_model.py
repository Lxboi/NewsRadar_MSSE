"""Phase 9 tests: models/logistic_model.py. Uses small, deterministically
constructed (not randomly sampled) synthetic feature+target tables so
accuracy assertions are non-flaky, plus the ColumnTransformer edge cases
(missing numeric values, a categorical column) the pipeline is built to
handle.
"""
import pytest

from src.models.logistic_model import LogisticDirectionModel


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
    # obi > 0 -> always up, obi < 0 -> always down: a linear model should
    # learn this trivially and near-perfectly.
    rows = []
    for i in range(n):
        obi = 0.5 if i % 2 == 0 else -0.5
        rows.append(_row(obi, up=(obi > 0), down=(obi < 0)))
    return rows


def test_fit_raises_when_no_row_has_a_resolved_target():
    rows = [{"obi": 0.1, "market_regime": "NORMAL_VOL"}]  # no target_* columns at all
    with pytest.raises(ValueError):
        LogisticDirectionModel(horizon_s=30, threshold_bps=10).fit(rows)


def test_predict_proba_before_fit_raises_runtime_error():
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10)
    with pytest.raises(RuntimeError):
        model.predict_proba([_row(0.1, up=True, down=False)])


def test_fit_then_predict_proba_learns_a_trivially_separable_relationship():
    rows = _separable_rows(200)
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba([_row(0.5, up=True, down=False), _row(-0.5, up=False, down=True)])
    assert proba[0]["probability_up"] > proba[0]["probability_down"]
    assert proba[1]["probability_down"] > proba[1]["probability_up"]


def test_predict_proba_output_shape_and_keys_match_signal_columns():
    rows = _separable_rows(200)
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba(rows[:5])
    assert len(proba) == 5
    for d in proba:
        assert set(d.keys()) == {"probability_down", "probability_neutral", "probability_up"}
        assert all(0.0 <= v <= 1.0 for v in d.values())
        assert d["probability_down"] + d["probability_neutral"] + d["probability_up"] == pytest.approx(1.0)


def test_handles_missing_numeric_feature_via_median_imputation():
    rows = _separable_rows(200)
    rows[0]["obi"] = None  # missing numeric value must not raise
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba([{"obi": None, "market_regime": "NORMAL_VOL", "news_score": None}])
    assert len(proba) == 1


def test_handles_categorical_market_regime_column():
    rows = []
    for i in range(100):
        regime = "LOW_VOL" if i % 2 == 0 else "HIGH_VOL"
        up = i % 4 < 2  # both classes present so the solver can fit
        rows.append(_row(0.1, regime=regime, up=up, down=not up))
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    # unseen category at predict time must not raise (handle_unknown="ignore")
    proba = model.predict_proba([_row(0.1, regime="EXTREME_VOL", up=True, down=False)])
    assert len(proba) == 1


def test_rows_with_flat_or_unresolved_target_are_excluded_from_training():
    rows = _separable_rows(50)
    rows.append({"obi": 0.9, "market_regime": "NORMAL_VOL"})  # unresolved target -> must be dropped, not error
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    assert model.pipeline is not None


def test_feature_columns_recorded_on_the_model_exclude_target_and_identifier_columns():
    rows = _separable_rows(50)
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0).fit(rows)
    assert "asset" not in model.feature_columns_
    assert "timestamp" not in model.feature_columns_
    assert "price" not in model.feature_columns_
    assert all(not c.startswith("target_") for c in model.feature_columns_)
