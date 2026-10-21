"""Phase 12 tests: notifications/alert_manager.py — the anti-spam gate
before anything reaches telegram_bot: mode-based quality gating, the
signal_filter (stricter than strategy/entry.py's own entry filter),
cooldown, probability-delta dedup, and structural enforcement of the
5-item ALERT_TYPES allowlist.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.notifications.alert_manager import AlertManager

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def make_signal(**overrides):
    signal = {
        "probability_up": 0.90,
        "probability_down": 0.05,
        "probability_neutral": 0.05,
        "signal_quality": "A",
        "expected_value": 10.0,
        "features_snapshot": {"spread": 1.0},
        "timestamp": BASE,
    }
    signal.update(overrides)
    return signal


def make_alert(**overrides):
    alert = {"type": "HIGH_CONVICTION_SIGNAL", "asset": "BTC/USD", "signal": make_signal()}
    alert.update(overrides)
    return alert


def test_unknown_alert_type_raises():
    with pytest.raises(ValueError):
        AlertManager().should_notify({"type": "SOMETHING_ELSE"})


def test_unknown_escalation_reason_raises():
    with pytest.raises(ValueError):
        AlertManager().should_notify(make_alert(escalation_reason="not_a_real_reason"), now=BASE)


def test_qualifying_sniper_signal_notifies():
    am = AlertManager(mode="SNIPER")
    assert am.should_notify(make_alert(), now=BASE) is True


def test_sniper_mode_requires_quality_a():
    am = AlertManager(mode="SNIPER")
    alert = make_alert(signal=make_signal(signal_quality="B"))
    assert am.should_notify(alert, now=BASE) is False


def test_sniper_mode_requires_exceptional_probability_beyond_base_min():
    am = AlertManager(mode="SNIPER", sniper_exceptional_probability_min=0.80)
    # Clears the base min_probability (0.68) but not SNIPER's own
    # tightened floor.
    alert = make_alert(signal=make_signal(probability_up=0.70, probability_down=0.20, probability_neutral=0.10))
    assert am.should_notify(alert, now=BASE) is False


def test_normal_mode_requires_quality_a_only():
    am = AlertManager(mode="NORMAL")
    assert am.should_notify(make_alert(), now=BASE) is True
    alert_b = make_alert(signal=make_signal(signal_quality="B"))
    assert am.should_notify(alert_b, now=BASE) is False


def test_aggressive_mode_still_blocked_by_the_global_required_quality():
    # Known config inconsistency (see module docstring): AGGRESSIVE's
    # notify_on_quality allows B, but required_signal_quality="A" is
    # documented as applying unconditionally -- implemented literally.
    am = AlertManager(mode="AGGRESSIVE")
    alert_b = make_alert(signal=make_signal(signal_quality="B"))
    assert am.should_notify(alert_b, now=BASE) is False


def test_aggressive_mode_with_required_quality_disabled_allows_b():
    am = AlertManager(mode="AGGRESSIVE", required_signal_quality=None)
    alert_b = make_alert(signal=make_signal(signal_quality="B"))
    assert am.should_notify(alert_b, now=BASE) is True


def test_neutral_signal_never_notifies():
    am = AlertManager(mode="SNIPER")
    alert = make_alert(signal=make_signal(probability_up=0.3, probability_down=0.3, probability_neutral=0.4, signal_quality="NO_TRADE"))
    assert am.should_notify(alert, now=BASE) is False


def test_missing_signal_never_notifies_fail_closed():
    am = AlertManager(mode="SNIPER")
    alert = {"type": "HIGH_CONVICTION_SIGNAL", "asset": "BTC/USD"}
    assert am.should_notify(alert, now=BASE) is False


def test_risk_blocked_prevents_notification_when_block_on_risk_lock():
    am = AlertManager(mode="SNIPER", block_on_risk_lock=True)
    alert = make_alert(risk_blocked=True)
    assert am.should_notify(alert, now=BASE) is False


def test_risk_blocked_ignored_when_block_on_risk_lock_disabled():
    am = AlertManager(mode="SNIPER", block_on_risk_lock=False)
    alert = make_alert(risk_blocked=True)
    assert am.should_notify(alert, now=BASE) is True


def test_spread_over_ceiling_blocks():
    am = AlertManager(mode="SNIPER")
    alert = make_alert(signal=make_signal(features_snapshot={"spread": 20.0}))
    assert am.should_notify(alert, now=BASE) is False


def test_stale_signal_blocked_when_now_given():
    am = AlertManager(mode="SNIPER", max_data_age_s=3)
    alert = make_alert()
    assert am.should_notify(alert, now=BASE + dt.timedelta(seconds=10)) is False


def test_freshness_skipped_when_now_not_given():
    am = AlertManager(mode="SNIPER")
    assert am.should_notify(make_alert(), now=None) is True


def test_non_signal_alert_types_bypass_signal_filter():
    am = AlertManager(mode="SNIPER")
    for alert_type in ("POSITION_OPENED", "POSITION_CLOSED", "RISK_ALERT", "DAILY_REPORT"):
        alert = {"type": alert_type, "asset": "BTC/USD"}
        assert am.should_notify(alert, now=BASE) is True


def test_cooldown_blocks_a_second_alert_of_the_same_type_and_asset():
    am = AlertManager(mode="SNIPER", cooldown_s_per_asset=300)
    alert = make_alert()
    am.record_sent(alert, now=BASE)
    fresh_alert = make_alert(signal=make_signal(timestamp=BASE + dt.timedelta(seconds=10), probability_up=0.85, probability_down=0.10))
    assert am.should_notify(fresh_alert, now=BASE + dt.timedelta(seconds=10)) is False


def test_cooldown_expires_after_the_configured_window():
    am = AlertManager(mode="SNIPER", cooldown_s_per_asset=300, dedupe_probability_delta_pct=3)
    alert = make_alert()
    am.record_sent(alert, now=BASE)
    now2 = BASE + dt.timedelta(seconds=400)
    # Probability must also move enough to clear dedup independently.
    moved = make_alert(signal=make_signal(probability_up=0.80, probability_down=0.15, timestamp=now2))
    assert am.should_notify(moved, now=now2) is True


def test_major_event_escalation_overrides_cooldown():
    am = AlertManager(mode="SNIPER", cooldown_s_per_asset=300, cooldown_override_on_major_event=True)
    alert = make_alert()
    am.record_sent(alert, now=BASE)
    now2 = BASE + dt.timedelta(seconds=10)
    escalated = make_alert(
        signal=make_signal(probability_up=0.80, probability_down=0.15, timestamp=now2),
        escalation_reason="direction_change",
    )
    assert am.should_notify(escalated, now=now2) is True


def test_dedup_blocks_a_near_identical_probability_even_after_cooldown():
    am = AlertManager(mode="SNIPER", dedupe_probability_delta_pct=3, cooldown_s_per_asset=300)
    alert = make_alert()
    am.record_sent(alert, now=BASE)
    now2 = BASE + dt.timedelta(seconds=1000)  # cooldown long expired
    near_identical = make_alert(signal=make_signal(probability_up=0.91, probability_down=0.04, timestamp=now2))
    assert am.should_notify(near_identical, now=now2) is False


def test_dedup_disabled_via_block_on_recent_duplicate_false():
    am = AlertManager(mode="SNIPER", block_on_recent_duplicate=False, cooldown_s_per_asset=1)
    alert = make_alert()
    am.record_sent(alert, now=BASE)
    now2 = BASE + dt.timedelta(seconds=5)
    identical = make_alert(signal=make_signal(timestamp=now2))
    assert am.should_notify(identical, now=now2) is True


def test_should_notify_never_mutates_state():
    am = AlertManager(mode="SNIPER")
    alert = make_alert()
    am.should_notify(alert, now=BASE)
    am.should_notify(alert, now=BASE)
    # Without record_sent(), repeated should_notify() calls never trip
    # cooldown/dedup against themselves.
    assert am.should_notify(alert, now=BASE) is True


def test_record_sent_is_required_to_start_a_cooldown():
    am = AlertManager(mode="SNIPER", cooldown_s_per_asset=300)
    alert = make_alert()
    assert am.should_notify(alert, now=BASE) is True
    assert am.should_notify(alert, now=BASE) is True  # no record_sent() -> still True


def test_different_assets_have_independent_cooldowns():
    am = AlertManager(mode="SNIPER", cooldown_s_per_asset=300)
    btc_alert = make_alert(asset="BTC/USD")
    am.record_sent(btc_alert, now=BASE)
    eth_alert = make_alert(asset="ETH/USD", signal=make_signal(timestamp=BASE))
    assert am.should_notify(eth_alert, now=BASE) is True


def test_invalid_mode_at_construction_raises():
    with pytest.raises(ValueError):
        AlertManager(mode="NOT_A_MODE")
