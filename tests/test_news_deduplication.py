"""Phase 5 tests: title-similarity dedup (section 22). See docs/09_tests_plan.md."""
import datetime as dt

from src.news.deduplication import (
    DeduplicationTracker,
    deduplicate_events,
    normalize_title,
    title_similarity,
)


def _t(seconds: int) -> dt.datetime:
    return dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)


def test_normalize_title_lowercases_and_strips_punctuation():
    assert normalize_title("Bitcoin Surges 10%!!") == "bitcoin surges 10"


def test_title_similarity_identical_titles_is_one():
    assert title_similarity("Bitcoin hits new high", "Bitcoin hits new high") == 1.0


def test_title_similarity_unrelated_titles_is_low():
    assert title_similarity("Bitcoin hits new high", "Local bakery wins award") < 0.4


def test_title_similarity_empty_string_is_zero():
    assert title_similarity("", "Bitcoin hits new high") == 0.0


def test_deduplicate_events_collapses_known_duplicate_fixture_articles():
    events = [
        {"event_uid": "a", "title": "Bitcoin surges past $100k on ETF inflows", "event_time": _t(0)},
        {"event_uid": "b", "title": "Bitcoin surges past $100k amid ETF inflows", "event_time": _t(60)},
        {"event_uid": "c", "title": "Local weather forecast for the weekend", "event_time": _t(90)},
    ]
    result = deduplicate_events(events)
    by_uid = {e["event_uid"]: e for e in result}
    assert by_uid["a"]["duplicate_of"] is None  # earliest -> canonical
    assert by_uid["b"]["duplicate_of"] == "a"
    assert by_uid["c"]["duplicate_of"] is None  # unrelated -> its own canonical


def test_deduplicate_events_respects_time_window():
    events = [
        {"event_uid": "a", "title": "Bitcoin surges past $100k on ETF inflows", "event_time": _t(0)},
        {"event_uid": "b", "title": "Bitcoin surges past $100k on ETF inflows", "event_time": _t(3600)},
    ]
    result = deduplicate_events(events, time_window_s=900)
    by_uid = {e["event_uid"]: e for e in result}
    assert by_uid["b"]["duplicate_of"] is None  # identical title, but outside the window


def test_deduplicate_events_does_not_mutate_input():
    events = [{"event_uid": "a", "title": "X", "event_time": _t(0)}]
    deduplicate_events(events)
    assert "duplicate_of" not in events[0]


def test_deduplication_tracker_streaming_matches_batch_semantics():
    tracker = DeduplicationTracker()
    e1 = {"event_uid": "a", "title": "Ethereum upgrade goes live on mainnet", "event_time": _t(0)}
    e2 = {"event_uid": "b", "title": "Ethereum upgrade goes live on the mainnet", "event_time": _t(30)}
    e3 = {"event_uid": "c", "title": "Completely different unrelated headline here", "event_time": _t(31)}

    assert tracker.check_and_add(e1) is None
    assert tracker.check_and_add(e2) == "a"
    assert tracker.check_and_add(e3) is None


def test_deduplication_tracker_bounded_history_evicts_oldest():
    tracker = DeduplicationTracker(max_history=2)
    tracker.check_and_add({"event_uid": "a", "title": "A", "event_time": _t(0)})
    tracker.check_and_add({"event_uid": "b", "title": "B", "event_time": _t(1)})
    tracker.check_and_add({"event_uid": "c", "title": "C", "event_time": _t(2)})
    assert len(tracker._history) == 2
    assert tracker._history[0]["event_uid"] == "b"
