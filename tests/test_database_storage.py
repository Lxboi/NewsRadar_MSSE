"""Phase 4 tests: SQLite schema creation and insert helpers, using a
throwaway file-based sqlite db per test (tmp_path). No network.
See docs/09_tests_plan.md."""
import datetime as dt

import pytest

from src.storage.database import (
    MarketData,
    NewsEvent,
    OrderbookFeatures,
    PerformanceSnapshot,
    Position,
    SQLiteRecorder,
    TradeFeatures,
    get_engine,
    get_sessionmaker,
    init_db,
    record_market_data,
    record_news_event,
    record_orderbook_features,
    record_performance_snapshot,
    record_position,
    record_trade_features,
)


def db_url_for(tmp_path):
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


def test_init_db_creates_all_expected_tables(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    engine = get_engine(url)
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    expected = {
        "news_events", "market_data", "orderbook_features", "trade_features",
        "signals", "orders", "fills", "positions", "performance",
    }
    assert expected.issubset(tables)


def test_init_db_is_idempotent(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    init_db(url)  # should not raise on a second call


def test_record_market_data_round_trip(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    now = dt.datetime.now(dt.timezone.utc)

    with Session() as session:
        record_market_data(
            session, asset="BTC", exchange="hyperliquid", timestamp=now,
            last_price=100.0, mid_price=100.05, best_bid=100.0, best_ask=100.1,
        )
        session.commit()

    with Session() as session:
        rows = session.query(MarketData).all()
        assert len(rows) == 1
        assert rows[0].asset == "BTC"
        assert rows[0].last_price == 100.0


def test_record_market_data_handles_none_values(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        record_market_data(
            session, asset="BTC", exchange="hyperliquid", timestamp=dt.datetime.now(dt.timezone.utc),
            last_price=None, mid_price=None, best_bid=None, best_ask=None,
        )
        session.commit()
    with Session() as session:
        row = session.query(MarketData).one()
        assert row.last_price == 0.0  # None coalesced to 0.0, not a crash


def test_record_orderbook_features_pulls_from_engine_metrics_shape(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    metrics = {
        "spread_abs": 0.1, "spread_bps": 10.0, "obi_1": 0.2, "obi_5": 0.1,
        "obi_10": 0.05, "obi_25": 0.02, "microprice": 100.05, "microprice_delta": 0.01,
        "liquidity_imbalance": 0.3, "wall_persistence": 5.0, "transient_liquidity_score": 0.1,
    }
    with Session() as session:
        record_orderbook_features(session, asset="ETH", timestamp=dt.datetime.now(dt.timezone.utc), metrics=metrics)
        session.commit()
    with Session() as session:
        row = session.query(OrderbookFeatures).one()
        assert row.obi_1 == 0.2
        assert row.liquidity_imbalance == 0.3


def test_record_trade_features_round_trip(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    stats = {
        "buy_volume": 5.0, "sell_volume": 2.0, "trade_imbalance": 0.428,
        "trade_velocity": 1.5, "avg_trade_size": 1.2, "large_trade_freq": 0.0,
    }
    with Session() as session:
        record_trade_features(session, asset="SOL", timestamp=dt.datetime.now(dt.timezone.utc), window_s=60, stats=stats)
        session.commit()
    with Session() as session:
        row = session.query(TradeFeatures).one()
        assert row.window_s == 60
        assert row.buy_volume == 5.0


def test_sqlite_recorder_write_asset_snapshot_writes_market_data_and_features(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    now = dt.datetime.now(dt.timezone.utc)
    engine_metrics = {
        "mid": 100.05, "best_bid": 100.0, "best_ask": 100.1,
        "spread_abs": 0.1, "spread_bps": 10.0, "obi_1": 0.1, "obi_5": 0.1,
        "obi_10": 0.1, "obi_25": 0.1, "microprice": 100.05, "microprice_delta": None,
        "liquidity_imbalance": None, "wall_persistence": None, "transient_liquidity_score": None,
    }
    trade_window_stats = {
        "1s": {"window_s": 1, "buy_volume": 1.0, "sell_volume": 0.0, "trade_imbalance": 1.0,
               "trade_velocity": 1.0, "avg_trade_size": 1.0, "large_trade_freq": None},
        "60s": {"window_s": 60, "buy_volume": 5.0, "sell_volume": 5.0, "trade_imbalance": 0.0,
                "trade_velocity": 0.1, "avg_trade_size": 1.0, "large_trade_freq": None},
    }

    recorder.write_asset_snapshot(
        asset="BTC", exchange="hyperliquid", timestamp=now,
        last_price=100.02, engine_metrics=engine_metrics, trade_window_stats=trade_window_stats,
    )

    with recorder.Session() as session:
        assert session.query(MarketData).count() == 1
        assert session.query(OrderbookFeatures).count() == 1
        assert session.query(TradeFeatures).count() == 2  # one per window


def test_sqlite_recorder_snapshot_without_trade_windows_still_writes(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    recorder.write_asset_snapshot(
        asset="BTC", exchange="hyperliquid", timestamp=dt.datetime.now(dt.timezone.utc),
        last_price=None, engine_metrics={}, trade_window_stats=None,
    )
    with recorder.Session() as session:
        assert session.query(MarketData).count() == 1
        assert session.query(TradeFeatures).count() == 0


# --- Phase 5: news_events insert helper / SQLiteRecorder.write_news_event ---


def _news_event(**overrides) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    event = {
        "event_uid": "abc123",
        "source": "CoinDesk",
        "url": "https://example.com/a",
        "title": "SEC approves spot ETF",
        "event_time": now,
        "ingestion_time": now,
        "event_type": "ETF",
        "sentiment": 0.5,
        "importance": 0.8,
        "credibility": 0.7,
        "novelty": 1.0,
        "market_relevance": 1.0,
        "affected_assets": ["BTC"],
        "duplicate_of": None,
        "raw_payload": {"id": "guid-1"},
    }
    event.update(overrides)
    return event


def test_record_news_event_inserts_a_row(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        result = record_news_event(session, event=_news_event())
        session.commit()
        assert result is not None
    with Session() as session:
        row = session.query(NewsEvent).one()
        assert row.event_uid == "abc123"
        assert row.event_type == "ETF"
        assert row.affected_assets == ["BTC"]
        assert row.duplicate_of is None


def test_record_news_event_is_idempotent_on_event_uid(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        record_news_event(session, event=_news_event())
        session.commit()
    with Session() as session:
        result = record_news_event(session, event=_news_event(title="a different title, same uid"))
        session.commit()
        assert result is None  # no second insert
    with Session() as session:
        assert session.query(NewsEvent).count() == 1


def test_record_news_event_resolves_duplicate_of_uid_to_canonical_int_id(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        record_news_event(session, event=_news_event(event_uid="canonical-1"))
        session.commit()
    with Session() as session:
        canonical_id = session.query(NewsEvent).filter_by(event_uid="canonical-1").one().id
        record_news_event(
            session, event=_news_event(event_uid="dup-1", duplicate_of="canonical-1")
        )
        session.commit()
    with Session() as session:
        dup_row = session.query(NewsEvent).filter_by(event_uid="dup-1").one()
        assert dup_row.duplicate_of == canonical_id


def test_record_news_event_duplicate_of_unresolvable_uid_degrades_to_none(tmp_path):
    """If the canonical event was never written (e.g. it didn't pass the
    caller's relevance gate), duplicate_of falls back to None instead of
    raising — the event is stored as if it were its own canonical."""
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        result = record_news_event(
            session, event=_news_event(event_uid="orphan-dup", duplicate_of="nonexistent-uid")
        )
        session.commit()
        assert result is not None
    with Session() as session:
        row = session.query(NewsEvent).filter_by(event_uid="orphan-dup").one()
        assert row.duplicate_of is None


def test_sqlite_recorder_write_news_event_round_trip_and_idempotency(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    assert recorder.write_news_event(_news_event()) is True
    assert recorder.write_news_event(_news_event()) is False  # same event_uid -> no-op
    with recorder.Session() as session:
        assert session.query(NewsEvent).count() == 1


def _closed_position(**overrides):
    # Exact shape strategy/strategy_engine.py: StrategyEngine._close_position()
    # produces (Phase 11) -- see that module for the full field list.
    now = dt.datetime.now(dt.timezone.utc)
    base = {
        "asset": "BTC", "side": "buy", "entry_price": 50000.0, "exit_price": 50100.0,
        "quantity": 0.02, "opened_at": now - dt.timedelta(seconds=30), "closed_at": now,
        "exit_reason": "fixed_tp", "gross_pnl": 2.0, "fees": 0.5, "slippage": 0.3, "net_pnl": 1.2,
        # extra bookkeeping keys the in-memory dict carries but the
        # Position table has no column for -- record_position() must
        # ignore these, not raise.
        "horizon_s": 30, "threshold_bps": 10, "entry_probability": 0.7, "entry_confidence": 0.8,
        "entry_signal_quality": "A", "entry_obi": 0.5, "entry_liquidity_imbalance": 0.4,
    }
    base.update(overrides)
    return base


def test_record_position_round_trip(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        record_position(session, closed_position=_closed_position())
        session.commit()
    with Session() as session:
        row = session.query(Position).one()
        assert row.asset == "BTC"
        assert row.side == "buy"
        assert row.entry_price == 50000.0
        assert row.exit_price == 50100.0
        assert row.exit_reason == "fixed_tp"
        assert row.net_pnl == 1.2


def test_record_position_ignores_extra_bookkeeping_keys():
    # Sanity: _closed_position() above genuinely includes keys with no
    # matching Position column (horizon_s, entry_confidence, etc.) --
    # asserting on the fixture itself so this regression test can't
    # silently stop testing what it claims to.
    extra_keys = set(_closed_position()) - {
        "asset", "side", "entry_price", "exit_price", "quantity", "opened_at",
        "closed_at", "exit_reason", "gross_pnl", "fees", "slippage", "net_pnl",
    }
    assert extra_keys  # the fixture must carry at least one non-column key


def test_sqlite_recorder_write_position(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    recorder.write_position(_closed_position(asset="ETH"))
    with recorder.Session() as session:
        row = session.query(Position).one()
        assert row.asset == "ETH"


def _performance_row(**overrides):
    base = {
        "period": "all_time", "trades": 3, "win_rate": 0.667, "gross_pnl": 6.0,
        "fees": 1.5, "slippage": 0.9, "net_pnl": 3.6, "max_drawdown": 0.5,
        "best_asset": "BTC", "best_setup": "fixed_tp", "worst_setup": "fixed_sl",
    }
    base.update(overrides)
    return base


def test_record_performance_snapshot_round_trip(tmp_path):
    url = db_url_for(tmp_path)
    init_db(url)
    Session = get_sessionmaker(get_engine(url))
    with Session() as session:
        record_performance_snapshot(session, row=_performance_row())
        session.commit()
    with Session() as session:
        row = session.query(PerformanceSnapshot).one()
        assert row.period == "all_time"
        assert row.trades == 3
        assert row.net_pnl == 3.6
        assert row.best_asset == "BTC"


def test_sqlite_recorder_write_performance_snapshot(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    recorder.write_performance_snapshot(_performance_row(period="daily"))
    with recorder.Session() as session:
        row = session.query(PerformanceSnapshot).one()
        assert row.period == "daily"


def test_performance_snapshot_row_from_to_performance_snapshot_row_writes_cleanly(tmp_path):
    # End-to-end with the real shaping helper, not just a hand-built dict
    # -- confirms to_performance_snapshot_row()'s output actually matches
    # what record_performance_snapshot()/PerformanceSnapshot expect.
    from src.monitoring.performance import PerformanceTracker, to_performance_snapshot_row

    tracker = PerformanceTracker()
    tracker.record_trade(_closed_position())
    snapshot = tracker.all_time_snapshot()
    row = to_performance_snapshot_row(snapshot)

    recorder = SQLiteRecorder(db_url_for(tmp_path))
    recorder.write_performance_snapshot(row)
    with recorder.Session() as session:
        db_row = session.query(PerformanceSnapshot).one()
        assert db_row.trades == 1
        assert db_row.net_pnl == snapshot["net_pnl"]
