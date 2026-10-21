"""Telegram bot: AGGRESSIVE/NORMAL/SNIPER modes (default SNIPER),
commands /status /performance /positions /signals /today /risk /mode
/stop, and a local kill switch independent of Telegram (sections 47-55).

Roadmap: PHASE 12. Split into two layers, deliberately:

1. **`handle_command()`** — a fully synchronous, plain-dict-in/
   string-out core with no dependency on `python-telegram-bot`'s
   `Update`/`Context`/`Application` objects at all. This is what's
   unit-tested here: every command's formatting and edge cases, real
   coverage with no network and no Telegram credentials.
2. **Live wiring** (`register_handlers()`, not built this phase) — a
   thin `python-telegram-bot` `Application`/`CommandHandler` adapter
   that calls `handle_command()` and sends its return value back to the
   chat. Deferred for the same reason Phase 7's
   `build_feature_table.py`/Phase 8's baseline-sweep example were: no
   real Telegram bot token exists in this sandbox to smoke-test against
   (`.env`'s `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are blank by
   design until the user fills them in), so wiring it now would be
   unverified code presented as verified. `main.py` integration is a
   later, live-validated step.

`send_alert()` is similarly synchronous and duck-typed: `bot_client` is
any object exposing `send_message(chat_id, text)` (sync signature),
matching `strategy/strategy_engine.py`'s optional `executor` pattern
from Phase 11 exactly — `python-telegram-bot`'s real `Bot.send_message`
is async-only (`requirements.txt: python-telegram-bot>=21.0`); the
production `bot_client` passed in by `main.py`'s eventual live wiring is
expected to be a small sync-adapter shim around it, keeping this module
itself testable without `asyncio` or a real network call, matching every
other module's synchronous-and-testable convention in this codebase.

Every data-fetching command (`/status`, `/performance`, `/positions`,
`/signals`, `/today`, `/risk`) is answered by an **injected, zero-argument
provider callable** — this module has no opinion on where positions or
signals actually live (`StrategyEngine.open_positions`, a `Signal`/
`Position` table query, ...); a caller wires whichever data source it
has. A provider left as `None` degrades to an explicit "not available"
message, never a crash — same missing-data convention as every other
Phase 11/12 module. `/mode` and `/stop` are the two commands with a
side effect: `/mode NEW_MODE` calls the injected `on_mode_change`
callback (the natural wiring point is
`alert_manager.mode = new_mode`); `/stop` calls the injected `on_stop`
callback (the natural wiring point is
`risk_manager.engage_kill_switch("telegram_stop_command")` —
`RiskManager.engage_kill_switch()` is already plain-method-callable
independent of Telegram, so this satisfies section 55's "local kill
switch independent of Telegram" by construction, not a second parallel
mechanism).
"""
from __future__ import annotations

COMMANDS = ("/status", "/performance", "/positions", "/signals", "/today", "/risk", "/mode", "/stop")

MODES = ("AGGRESSIVE", "NORMAL", "SNIPER")
DEFAULT_MODE = "SNIPER"

ALERT_TYPE_LABELS = {
    "HIGH_CONVICTION_SIGNAL": "\U0001F3AF High-Conviction Signal",
    "POSITION_OPENED": "✅ Position Opened",
    "POSITION_CLOSED": "\U0001F3C1 Position Closed",
    "RISK_ALERT": "⚠️ Risk Alert",
    "DAILY_REPORT": "\U0001F4CA Daily Report",
}


def format_alert(alert: dict) -> str:
    """Module-level, pure — the same formatting a `send_alert()` call
    uses, exposed standalone so a caller (or a test) can preview exactly
    what would be sent without a `TelegramBot` instance."""
    label = ALERT_TYPE_LABELS.get(alert.get("type"), str(alert.get("type")))
    lines = [label]
    asset = alert.get("asset")
    if asset:
        lines.append(f"Asset: {asset}")

    signal = alert.get("signal")
    if signal:
        lines.append(f"Signal quality: {signal.get('signal_quality')}")
        lines.append(f"P(up)={signal.get('probability_up')}  P(down)={signal.get('probability_down')}")
        lines.append(f"Expected value: {signal.get('expected_value')} bps")
        lines.append(f"Confidence: {signal.get('confidence')}")

    position = alert.get("position")
    if position:
        lines.append(f"Side: {position.get('side')}  Qty: {position.get('quantity')}")
        if position.get("exit_reason"):
            lines.append(f"Exit reason: {position.get('exit_reason')}")
        if position.get("net_pnl") is not None:
            lines.append(f"Net PnL: {position.get('net_pnl')}")

    reason = alert.get("reason")
    if reason:
        lines.append(f"Reason: {reason}")

    message = alert.get("message")
    if message:
        lines.append(message)

    return "\n".join(lines)


def _format_provider_result(label: str, provider) -> str:
    if provider is None:
        return f"{label}: not available yet (no data provider wired to this bot instance)."
    data = provider()
    if isinstance(data, dict):
        if not data:
            return f"{label}: no data."
        body = "\n".join(f"  {k}: {v}" for k, v in data.items())
        return f"{label}:\n{body}"
    if isinstance(data, (list, tuple)):
        if not data:
            return f"{label}: none."
        lines = [f"{label} ({len(data)}):"]
        for item in data:
            lines.append(f"  - {item}")
        return "\n".join(lines)
    if not data:
        return f"{label}: no data."
    return f"{label}: {data}"


