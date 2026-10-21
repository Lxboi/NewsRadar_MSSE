"""Phase 12 tests: notifications/telegram_bot.py — the synchronous,
network-free command core (`handle_command()`) and alert formatting
(`format_alert()`/`send_alert()`), all testable without a real Telegram
bot token or the `python-telegram-bot` Application/Update machinery.
"""
from __future__ import annotations

import pytest

from src.notifications.telegram_bot import COMMANDS, TelegramBot, format_alert


class FakeBotClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_unknown_command_returns_a_helpful_message_not_a_crash():
    bot = TelegramBot()
    result = bot.handle_command("/bogus")
    assert "Unknown command" in result
    assert "/status" in result  # lists valid commands


def test_every_documented_command_is_handled_without_raising():
    bot = TelegramBot()
    for command in COMMANDS:
        result = bot.handle_command(command)
        assert isinstance(result, str)


def test_provider_not_wired_gives_explicit_not_available_message():
    bot = TelegramBot()
    result = bot.handle_command("/status")
    assert "not available" in result.lower()


def test_provider_dict_result_is_formatted():
    bot = TelegramBot(status_provider=lambda: {"trading_mode": "RESEARCH", "assets": 3})
    result = bot.handle_command("/status")
    assert "trading_mode" in result
    assert "RESEARCH" in result


def test_provider_list_result_is_formatted_with_count():
    bot = TelegramBot(positions_provider=lambda: [{"asset": "BTC/USD"}, {"asset": "ETH/USD"}])
    result = bot.handle_command("/positions")
    assert "(2)" in result
    assert "BTC/USD" in result


def test_provider_empty_list_says_none():
    bot = TelegramBot(positions_provider=lambda: [])
    result = bot.handle_command("/positions")
    assert "none" in result.lower()


def test_all_six_data_commands_reach_their_own_provider():
    calls = []
    def make_provider(name):
        def provider():
            calls.append(name)
            return {"ok": True}
        return provider

    bot = TelegramBot(
        status_provider=make_provider("status"),
        performance_provider=make_provider("performance"),
        positions_provider=make_provider("positions"),
        signals_provider=make_provider("signals"),
        today_provider=make_provider("today"),
        risk_provider=make_provider("risk"),
    )
    for command in ("/status", "/performance", "/positions", "/signals", "/today", "/risk"):
        bot.handle_command(command)
    assert calls == ["status", "performance", "positions", "signals", "today", "risk"]


def test_mode_with_no_args_reports_current_mode():
    bot = TelegramBot(mode="SNIPER")
    assert bot.handle_command("/mode") == "Current mode: SNIPER"


# -- /signals: lean formatting, regression for the oversized-message bug ----
# Found on a real paper-trading run (2026-08-25, Raspberry Pi): /signals
# produced no reply at all while /performance (a much smaller payload)
# worked fine right next to it. Root cause: a real SignalEngine signal
# dict embeds features_snapshot (the entire raw feature row) and
# model_calibration (the promoted model's full calibration-curve
# diagnostics -- itself a deeply nested dict of per-class reliability
# buckets), and the old _format_provider_result() dumped every signal's
# full dict repr into one Telegram message -- for 3 assets, comfortably
# past Telegram's ~4096-character limit, which send_message rejects
# silently (python-telegram-bot's default error handling just logs it).

def _real_shaped_signal(asset: str, huge_calibration: bool = True) -> dict:
    signal = {
        "asset": asset, "timestamp": "2026-08-25T18:00:00+00:00",
        "horizon_s": 60, "threshold_bps": 10, "signal": 1,
        "probability_up": 0.2652, "probability_down": 0.0812, "probability_neutral": 0.6536,
        "expected_move": 1.84, "expected_value": -3.16, "signal_quality": "C",
        "market_regime": "NORMAL_VOL", "model_version": f"{asset}_60s_10bps_logistic_v1",
        "confidence": 0.61,
        # The two fields that blew past Telegram's message limit in the
        # real run -- deliberately huge here too, so the regression test
        # actually exercises the size problem, not just the field names.
        "features_snapshot": {f"feature_{i}": float(i) / 3 for i in range(30)},
        "model_calibration": (
            {"n_scored": 86268, "brier_score": 0.239, "log_loss": 0.501, "roc_auc_macro": 0.723,
             "calibration_curve": {
                 cls: {"mean_predicted": [i / 100 for i in range(10)], "fraction_positive": [i / 100 for i in range(10)]}
                 for cls in ("probability_down", "probability_neutral", "probability_up")
             }}
            if huge_calibration else None
        ),
    }
    return signal


def test_signals_summary_excludes_the_oversized_fields():
    signal = _real_shaped_signal("BTC")
    bot = TelegramBot(signals_provider=lambda: [signal])
    result = bot.handle_command("/signals")
    assert "features_snapshot" not in result
    assert "model_calibration" not in result
    assert "calibration_curve" not in result
    # The fields that actually matter for "what did the model just see"
    # are still there.
    assert "BTC" in result
    assert "0.265" in result  # probability_up, formatted
    assert "NORMAL_VOL" in result


