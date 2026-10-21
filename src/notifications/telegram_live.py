"""Live wiring for `notifications/telegram_bot.py`'s `TelegramBot` — the
`register_handlers()` layer that module's own docstring named but
explicitly deferred ("not built this phase... no real Telegram bot
token exists in this sandbox to smoke-test against"). Built now because
the user configured `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env` on
their own machine — the exact trigger that docstring said this step was
waiting for.

Roadmap: PHASE 15 addendum. `TelegramBot.handle_command()`/`send_alert()`
(Phase 12) are unchanged — every command's formatting/logic stays
exactly as already tested; this module is purely the `python-telegram-
bot` adapter layer the Phase 12 docstring already specified, following
the same "keep the real logic synchronous and testable, keep the
asyncio/network plumbing in a thin separate layer" split every other
phase in this codebase already follows for `main.py`.

## Why a manual `Application` lifecycle, not `run_polling()`

`python-telegram-bot`'s `Application.run_polling()` takes over the
entire asyncio event loop until a stop signal — fine for a bot that's
the whole program, wrong here, where `main.py` already owns the event
loop and runs a dozen other tasks (collectors, consumers, displays,
paper trading). Instead, `main.py` calls `initialize()`/`start()`/
`updater.start_polling()` manually — the same three calls `run_polling()`
performs internally, and the pattern `python-telegram-bot`'s own
maintainers document as the supported way to embed an `Application`
inside an existing asyncio program that has other work to do (see the
`Application`/`Updater` docs and GitHub Discussion #3310) — runs it as
one more piece of a loop `main.py` already owns, and calls
`updater.stop()`/`stop()`/`shutdown()` in the same `finally` block every
other task is cancelled in.

## `AsyncioTelegramBotClient` — the sync-adapter shim `telegram_bot.py`'s
## own docstring already anticipated

`TelegramBot.send_alert()` calls `self.bot_client.send_message(chat_id,
text)` synchronously (Phase 12, deliberately framework-agnostic — see
that module's docstring). The real `telegram.Bot.send_message` is
async-only. `AsyncioTelegramBotClient` is the small shim that docstring
predicted: a `send_message(chat_id, text)` method with a synchronous
*signature* that schedules the real async send via
`asyncio.create_task()` rather than blocking — safe specifically because
every call site (`AssetTradingUnit._send_alert()`,
`PaperTradingEngine._check_risk_transition()`) already runs inside
`main.py`'s single running event loop. A send failure (network hiccup,
Telegram API error) is caught and logged inside the scheduled task
itself, not left to propagate — a fire-and-forget task's own exception
would otherwise surface nowhere, not even a log line, unlike a directly
awaited call.

## Chat-ID allowlist — every handler checks it, not just documented as a
## config assumption

A Telegram bot's username is discoverable by anyone who finds it;
without an explicit check, any stranger who messages this bot could
read live PnL/positions via `/status`/`/performance`/`/positions`, or
use `/stop`/`/mode` to interfere with a running paper-trading session.
Every command handler compares `update.effective_chat.id` against the
one configured `chat_id` and silently ignores anything else — no reply
at all, since confirming a mismatched chat got *any* response would
itself leak that this bot exists and is live, which is worse than
staying silent.

## Not configured -> inert, not an error

`build_live_telegram_bot()` returns `(None, None)` when
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't both set in `.env` —
"Telegram not configured yet" is exactly as normal a state as "no
promoted model yet" (`live/trading_loop.py: load_promoted_model()`) or
"Kraken gates not flipped" (`execution/kraken_executor.py`): alerts
degrade to console logging (`AssetTradingUnit._send_alert()`'s existing
`telegram_bot is None` fallback, unchanged), never a crash.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from src.notifications.telegram_bot import COMMANDS, TelegramBot

logger = logging.getLogger("telegram_live")


class AsyncioTelegramBotClient:
    """See module docstring. Wraps a real `telegram.Bot` (i.e.
    `Application.bot`) behind the sync `send_message(chat_id, text)`
    shape `TelegramBot.send_alert()` expects."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def send_message(self, chat_id, text: str) -> None:
        async def _send() -> None:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                logger.exception("failed to send Telegram alert to chat_id=%s", chat_id)

        asyncio.create_task(_send())


def _authorized(update: Update, chat_id) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == chat_id


def register_handlers(application: Application, telegram_bot: TelegramBot, chat_id) -> None:
    """Registers one `CommandHandler` per `telegram_bot.py: COMMANDS`
    entry. Each handler: checks the chat-ID allowlist (see module
    docstring), calls the already-tested synchronous
    `TelegramBot.handle_command()`, and replies with its return value."""

    def _make_handler(command: str):
        async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not _authorized(update, chat_id):
                return
            text = telegram_bot.handle_command(command, list(context.args or []))
            await update.message.reply_text(text)

        return _handler

    for command in COMMANDS:
        # CommandHandler wants the bare command name, no leading "/".
        application.add_handler(CommandHandler(command.lstrip("/"), _make_handler(command)))


def build_live_telegram_bot(
    *,
    token: "str | None",
    chat_id_raw: "str | None",
    mode: str,
    paper_trading_engine,
    risk_manager,
    alert_manager,
) -> "tuple[Application, TelegramBot] | tuple[None, None]":
    """The real `main.py` wiring: builds a `python-telegram-bot`
    `Application` and the `TelegramBot` (Phase 12) it drives, with every
    data provider bound directly to `paper_trading_engine`'s own
    already-public methods (`status`/`performance`/`open_positions`/
    `recent_signals`/`today`/`risk` — `live/trading_loop.py`, Phase 15)
    and `on_mode_change`/`on_stop` wired exactly the way
    `telegram_bot.py`'s own module docstring specifies
    (`alert_manager.mode = new_mode`,
    `risk_manager.engage_kill_switch("telegram_stop_command")`).

    Returns `(None, None)` — not a raise — when `token`/`chat_id_raw`
    aren't both set; see the module docstring's "not configured ->
    inert" note."""
    if not token or not chat_id_raw:
        return None, None
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        # Group/channel usernames (e.g. "@my_channel") are valid
        # chat_id values too -- passed through untouched rather than
        # forced into an int this codebase can't assume.
        chat_id = chat_id_raw

    application = ApplicationBuilder().token(token).build()
    bot_client = AsyncioTelegramBotClient(application.bot)

    def on_mode_change(new_mode: str) -> None:
        alert_manager.mode = new_mode

    def on_stop() -> None:
        risk_manager.engage_kill_switch("telegram_stop_command")

    telegram_bot = TelegramBot(
        mode=mode,
        bot_client=bot_client,
        chat_id=chat_id,
        status_provider=paper_trading_engine.status,
        performance_provider=paper_trading_engine.performance,
        positions_provider=paper_trading_engine.open_positions,
        signals_provider=paper_trading_engine.recent_signals,
        today_provider=paper_trading_engine.today,
        risk_provider=paper_trading_engine.risk,
        on_mode_change=on_mode_change,
        on_stop=on_stop,
    )
    register_handlers(application, telegram_bot, chat_id)
    return application, telegram_bot
