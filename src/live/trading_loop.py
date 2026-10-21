"""Wires Phases 7/9/11/12/13 into one live paper-trading pipeline
(sections 26, 33-43, 47-55): live `FeatureEngine` -> `SignalEngine` ->
`StrategyEngine` (executor=`PaperExecutor`, risk_state=`RiskManager.
state`) -> `AlertManager`/`TelegramBot` -> `PerformanceTracker`, one
asset at a time, off `main.py`'s live Hyperliquid/news feed.

Roadmap: PHASE 15. New (not a Phase 1 stub — no phase before this one
had every prerequisite module built). This is the first module that
actually *runs* a signal through a strategy against live/paper data;
every phase since 11 built and unit-tested a piece of this pipeline in
isolation and explicitly deferred the "wire it into `main.py`'s live
loop" step to a later phase (see each of `docs/16-19_*.md`'s "no
live-run this phase" sections) — this is that later phase.

## Why not just add this to `main.py` directly

`main.py` is already a fairly large asyncio entrypoint (Phases 2-5's
collector/consumer/display tasks). Rather than growing it further, the
actual orchestration logic lives here as a plain, synchronous, framework-
agnostic class (`PaperTradingEngine`) that `main.py` only has to feed
events into and call `tick()` on periodically — the same "keep the
asyncio plumbing in `main.py`, keep the actual logic testable without
asyncio" split every other phase's modules already follow (`StrategyEngine`,
`RiskManager`, `AlertManager`, `TelegramBot` are all plain synchronous
classes for exactly this reason).

## One `AssetTradingUnit` per asset, one shared account below them

Mirrors the account-vs-per-question distinction `RiskManager` (Phase 12,
one instance per account) vs. `SignalEngine`/`StrategyEngine` (Phase 11,
one instance per `(asset, horizon_s, threshold_bps)` question) already
established: `PaperTradingEngine` owns exactly one `RiskManager`,
`AlertManager`, `TelegramBot`, and `PerformanceTracker` — shared across
every asset, since risk/performance/notification state is genuinely
account-scoped, not per-asset — and constructs one `AssetTradingUnit`
per enabled asset, each with its own `FeatureEngine`, `SignalEngine`,
`StrategyEngine`, and `PaperExecutor`.

## `model_loader`: a duck-typed dependency, not a hard `ModelRegistry` coupling

`PaperTradingEngine` never imports `ModelRegistry`/`model_persistence`
directly — it accepts an injected `model_loader(asset) -> dict | None`
callable (the same "accept a narrow duck-typed dependency" convention
`StrategyEngine`'s `executor` and `PaperExecutor`'s `price_lookup`
already use). `load_promoted_model()` below is the real implementation
`main.py` wires in (reads `ModelRegistry.current_promoted()` +
`model_persistence.load_model()`); a test can inject a fake without
touching disk or a real registry file.

**An asset with no promoted model is skipped, not fatal**: `model_loader`
returning `None` for one asset logs a warning and excludes that asset
from `self.units` — BTC having a trained, promoted model while ETH
doesn't yet shouldn't crash the whole paper-trading run for every asset.
`self.skipped_assets` records exactly which ones and why a caller (the
live display, `/status`) can surface it rather than silently trading
fewer assets than the user thinks.

## `PaperExecutor`'s `price_lookup`: each unit's own live feature engine

Each `AssetTradingUnit` defaults its `PaperExecutor`'s `price_lookup` to
a closure reading its own `FeatureEngine`'s internal `OrderBookEngine.
metrics()` (`mid`/`spread_bps`) — the same live book state the feature
row itself was just built from, so a simulated fill is never priced
against a stale or different quote than the one the signal was scored
against. This is deliberately independent of `main.py`'s own separate
per-asset `OrderBookEngine` (used for the console display) — two
instances fed the same raw messages, expected to hold near-identical but
not necessarily byte-identical state, the same "expected to diverge,
not reconciled" call already made for `PaperExecutor`'s ledger vs.
`StrategyEngine`'s bookkeeping (Phase 13) and reused here one layer up.

## Alerts: `HIGH_CONVICTION_SIGNAL` every tick, `POSITION_OPENED`/
## `POSITION_CLOSED` on action, `RISK_ALERT` on a blocked-state transition

Every `tick()` call re-derives the unit's freshest signal (via
`SignalEngine.build_signal()`) and offers it to `AlertManager` as a
`HIGH_CONVICTION_SIGNAL` candidate regardless of whether `StrategyEngine`
itself opened a position that tick (a signal can be alert-worthy under
`config/telegram.yaml`'s tighter `signal_filter` without being
trade-worthy under `strategy.yaml`'s looser `entry_filter`, or vice
versa — the two filters are deliberately different questions, per
`alert_manager.py`'s own module docstring). `POSITION_OPENED`/
`POSITION_CLOSED` alerts fire directly off `StrategyEngine.process_row()`'s
own action list. `RISK_ALERT` fires once, account-wide, the moment
`RiskManager.state()["blocked"]` flips `False` -> `True` (not on every
subsequent blocked tick — an edge-triggered check, avoiding a `RISK_ALERT`
storm for as long as the account stays blocked).

**`DAILY_REPORT` is deliberately not generated here** — it's naturally a
scheduled (once-per-UTC-day), not tick-driven, job; left for `main.py`'s
own periodic-task wiring (the same convention `sqlite_snapshot_writer`/
`news_live_display` already use for their own fixed-interval loops)
rather than guessed at inside this module.
"""
from __future__ import annotations

