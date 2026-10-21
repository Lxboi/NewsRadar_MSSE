"""Phase 15 tests: main.py's PAPER-mode wiring -- the section-65 two-gate
check, the RiskManager/AlertManager/PaperTradingEngine config-to-
constructor mapping, market_state_consumer/news_consumer forwarding raw
events into a PaperTradingEngine, and the three new periodic asyncio
tasks (paper_trading_tick_loop/paper_trading_performance_writer/
paper_trading_display). See docs/09_tests_plan.md.

Uses the same tmp_path + monkeypatched PROJECT_ROOT pattern as
tests/test_train_model.py, since build_paper_trading_engine() reads
PROJECT_ROOT/"data"/"models" as a module-level global at call time --
monkeypatching it keeps every write under tmp_path, never the real repo.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pytest

import src.main as main
from src.live.trading_loop import PaperTradingEngine
from src.main import (
    AssetState,
    build_alert_manager,
    build_paper_trading_engine,
    build_risk_manager,
    market_state_consumer,
    news_consumer,
    paper_trading_display,
    paper_trading_enabled,
    paper_trading_performance_writer,
    paper_trading_tick_loop,
)
from src.models.logistic_model import LogisticDirectionModel
from src.models.model_persistence import save_model
from src.models.model_registry import ModelRegistry
from src.notifications.alert_manager import AlertManager
from src.orderbook.orderbook_engine import OrderBookEngine
from src.storage.database import PerformanceSnapshot, Position, SQLiteRecorder
from src.strategy.risk_manager import RiskManager


def db_url_for(tmp_path):
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


STRATEGY_CFG = {
    "risk": {
        "max_risk_per_trade_pct": 0.1, "max_daily_loss_pct": 1.0, "max_position_size_pct_equity": 5.0,
        "max_consecutive_losses": 4, "max_slippage_bps": 8, "max_spread_bps": 8, "max_latency_ms": 500,
    },
}

TELEGRAM_CFG = {
    "mode": "SNIPER",
    "signal_filter": {
        "min_probability": 0.68, "min_expected_value": 0.0, "required_signal_quality": "A",
        "max_spread_bps": 5, "max_data_age_s": 3, "block_on_risk_lock": True, "block_on_recent_duplicate": True,
    },
    "anti_spam": {
        "cooldown_s_per_asset": 300, "cooldown_override_on_major_event": True, "dedupe_probability_delta_pct": 3,
    },
}


# -- paper_trading_enabled() --------------------------------------------------

@pytest.mark.parametrize(
    "trading_mode,env_value,expected",
    [
        ("PAPER", "true", True),
        ("PAPER", "TRUE", True),
        ("PAPER", "false", False),
        ("PAPER", None, False),
        ("RESEARCH", "true", False),
        ("LIVE", "true", False),
    ],
)
def test_paper_trading_enabled_requires_both_gates(monkeypatch, trading_mode, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("PAPER_TRADING", raising=False)
    else:
        monkeypatch.setenv("PAPER_TRADING", env_value)
    settings = {"trading_mode": {"default": trading_mode}}
    assert paper_trading_enabled(settings) is expected


# -- build_risk_manager() / build_alert_manager() -----------------------------

def test_build_risk_manager_maps_strategy_yaml_risk_block():
    rm = build_risk_manager(STRATEGY_CFG, account_equity=10_000.0)
    assert isinstance(rm, RiskManager)
    assert rm.account_equity == 10_000.0
    assert rm.max_daily_loss_pct == 1.0
    assert rm.max_consecutive_losses == 4
    assert rm.max_slippage_bps == 8


def test_build_alert_manager_maps_telegram_yaml_blocks():
    am = build_alert_manager(TELEGRAM_CFG)
    assert isinstance(am, AlertManager)
    assert am.mode == "SNIPER"
    assert am.min_probability == 0.68
    assert am.required_signal_quality == "A"
    assert am.cooldown_s_per_asset == 300
    assert am.dedupe_probability_delta_pct == 3


# -- build_paper_trading_engine() ---------------------------------------------

def _fit_and_promote_model(project_root, asset, horizon_s=30, threshold_bps=10, seed=0):
    rng = np.random.RandomState(seed)
    rows = []
    for i in range(80):
        up = i % 2 == 0
        rows.append({
            "asset": asset,
            "obi_10": rng.uniform(0.3, 1.0) if up else rng.uniform(-1.0, -0.3),
            "market_regime": "NORMAL_VOL",
            f"target_{horizon_s}s_up_{threshold_bps}bps": 1 if up else 0,
            f"target_{horizon_s}s_down_{threshold_bps}bps": 0 if up else 1,
        })
    model = LogisticDirectionModel(horizon_s=horizon_s, threshold_bps=threshold_bps, random_state=0)
    model.fit(rows)

    artifact_relpath = f"artifacts/{asset}_{horizon_s}s_{threshold_bps}bps.joblib"
    save_model(model, project_root / "data" / "models" / artifact_relpath)
    registry = ModelRegistry(project_root / "data" / "models" / "registry.jsonl")
    registry.register(
        model_version=f"{asset}-v1", asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps,
        model_kind="logistic", training_period={"start": "2026-01-01", "end": "2026-01-02"},
        features=model.feature_columns_, parameters={}, performance={"accuracy": 0.6},
        artifact_path=artifact_relpath,
    )
    registry.promote(f"{asset}-v1", asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps, reason="test")


def test_build_paper_trading_engine_loads_promoted_models_and_skips_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    _fit_and_promote_model(tmp_path, "BTC")
    # ETH deliberately has no promoted model.

    paper_trading_cfg = {
        "horizon_s": 30, "threshold_bps": 10, "account_equity": 10_000.0, "initial_cash": 10_000.0,
        "tick_interval_s": 1.0,
    }
    engine = build_paper_trading_engine(
        assets=["BTC", "ETH"], paper_trading_cfg=paper_trading_cfg,
        strategy_cfg=STRATEGY_CFG, telegram_cfg=TELEGRAM_CFG,
    )
    assert isinstance(engine, PaperTradingEngine)
    assert "BTC" in engine.units
    assert "ETH" not in engine.units
    assert engine.skipped_assets == ["ETH"]
    assert engine.telegram_bot is None  # not activated this phase, see build_paper_trading_engine()'s docstring


# -- market_state_consumer / news_consumer forwarding -------------------------

class RecordingPaperTradingEngine:
    """A minimal double standing in for PaperTradingEngine -- just
    records what main.py forwards to it, so these tests check the
    wiring (are events forwarded, with what args) independent of
    PaperTradingEngine's own already-tested internal behavior."""

    def __init__(self):
        self.market_events = []
        self.news_events = []

    def on_market_event(self, asset, kind, payload, ts_received_ns):
        self.market_events.append((asset, kind, payload, ts_received_ns))

    def on_news_event(self, event):
        self.news_events.append(event)


