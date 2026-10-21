"""Phase 4 tests: main.py's persistence wiring (market_state_consumer with
a ParquetStore, and sqlite_snapshot_writer) end to end, using tmp_path for
both stores. No network. See docs/09_tests_plan.md."""
import asyncio
import datetime as dt

import pytest

from src.main import AssetState, market_state_consumer, sqlite_snapshot_writer
from src.orderbook.orderbook_engine import OrderBookEngine
from src.storage.database import MarketData, SQLiteRecorder
from src.storage.parquet_store import ParquetStore


def db_url_for(tmp_path):
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.mark.asyncio
async def test_market_state_consumer_persists_raw_events_to_parquet(tmp_path):
    parquet_store = ParquetStore(root=tmp_path / "raw", flush_every=100)
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {"BTC": OrderBookEngine(coin="BTC")}
    queue: asyncio.Queue = asyncio.Queue()

    await queue.put(
        {
            "source": "hyperliquid",
            "ts_received": 1_700_000_000_000_000_000,
            "payload": {
                "type": "l2Book", "coin": "BTC", "ts_exchange_ms": 1,
                "best_bid": 100.0, "best_ask": 100.1,
                "bids": [(100.0, 1.0, 1)], "asks": [(100.1, 1.0, 1)],
            },
        }
    )
    await queue.put(
        {
            "source": "hyperliquid",
            "ts_received": 1_700_000_000_000_000_001,
            "payload": {
                "type": "trades", "coin": "BTC",
                "trades": [{"coin": "BTC", "side": "B", "price": 100.1, "size": 1.0, "ts_exchange_ms": 2, "tid": 1}],
            },
        }
    )

    task = asyncio.create_task(market_state_consumer("test", queue, state, engines, parquet_store))
    for _ in range(20):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert parquet_store.pending_row_count() == 3  # 2 l2 levels (1 bid + 1 ask) + 1 trade
    written = parquet_store.flush_all()
    assert len(written) == 2  # one part file for hyperliquid_l2, one for hyperliquid_trades


@pytest.mark.asyncio
async def test_market_state_consumer_without_parquet_store_still_works(tmp_path):
    """parquet_store is optional — passing None must not break Phase 2/3
    behavior (display state + engine updates)."""
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {"BTC": OrderBookEngine(coin="BTC")}
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(
        {
            "source": "hyperliquid", "ts_received": 1,
            "payload": {"type": "l2Book", "coin": "BTC", "ts_exchange_ms": 1,
                        "best_bid": 100.0, "best_ask": 100.1, "bids": [(100.0, 1.0, 1)], "asks": [(100.1, 1.0, 1)]},
        }
    )
    task = asyncio.create_task(market_state_consumer("test", queue, state, engines, None))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert state["BTC"].best_bid == 100.0


@pytest.mark.asyncio
async def test_sqlite_snapshot_writer_skips_assets_with_no_book_yet(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {"BTC": OrderBookEngine(coin="BTC")}  # never fed an l2Book -> engine.current is None

    task = asyncio.create_task(sqlite_snapshot_writer(recorder, state, engines, interval_s=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with recorder.Session() as session:
        assert session.query(MarketData).count() == 0  # nothing written — no snapshot yet


@pytest.mark.asyncio
async def test_sqlite_snapshot_writer_writes_once_book_exists(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {"BTC": OrderBookEngine(coin="BTC")}
    engines["BTC"].on_l2book(
        {"type": "l2Book", "coin": "BTC", "ts_exchange_ms": 1, "bids": [(100.0, 1.0, 1)], "asks": [(100.1, 1.0, 1)]},
        ts_received_ns=1,
    )

    task = asyncio.create_task(sqlite_snapshot_writer(recorder, state, engines, interval_s=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with recorder.Session() as session:
        assert session.query(MarketData).count() >= 1