import datetime as dt
import logging

from src.execution.paper_executor import PaperExecutor
from src.features.feature_engine import FeatureEngine
from src.models.model_persistence import load_model
from src.monitoring.performance import PerformanceTracker
from src.notifications.telegram_bot import format_alert
from src.signals.signal_engine import SignalEngine
from src.strategy.strategy_engine import StrategyEngine

logger = logging.getLogger("trading_loop")

DEFAULT_SAMPLING_INTERVAL_S = 1.0


def load_promoted_model(registry, model_registry_root, asset: str, horizon_s: int, threshold_bps: float) -> "dict | None":
    """The real `model_loader` implementation `main.py` wires into
    `PaperTradingEngine` — reads `ModelRegistry.current_promoted()` for
    `(asset, horizon_s, threshold_bps)` and loads the artifact it points
    at via `model_persistence.load_model()`. Returns `None` (not a raise)
    if nothing has ever been promoted for this key, or if the promoted
    record has no `artifact_path` (an older/hand-registered record) —
    both are "no live model available yet," not an error, and
    `PaperTradingEngine` treats either the same way (skip this asset,
    log why)."""
    promoted = registry.current_promoted(asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps)
    if promoted is None:
        logger.warning(
            "[%s] no promoted model for horizon_s=%s threshold_bps=%s -- run scripts/train_model.py "
            "with --promote once you have a candidate you trust", asset, horizon_s, threshold_bps,
        )
        return None
    artifact_path = promoted.get("artifact_path")
    if not artifact_path:
        logger.warning(
            "[%s] promoted model_version=%s has no artifact_path recorded -- was it registered before "
            "Phase 15's model_persistence.py existed? Re-register with scripts/train_model.py.",
            asset, promoted["model_version"],
        )
        return None
    model = load_model(model_registry_root / artifact_path)
    return {
        "model": model,
        "model_version": promoted["model_version"],
        "horizon_s": promoted["horizon_s"],
        "threshold_bps": promoted["threshold_bps"],
        "model_calibration": promoted.get("calibration"),
    }


