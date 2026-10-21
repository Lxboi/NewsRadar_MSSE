"""Phase 6 tests: news/market clock discipline (docs/11_news_market_sync.md).

Mechanically checks the two claims that audit document makes:

1. Every timestamp this system produces is UTC and tz-aware — nothing in
   the collection or storage path silently becomes naive or local-time
   (section 29 needs this to even make sense; a naive datetime is
   ambiguous about what it's relative to).
2. The no-look-ahead join contract holds: "which news is visible as of
   time t" depends only on `ingestion_time`, never on `event_time`, and is
   invariant to insertion order — the actual property Phase 7's feature
   engine will lean on.

No network. Real SQLite I/O against tmp_path, same pattern as
test_database_storage.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import random

import pytest

from src.collectors.hyperliquid_ws import HyperliquidWebSocketCollector
from src.collectors.rss_collector import _parse_event_time, normalize_entry
from src.storage.database import (
    NewsEvent,
    get_engine,
    get_sessionmaker,
    init_db,
    record_news_event,
)


def db_url_for(tmp_path):
    return f"sqlite:///{(tmp_path / 'clock.db').as_posix()}"


# -----------------------------------------------------------------------------
# 1. Every timestamp is UTC and tz-aware
# -----------------------------------------------------------------------------


def test_rss_normalize_entry_event_time_and_ingestion_time_are_utc_aware():
    ingestion_time = dt.datetime.now(dt.timezone.utc)
    feed_cfg = {"name": "test-feed", "category": "crypto_media", "url": "http://example.com/rss"}
    entry = {
        "id": "entry-1",
        "link": "http://example.com/article",
        "title": "Some headline",
        "summary": "Some summary",
        "published_parsed": (2026, 8, 18, 12, 0, 0, 0, 0, 0),
    }

    event = normalize_entry(feed_cfg, entry, ingestion_time)

    assert event["event_time"].tzinfo is not None
    assert event["event_time"].tzinfo == dt.timezone.utc
    assert event["ingestion_time"].tzinfo is not None
    assert event["ingestion_time"].tzinfo == dt.timezone.utc


def test_rss_parse_event_time_falls_back_to_tz_aware_ingestion_time():
    fallback = dt.datetime.now(dt.timezone.utc)
    entry_with_no_dates = {}

    result = _parse_event_time(entry_with_no_dates, fallback)

    assert result.tzinfo is not None
    assert result.tzinfo == dt.timezone.utc
    assert result == fallback


@pytest.mark.asyncio
async def test_hyperliquid_ts_received_is_epoch_ns_unambiguous_utc():
    """Hyperliquid's ts_received is an int (time.time_ns()), not a
    datetime — verifies it round-trips as plain Unix-epoch nanoseconds
    (inherently UTC, no tzinfo ambiguity possible) rather than something
    that could quietly become wall-clock/local time."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    collector = HyperliquidWebSocketCollector(
        ws_url="wss://example.invalid", assets=["BTC"], out_queue=queue
    )

    before_ns = __import__("time").time_ns()
    raw = (
        '{"channel":"l2Book","data":{"coin":"BTC","time":1,'
        '"levels":[[{"px":"100","sz":"1","n":1}],[{"px":"101","sz":"1","n":1}]]}}'
    )
    await collector._handle_message(raw)
    after_ns = __import__("time").time_ns()

    item = queue.get_nowait()
    assert isinstance(item["ts_received"], int)
    # Sane epoch-ns range: strictly between two real time.time_ns() calls
    # bracketing the call — could never happen if this were e.g. a small
    # local/relative counter.
    assert before_ns <= item["ts_received"] <= after_ns


def test_sqlite_news_event_round_trip_preserves_utc_tzinfo(tmp_path):
    """Regression test for the exact bug found during Phase 6's audit:
    SQLite has no native datetime type, and a plain `DateTime(timezone=True)`
    column silently drops tzinfo on read-back. `UTCDateTime` (see
    src/storage/database.py) must keep both event_time and ingestion_time
    tz-aware and equal to UTC through a real insert + fresh read."""
    url = db_url_for(tmp_path)
    init_db(url)
    engine = get_engine(url)
    Session = get_sessionmaker(engine)

    event_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    ingestion_time = dt.datetime.now(dt.timezone.utc)

    with Session() as session:
        row = record_news_event(
            session,
            event={
                "event_uid": "clock-test-uid",
                "source": "test-source",
                "url": "http://example.com/a",
                "title": "Round trip test",
                "event_time": event_time,
                "ingestion_time": ingestion_time,
            },
        )
        session.commit()
        row_id = row.id

    with Session() as session:
        fetched = session.get(NewsEvent, row_id)
        assert fetched.event_time.tzinfo == dt.timezone.utc
        assert fetched.ingestion_time.tzinfo == dt.timezone.utc
        # Not just "has a tzinfo" — the actual instant must be unchanged
        # (SQLite stores naive text/number internally; UTCDateTime must
        # convert to UTC before stripping tzinfo on write, not just strip
        # whatever tzinfo happened to be passed in).
        assert fetched.event_time == event_time
        assert fetched.ingestion_time == ingestion_time


