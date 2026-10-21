"""Phase 15 addendum tests: notifications/telegram_live.py — the
`python-telegram-bot` adapter layer (`AsyncioTelegramBotClient`,
`register_handlers()`, `build_live_telegram_bot()`) that drives the
already-tested, network-free `TelegramBot` core (`test_telegram_bot.py`).

No real Telegram token or network access is used anywhere here:
`ApplicationBuilder().token(...).build()` only validates the token's
*shape* locally and never contacts Telegram's servers until polling
actually starts (which this test suite never triggers), so a
syntactically-plausible fake token is safe to use throughout.
"""
from __future__ import annotations

import asyncio

import pytest

from src.notifications.telegram_bot import COMMANDS
from src.notifications.telegram_live import (
    AsyncioTelegramBotClient,
    build_live_telegram_bot,
    register_handlers,
)

FAKE_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"


class FakeEngine:
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


class FakeAlertManager:
    def __init__(self):
        self.mode = "SNIPER"


class FakeRiskManager:
    def __init__(self):
        self.killed_reason = None

    def engage_kill_switch(self, reason):
        self.killed_reason = reason


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id):
        self.effective_chat = FakeChat(chat_id) if chat_id is not None else None
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("simulated network failure")
        self.sent.append((chat_id, text))


def _build(token=FAKE_TOKEN, chat_id_raw="987654321"):
    engine = FakeEngine()
    alert_manager = FakeAlertManager()
    risk_manager = FakeRiskManager()
    application, telegram_bot = build_live_telegram_bot(
        token=token,
        chat_id_raw=chat_id_raw,
        mode="SNIPER",
        paper_trading_engine=engine,
        risk_manager=risk_manager,
        alert_manager=alert_manager,
    )
    return application, telegram_bot, engine, alert_manager, risk_manager


# --- build_live_telegram_bot(): not-configured -> inert ---------------


def test_missing_token_returns_none_none():
    application, telegram_bot = build_live_telegram_bot(
        token=None,
        chat_id_raw="987654321",
        mode="SNIPER",
        paper_trading_engine=None,
        risk_manager=None,
        alert_manager=None,
    )
    assert (application, telegram_bot) == (None, None)


def test_missing_chat_id_returns_none_none():
    application, telegram_bot = build_live_telegram_bot(
        token=FAKE_TOKEN,
        chat_id_raw=None,
        mode="SNIPER",
        paper_trading_engine=None,
        risk_manager=None,
        alert_manager=None,
    )
    assert (application, telegram_bot) == (None, None)


def test_empty_string_token_is_treated_as_not_configured():
    application, telegram_bot = build_live_telegram_bot(
        token="",
        chat_id_raw="987654321",
        mode="SNIPER",
        paper_trading_engine=None,
        risk_manager=None,
        alert_manager=None,
    )
    assert (application, telegram_bot) == (None, None)


# --- build_live_telegram_bot(): configured -> a working pair ----------


def test_configured_returns_a_working_pair_with_providers_bound():
    application, telegram_bot, engine, _, _ = _build()
    assert application is not None
    assert telegram_bot is not None
    assert telegram_bot.handle_command("/status") == "Status: STATUS_OK"
    assert telegram_bot.handle_command("/performance") == "Performance: PERF_OK"
    assert telegram_bot.handle_command("/positions") == "Open positions: POSITIONS_OK"
    assert telegram_bot.handle_command("/signals") == "Recent signals: SIGNALS_OK"
    assert telegram_bot.handle_command("/today") == "Today: TODAY_OK"
    assert telegram_bot.handle_command("/risk") == "Risk state: RISK_OK"


def test_numeric_chat_id_is_parsed_to_int():
    _, telegram_bot, _, _, _ = _build(chat_id_raw="987654321")
    assert telegram_bot.chat_id == 987654321
    assert isinstance(telegram_bot.chat_id, int)


def test_non_numeric_chat_id_passed_through_unchanged():
    # Group/channel usernames (e.g. "@my_channel") are valid chat_id
    # values too -- must not be coerced.
    _, telegram_bot, _, _, _ = _build(chat_id_raw="@my_channel")
    assert telegram_bot.chat_id == "@my_channel"


def test_register_handlers_registers_one_per_command():
    application, _, _, _, _ = _build()
    handlers = application.handlers[0]
    assert len(handlers) == len(COMMANDS)


def test_on_mode_change_mutates_alert_manager_mode():
    _, telegram_bot, _, alert_manager, _ = _build()
    telegram_bot.on_mode_change("AGGRESSIVE")
    assert alert_manager.mode == "AGGRESSIVE"


def test_on_stop_engages_kill_switch_with_telegram_reason():
    _, telegram_bot, _, _, risk_manager = _build()
    telegram_bot.on_stop()
    assert risk_manager.killed_reason == "telegram_stop_command"


# --- register_handlers(): chat-ID allowlist ----------------------------


@pytest.mark.asyncio
async def test_authorized_chat_gets_a_reply():
    application, telegram_bot, _, _, _ = _build(chat_id_raw="987654321")
    handler = application.handlers[0][0]
    update = FakeUpdate(987654321)
    await handler.callback(update, FakeContext())
    assert update.message.replies == ["Status: STATUS_OK"]


@pytest.mark.asyncio
async def test_unauthorized_chat_is_silently_ignored():
    application, telegram_bot, _, _, _ = _build(chat_id_raw="987654321")
    handler = application.handlers[0][0]
    update = FakeUpdate(111111)
    await handler.callback(update, FakeContext())
    assert update.message.replies == []


@pytest.mark.asyncio
async def test_update_with_no_effective_chat_is_silently_ignored():
    application, telegram_bot, _, _, _ = _build(chat_id_raw="987654321")
    handler = application.handlers[0][0]
    update = FakeUpdate(None)
    await handler.callback(update, FakeContext())
    assert update.message.replies == []


@pytest.mark.asyncio
async def test_every_command_has_a_working_authorized_handler():
    application, telegram_bot, _, _, _ = _build(chat_id_raw="987654321")
    handlers = application.handlers[0]
    assert len(handlers) == len(COMMANDS)
    for handler in handlers:
        update = FakeUpdate(987654321)
        await handler.callback(update, FakeContext())
        assert len(update.message.replies) == 1
        assert isinstance(update.message.replies[0], str)


# --- AsyncioTelegramBotClient -------------------------------------------


@pytest.mark.asyncio
async def test_send_message_schedules_and_executes_the_async_send():
    fake_bot = FakeBot()
    client = AsyncioTelegramBotClient(fake_bot)
    client.send_message(42, "hello")
    await asyncio.sleep(0.05)
    assert fake_bot.sent == [(42, "hello")]


@pytest.mark.asyncio
async def test_send_message_failure_is_swallowed_not_propagated():
    fake_bot = FakeBot(fail=True)
    client = AsyncioTelegramBotClient(fake_bot)
    # Must not raise -- a fire-and-forget task's exception is caught and
    # logged inside the scheduled coroutine itself (see module docstring).
    client.send_message(42, "hello")
    await asyncio.sleep(0.05)
    assert fake_bot.sent == []
