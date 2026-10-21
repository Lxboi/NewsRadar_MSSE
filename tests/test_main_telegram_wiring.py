"""Phase 15 addendum tests: main.py's `wire_live_telegram()` -- the glue
between `build_paper_trading_engine()`'s already-tested engine and the
new `notifications/telegram_live.py` adapter layer (its own tests live in
tests/test_telegram_live.py and cover the adapter itself in isolation;
these tests cover only what main.py does with it: reading env vars,
rewiring the engine/units, and returning the Application for the
lifecycle calls in run()).

Default-off regression is the most important case here, matching this
project's established testing philosophy (test_main_paper_trading_wiring.py,
test_main_news_wiring.py): Telegram must stay completely inert unless
both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
"""
from __future__ import annotations

import logging

import pytest

from src.main import wire_live_telegram

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


class FakeUnit:
    def __init__(self):
        self.telegram_bot = None


class FakeAlertManager:
    mode = "SNIPER"


class FakeRiskManager:
    def engage_kill_switch(self, reason):
        pass


class FakeEngine:
    """Stands in for PaperTradingEngine -- has the same public shape
    wire_live_telegram() touches (risk_manager/alert_manager/units/
    telegram_bot attributes, and the six provider methods
    build_live_telegram_bot() binds), without needing a real trained
    model or the rest of the trading pipeline."""

    def __init__(self):
        self.risk_manager = FakeRiskManager()
        self.alert_manager = FakeAlertManager()
        self.telegram_bot = None
        self.units = {"BTC": FakeUnit(), "ETH": FakeUnit()}

    def status(self):
        return "STATUS_OK"

    def performance(self):
        return "PERF_OK"

    def open_positions(self):
        return "POSITIONS_OK"

    def recent_signals(self):
        return "SIGNALS_OK"

    def today(self):
        return "TODAY_OK"

    def risk(self):
        return "RISK_OK"


def test_paper_trading_engine_none_returns_none_and_touches_nothing():
    assert wire_live_telegram(None, TELEGRAM_CFG) is None


def test_missing_env_vars_leaves_engine_untouched(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    engine = FakeEngine()

    result = wire_live_telegram(engine, TELEGRAM_CFG)

    assert result is None
    assert engine.telegram_bot is None
    assert all(unit.telegram_bot is None for unit in engine.units.values())


def test_only_token_set_still_stays_inert(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE-TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    engine = FakeEngine()

    result = wire_live_telegram(engine, TELEGRAM_CFG)

    assert result is None
    assert engine.telegram_bot is None


def test_both_env_vars_set_wires_engine_and_every_unit(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE-TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    engine = FakeEngine()

    result = wire_live_telegram(engine, TELEGRAM_CFG)

    assert result is not None  # the python-telegram-bot Application
    assert engine.telegram_bot is not None
    for unit in engine.units.values():
        assert unit.telegram_bot is engine.telegram_bot
    # Providers correctly bound to this engine, not left unset.
    assert engine.telegram_bot.handle_command("/status") == "Status: STATUS_OK"


def test_configured_logs_chat_id_not_token(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE-TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    engine = FakeEngine()

    with caplog.at_level(logging.INFO):
        wire_live_telegram(engine, TELEGRAM_CFG, log=logging.getLogger("test_wire_live_telegram"))

    assert any("TELEGRAM LIVE-WIRED" in r.message for r in caplog.records)
    assert not any("FAKE-TOKEN" in r.message for r in caplog.records)