class AssetTradingUnit:
    """One asset's full live pipeline. See module docstring."""

    def __init__(
        self,
        asset: str,
        *,
        model,
        model_version: str,
        horizon_s: int,
        threshold_bps: float,
        risk_manager,
        performance_tracker: PerformanceTracker,
        model_calibration: "dict | None" = None,
        alert_manager=None,
        telegram_bot=None,
        sampling_interval_s: float = DEFAULT_SAMPLING_INTERVAL_S,
        strategy_engine_kwargs: "dict | None" = None,
        paper_executor_kwargs: "dict | None" = None,
    ) -> None:
        self.asset = asset
        self.feature_engine = FeatureEngine(asset=asset, sampling_interval_s=sampling_interval_s)
        self.signal_engine = SignalEngine(
            model, asset=asset, horizon_s=horizon_s, threshold_bps=threshold_bps,
            model_version=model_version, model_calibration=model_calibration,
        )
        self.executor = PaperExecutor(price_lookup=self._live_price_lookup, **(paper_executor_kwargs or {}))
        self.strategy_engine = StrategyEngine(
            self.signal_engine, risk_state=risk_manager.state, executor=self.executor,
            **(strategy_engine_kwargs or {}),
        )
        self.risk_manager = risk_manager
        self.alert_manager = alert_manager
        self.telegram_bot = telegram_bot
        self.performance_tracker = performance_tracker
        self.last_signal: "dict | None" = None

    def _live_price_lookup(self, asset: str) -> "dict | None":
        metrics = self.feature_engine.engine.metrics()
        if metrics.get("mid") is None:
            return None
        return {"price": metrics["mid"], "spread_bps": metrics.get("spread_bps")}

    def on_market_event(self, kind: str, payload: dict, ts_received_ns: int) -> None:
        self.feature_engine.on_market_event(kind, payload, ts_received_ns)

    def on_news_event(self, event: dict) -> None:
        self.feature_engine.on_news_event(event)

    def tick(self, *, now: "dt.datetime | None" = None) -> "list[dict]":
        """Advances this asset's strategy by one tick. Returns
        `StrategyEngine.process_row()`'s action list (empty if nothing
        happened, or if there's no live book snapshot for this asset
        yet)."""
        now = now if now is not None else dt.datetime.now(dt.timezone.utc)
        t_ns = int(now.timestamp() * 1_000_000_000)
        row = self.feature_engine.build_row(t_ns, now)
        if row.get("price") is None:
            return []  # no book snapshot for this asset yet -- nothing to score

        actions = self.strategy_engine.process_row(row, now=now)
        for action in actions:
            self._handle_action(action, now=now)

        if len(self.strategy_engine.open_positions) < self.strategy_engine.max_open_positions:
            self.last_signal = self.signal_engine.build_signal(row, now=now)
        self._maybe_alert_signal(now=now)
        return actions

    def _handle_action(self, action: dict, *, now: "dt.datetime") -> None:
        if action["action"] == "closed":
            position = action["position"]
            self.risk_manager.record_trade(position)
            self.performance_tracker.record_trade(position)
            self._send_alert({"type": "POSITION_CLOSED", "asset": self.asset, "position": position}, now=now)
        elif action["action"] == "opened":
            self._send_alert(
                {"type": "POSITION_OPENED", "asset": self.asset, "position": action["position"], "signal": action["signal"]},
                now=now,
            )

    def _maybe_alert_signal(self, *, now: "dt.datetime") -> None:
        if self.last_signal is None or self.last_signal.get("signal") == 0:
            return
        self._send_alert(
            {"type": "HIGH_CONVICTION_SIGNAL", "asset": self.asset, "signal": self.last_signal,
             "risk_blocked": self.risk_manager.state().get("blocked", False)},
            now=now,
        )

    def _send_alert(self, alert: dict, *, now: "dt.datetime") -> None:
        if self.alert_manager is None:
            return
        try:
            should_notify = self.alert_manager.should_notify(alert, now=now)
        except ValueError:
            logger.exception("[%s] alert_manager rejected malformed alert: %r", self.asset, alert.get("type"))
            return
        if not should_notify:
            return
        self.alert_manager.record_sent(alert, now=now)
        if self.telegram_bot is not None:
            self.telegram_bot.send_alert(alert)
        else:
            logger.info("[%s] ALERT (no Telegram bot wired): %s", self.asset, format_alert(alert))


