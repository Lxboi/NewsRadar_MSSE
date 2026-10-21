"""Phase 5 tests: main.py's news pipeline wiring (news_consumer,
news_live_display) end to end, using tmp_path for storage. No network.
See docs/09_tests_plan.md."""
import asyncio
import datetime as dt
from collections import deque

import pytest

from src.main import news_consumer, news_live_display
from src.news.pipeline import NewsPipeline
from src.storage.database import NewsEvent, SQLiteRecorder
from src.storage.parquet_store import ParquetStore


def db_url_for(tmp_path):
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


def _raw_item(title: str, seconds: int = 0) -> dict:
    t = dt.datetime(2026, 8, 18, 12, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return {
        "source": "rss",
        "ts_received": 1_700_000_000_000_000_000 + seconds * 1_000_000_000,
        "payload": {
            "source": "CoinDesk",
            "category": "crypto_media",
            "url": f"https://example.com/{seconds}",
            "title": title,
            "summary": "",
            "event_time": t,
            "ingestion_time": t,
            "event_uid": f"uid-{seconds}",
            "raw_payload": {},
        },
    }


@pytest.mark.asyncio
async def test_news_consumer_persists_relevant_items_to_sqlite_and_parquet(tmp_path):
    parquet_store = ParquetStore(root=tmp_path / "raw", flush_every=100)
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    pipeline = NewsPipeline(keywords=["Bitcoin", "ETF"], rss_feeds=[{"name": "CoinDesk", "credibility": 0.7}])
    recent_news: "deque" = deque(maxlen=10)
    queue: asyncio.Queue = asyncio.Queue()

    await queue.put(_raw_item("SEC approves Bitcoin ETF"))
    await queue.put(_raw_item("Local bakery wins award", seconds=1))  # irrelevant -> not written to SQLite

    task = asyncio.create_task(news_consumer("test", queue, pipeline, recorder, parquet_store, recent_news))
    for _ in range(20):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with recorder.Session() as session:
        rows = session.query(NewsEvent).all()
        assert len(rows) == 1
        assert rows[0].title == "SEC approves Bitcoin ETF"

    assert len(recent_news) == 1
    # both raw items (relevant and irrelevant) are persisted to Parquet for replay/audit
    written = parquet_store.flush_all()
    assert parquet_store.pending_row_count() == 0
    assert len(written) == 1  # both rows land in the same news_raw/date=... partition


@pytest.mark.asyncio
async def test_news_consumer_works_without_parquet_store(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    pipeline = NewsPipeline(keywords=["Bitcoin"], rss_feeds=[])
    recent_news: "deque" = deque(maxlen=10)
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_raw_item("Bitcoin hits new high"))

    task = asyncio.create_task(news_consumer("test", queue, pipeline, recorder, None, recent_news))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with recorder.Session() as session:
        assert session.query(NewsEvent).count() == 1


@pytest.mark.asyncio
async def test_news_consumer_duplicate_not_added_to_recent_news_display(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    pipeline = NewsPipeline(keywords=["Bitcoin"], rss_feeds=[])
    recent_news: "deque" = deque(maxlen=10)
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_raw_item("Bitcoin surges past $100k on ETF inflows", seconds=0))
    await queue.put(_raw_item("Bitcoin surges past $100k amid ETF inflows", seconds=1))

    task = asyncio.create_task(news_consumer("test", queue, pipeline, recorder, None, recent_news))
    for _ in range(20):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # both rows exist in SQLite (duplicates are stored, just linked)...
    with recorder.Session() as session:
        assert session.query(NewsEvent).count() == 2
    # ...but only the canonical (non-duplicate) shows up in the live display feed
    assert len(recent_news) == 1


@pytest.mark.asyncio
async def test_news_live_display_handles_empty_history_without_crashing():
    recent_news: "deque" = deque()
    task = asyncio.create_task(news_live_display(recent_news, interval_s=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)  # no exception raised


@pytest.mark.asyncio
async def test_news_live_display_logs_recent_items(caplog):
    now = dt.datetime.now(dt.timezone.utc)
    recent_news = deque(
        [
            {
                "event_type": "ETF",
                "source": "CoinDesk",
                "affected_assets": ["BTC"],
                "sentiment": 0.5,
                "importance": 0.8,
                "credibility": 0.7,
                "novelty": 1.0,
                "ingestion_time": now,
                "initial_impact": 0.4,
                "half_life_s": 300,
                "title": "SEC approves Bitcoin ETF",
            }
        ]
    )
    with caplog.at_level("INFO"):
        task = asyncio.create_task(news_live_display(recent_news, interval_s=0.05))
        await asyncio.sleep(0.12)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert "SEC approves Bitcoin ETF" in caplog.text
