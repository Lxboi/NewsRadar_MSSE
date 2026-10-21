# Risk Management + Telegram Notifications — Implementation Notes (Phase 12)

Sections 40, 47-55, 66: hard account-level risk limits, position sizing,
and the SNIPER-default Telegram notification pipeline —
`src/strategy/{risk_manager,position_sizing}.py` and
`src/notifications/{alert_manager,telegram_bot}.py`.

## `strategy/risk_manager.py` — the account-level circuit breaker

`RiskManager` tracks state across trades (running equity, the current UTC
day's PnL, a consecutive-loss streak) and exposes `state() ->
{"blocked": bool, "reason": str | None}` — exactly `strategy/entry.py`'s
Phase 11 placeholder contract. `state` is a bound method, so
`StrategyEngine(risk_state=risk_manager.state)` wires in directly with no
change on either side — this was the whole point of documenting that
placeholder shape a phase early.

`record_trade(closed_position)` reads exactly the dict shape
`StrategyEngine._close_position()` (Phase 11) and
`backtest/event_backtester.py`'s trade dicts already produce (`net_pnl`,
`closed_at`) — a real trade and a backtested one are graded by risk
management identically, no translation layer. Daily PnL resets on a UTC
day rollover (detected from each trade's own `closed_at`, not a wall-clock
read, keeping this deterministic/testable exactly like every other
`now`-as-parameter convention in this codebase); the consecutive-loss
streak resets on any non-negative `net_pnl` (a win *or* an exact
breakeven ends the streak — only a strictly negative trade extends it).

Checked in a fixed priority order in `state()` — kill switch first (an
explicit operator decision always wins), then a sticky execution-quality
breach, then daily loss, then consecutive losses — matching the "one
reason wins" convention `strategy/exit.py` already established.

**What this deliberately does not duplicate**: `max_open_positions` is
also a `strategy.yaml: risk` key, but `StrategyEngine.max_open_positions`
(Phase 11) already enforces it structurally — there's no path to bypass
it, so re-checking it here would be redundant, not defense-in-depth.

**A layering choice worth flagging explicitly**: `strategy.yaml`'s
`entry_filter.max_spread_bps` (5, the strategy's own tunable "is this
trade still worth it" filter, `strategy/entry.py`) is *tighter* than this
module's `risk.max_spread_bps`/`max_slippage_bps` (8/8, an account-level
hard ceiling, independent of whatever the strategy filter happens to be
configured to). That the hard ceiling is currently looser than the
tunable filter — so it rarely binds today — is intentional layering
(a defense-in-depth backstop, not the primary gate), not a bug fixed by
guessing which number was meant to change.

`record_fill_quality(slippage_bps=, latency_ms=)` has no real caller yet
— slippage/latency are only known after a fill, and Phase 13/14's
executor doesn't exist. Same "documented but not yet wireable" pattern
already used for `entry.py`'s `risk_state` and `strategy_engine.py`'s
optional `executor` in Phase 11.

## `strategy/position_sizing.py` — pure, risk-based, only ever scaled down

`size_position(risk_inputs) -> float` is a pure function (no state, no
I/O) — same convention as `direction_from_probabilities()`. Base size is
the classic fixed-fractional formula: risk a fixed `risk_per_trade_pct`
of `account_equity` against `stop_distance_bps`. No stop distance means
nothing to size a risk-based position against, so it returns `0.0` rather
than guessing a default notional — section 40's "extremely conservative"
taken literally: fail safe to no position, never to an arbitrary one.

Three independent multipliers then only ever scale the base size *down*:
`signal_confidence` (from `signals/confidence.py`), `volatility`
(relative to an illustrative reference), and `expected_value_bps` (from
`signal_engine.py`'s `expected_value` — non-positive EV collapses the
size to `0.0` outright, since sizing a trade the entry filter would
reject anyway is never useful). Each defaults to a neutral `1.0`
multiplier when its input is missing — never a boost, the mirror image of
`entry.py`/`exit.py`'s "missing data never blocks" convention, applied to
whichever direction "favorable" points here. A final
`max_position_size_pct_equity` ceiling clamps the result — on the
function's own plain defaults (`risk_per_trade_pct=0.1`,
`max_position_size_pct_equity=5.0`), this ceiling binds before the raw
formula does for anything but a very wide stop, which the test suite
documents explicitly rather than leaving implicit.

## `notifications/alert_manager.py` — the anti-spam gate

`AlertManager.should_notify(alert, *, now=None) -> bool` is a pure
decision function, matching `should_enter()`/`should_exit()`'s own
"decide, never mutate" convention — a caller that actually sends the
alert calls `record_sent()` afterwards as an explicit second step, rather
than folding the state update into `should_notify()`'s return. This
keeps "would this be sent" (e.g. a `/status`-style dry-run preview)
cleanly separable from "this was sent."

`alert["type"]` must be one of section 51's 5 `ALERT_TYPES` — anything
else raises `ValueError`, the same "structural, not conventional"
enforcement `backtest/orderbook_backtester.py` (Phase 10) used for its
news-column ablation. Section 52's `never_notify` list (every news item,
every OBI tick, every C-signal, ...) is satisfied by construction: none
of those categories can even reach this gate as a valid `alert["type"]`.

For `HIGH_CONVICTION_SIGNAL` alerts, `_passes_signal_filter()` reuses
`direction_from_probabilities()` (the same shared primitive
`signal_engine.py`/`scoring.py`/`exit.py` already use) and checks
`telegram.yaml: signal_filter`'s thresholds — deliberately **stricter**
than `strategy/entry.py`'s own `entry_filter` (`min_probability=0.68` vs
trading's `0.62`, `required_signal_quality="A"`), since this filter
answers "is this worth interrupting the operator for," not "is this
worth trading." A signal can legitimately be traded without ever
generating a notification.

Cooldown (`cooldown_s_per_asset`, keyed per `(asset, alert_type)`) and
probability-delta dedup (`dedupe_probability_delta_pct`, keyed per
asset) are independent checks — a near-identical probability keeps
suppressing new alerts even *after* the cooldown window has elapsed
(matching the config comment's literal example: "LONG 71/72/73% collapse
into one alert" describes persistent suppression while the edge stays in
a narrow band, not just a fixed time window). An `escalation_reason` in
section 50's `escalate_on` list overrides the cooldown (not the dedup) —
a major event should interrupt a quiet period, but shouldn't cause a
literally-unchanged signal to re-alert.

**Two things flagged rather than silently resolved**, both documented in
the module docstring in full and worth restating here:

1. `config/telegram.yaml: modes.AGGRESSIVE.notify_on_quality` allows
   `["A", "B"]`, but `signal_filter.required_signal_quality: "A"` is
   documented as applying unconditionally ("ALL must pass"). Implemented
   literally, this makes AGGRESSIVE's "B" allowance currently
   unreachable — a real config inconsistency, not resolved by guessing
   which value should change. `required_signal_quality` can be set to
   `None` to disable that global override and let AGGRESSIVE's own
   quality list take effect, if that's the intended fix.
2. SNIPER mode's `require_exceptional_edge: true` isn't otherwise
   defined by the master prompt. Interpreted here as a further-tightened
   probability floor (`sniper_exceptional_probability_min`, default
   `0.75`) layered on top of every other `signal_filter` check — an
   interpretive choice, not a spec-given number, flagged the same way
   every other un-tuned default in this codebase is.

## `notifications/telegram_bot.py` — synchronous core, live wiring deferred

Split deliberately into two layers. `handle_command(command, args)` is
fully synchronous and independent of `python-telegram-bot`'s
`Update`/`Context`/`Application` objects — every command's formatting and
edge case is genuinely unit-tested here, no network, no credentials.
Live wiring (a thin `Application`/`CommandHandler` adapter calling
`handle_command()`) isn't built this phase: `.env`'s
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are blank by design until the
user fills them in, so there's no real bot to smoke-test against in this
sandbox — writing that adapter now would be unverified code presented as
verified, the same reasoning behind every prior phase's "no live-run this
phase" calls (Phase 7's `build_feature_table.py`, Phase 8's baseline
sweep). `main.py` integration is a later, live-validated step.

Every data command (`/status`, `/performance`, `/positions`, `/signals`,
`/today`, `/risk`) is answered by an injected, zero-argument provider
callable — this module has no opinion on where positions or signals
actually live. A provider left `None` degrades to an explicit
"not available yet" message, never a crash. `/mode` and `/stop` are the
two commands with a side effect, each via an injected callback
(`on_mode_change`, `on_stop`) — the natural wiring points are
`alert_manager.mode = new_mode` and
`risk_manager.engage_kill_switch("telegram_stop_command")`.
`RiskManager.engage_kill_switch()` is already a plain method any caller
can invoke — `/stop` is one caller among possibly several, which
satisfies section 55's "a local kill switch independent of Telegram" by
construction, not a second parallel mechanism.

`send_alert()` is synchronous and duck-typed (`bot_client.send_message
(chat_id, text)`, sync signature) — matching `strategy_engine.py`'s
optional `executor` pattern exactly. `python-telegram-bot`'s real
`Bot.send_message` is async-only; the production `bot_client` a later
`main.py` integration supplies is expected to be a small sync-adapter
shim, keeping this module itself testable without `asyncio` or a real
network call.

## Testing

83 new tests: `test_risk_manager.py` (19 — daily loss/consecutive-loss
breach and reset behavior, UTC day rollover, kill switch priority over
every other breach, sticky execution-quality breaches, missing-data
graceful defaults), `test_position_sizing.py` (19 — the base formula,
each multiplier's proportionality/clamping/neutral-default behavior
independently and combined, the equity ceiling both as a dedicated test
and as a documented interaction with the plain defaults),
`test_alert_manager.py` (25 — mode-based quality gating including the
flagged AGGRESSIVE/required-quality inconsistency in both its literal
and disabled form, SNIPER's exceptional-edge floor, cooldown/dedup
independence and interaction, escalation overriding cooldown but not
dedup, structural rejection of an unknown alert type or escalation
reason, `should_notify()`'s no-mutation guarantee), `test_telegram_bot.py`
(20 — every command handled without a provider wired, provider-shaped
formatting for dicts/lists/empty results, `/mode` and `/stop`'s
callback wiring and invalid-input handling, alert formatting per type,
`send_alert()`'s duck-typed client). 554 total, all passing.

Two real bugs caught by this suite before delivery, both fixed in the
implementation (not the test): `RiskManager.engage_kill_switch()`
defaulted its `reason` parameter to the literal string `"manual"`
instead of `None`, which meant `state()`'s `reason or REASON_KILL_SWITCH`
fallback could never actually fire — fixed to default to `None`. And
`telegram_bot.py`'s `_format_provider_result()` checked `if not data`
before branching on type, which caught an empty list as generic
"no data." before it ever reached the list-specific "none." message —
fixed by moving the emptiness check inside each type branch. The
position-sizing test suite also surfaced a design point worth stating
plainly rather than leaving as a surprising test failure: on
`size_position()`'s own plain defaults, the 5% equity ceiling binds
*before* the raw risk-based formula does — most multiplier tests
deliberately pass a very high `max_position_size_pct_equity` to isolate
the multiplier math from the ceiling, with one dedicated test confirming
the ceiling-binds-by-default behavior explicitly.

## No live-run this phase

Same reasoning as Phases 8-11: no real Telegram bot token exists in this
sandbox, and `RiskManager`/`AlertManager` have no live account/position
feed to run against yet (`main.py` isn't wired to Phase 11's
`StrategyEngine` either). Wiring all of this together — `StrategyEngine`
feeding `RiskManager.record_trade()`, `AlertManager` gating
`TelegramBot.send_alert()` calls, `/mode`/`/stop` reaching back into
`AlertManager`/`RiskManager` — is a `main.py` integration step for
whenever paper execution (Phase 13) makes a live-shaped run meaningful.