def _fmt(value, spec: "str | None" = None) -> str:
    if value is None:
        return "n/a"
    return format(value, spec) if spec else str(value)


def _format_signal_summary(signal: dict) -> str:
    """One human-readable line for a `signals/signal_engine.py: SignalEngine`
    payload -- deliberately excludes `features_snapshot` (the entire raw
    feature row, ~25+ columns) and `model_calibration` (the promoted
    model's full calibration-curve diagnostics, itself a deeply nested
    dict of per-class reliability buckets). Both are real, useful fields
    for a caller working with the dict directly (a notebook, a log line),
    but were never meant to round-trip through a Telegram reply: a
    handful of these full dicts (`/signals` renders one per asset) can
    push a single message past Telegram's ~4096-character limit, which
    `Bot.send_message`/`Message.reply_text` reject outright -- silently,
    from the user's point of view, since `python-telegram-bot`'s default
    error handling just logs the exception rather than surfacing it back
    to the chat. Found exactly this way: a real paper-trading run where
    `/signals` produced no reply at all, `/performance`'s much smaller
    payload right next to it working fine."""
    return (
        f"  - {signal.get('asset', 'n/a')}: signal={signal.get('signal', 'n/a')} "
        f"quality={signal.get('signal_quality', 'n/a')} "
        f"prob_up={_fmt(signal.get('probability_up'), '.3f')} "
        f"prob_down={_fmt(signal.get('probability_down'), '.3f')} "
        f"EV_bps={_fmt(signal.get('expected_value'), '.2f')} "
        f"regime={signal.get('market_regime', 'n/a')} "
        f"ts={signal.get('timestamp', 'n/a')}"
    )


class TelegramBot:
    def __init__(
        self,
        *,
        mode: str = DEFAULT_MODE,
        bot_client=None,
        chat_id=None,
        status_provider=None,
        performance_provider=None,
        positions_provider=None,
        signals_provider=None,
        today_provider=None,
        risk_provider=None,
        on_mode_change=None,
        on_stop=None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        self.mode = mode
        self.bot_client = bot_client
        self.chat_id = chat_id
        self.status_provider = status_provider
        self.performance_provider = performance_provider
        self.positions_provider = positions_provider
        self.signals_provider = signals_provider
        self.today_provider = today_provider
        self.risk_provider = risk_provider
        self.on_mode_change = on_mode_change
        self.on_stop = on_stop

    def handle_command(self, command: str, args: "list[str] | None" = None) -> str:
        args = args or []
        if command not in COMMANDS:
            return f"Unknown command: {command}. Try one of: {', '.join(COMMANDS)}"
        handler = getattr(self, f"_handle_{command[1:]}")
        return handler(args)

    def _handle_status(self, args: "list[str]") -> str:
        return _format_provider_result("Status", self.status_provider)

    def _handle_performance(self, args: "list[str]") -> str:
        return _format_provider_result("Performance", self.performance_provider)

    def _handle_positions(self, args: "list[str]") -> str:
        return _format_provider_result("Open positions", self.positions_provider)

    def _handle_signals(self, args: "list[str]") -> str:
        # Real `SignalEngine` payloads (identified by the "asset"/"signal"
        # keys every one of them has) get the lean summary formatter --
        # see `_format_signal_summary()`'s docstring for why. Anything
        # else (not wired, empty, or a shape a test/caller made up) falls
        # back to the generic formatter unchanged.
        if self.signals_provider is None:
            return _format_provider_result("Recent signals", None)
        data = self.signals_provider()
        if isinstance(data, (list, tuple)) and data and all(
            isinstance(item, dict) and "asset" in item and "signal" in item for item in data
        ):
            lines = [f"Recent signals ({len(data)}):"]
            lines.extend(_format_signal_summary(s) for s in data)
            return "\n".join(lines)
        return _format_provider_result("Recent signals", lambda: data)

    def _handle_today(self, args: "list[str]") -> str:
        return _format_provider_result("Today", self.today_provider)

    def _handle_risk(self, args: "list[str]") -> str:
        return _format_provider_result("Risk state", self.risk_provider)

    def _handle_mode(self, args: "list[str]") -> str:
        if not args:
            return f"Current mode: {self.mode}"
        new_mode = args[0].upper()
        if new_mode not in MODES:
            return f"Unknown mode {args[0]!r}. Valid modes: {', '.join(MODES)}"
        self.mode = new_mode
        if self.on_mode_change is not None:
            self.on_mode_change(new_mode)
        return f"Mode set to {new_mode}"

    def _handle_stop(self, args: "list[str]") -> str:
        if self.on_stop is None:
            return "Kill switch not wired to this bot instance -- no action taken."
        self.on_stop()
        return "Kill switch engaged -- no new trades will be opened."

    def send_alert(self, alert: dict) -> "str | None":
        """Formats `alert` and, if a `bot_client`/`chat_id` are wired,
        sends it. Always returns the formatted text (so a caller with no
        `bot_client` yet — e.g. this sandbox, with no real Telegram
        token — can still see exactly what would have been sent)."""
        text = format_alert(alert)
        if self.bot_client is not None and self.chat_id is not None:
            self.bot_client.send_message(self.chat_id, text)
        return text
