"""Phase 12 tests: strategy/risk_manager.py — account-level hard limits
(daily loss, consecutive losses, execution-quality breaches, a manual
kill switch), exposed via a zero-arg state() matching strategy/entry.py's
risk_state placeholder contract.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.strategy.risk_manager import (
    REASON_CONSECUTIVE_LOSSES,
    REASON_DAILY_LOSS_LIMIT,
    REASON_KILL_SWITCH,
    REASON_MAX_LATENCY,
    REASON_MAX_SLIPPAGE,
    RiskManager,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def test_initial_state_is_unblocked():
    rm = RiskManager(account_equity=10_000.0)
    assert rm.state() == {"blocked": False, "reason": None}


def test_record_trade_updates_running_equity():
    rm = RiskManager(account_equity=10_000.0)
    rm.record_trade({"net_pnl": 50.0, "closed_at": BASE})
    assert rm.account_equity == pytest.approx(10_050.0)
    rm.record_trade({"net_pnl": -20.0, "closed_at": BASE})
    assert rm.account_equity == pytest.approx(10_030.0)


def test_daily_loss_limit_breach_blocks():
    rm = RiskManager(account_equity=1000.0, max_daily_loss_pct=1.0)
    rm.record_trade({"net_pnl": -50.0, "closed_at": BASE})  # -5% >> 1% limit
    assert rm.state() == {"blocked": True, "reason": REASON_DAILY_LOSS_LIMIT}


def test_daily_loss_within_limit_does_not_block():
    rm = RiskManager(account_equity=1000.0, max_daily_loss_pct=1.0)
    rm.record_trade({"net_pnl": -5.0, "closed_at": BASE})  # -0.5% < 1%
    assert rm.state()["blocked"] is False


def test_daily_pnl_resets_on_utc_day_rollover():
    rm = RiskManager(account_equity=1000.0, max_daily_loss_pct=1.0)
    rm.record_trade({"net_pnl": -9.0, "closed_at": BASE})
    assert rm.state()["blocked"] is False  # under 1% still
    # Same UTC day, a further loss crosses the limit.
    rm.record_trade({"net_pnl": -2.0, "closed_at": BASE + dt.timedelta(hours=1)})
    assert rm.state() == {"blocked": True, "reason": REASON_DAILY_LOSS_LIMIT}
    # Next UTC day: daily accumulator resets, a small loss no longer blocks
    # even though cumulative equity is still down overall.
    rm.record_trade({"net_pnl": -1.0, "closed_at": BASE + dt.timedelta(days=1)})
    assert rm.state()["blocked"] is False


def test_consecutive_losses_breach_blocks():
    rm = RiskManager(account_equity=10_000.0, max_consecutive_losses=3, max_daily_loss_pct=100.0)
    for _ in range(3):
        rm.record_trade({"net_pnl": -1.0, "closed_at": BASE})
    assert rm.state() == {"blocked": True, "reason": REASON_CONSECUTIVE_LOSSES}


def test_a_win_resets_the_consecutive_loss_streak():
    rm = RiskManager(account_equity=10_000.0, max_consecutive_losses=3, max_daily_loss_pct=100.0)
    rm.record_trade({"net_pnl": -1.0, "closed_at": BASE})
    rm.record_trade({"net_pnl": -1.0, "closed_at": BASE})
    rm.record_trade({"net_pnl": 5.0, "closed_at": BASE})  # win resets streak
    rm.record_trade({"net_pnl": -1.0, "closed_at": BASE})
    assert rm.state()["blocked"] is False


def test_breakeven_trade_resets_the_streak_too():
    rm = RiskManager(account_equity=10_000.0, max_consecutive_losses=2, max_daily_loss_pct=100.0)
    rm.record_trade({"net_pnl": -1.0, "closed_at": BASE})
    rm.record_trade({"net_pnl": 0.0, "closed_at": BASE})  # exact breakeven, not a loss
    rm.record_trade({"net_pnl": -1.0, "closed_at": BASE})
    assert rm.state()["blocked"] is False  # only 1 consecutive loss since the breakeven


def test_kill_switch_blocks_and_reports_its_reason():
    rm = RiskManager(account_equity=10_000.0)
    rm.engage_kill_switch("daily_loss_limit_manual")
    assert rm.state() == {"blocked": True, "reason": "daily_loss_limit_manual"}


def test_kill_switch_default_reason():
    rm = RiskManager(account_equity=10_000.0)
    rm.engage_kill_switch()
    assert rm.state() == {"blocked": True, "reason": REASON_KILL_SWITCH}


def test_disengage_kill_switch_unblocks():
    rm = RiskManager(account_equity=10_000.0)
    rm.engage_kill_switch()
    rm.disengage_kill_switch()
    assert rm.state()["blocked"] is False


def test_kill_switch_takes_priority_over_every_other_breach():
    rm = RiskManager(account_equity=1000.0, max_daily_loss_pct=1.0)
    rm.record_trade({"net_pnl": -50.0, "closed_at": BASE})  # daily loss breach too
    rm.engage_kill_switch("operator_stop")
    assert rm.state() == {"blocked": True, "reason": "operator_stop"}


def test_record_fill_quality_slippage_breach():
    rm = RiskManager(account_equity=10_000.0, max_slippage_bps=8)
    rm.record_fill_quality(slippage_bps=20.0)
    assert rm.state() == {"blocked": True, "reason": REASON_MAX_SLIPPAGE}


def test_record_fill_quality_latency_breach():
    rm = RiskManager(account_equity=10_000.0, max_latency_ms=500)
    rm.record_fill_quality(latency_ms=900.0)
    assert rm.state() == {"blocked": True, "reason": REASON_MAX_LATENCY}


def test_record_fill_quality_within_limits_does_not_block():
    rm = RiskManager(account_equity=10_000.0, max_slippage_bps=8, max_latency_ms=500)
    rm.record_fill_quality(slippage_bps=2.0, latency_ms=100.0)
    assert rm.state()["blocked"] is False


def test_execution_breach_is_sticky_until_reset():
    rm = RiskManager(account_equity=10_000.0, max_slippage_bps=8)
    rm.record_fill_quality(slippage_bps=20.0)
    rm.record_fill_quality(slippage_bps=1.0)  # a later good fill doesn't self-clear
    assert rm.state()["blocked"] is True
    rm.reset_execution_breach()
    assert rm.state()["blocked"] is False


def test_daily_loss_pct_is_positive_when_losing_and_non_positive_when_winning():
    rm = RiskManager(account_equity=1000.0)
    rm.record_trade({"net_pnl": -10.0, "closed_at": BASE})
    assert rm.daily_loss_pct() > 0
    rm2 = RiskManager(account_equity=1000.0)
    rm2.record_trade({"net_pnl": 10.0, "closed_at": BASE})
    assert rm2.daily_loss_pct() <= 0


def test_record_trade_missing_net_pnl_defaults_to_zero_no_crash():
    rm = RiskManager(account_equity=1000.0)
    rm.record_trade({"closed_at": BASE})
    assert rm.account_equity == pytest.approx(1000.0)
    assert rm.state()["blocked"] is False


def test_record_trade_missing_closed_at_still_updates_equity_and_streak():
    rm = RiskManager(account_equity=1000.0, max_daily_loss_pct=100.0)
    rm.record_trade({"net_pnl": -5.0})
    assert rm.account_equity == pytest.approx(995.0)
