# Live Paper Trading — Implementation Notes (Phase 15)

Sections 26, 33-43, 47-55, 65: the real execution phase — every prior
phase's built-but-never-connected pipeline (Phase 7's `FeatureEngine`,
Phase 9's trained models, Phase 11's `SignalEngine`/`StrategyEngine`,
Phase 12's `RiskManager`/`AlertManager`/`TelegramBot`, Phase 13's
`PaperExecutor`/`PerformanceTracker`) wired into one pipeline that
actually runs, gated behind PAPER mode. Requested directly by the user
("Construit moi la phase d'exécution réel...") rather than paced through
the roadmap like the phases before it.

## Why this needed a new module, not a patch to `main.py`

`main.py` is already a fairly large asyncio entrypoint (Phases 2-5's
collector/consumer/display tasks). `src/live/trading_loop.py` keeps the
actual orchestration logic — `AssetTradingUnit` (one asset) and
`PaperTradingEngine` (the account-level owner of `RiskManager`/
`AlertManager`/`TelegramBot`/`PerformanceTracker`, one per enabled
asset with a promoted model) — as plain, synchronous, framework-agnostic
classes. `main.py` only has to construct one, feed it the same raw
events its other consumers already receive, and call `tick()`
periodically. Same "asyncio plumbing stays in `main.py`, real logic stays
testable without asyncio" split every earlier phase's modules already
follow.

## `FeatureEngine` gained a real incremental API — not a second implementation

Before this phase, `FeatureEngine.build_rows()` only worked in offline
batch mode (Phase 7, replaying a full Parquet partition at once). A live
loop needs to call it one event at a time. Rather than writing a second,
parallel "live feature builder" and risking silent drift from the
already-tested batch path, `build_rows()` was refactored into a thin loop
over three new public methods — `on_market_event()`, `on_news_event()`,
`build_row()` — and `trading_loop.py` calls those same three methods
directly. A dedicated equivalence test
(`test_incremental_api_matches_build_rows_exactly`) confirms the
incremental and batch paths produce byte-identical rows; there is now
exactly one feature-computation code path, not two that could disagree.

## A real, previously-silent gap found and fixed: `liquidity_imbalance`

While wiring `StrategyEngine`'s exit candidates against real feature
rows, `strategy/exit.py: _check_liquidity_reversal()` and
`StrategyEngine._open_position()` turned out to read
`row.get("liquidity_imbalance")` / `features.get("liquidity_imbalance")`
— a column `FeatureEngine._build_row()` never actually populated (only
`liquidity_change` existed). This never crashed, since the missing-data
convention throughout this codebase degrades a missing feature to "skip
this check," not an error — which is exactly why it went unnoticed: the
`liquidity_reversal` exit has been silently, permanently inert on every
real feature row ever built, since Phase 11. Fixed by adding the column,
sourced for free from `OrderBookEngine.metrics()["liquidity_imbalance"]`
(already computed, just never threaded through). Covered by a dedicated
regression test.

## Model persistence: a real gap Phase 9 correctly left open

`ModelRegistry` (Phase 9) is deliberately metadata-only — an audit trail
of what was trained and when, never the fitted model object itself. That
was the right scope for Phase 9 (an audit log should stay lightweight
JSONL, not scikit-learn-pipeline-sized), but Phase 15 genuinely needs to
load a fitted model back in a different process. `src/models/
model_persistence.py` (`save_model()`/`load_model()` via `joblib`, the
scikit-learn-recommended serialization for fitted pipeline objects) fills
that gap, plus a new optional, backward-compatible `artifact_path` field
on `ModelRegistry.register()` — older registry records with no
`artifact_path` still read cleanly (`.get()`, never a `KeyError`).

## `scripts/train_model.py` — one CLI, no promotion by default

Trains one `(asset, horizon_s, threshold_bps)` candidate from an
already-built `features_v1` table: chronological, purged train/validation
split (never random — the same section-45 discipline
`backtest/walk_forward.py: WalkForwardValidator` already established,
applied here to a single split point rather than a full walk-forward
study, which is a deliberate scope choice — this script's job is "produce
one candidate to paper-trade," not the full walk-forward statistical
validation Phase 10 already provides separately), fits, evaluates
(directional accuracy + calibration diagnostics), saves the artifact,
registers it. `--promote` requires `--reason`, matching
`ModelRegistry.promote()`'s own requirement — nothing about running this
script can put a model into live paper trading without a human
explicitly saying so.