class PaperTradingEngine:
    """Owns the shared account-level state and one `AssetTradingUnit` per
    asset with a promoted model available. See module docstring."""

    def __init__(
        self,
        *,
        assets: "list[str]",
        model_loader,
        risk_manager,
        alert_manager=None,
        telegram_bot=None,
        performance_tracker: "PerformanceTracker | None" = None,
        sampling_interval_s: float = DEFAULT_SAMPLING_INTERVAL_S,
        strategy_engine_kwargs: "dict | None" = None,
        paper_executor_kwargs: "dict | None" = None,
    ) -> None:
        self.risk_manager = risk_manager
        self.alert_manager = alert_manager
        self.telegram_bot = telegram_bot
        self.performance_tracker = performance_tracker or PerformanceTracker()
        self.units: "dict[str, AssetTradingUnit]" = {}
        self.skipped_assets: "list[str]" = []
        self._was_blocked = False

        for asset in assets:
            loaded = model_loader(asset)
            if loaded is None:
                self.skipped_assets.append(asset)
                continue
            self.units[asset] = AssetTradingUnit(
                asset,
                model=loaded["model"], model_version=loaded["model_version"],
                horizon_s=loaded["horizon_s"], threshold_bps=loaded["threshold_bps"],
                model_calibration=loaded.get("model_calibration"),
                risk_manager=risk_manager, alert_manager=alert_manager, telegram_bot=telegram_bot,
                performance_tracker=self.performance_tracker, sampling_interval_s=sampling_interval_s,
                strategy_engine_kwargs=strategy_engine_kwargs, paper_executor_kwargs=paper_executor_kwargs,
            )
        if self.skipped_assets:
            logger.warning("paper trading skipped for asset(s) with no promoted model: %s", self.skipped_assets)

    def on_market_event(self, asset: str, kind: str, payload: dict, ts_received_ns: int) -> None:
        unit = self.units.get(asset)
        if unit is not None:
            unit.on_market_event(kind, payload, ts_received_ns)

    def on_news_event(self, event: dict) -> None:
        for unit in self.units.values():
            unit.on_news_event(event)

    def tick(self, *, now: "dt.datetime | None" = None) -> "dict[str, list[dict]]":
        """Advances every asset's strategy by one tick. Returns
        `{asset: actions}` for every asset that has a unit (i.e. a
        promoted model) — assets in `skipped_assets` are absent, not
        present with an empty list, so a caller can distinguish
        "nothing happened this tick" from "this asset isn't trading at
        all."""
        now = now if now is not None else dt.datetime.now(dt.timezone.utc)
        result = {asset: unit.tick(now=now) for asset, unit in self.units.items()}
        self._check_risk_transition(now=now)
        return result

    def _check_risk_transition(self, *, now: "dt.datetime") -> None:
        state = self.risk_manager.state()
        blocked = state.get("blocked", False)
        if blocked and not self._was_blocked and self.alert_manager is not None:
            alert = {"type": "RISK_ALERT", "reason": state.get("reason")}
            try:
                if self.alert_manager.should_notify(alert, now=now):
                    self.alert_manager.record_sent(alert, now=now)
                    if self.telegram_bot is not None:
                        self.telegram_bot.send_alert(alert)
                    else:
                        logger.warning("RISK_ALERT (no Telegram bot wired): reason=%s", state.get("reason"))
            except ValueError:
                logger.exception("alert_manager rejected the RISK_ALERT type -- check ALERT_TYPES")
        self._was_blocked = blocked

    # -- TelegramBot provider callables (zero-argument, per that module's contract) --

    def status(self) -> dict:
        return {
            "trading_assets": sorted(self.units), "skipped_assets": sorted(self.skipped_assets),
            "risk_state": self.risk_manager.state(),
        }

    def open_positions(self) -> "list[dict]":
        return [p for unit in self.units.values() for p in unit.strategy_engine.open_positions]

    def recent_signals(self) -> "list[dict]":
        return [unit.last_signal for unit in self.units.values() if unit.last_signal is not None]

    def performance(self) -> dict:
        return self.performance_tracker.all_time_snapshot()

    def today(self) -> dict:
        return self.performance_tracker.daily_snapshot(now=dt.datetime.now(dt.timezone.utc))

    def risk(self) -> dict:
        return self.risk_manager.state()