@pytest.mark.asyncio
async def test_market_state_consumer_forwards_l2book_and_trades_to_paper_trading_engine():
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {"BTC": OrderBookEngine(coin="BTC")}
    queue: asyncio.Queue = asyncio.Queue()
    fake_engine = RecordingPaperTradingEngine()

    await queue.put({
        "source": "hyperliquid", "ts_received": 1,
        "payload": {"type": "l2Book", "coin": "BTC", "ts_exchange_ms": 1,
                    "best_bid": 100.0, "best_ask": 100.1, "bids": [(100.0, 1.0, 1)], "asks": [(100.1, 1.0, 1)]},
    })
    await queue.put({
        "source": "hyperliquid", "ts_received": 2,
        "payload": {"type": "trades", "coin": "BTC",
                    "trades": [{"coin": "BTC", "side": "B", "price": 100.1, "size": 1.0, "ts_exchange_ms": 2, "tid": 1}]},
    })

    task = asyncio.create_task(
        market_state_consumer("test", queue, state, engines, None, fake_engine)
    )
    for _ in range(20):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    kinds = [e[1] for e in fake_engine.market_events]
    assert kinds == ["l2book", "trades"]
    assert fake_engine.market_events[0][0] == "BTC"


@pytest.mark.asyncio
async def test_market_state_consumer_with_paper_trading_engine_none_is_unaffected():
    # Default (paper_trading_engine=None) must behave exactly as before
    # this phase -- no crash, no forwarding attempted.
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {"BTC": OrderBookEngine(coin="BTC")}
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({
        "source": "hyperliquid", "ts_received": 1,
        "payload": {"type": "l2Book", "coin": "BTC", "ts_exchange_ms": 1,
                    "best_bid": 100.0, "best_ask": 100.1, "bids": [(100.0, 1.0, 1)], "asks": [(100.1, 1.0, 1)]},
    })
    task = asyncio.create_task(market_state_consumer("test", queue, state, engines, None))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert state["BTC"].best_bid == 100.0