**A real bug caught by this script's own smoke-testing**: `--threshold-
bps` is declared `type=float` in argparse (so `--threshold-bps 10`
becomes `10.0`), but `models/evaluation.py: actual_direction()` looks up
target columns via `f"target_{horizon_s}s_up_{threshold_bps}bps"`, while
`compute_targets()` always writes those columns with plain int values
(`config/strategy.yaml: move_thresholds_bps` is `[5, 10, 15, 25, 50]`,
all whole numbers). An unnormalized `10.0` silently matched zero target
columns, surfacing several calls later as a confusing "nothing to train
on" error rather than the real cause. Fixed by normalizing to `int`
whenever the CLI value is whole, immediately after argparse — a
documented workaround for a pre-existing fragility in already-tested
Phase 7/9 code this script doesn't own, not a change to that code.

## `PaperTradingEngine` — account-scoped state, per-asset trading units

Mirrors the account-vs-per-question distinction `RiskManager` (one
instance per account) vs. `SignalEngine`/`StrategyEngine` (one instance
per `(asset, horizon_s, threshold_bps)` question) already established:
one `RiskManager`/`AlertManager`/`TelegramBot`/`PerformanceTracker`
shared across every asset, one `AssetTradingUnit` (its own
`FeatureEngine`, `SignalEngine`, `StrategyEngine`, `PaperExecutor`) per
enabled asset.

**An asset with no promoted model is skipped, not fatal.** `model_loader`
returning `None` for one asset logs a warning and excludes that asset
from `engine.units`; `engine.skipped_assets` records which ones, and
`main.py` logs the same information at startup (and the paper-trading
display panel surfaces it every cycle via `status()`). BTC having a
trained, promoted model while ETH doesn't yet shouldn't block paper
trading for every asset.

**`model_loader` is duck-typed, not a hard `ModelRegistry` coupling** —
`PaperTradingEngine` never imports `ModelRegistry`/`model_persistence`
directly; it accepts an injected `model_loader(asset) -> dict | None`
callable. `load_promoted_model()` (in `trading_loop.py`) is the real
implementation `main.py` wires in; tests inject a fake without touching
disk.

**Alerts**: every `tick()` re-derives the freshest signal and offers it
to `AlertManager` as `HIGH_CONVICTION_SIGNAL` regardless of whether
`StrategyEngine` actually opened a position that tick (a signal can be
alert-worthy under `telegram.yaml`'s tighter `signal_filter` without
being trade-worthy under `strategy.yaml`'s looser `entry_filter`, or vice
versa — deliberately different questions, per `alert_manager.py`'s own
docstring). `POSITION_OPENED`/`POSITION_CLOSED` fire directly off
`StrategyEngine.process_row()`'s action list. `RISK_ALERT` is
edge-triggered — fires once, account-wide, the moment
`RiskManager.state()["blocked"]` flips `False → True`, not on every
subsequent blocked tick, avoiding an alert storm for as long as the
account stays blocked. `DAILY_REPORT` is deliberately not generated
here — it's a scheduled, once-per-UTC-day job, left for `main.py`'s own
periodic-task wiring, not guessed at inside this module.

## Telegram: built and tested, activated via a Phase 15 addendum once a real token exists

`notifications/telegram_bot.py` (Phase 12) is real, already-tested logic.
`build_paper_trading_engine()` still constructs `PaperTradingEngine` with
`telegram_bot=None` — alerts always start out console-only, falling back
to structured logging via `AssetTradingUnit._send_alert()` when
`telegram_bot is None` — mirroring Phase 14's "build it, don't activate
it" scope decision for `KrakenExecutor`. What changed in the addendum
below is that `main.py` now *can* replace that `None` with a real,
live-polling bot, the moment `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are
both set in `.env` — see the next section. With neither var set (the
default), behavior is unchanged from the paragraph above: monitoring a
PAPER run means watching the console log or querying SQLite directly —
see `docs/21_paper_trading_methodology.md`.

## Telegram live wiring — Phase 15 addendum (`notifications/telegram_live.py`)

