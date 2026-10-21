"""Phase 15 tests: live/trading_loop.py -- the PaperTradingEngine/
AssetTradingUnit orchestrator wiring FeatureEngine -> SignalEngine ->
StrategyEngine -> RiskManager/AlertManager/TelegramBot ->
PerformanceTracker into one runnable pipeline.

Uses a small `LogisticDirectionModel` fit on synthetic obi_10-vs-target
rows (same convention as test_model_persistence.py) rather than mocking
predict_proba, so these tests exercise the real Phase 9 model contract
SignalEngine actually depends on, not a stand-in.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from src.live.trading_loop import PaperTradingEngine, load_promoted_model
from src.models.logistic_model import LogisticDirectionModel
from src.models.model_persistence import save_model
from src.models.model_registry import ModelRegistry
from src.notifications.alert_manager import AlertManager
from src.strategy.risk_manager import RiskManager

NOW = dt.datetime(2026, 8, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def make_rows(n=80, seed=0):
    rng = np.random.RandomState(seed)
    rows = []
    for i in range(n):
        up = i % 2 == 0
        rows.append({
            "asset": "BTC",
            "obi_10": rng.uniform(0.3, 1.0) if up else rng.uniform(-1.0, -0.3),
            "market_regime": "NORMAL_VOL",
            "target_30s_up_10bps": 1 if up else 0,
            "target_30s_down_10bps": 0 if up else 1,
        })
    return rows


def fit_model(seed=0):
    model = LogisticDirectionModel(horizon_s=30, threshold_bps=10, random_state=0)
    model.fit(make_rows(seed=seed))
    return model


def loaded_model_dict(model, version="test-v1"):
    return {
        "model": model,
        "model_version": version,
        "horizon_s": 30,
        "threshold_bps": 10,
        "model_calibration": None,
    }


def l2book_payload(*, ts, best_bid, best_ask, bid_size=50.0, ask_size=1.0):
    """A synthetic l2Book payload matching
    collectors.hyperliquid_ws.parse_hyperliquid_message's shape:
    bids/asks as (price, size, n_orders) tuples, best-first."""
    return {
        "coin": "BTC",
        "ts_exchange_ms": int(ts.timestamp() * 1000),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bids": [(best_bid, bid_size, 10), (best_bid - 10, bid_size, 8)],
        "asks": [(best_ask, ask_size, 1), (best_ask + 10, ask_size, 1)],
    }


def engine_with_forced_entry(*, assets, model_loader, risk_manager=None, alert_manager=None, telegram_bot=None,
                              performance_tracker=None):
    """A PaperTradingEngine whose entry filter is loosened enough that a
    strongly-imbalanced synthetic book reliably opens a position --
    matches the exact kwargs validated by manual smoke-testing (default
    thresholds reject a synthetic single-book scenario on
    min_expected_value, not because the wiring is broken)."""
    return PaperTradingEngine(
        assets=assets,
        model_loader=model_loader,
        risk_manager=risk_manager or RiskManager(account_equity=10_000.0),
        alert_manager=alert_manager,
        telegram_bot=telegram_bot,
        performance_tracker=performance_tracker,
        strategy_engine_kwargs={
            "entry_kwargs": {
                "probability_min": 0.0,
                "min_expected_value": None,
                "max_spread_bps": None,
                "allowed_regimes": None,
            },
        },
    )


class FakeTelegramBot:
    def __init__(self):
        self.sent = []

    def send_alert(self, alert):
        self.sent.append(alert)
        return "sent"


# -- construction / skip behavior --------------------------------------------

def test_asset_with_no_promoted_model_is_skipped_not_fatal():
    model = fit_model()

    def model_loader(asset):
        return loaded_model_dict(model) if asset == "BTC" else None

    engine = PaperTradingEngine(
        assets=["BTC", "ETH"], model_loader=model_loader, risk_manager=RiskManager(account_equity=10_000.0),
    )
    assert "BTC" in engine.units
    assert "ETH" not in engine.units
    assert engine.skipped_assets == ["ETH"]


def test_all_assets_skipped_leaves_an_empty_but_working_engine():
    engine = PaperTradingEngine(
        assets=["BTC", "ETH"], model_loader=lambda asset: None, risk_manager=RiskManager(account_equity=10_000.0),
    )
    assert engine.units == {}
    assert engine.skipped_assets == ["BTC", "ETH"]
    assert engine.tick(now=NOW) == {}


# -- tick() with no book snapshot yet ----------------------------------------

def test_tick_with_no_book_snapshot_yet_returns_empty_actions_not_a_crash():
    model = fit_model()
    engine = engine_with_forced_entry(assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model))
    result = engine.tick(now=NOW)
    assert result == {"BTC": []}
    assert engine.recent_signals() == []


# -- full open -> close cycle -------------------------------------------------

def test_full_open_and_close_cycle_updates_risk_and_performance_and_alerts():
    model = fit_model()
    telegram_bot = FakeTelegramBot()
    alert_manager = AlertManager()
    risk_manager = RiskManager(account_equity=10_000.0)
    engine = engine_with_forced_entry(
        assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model),
        risk_manager=risk_manager, alert_manager=alert_manager, telegram_bot=telegram_bot,
    )

    # Strongly bid-imbalanced book -> obi_10 skewed positive -> the
    # trained model's "up" probability should dominate.
    engine.on_market_event("BTC", "l2book", l2book_payload(ts=NOW, best_bid=50000.0, best_ask=50010.0),
                            ts_received_ns=int(NOW.timestamp() * 1e9))
    result = engine.tick(now=NOW)
    assert result["BTC"] and result["BTC"][0]["action"] == "opened"
    assert len(engine.open_positions()) == 1

    # Move the mid up >15bps (fixed take-profit) to force a close.
    t_move = NOW + dt.timedelta(seconds=20)
    engine.on_market_event(
        "BTC", "l2book", l2book_payload(ts=t_move, best_bid=50200.0, best_ask=50210.0),
        ts_received_ns=int(t_move.timestamp() * 1e9),
    )
    result = engine.tick(now=t_move)
    actions = result["BTC"]
    # A close frees a slot the same tick, and the signal is still
    # strongly bullish (unchanged synthetic imbalance) -- the strategy
    # legitimately re-enters immediately (matches StrategyEngine.
    # process_row()'s documented close-then-reopen-in-one-call order).
    # What this test verifies is the close itself and its downstream
    # bookkeeping, not that the account goes flat.
    assert [a["action"] for a in actions] == ["closed", "opened"]

    perf = engine.performance()
    assert perf["trades"] == 1
    assert perf["net_pnl"] > 0  # favorable move, should be a winning trade

    alert_types = [a["type"] for a in telegram_bot.sent]
    assert "POSITION_OPENED" in alert_types
    assert "POSITION_CLOSED" in alert_types


def test_high_conviction_signal_alert_fires_even_without_a_trade():
    # A tight entry filter (defaults) means the strategy won't open a
    # position, but AlertManager's own looser signal_filter can still
    # consider the same signal alert-worthy -- the two filters are
    # deliberately different questions (see alert_manager.py).
    model = fit_model()
    telegram_bot = FakeTelegramBot()
    # min_expected_value=None: the synthetic single-book scenario used
    # across these tests produces a strongly directional but cost-negative
    # expected_value (a real, correctly-computed cost model reacting to an
    # unrealistic static book) -- irrelevant to what this test checks
    # (that the signal-alert path is independent of the trade-entry path).
    alert_manager = AlertManager(mode="AGGRESSIVE", min_expected_value=None)
    engine = PaperTradingEngine(
        assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model),
        risk_manager=RiskManager(account_equity=10_000.0), alert_manager=alert_manager, telegram_bot=telegram_bot,
        strategy_engine_kwargs={"entry_kwargs": {"probability_min": 1.1}},  # impossible to pass -> never opens
    )
    engine.on_market_event("BTC", "l2book", l2book_payload(ts=NOW, best_bid=50000.0, best_ask=50010.0),
                            ts_received_ns=int(NOW.timestamp() * 1e9))
    result = engine.tick(now=NOW)
    assert result["BTC"] == []  # confirms no trade was made
    alert_types = [a["type"] for a in telegram_bot.sent]
    assert "HIGH_CONVICTION_SIGNAL" in alert_types


# -- edge-triggered RISK_ALERT -------------------------------------------------

def test_risk_alert_fires_once_on_the_blocked_transition_not_every_tick():
    model = fit_model()
    telegram_bot = FakeTelegramBot()
    alert_manager = AlertManager()
    risk_manager = RiskManager(account_equity=10_000.0)
    engine = engine_with_forced_entry(
        assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model),
        risk_manager=risk_manager, alert_manager=alert_manager, telegram_bot=telegram_bot,
    )
    risk_manager.engage_kill_switch(reason="manual test stop")

    engine.tick(now=NOW)
    engine.tick(now=NOW + dt.timedelta(seconds=1))
    engine.tick(now=NOW + dt.timedelta(seconds=2))

    risk_alerts = [a for a in telegram_bot.sent if a["type"] == "RISK_ALERT"]
    assert len(risk_alerts) == 1


def test_no_risk_alert_when_never_blocked():
    model = fit_model()
    telegram_bot = FakeTelegramBot()
    alert_manager = AlertManager()
    engine = engine_with_forced_entry(
        assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model),
        alert_manager=alert_manager, telegram_bot=telegram_bot,
    )
    engine.tick(now=NOW)
    assert [a for a in telegram_bot.sent if a["type"] == "RISK_ALERT"] == []


# -- news events broadcast to every unit --------------------------------------

def test_news_event_is_broadcast_to_every_asset_unit():
    model = fit_model()
    engine = engine_with_forced_entry(
        assets=["BTC", "ETH"],
        model_loader=lambda asset: loaded_model_dict(model, version=f"{asset}-v1"),
    )
    news = {
        "headline": "test headline", "importance": 0.9, "timestamp": NOW, "credibility": 0.8,
        "market_relevance": 0.9,  # no affected_assets -> broad market-wide relevance, see feature_engine.py
    }
    engine.on_news_event(news)
    # Broadcast reached both units' own FeatureEngine (not just one) --
    # each asset gets its own independent news relevance judgement.
    assert engine.units["BTC"].feature_engine._last_relevant_news == news
    assert engine.units["ETH"].feature_engine._last_relevant_news == news


# -- TelegramBot provider methods ---------------------------------------------

def test_status_reports_trading_and_skipped_assets():
    model = fit_model()

    def model_loader(asset):
        return loaded_model_dict(model) if asset == "BTC" else None

    engine = PaperTradingEngine(
        assets=["BTC", "ETH"], model_loader=model_loader, risk_manager=RiskManager(account_equity=10_000.0),
    )
    status = engine.status()
    assert status["trading_assets"] == ["BTC"]
    assert status["skipped_assets"] == ["ETH"]
    assert status["risk_state"] == {"blocked": False, "reason": None}


def test_performance_today_and_risk_provider_methods_return_shaped_dicts():
    model = fit_model()
    engine = engine_with_forced_entry(assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model))
    perf = engine.performance()
    today = engine.today()
    risk = engine.risk()
    assert perf["trades"] == 0
    assert "trades" in today
    assert risk == {"blocked": False, "reason": None}


def test_open_positions_and_recent_signals_aggregate_across_units():
    model = fit_model()
    engine = engine_with_forced_entry(assets=["BTC"], model_loader=lambda asset: loaded_model_dict(model))
    assert engine.open_positions() == []
    assert engine.recent_signals() == []

    engine.on_market_event("BTC", "l2book", l2book_payload(ts=NOW, best_bid=50000.0, best_ask=50010.0),
                            ts_received_ns=int(NOW.timestamp() * 1e9))
    engine.tick(now=NOW)
    assert len(engine.open_positions()) == 1
    # A position is open at max_open_positions -> no fresh signal is
    # scored for last_signal this tick (see AssetTradingUnit.tick()).
    assert engine.recent_signals() == []


# -- load_promoted_model() ----------------------------------------------------

def test_load_promoted_model_returns_none_when_nothing_promoted(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.jsonl")
    result = load_promoted_model(registry, tmp_path, "BTC", 30, 10)
    assert result is None


def test_load_promoted_model_returns_none_when_promoted_record_has_no_artifact_path(tmp_path):
    registry = ModelRegistry(tmp_path / "registry.jsonl")
    registry.register(
        model_version="v1", asset="BTC", horizon_s=30, threshold_bps=10, model_kind="logistic",
        training_period={"start": "2026-01-01", "end": "2026-02-01"}, features=["obi_10"],
        parameters={}, performance={"accuracy": 0.6},
    )
    registry.promote("v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="test")
    result = load_promoted_model(registry, tmp_path, "BTC", 30, 10)
    assert result is None


def test_load_promoted_model_loads_the_real_artifact(tmp_path):
    model = fit_model()
    artifact_relpath = "artifacts/BTC_30s_10bps_v1.joblib"
    save_model(model, tmp_path / artifact_relpath)

    registry = ModelRegistry(tmp_path / "registry.jsonl")
    registry.register(
        model_version="v1", asset="BTC", horizon_s=30, threshold_bps=10, model_kind="logistic",
        training_period={"start": "2026-01-01", "end": "2026-02-01"}, features=["obi_10"],
        parameters={}, performance={"accuracy": 0.6}, calibration={"brier_score": 0.2},
        artifact_path=artifact_relpath,
    )
    registry.promote("v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="best walk-forward result")

    result = load_promoted_model(registry, tmp_path, "BTC", 30, 10)
    assert result is not None
    assert result["model_version"] == "v1"
    assert result["horizon_s"] == 30
    assert result["threshold_bps"] == 10
    assert result["model_calibration"] == {"brier_score": 0.2}
    # The loaded model is genuinely usable, not just a placeholder.
    rows = make_rows(n=5, seed=2)
    assert result["model"].predict_proba(rows)


def test_load_promoted_model_feeds_directly_into_a_working_paper_trading_engine(tmp_path):
    # End-to-end: register+promote+save via the real ModelRegistry/
    # model_persistence, then load it back through load_promoted_model()
    # exactly as main.py's real wiring will -- no fake model_loader.
    model = fit_model()
    artifact_relpath = "artifacts/BTC_30s_10bps_v1.joblib"
    save_model(model, tmp_path / artifact_relpath)
    registry = ModelRegistry(tmp_path / "registry.jsonl")
    registry.register(
        model_version="v1", asset="BTC", horizon_s=30, threshold_bps=10, model_kind="logistic",
        training_period={"start": "2026-01-01", "end": "2026-02-01"}, features=["obi_10"],
        parameters={}, performance={"accuracy": 0.6}, artifact_path=artifact_relpath,
    )
    registry.promote("v1", asset="BTC", horizon_s=30, threshold_bps=10, reason="test")

    def model_loader(asset):
        return load_promoted_model(registry, tmp_path, asset, 30, 10)

    engine = PaperTradingEngine(assets=["BTC"], model_loader=model_loader, risk_manager=RiskManager(account_equity=10_000.0))
    assert "BTC" in engine.units
    assert engine.tick(now=NOW) == {"BTC": []}  # no book yet, but wiring works end-to-end