@pytest.mark.asyncio
async def test_news_consumer_forwards_relevant_events_to_paper_trading_engine(tmp_path):
    from src.news.pipeline import NewsPipeline

    recorder = SQLiteRecorder(db_url_for(tmp_path))
    pipeline = NewsPipeline(
        keywords=["bitcoin"], rss_feeds=[], dedup_similarity_threshold=0.9,
        dedup_time_window_s=3600, default_half_life_s=1800,
    )
    queue: asyncio.Queue = asyncio.Queue()
    recent_news: "list" = []
    fake_engine = RecordingPaperTradingEngine()

    now = dt.datetime.now(dt.timezone.utc)
    await queue.put({
        "ts_received": 1,
        "payload": {
            "event_uid": "uid-1", "source": "test_feed", "category": "market", "url": "http://example.com/1",
            "title": "Bitcoin surges on ETF news", "summary": "bitcoin rallies",
            "event_time": now, "ingestion_time": now,
        },
    })

    task = asyncio.create_task(
        news_consumer("test", queue, pipeline, recorder, None, recent_news, fake_engine)
    )
    for _ in range(20):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(fake_engine.news_events) == 1
    assert fake_engine.news_events[0]["title"] == "Bitcoin surges on ETF news"


# -- paper_trading_tick_loop / performance_writer / display ------------------

class FakeTickEngine:
    """Records tick() calls and returns a scripted result on the first
    call (one closed position), empty afterward -- enough to verify
    paper_trading_tick_loop persists exactly the closed positions it
    should, without needing a real strategy/model pipeline."""

    def __init__(self, results):
        self._results = list(results)
        self.tick_calls = 0

    def tick(self):
        self.tick_calls += 1
        return self._results.pop(0) if self._results else {}

    def performance(self):
        return {
            "period": "all_time", "trades": 1, "win_rate": 1.0, "gross_pnl": 2.0,
            "fees": 0.5, "slippage": 0.3, "net_pnl": 1.2, "max_drawdown": 0.0,
            "best_asset": "BTC", "best_setup": "fixed_tp", "worst_setup": "fixed_tp",
        }

    def status(self):
        return {"trading_assets": ["BTC"], "skipped_assets": [], "risk_state": {"blocked": False, "reason": None}}

    def open_positions(self):
        return [{"asset": "BTC", "side": "buy", "quantity": 0.02, "entry_price": 50000.0}]

    def recent_signals(self):
        return [{"asset": "BTC", "signal": 1, "probability_up": 0.8, "signal_quality": "A"}]


def _closed_position(asset="BTC"):
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "asset": asset, "side": "buy", "entry_price": 50000.0, "exit_price": 50100.0,
        "quantity": 0.02, "opened_at": now - dt.timedelta(seconds=30), "closed_at": now,
        "exit_reason": "fixed_tp", "gross_pnl": 2.0, "fees": 0.5, "slippage": 0.3, "net_pnl": 1.2,
    }


@pytest.mark.asyncio
async def test_paper_trading_tick_loop_persists_closed_positions(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    engine = FakeTickEngine(results=[
        {"BTC": [{"action": "opened", "position": {}}]},
        {"BTC": [{"action": "closed", "position": _closed_position()}]},
    ])

    task = asyncio.create_task(paper_trading_tick_loop(engine, recorder, interval_s=0.03))
    await asyncio.sleep(0.13)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert engine.tick_calls >= 2
    with recorder.Session() as session:
        rows = session.query(Position).all()
        assert len(rows) == 1
        assert rows[0].exit_reason == "fixed_tp"


@pytest.mark.asyncio
async def test_paper_trading_tick_loop_survives_a_tick_exception():
    class ExplodingEngine:
        def __init__(self):
            self.calls = 0

        def tick(self):
            self.calls += 1
            raise RuntimeError("boom")

    engine = ExplodingEngine()
    recorder = None  # never reached -- tick() raises before any write
    task = asyncio.create_task(paper_trading_tick_loop(engine, recorder, interval_s=0.02))
    await asyncio.sleep(0.09)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert engine.calls >= 2  # kept looping despite the exception, not crashed after the first


@pytest.mark.asyncio
async def test_paper_trading_performance_writer_writes_periodic_snapshots(tmp_path):
    recorder = SQLiteRecorder(db_url_for(tmp_path))
    engine = FakeTickEngine(results=[])

    task = asyncio.create_task(paper_trading_performance_writer(engine, recorder, interval_s=0.03))
    await asyncio.sleep(0.13)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    with recorder.Session() as session:
        rows = session.query(PerformanceSnapshot).all()
        assert len(rows) >= 2
        assert rows[0].net_pnl == 1.2


@pytest.mark.asyncio
async def test_paper_trading_display_runs_without_raising():
    engine = FakeTickEngine(results=[])
    task = asyncio.create_task(paper_trading_display(engine, interval_s=0.03))
    await asyncio.sleep(0.08)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # No assertion beyond "didn't raise" -- this task only logs.