Added after the rest of this phase, once a real Telegram bot token
became available to configure. `telegram_bot.py`'s own module docstring
had already named this exact step — `register_handlers()` — as
deferred, specifically because no real token existed in the sandbox to
smoke-test against; this addendum is that step, built once the trigger
condition it was waiting for was met.

**Manual `Application` lifecycle, not `run_polling()`.**
`python-telegram-bot`'s `Application.run_polling()` takes over the whole
asyncio event loop until a stop signal — fine for a bot that's the whole
program, wrong here, where `main.py` already owns the loop and runs a
dozen other tasks (collectors, consumers, displays, paper trading).
Instead `main.py` calls `application.initialize()` /
`application.start()` / `application.updater.start_polling()` manually
right before its existing `await stop_event.wait()` — the same three
calls `run_polling()` performs internally, and the pattern
`python-telegram-bot`'s own maintainers document as the supported way to
embed an `Application` inside an existing asyncio program with other
work to do (see the `Application`/`Updater` docs and GitHub Discussion
#3310) — and calls `updater.stop()` / `stop()` / `shutdown()` in the same
`finally` block every other task is cancelled in.

**`AsyncioTelegramBotClient` — the sync-adapter shim.**
`TelegramBot.send_alert()` calls `self.bot_client.send_message(chat_id,
text)` synchronously, by design (Phase 12, framework-agnostic). The real
`telegram.Bot.send_message` is async-only. `AsyncioTelegramBotClient`
wraps it behind a sync-signature `send_message()` that schedules the
real send via `asyncio.create_task()` (fire-and-forget) rather than
blocking — safe because every call site already runs inside `main.py`'s
single running event loop. A send failure is caught and logged inside
the scheduled task itself, not left to propagate silently.

**Chat-ID allowlist.** A bot's username is discoverable by anyone; every
registered command handler compares `update.effective_chat.id` against
the one configured `chat_id` and silently ignores anything else — no
reply at all, since replying at all would itself leak that this bot
exists and is live to an unauthorized sender, worse than staying silent.

**Not configured -> inert, not an error.**
`build_live_telegram_bot()` returns `(None, None)` when
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't both set — the same
convention `load_promoted_model()` and `KrakenExecutor`'s gates already
use. `main.py`'s `wire_live_telegram()` is the glue: it calls
`build_live_telegram_bot()` with `paper_trading_engine`'s own
`risk_manager`/`alert_manager` and its six provider methods
(`status`/`performance`/`open_positions`/`recent_signals`/`today`/
`risk`), and on success mutates `paper_trading_engine.telegram_bot` and
every `unit.telegram_bot` in `paper_trading_engine.units.values()` in
place — both are plain, unprotected instance attributes, so no change to
`trading_loop.py` was needed. `/mode` and `/stop` are wired exactly as
`telegram_bot.py`'s own docstring specified:
`alert_manager.mode = new_mode` and
`risk_manager.engage_kill_switch("telegram_stop_command")`.

Tests: `tests/test_telegram_live.py` covers the adapter layer in
isolation (fake `Application`/`Update`/bot doubles, no real network or
token); `tests/test_main_telegram_wiring.py` covers `wire_live_telegram()`
itself, with the default-off case (neither env var set -> engine
untouched) as the most important regression to hold.

## `main.py` wiring — the section-65 two-gate pattern, enforced in code

`paper_trading_enabled(settings)` requires **both**
`trading_mode.default == "PAPER"` (`config/settings.yaml`) **and**
`PAPER_TRADING=true` (`.env`) — settings.yaml has documented this
requirement since Phase 1; this phase is what actually enforces it. With
either gate closed (the default), `paper_trading_engine` stays `None`
and every code path this phase added is a no-op: `main.py`'s behavior is
byte-for-byte identical to before Phase 15 existed. When both gates are
open, `main.py`:

- Builds one `RiskManager` from `strategy.yaml: risk`, one `AlertManager`
  from `telegram.yaml`, and one `PaperTradingEngine` via
  `build_paper_trading_engine()` — every config value is mapped
  explicitly (`build_risk_manager()`/`build_alert_manager()`), not left
  to happen to match a `DEFAULT_*` constant, so tuning `strategy.yaml`/
  `telegram.yaml` actually takes effect in a live run.
- Forwards every `l2Book`/`trades` message `market_state_consumer`
  already receives into `paper_trading_engine.on_market_event()`, and
  every relevant news item `news_consumer` already scores into
  `paper_trading_engine.on_news_event()` — the exact same raw events the
  console display and Parquet/SQLite writers already see, not a second,
  separately-fetched feed.
- Runs three new periodic tasks, only when paper trading is enabled:
  `paper_trading_tick_loop` (ticks every `paper_trading.tick_interval_s`,
  persists any closed position to SQLite's `positions` table
  immediately), `paper_trading_performance_writer` (persists an
  all-time `PerformanceSnapshot` row every
  `paper_trading.performance_snapshot_interval_s`), and
  `paper_trading_display` (a console panel — open positions, latest
  signal per asset, all-time performance — alongside the existing
  `live_display`/`news_live_display` panels).
- Calls `wire_live_telegram(paper_trading_engine, telegram_cfg)`
  (Phase 15 addendum, see the Telegram section above) — a no-op unless
  both `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set in `.env`.

`positions`/`performance` have existed in the SQLite schema since Phase
4 but had no write helper until this phase gave `trading_loop.py` a
reason to actually produce rows that need to survive a process restart —
`storage/database.py` gained `record_position()`/
`record_performance_snapshot()` and `SQLiteRecorder.write_position()`/
`write_performance_snapshot()`, following the exact same "session in,
nothing committed" / "one `Session`, one commit per call" conventions
every earlier `record_*`/`write_*` helper in that module already uses.

## `config/settings.yaml: paper_trading` — new block, Phase 15

`tick_interval_s`, `horizon_s`/`threshold_bps` (which promoted model to
load per asset — must match a key someone has actually promoted via
`scripts/train_model.py --promote`), `initial_cash`/`account_equity`
(kept equal by convention), `performance_snapshot_interval_s`. See the
block's own inline comments in `config/settings.yaml` for the full
two-gate explanation.

## Testing

47 new tests this phase: `tests/test_feature_engine.py` (+4, incremental
API equivalence and the `liquidity_imbalance` regression),
`tests/test_model_registry.py` (+2, `artifact_path`),
`tests/test_model_persistence.py` (5, new file — save/load round-trip
for both model classes, missing-artifact and wrong-class errors),
`tests/test_train_model.py` (10, new file — end-to-end CLI runs against
a real synthetic `features_v1` Parquet table under `tmp_path`, promotion
gating, the threshold_bps-normalization regression, `--calibrate`/
`--model tree`, empty-split/no-rows `SystemExit` paths),
`tests/test_trading_loop.py` (15, new file — skip-on-no-model, a full
open→close cycle with real `RiskManager`/`PerformanceTracker`/alert
bookkeeping, edge-triggered `RISK_ALERT`, news broadcast,
`load_promoted_model()` against a real `ModelRegistry`/artifact),
`tests/test_database_storage.py` (+6, `record_position`/
`record_performance_snapshot`/`SQLiteRecorder` write helpers, including
an end-to-end run through the real `PerformanceTracker` shaping helper),
`tests/test_main_paper_trading_wiring.py` (16, new file — the two-gate
check across every mode/env combination, config-to-constructor mapping
against both hand-built fixtures and the real YAML files, event
forwarding from `market_state_consumer`/`news_consumer`, and the three
new periodic tasks including a tick-exception-doesn't-kill-the-loop
regression). 699 tests total, all passing.

Every new class/function was also manually smoke-tested end-to-end
before the formal suite was written (per this project's established
discipline): a scripted `PaperTradingEngine` run against a synthetic,
strongly-imbalanced order book confirmed a real open → take-profit exit
→ `RiskManager.record_trade()` → `PerformanceTracker.record_trade()` →
`POSITION_OPENED`/`POSITION_CLOSED` alert cycle, end to end, before any
test asserted on it.

## No live-run this phase in the sandbox — see the methodology doc instead

This sandbox has no live Hyperliquid connection and no trained/promoted
model yet (no history has been collected here to train one from) — the
same reason every prior phase stopped short of an actual live run.
Running an actual PAPER-mode paper-trading session is now genuinely
possible end-to-end, but it needs to happen on the user's own machine
against real collected history. `docs/21_paper_trading_methodology.md`
is the step-by-step guide for doing exactly that.
