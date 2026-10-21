"""Phase 5 test: NewsPipeline composes classify/sentiment/relevance/
credibility/novelty/dedup into one scored event per raw item. See
docs/09_tests_plan.md."""
import datetime as dt

from src.news.pipeline import NewsPipeline

FEEDS = [{"name": "CoinDesk", "credibility": 0.7}]
KEYWORDS = ["Bitcoin", "SEC", "ETF"]


def _raw(title: str, source: str = "CoinDesk", seconds: int = 0) -> dict:
    t = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return {
        "source": source,
        "category": "crypto_media",
        "url": "https://example.com/x",
        "title": title,
        "summary": "",
        "event_time": t,
        "ingestion_time": t,
        "event_uid": f"uid-{seconds}",
        "raw_payload": {},
    }


def _pipeline() -> NewsPipeline:
    return NewsPipeline(keywords=KEYWORDS, rss_feeds=FEEDS)


def test_process_returns_none_for_missing_title():
    pipeline = _pipeline()
    assert pipeline.process({"title": "", "summary": ""}) is None


def test_process_produces_all_expected_fields():
    pipeline = _pipeline()
    result = pipeline.process(_raw("SEC approves Bitcoin ETF"))
    for key in (
        "event_type", "sentiment", "market_relevance", "affected_assets",
        "credibility", "novelty", "importance", "duplicate_of", "half_life_s", "initial_impact",
    ):
        assert key in result


def test_process_scores_credibility_from_configured_feed():
    pipeline = _pipeline()
    result = pipeline.process(_raw("SEC approves Bitcoin ETF"))
    assert result["credibility"] == 0.7


def test_process_irrelevant_article_has_zero_relevance_and_importance():
    pipeline = _pipeline()
    result = pipeline.process(_raw("Local bakery wins award"))
    assert result["market_relevance"] == 0.0
    assert result["importance"] == 0.0


def test_process_first_event_is_fully_novel():
    pipeline = _pipeline()
    result = pipeline.process(_raw("SEC approves Bitcoin ETF"))
    assert result["novelty"] == 1.0


def test_process_flags_near_duplicate_and_reduces_novelty():
    pipeline = _pipeline()
    first = pipeline.process(_raw("SEC approves Bitcoin ETF today", seconds=0))
    second = pipeline.process(_raw("SEC approves the Bitcoin ETF today", seconds=30))
    assert first["duplicate_of"] is None
    assert second["duplicate_of"] == first["event_uid"]
    assert second["novelty"] < 0.3


def test_process_does_not_mutate_raw_event():
    pipeline = _pipeline()
    raw = _raw("SEC approves Bitcoin ETF")
    pipeline.process(raw)
    assert "event_type" not in raw