def test_signals_summary_stays_under_telegram_message_limit_for_three_assets():
    # The exact real-world shape that produced no reply: three assets,
    # each with a full features_snapshot + model_calibration payload.
    signals = [_real_shaped_signal(a) for a in ("BTC", "ETH", "SOL")]
    bot = TelegramBot(signals_provider=lambda: signals)
    result = bot.handle_command("/signals")
    assert len(result) < 4096
    assert result.count("\n") == 3  # header + one line per asset


def test_signals_summary_handles_missing_optional_fields_without_crashing():
    minimal = {"asset": "BTC", "signal": 0}
    bot = TelegramBot(signals_provider=lambda: [minimal])
    result = bot.handle_command("/signals")
    assert "BTC" in result
    assert "n/a" in result  # missing probability/EV/etc. degrade, not crash


def test_signals_empty_list_still_says_none():
    bot = TelegramBot(signals_provider=lambda: [])
    result = bot.handle_command("/signals")
    assert "none" in result.lower()


def test_signals_non_signal_shaped_data_falls_back_to_generic_formatting():
    # A provider returning something that isn't a list of real signal
    # dicts (e.g. a test double, or a future caller with a different
    # shape) must not crash -- falls back to the pre-existing generic
    # formatter unchanged.
    bot = TelegramBot(signals_provider=lambda: {"ok": True})
    result = bot.handle_command("/signals")
    assert "ok" in result
    assert "True" in result


def test_mode_with_valid_arg_changes_mode_and_calls_callback():
    changed = []
    bot = TelegramBot(mode="SNIPER", on_mode_change=changed.append)
    result = bot.handle_command("/mode", ["aggressive"])
    assert bot.mode == "AGGRESSIVE"
    assert changed == ["AGGRESSIVE"]
    assert "AGGRESSIVE" in result


def test_mode_with_invalid_arg_does_not_change_mode():
    bot = TelegramBot(mode="SNIPER")
    result = bot.handle_command("/mode", ["not_a_mode"])
    assert bot.mode == "SNIPER"
    assert "Unknown mode" in result


def test_constructor_rejects_invalid_mode():
    with pytest.raises(ValueError):
        TelegramBot(mode="NOT_A_MODE")


def test_stop_with_no_callback_reports_not_wired_and_does_nothing():
    bot = TelegramBot()
    result = bot.handle_command("/stop")
    assert "not wired" in result.lower()


def test_stop_with_callback_invokes_it():
    called = []
    bot = TelegramBot(on_stop=lambda: called.append(True))
    result = bot.handle_command("/stop")
    assert called == [True]
    assert "engaged" in result.lower()


def test_format_alert_high_conviction_signal():
    alert = {
        "type": "HIGH_CONVICTION_SIGNAL",
        "asset": "BTC/USD",
        "signal": {"signal_quality": "A", "probability_up": 0.9, "probability_down": 0.05, "expected_value": 12.0, "confidence": 0.8},
    }
    text = format_alert(alert)
    assert "BTC/USD" in text
    assert "A" in text
    assert "0.9" in text


def test_format_alert_position_closed_includes_pnl_and_exit_reason():
    alert = {
        "type": "POSITION_CLOSED",
        "asset": "ETH/USD",
        "position": {"side": "sell", "quantity": 5.0, "exit_reason": "fixed_tp", "net_pnl": 12.5},
    }
    text = format_alert(alert)
    assert "fixed_tp" in text
    assert "12.5" in text


def test_format_alert_risk_alert_includes_reason():
    alert = {"type": "RISK_ALERT", "reason": "daily_loss_limit_breached"}
    text = format_alert(alert)
    assert "daily_loss_limit_breached" in text


def test_format_alert_unknown_type_does_not_crash():
    text = format_alert({"type": "SOMETHING_ELSE"})
    assert "SOMETHING_ELSE" in text


def test_send_alert_returns_formatted_text_even_without_a_bot_client():
    bot = TelegramBot()
    text = bot.send_alert({"type": "DAILY_REPORT", "message": "Summary for today."})
    assert "Summary for today." in text


def test_send_alert_calls_bot_client_when_wired():
    client = FakeBotClient()
    bot = TelegramBot(bot_client=client, chat_id="chat-1")
    bot.send_alert({"type": "POSITION_OPENED", "asset": "BTC/USD", "position": {"side": "buy", "quantity": 1.0}})
    assert len(client.sent) == 1
    assert client.sent[0][0] == "chat-1"
    assert "BTC/USD" in client.sent[0][1]


def test_send_alert_does_not_call_bot_client_when_chat_id_missing():
    client = FakeBotClient()
    bot = TelegramBot(bot_client=client, chat_id=None)
    bot.send_alert({"type": "DAILY_REPORT", "message": "x"})
    assert client.sent == []
