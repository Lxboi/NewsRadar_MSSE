"""Phase 15 addendum 7 tests: models/news_model.py. Mirrors
test_logistic_model.py's style (deterministic synthetic rows, not
random sampling) since NewsDirectionModel reuses LogisticDirectionModel's
pipeline wholesale -- these tests focus on what's actually different:
the restricted feature set and the missing-column error.
"""
import pytest

from src.models.logistic_model import LogisticDirectionModel
from src.models.news_model import NEWS_FEATURE_COLUMNS, NewsDirectionModel


def _row(news_score, *, up, down, **extra_news):
    row = {
        "asset": "BTC",
        "timestamp": 0,
        "price": 100.0,
        "obi": 0.0,  # a non-news microstructure column, present but must be ignored
        "market_regime": "NORMAL_VOL",
        "news_score": news_score,
        "news_importance": extra_news.get("news_importance", 0.5),
        "news_novelty": extra_news.get("news_novelty", 0.5),
        "news_age": extra_news.get("news_age", 60.0),
        "news_credibility": extra_news.get("news_credibility", 0.5),
        "target_60s_up_10bps": 1 if up else 0,
        "target_60s_down_10bps": 1 if down else 0,
    }
    return row


def _separable_rows(n=200):
    # news_score > 0 -> always up, news_score < 0 -> always down, same
    # trivially-learnable-by-a-linear-model shape test_logistic_model.py
    # uses for `obi` -- the point here is the feature *restriction*, not
    # re-testing logistic regression's own fitting behavior.
    rows = []
    for i in range(n):
        score = 0.5 if i % 2 == 0 else -0.5
        rows.append(_row(score, up=(score > 0), down=(score < 0)))
    return rows


def test_fit_raises_when_a_news_column_is_missing():
    rows = _separable_rows(50)
    for row in rows:
        del row["news_credibility"]
    with pytest.raises(ValueError, match="news_credibility"):
        NewsDirectionModel(horizon_s=60, threshold_bps=10).fit(rows)


def test_fit_raises_when_no_row_has_a_resolved_target():
    rows = [_row(0.1, up=False, down=False)]
    rows[0].pop("target_60s_up_10bps")
    rows[0].pop("target_60s_down_10bps")
    with pytest.raises(ValueError):
        NewsDirectionModel(horizon_s=60, threshold_bps=10).fit(rows)


def test_predict_proba_before_fit_raises_runtime_error():
    model = NewsDirectionModel(horizon_s=60, threshold_bps=10)
    with pytest.raises(RuntimeError):
        model.predict_proba([_row(0.1, up=True, down=False)])


def test_fit_then_predict_proba_learns_the_separable_news_score_relationship():
    rows = _separable_rows(200)
    model = NewsDirectionModel(horizon_s=60, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba([_row(0.5, up=True, down=False), _row(-0.5, up=False, down=True)])
    assert proba[0]["probability_up"] > proba[0]["probability_down"]
    assert proba[1]["probability_down"] > proba[1]["probability_up"]


def test_predict_proba_output_shape_and_keys_match_signal_columns():
    rows = _separable_rows(200)
    model = NewsDirectionModel(horizon_s=60, threshold_bps=10, random_state=0).fit(rows)
    proba = model.predict_proba(rows[:5])
    assert len(proba) == 5
    for d in proba:
        assert set(d.keys()) == {"probability_down", "probability_neutral", "probability_up"}
        assert d["probability_down"] + d["probability_neutral"] + d["probability_up"] == pytest.approx(1.0)


def test_feature_columns_recorded_are_exactly_the_news_columns_not_microstructure_ones():
    # The whole point of this model -- `obi` is present on every row
    # (see _row()) but must never end up in feature_columns_.
    rows = _separable_rows(50)
    model = NewsDirectionModel(horizon_s=60, threshold_bps=10, random_state=0).fit(rows)
    assert set(model.feature_columns_) == set(NEWS_FEATURE_COLUMNS)
    assert "obi" not in model.feature_columns_
    assert "market_regime" not in model.feature_columns_


def test_logistic_direction_model_is_unaffected_by_the_refactor():
    # Pins that LogisticDirectionModel (the parent class) still trains on
    # every non-identifier/non-target column, including `obi` -- the
    # _feature_columns() override point NewsDirectionModel uses must not
    # have changed the parent's own default behavior.
    rows = _separable_rows(50)
    for row in rows:
        row["obi"] = row["news_score"]  # give the microstructure column real signal too
    model = LogisticDirectionModel(horizon_s=60, threshold_bps=10, random_state=0).fit(rows)
    assert "obi" in model.feature_columns_
    assert set(NEWS_FEATURE_COLUMNS) <= set(model.feature_columns_)