def test_sqlite_news_event_rejects_naive_datetime(tmp_path):
    """UTCDateTime.process_bind_param must raise rather than silently
    accept a naive datetime — section 29 depends on every timestamp being
    unambiguously UTC, so a caller that forgets tzinfo should fail loudly
    at insert time, not produce a quietly-wrong row."""
    url = db_url_for(tmp_path)
    init_db(url)
    engine = get_engine(url)
    Session = get_sessionmaker(engine)

    naive_now = dt.datetime.now()  # deliberately no tzinfo

    with Session() as session:
        record_news_event(
            session,
            event={
                "event_uid": "clock-test-naive",
                "source": "test-source",
                "url": "http://example.com/b",
                "title": "Naive datetime should be rejected",
                "event_time": naive_now,
                "ingestion_time": dt.datetime.now(dt.timezone.utc),
            },
        )
        with pytest.raises(Exception):
            session.commit()


# -----------------------------------------------------------------------------
# 2. As-of visibility depends only on ingestion_time, never event_time,
#    and is invariant to insertion order (the no-look-ahead property).
# -----------------------------------------------------------------------------


def _visible_as_of(session, as_of: dt.datetime) -> set[str]:
    """What Phase 7's feature engine is allowed to see at time `as_of`:
    ingestion_time <= as_of, full stop — event_time is never part of the
    filter (section 29)."""
    rows = (
        session.query(NewsEvent)
        .filter(NewsEvent.ingestion_time <= as_of)
        .all()
    )
    return {row.event_uid for row in rows}


def test_asof_news_visibility_depends_only_on_ingestion_time(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    engine = get_engine(url)
    Session = get_sessionmaker(engine)

    base = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)

    # Deliberately disagreeing event_time/ingestion_time: event A claims to
    # have been published (event_time) *before* the window opens, but this
    # system didn't actually observe it (ingestion_time) until later —
    # e.g. a backdated or slow-to-propagate RSS entry. A feature computed
    # "as of" t=base+1min must NOT see it, even though its self-reported
    # event_time would suggest it should.
    events = [
        {
            "event_uid": "A-backdated",
            "event_time": base - dt.timedelta(hours=2),  # claims to be old
            "ingestion_time": base + dt.timedelta(minutes=10),  # actually observed late
        },
        {
            "event_uid": "B-oldest-ingested",
            "event_time": base,
            "ingestion_time": base,
        },
        {
            "event_uid": "C-mid-ingested",
            "event_time": base + dt.timedelta(minutes=30),  # published "in the future"
            "ingestion_time": base + dt.timedelta(minutes=2),  # but observed early
        },
        {
            "event_uid": "D-late-ingested",
            "event_time": base - dt.timedelta(minutes=1),
            "ingestion_time": base + dt.timedelta(minutes=20),
        },
    ]

    with Session() as session:
        for e in events:
            record_news_event(
                session,
                event={
                    "event_uid": e["event_uid"],
                    "source": "test-source",
                    "url": f"http://example.com/{e['event_uid']}",
                    "title": e["event_uid"],
                    "event_time": e["event_time"],
                    "ingestion_time": e["ingestion_time"],
                },
            )
        session.commit()

    as_of = base + dt.timedelta(minutes=5)
    with Session() as session:
        visible = _visible_as_of(session, as_of)

    # Only B (ingested at base) and C (ingested at base+2min) are <= as_of.
    # A and D were ingested later than as_of and must not be visible,
    # despite A's event_time being far in the past.
    assert visible == {"B-oldest-ingested", "C-mid-ingested"}


def test_asof_news_visibility_is_invariant_to_insertion_order(tmp_path):
    """Same synthetic event set as above, inserted in a shuffled order —
    the as-of-t visibility result must be identical regardless of the
    order rows landed in the table."""
    base = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
    events = [
        {
            "event_uid": f"E{i}",
            "event_time": base + dt.timedelta(minutes=random.Random(i).randint(-60, 60)),
            "ingestion_time": base + dt.timedelta(minutes=i * 3),
        }
        for i in range(10)
    ]
    as_of = base + dt.timedelta(minutes=15)
    expected_visible = {e["event_uid"] for e in events if e["ingestion_time"] <= as_of}
    assert expected_visible  # sanity: the test fixture actually exercises the filter
    assert len(expected_visible) < len(events)  # and actually excludes some rows

    shuffled = list(events)
    random.Random(42).shuffle(shuffled)

    results = []
    for attempt, ordering in enumerate((events, shuffled, list(reversed(events)))):
        db_url = f"sqlite:///{(tmp_path / f'order_{attempt}.db').as_posix()}"
        init_db(db_url)
        engine = get_engine(db_url)
        Session = get_sessionmaker(engine)
        with Session() as session:
            for e in ordering:
                record_news_event(
                    session,
                    event={
                        "event_uid": e["event_uid"],
                        "source": "test-source",
                        "url": f"http://example.com/{e['event_uid']}",
                        "title": e["event_uid"],
                        "event_time": e["event_time"],
                        "ingestion_time": e["ingestion_time"],
                    },
                )
            session.commit()
        with Session() as session:
            results.append(_visible_as_of(session, as_of))

    assert results[0] == expected_visible
    assert results[1] == expected_visible
    assert results[2] == expected_visible
