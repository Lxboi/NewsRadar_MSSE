"""Entrypoint — wires the asyncio task graph described in
docs/02_asyncio_architecture.md.

Phase 2 (section 72): connect to Hyperliquid for BTC/ETH/SOL and display
live top-of-book/trade data. Phase 3 (sections 12-18): feed the same
l2Book/trades events into a per-asset OrderBookEngine and add spread, OBI,
microprice, liquidity and sweep/absorption metrics to the live display.
Phase 4 (section 60): persist raw l2Book levels + trades to Parquet as
they arrive, and commit a per-asset SQLite snapshot (market_data,
orderbook_features, trade_features) every `sqlite_snapshot_interval_s`.
Phase 5 (sections 20-26, 72): poll the RSS feeds in config/sources.yaml,
score every item (classification, sentiment, relevance, credibility,
novelty, dedup), persist raw items to Parquet and scored/relevant items to
SQLite's news_events table, and print a rolling "recent news" panel to the
live display.

Phase 15 (section 65, PAPER activation): if BOTH `trading_mode.default ==
"PAPER"` (settings.yaml) AND `PAPER_TRADING=true` (.env) are set, builds a
`live/trading_loop.py: PaperTradingEngine` (one asset per configured model
with a promoted candidate — see `scripts/train_model.py --promote`),
feeds it the same live l2Book/trades/news events `market_state_consumer`/
`news_consumer` already receive, ticks it on `paper_trading.tick_interval_s`
(settings.yaml), persists closed positions and periodic performance
snapshots to SQLite, and prints a paper-trading panel alongside the
existing displays. With either gate closed (the default), every one of
these additions is inert: `paper_trading_engine` stays `None`, and this
module's behavior is byte-for-byte identical to before this phase existed.
See docs/20_live_paper_trading.md and
docs/21_paper_trading_methodology.md (how to actually run a test).

Run with the venv active, from the crypto_radar/ directory:

    python -m src.main

or, from src/:

    python main.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.collectors.hyperliquid_ws import HyperliquidWebSocketCollector, ReconnectPolicy
from src.collectors.news_collector import NewsCollector
from src.live.trading_loop import PaperTradingEngine, load_promoted_model
from src.models.model_registry import ModelRegistry
from src.monitoring.performance import PerformanceTracker, to_performance_snapshot_row
from src.news.event_detector import apply_decay
from src.news.pipeline import NewsPipeline
from src.notifications.alert_manager import AlertManager
from src.notifications.telegram_live import build_live_telegram_bot
from src.orderbook.orderbook_engine import OrderBookEngine
from src.storage.database import SQLiteRecorder
from src.storage.parquet_store import ParquetStore
from src.strategy.risk_manager import RiskManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


@dataclass
class AssetState:
    """Just enough state to display live top-of-book/trade data. The
    heavier microstructure metrics (OBI, microprice, liquidity, sweeps,
    absorption) live in the asset's OrderBookEngine instead — this stays
    intentionally simple since it only feeds the display's first line."""

    symbol: str
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    last_trade_side: str | None = None
    l2_updates: int = 0
    trade_updates: int = 0
    last_candle_close: float | None = None
    last_update_monotonic: float = field(default=0.0)

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread_bps(self) -> float | None:
        mid = self.mid
        if mid and mid > 0 and self.best_bid is not None and self.best_ask is not None:
            return (self.best_ask - self.best_bid) / mid * 10_000
        return None

    def age_s(self) -> float:
        if self.last_update_monotonic == 0.0:
            return float("inf")
        return time.monotonic() - self.last_update_monotonic


def paper_trading_enabled(settings: dict) -> bool:
    """Section 65's two-gate check, Phase 15's PAPER activation instance
    of it: `trading_mode.default == "PAPER"` in settings.yaml AND
    `PAPER_TRADING=true` in .env must BOTH hold. A single flag flipped on
    its own (a stray env var on a RESEARCH-mode box, or a YAML edit with
    no corresponding .env change) is deliberately not enough — matching
    settings.yaml's own documented "PAPER requires PAPER_TRADING=true in
    .env" comment exactly, now actually enforced in code rather than
    just documented."""
    trading_mode = settings["trading_mode"]["default"]
    env_flag = os.getenv("PAPER_TRADING", "false").strip().lower() == "true"
    return trading_mode == "PAPER" and env_flag


def build_risk_manager(strategy_cfg: dict, *, account_equity: float) -> RiskManager:
    """Maps strategy.yaml's `risk:` block onto `RiskManager`'s
    constructor 1:1 — explicit, not relied on via `RiskManager`'s own
    DEFAULT_* constants (which do mirror this file today, per their own
    comments, but an operator tuning strategy.yaml should see that tuning
    actually take effect in a live paper-trading run without a code
    change)."""
    risk_cfg = strategy_cfg["risk"]
    return RiskManager(
        account_equity=account_equity,
        max_risk_per_trade_pct=risk_cfg["max_risk_per_trade_pct"],
        max_daily_loss_pct=risk_cfg["max_daily_loss_pct"],
        max_position_size_pct_equity=risk_cfg["max_position_size_pct_equity"],
        max_consecutive_losses=risk_cfg["max_consecutive_losses"],
        max_slippage_bps=risk_cfg["max_slippage_bps"],
        max_spread_bps=risk_cfg["max_spread_bps"],
        max_latency_ms=risk_cfg["max_latency_ms"],
    )


def build_alert_manager(telegram_cfg: dict) -> AlertManager:
    """Maps telegram.yaml's `mode`/`signal_filter`/`anti_spam` blocks
    onto `AlertManager`'s constructor 1:1 — same reasoning as
    `build_risk_manager()` above."""
    signal_filter = telegram_cfg["signal_filter"]
    anti_spam = telegram_cfg["anti_spam"]
    return AlertManager(
        mode=telegram_cfg["mode"],
        cooldown_s_per_asset=anti_spam["cooldown_s_per_asset"],
        cooldown_override_on_major_event=anti_spam["cooldown_override_on_major_event"],
        dedupe_probability_delta_pct=anti_spam["dedupe_probability_delta_pct"],
        block_on_recent_duplicate=signal_filter["block_on_recent_duplicate"],
        block_on_risk_lock=signal_filter["block_on_risk_lock"],
        min_probability=signal_filter["min_probability"],
        min_expected_value=signal_filter["min_expected_value"],
        required_signal_quality=signal_filter["required_signal_quality"],
        max_spread_bps=signal_filter["max_spread_bps"],
        max_data_age_s=signal_filter["max_data_age_s"],
    )


def build_paper_trading_engine(
    *, assets: "list[str]", paper_trading_cfg: dict, strategy_cfg: dict, telegram_cfg: dict,
) -> PaperTradingEngine:
    """Assembles the real `model_loader` (`live/trading_loop.py:
    load_promoted_model()` closed over this process's `ModelRegistry` and
    artifact root, plus this run's configured `(horizon_s,
    threshold_bps)` — see `paper_trading.horizon_s`/`threshold_bps` in
    settings.yaml) and every other Phase 15 dependency, then constructs
    one `PaperTradingEngine` for `assets`.

    `telegram_bot=None`: `notifications/telegram_bot.py` (Phase 12) is
    real, tested logic — it has just never been wired to an actual
    Telegram `bot_client` (no `python-telegram-bot` polling loop exists
    in this codebase yet), the same "built, not activated" scope decision
    already made for Phase 14's `KrakenExecutor`. Alerts still happen —
    `AssetTradingUnit._send_alert()` falls back to structured logging
    when `telegram_bot is None` — they just don't leave this process yet.
    Wiring a real bot_client is a separate, later activation step, not
    part of "build the execution phase."""
    model_registry_root = PROJECT_ROOT / "data" / "models"
    registry = ModelRegistry(model_registry_root / "registry.jsonl")
    horizon_s = paper_trading_cfg["horizon_s"]
    threshold_bps = paper_trading_cfg["threshold_bps"]

    def model_loader(asset: str) -> "dict | None":
        return load_promoted_model(registry, model_registry_root, asset, horizon_s, threshold_bps)

    return PaperTradingEngine(
        assets=assets,
        model_loader=model_loader,
        risk_manager=build_risk_manager(strategy_cfg, account_equity=paper_trading_cfg["account_equity"]),
        alert_manager=build_alert_manager(telegram_cfg),
        telegram_bot=None,
        performance_tracker=PerformanceTracker(initial_equity=paper_trading_cfg["initial_cash"]),
        sampling_interval_s=paper_trading_cfg["tick_interval_s"],
        paper_executor_kwargs={"initial_cash": paper_trading_cfg["initial_cash"]},
    )


def wire_live_telegram(
    paper_trading_engine: "PaperTradingEngine | None", telegram_cfg: dict, *, log: "logging.Logger | None" = None,
) -> "object | None":
    """Phase 15 addendum. `paper_trading_engine`'s `telegram_bot` starts
    as `None` (see build_paper_trading_engine()'s docstring) -- if
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set in `.env`, builds the
    real python-telegram-bot `Application` (notifications/telegram_live.py)
    and rewires the already-constructed engine and every one of its units
    onto it (both are plain, unprotected instance attributes). Returns
    the `Application` for the caller to drive the polling lifecycle on
    (see notifications/telegram_live.py's module docstring for why that's
    manual rather than `run_polling()`), or `None` if paper trading is
    off or Telegram isn't configured -- either way alerts keep falling
    back to console logging, exactly as before this addendum."""
    log = log or logging.getLogger("main")
    if paper_trading_engine is None:
        return None
    telegram_application, telegram_bot = build_live_telegram_bot(
        token=os.getenv("TELEGRAM_BOT_TOKEN"),
        chat_id_raw=os.getenv("TELEGRAM_CHAT_ID"),
        mode=telegram_cfg["mode"],
        paper_trading_engine=paper_trading_engine,
        risk_manager=paper_trading_engine.risk_manager,
        alert_manager=paper_trading_engine.alert_manager,
    )
    if telegram_application is None:
        log.info(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing from .env) "
            "-- alerts stay console-only"
        )
        return None
    paper_trading_engine.telegram_bot = telegram_bot
    for unit in paper_trading_engine.units.values():
        unit.telegram_bot = telegram_bot
    log.info(
        "TELEGRAM LIVE-WIRED: alerts and /status /performance /positions /signals /today /risk /mode /stop "
        "will reach chat_id=%s", telegram_bot.chat_id,
    )
    return telegram_application


async def paper_trading_tick_loop(
    engine: PaperTradingEngine, recorder: SQLiteRecorder, interval_s: float,
) -> None:
    """Ticks every asset's strategy every `interval_s` and persists any
    closed positions to SQLite immediately (`positions` table) — closes
    are infrequent relative to the tick rate, unlike
    `sqlite_snapshot_writer`'s periodic metrics snapshot, so writing one
    per close rather than batching is the right granularity here."""
    log = logging.getLogger("paper_trading_tick_loop")
    while True:
        await asyncio.sleep(interval_s)
        try:
            results = engine.tick()
        except Exception:
            log.exception("PaperTradingEngine.tick() raised -- paper trading loop continuing")
            continue
        for asset, actions in results.items():
            for action in actions:
                if action["action"] == "closed":
                    try:
                        recorder.write_position(action["position"])
                    except Exception:
                        log.exception("failed to persist closed position for %s", asset)


async def paper_trading_performance_writer(
    engine: PaperTradingEngine, recorder: SQLiteRecorder, interval_s: float,
) -> None:
    """Periodically persists an all_time `PerformanceSnapshot` row —
    the paper-trading-side equivalent of `sqlite_snapshot_writer()`,
    same "periodic, not per-event, since SQLite/the Pi's I/O don't want
    a write on every tick" reasoning."""
    log = logging.getLogger("paper_trading_performance_writer")
    while True:
        await asyncio.sleep(interval_s)
        try:
            row = to_performance_snapshot_row(engine.performance(), timestamp=dt.datetime.now(dt.timezone.utc))
            recorder.write_performance_snapshot(row)
        except Exception:
            log.exception("failed to write performance snapshot")


async def paper_trading_display(engine: PaperTradingEngine, interval_s: float = 15.0) -> None:
    """Prints open positions, the freshest signal per asset, and the
    all-time performance snapshot — the execution-side equivalent of
    `live_display()`/`news_live_display()`, so a PAPER run gives the
    operator something concrete to eyeball the same way Phases 2-5 did."""
    log = logging.getLogger("paper_trading_display")
    while True:
        await asyncio.sleep(interval_s)
        status = engine.status()
        perf = engine.performance()
        risk_state = status["risk_state"]
        lines = [
            f"  trading={status['trading_assets']} skipped={status['skipped_assets']} "
            f"risk_blocked={risk_state['blocked']} reason={risk_state.get('reason')}",
            f"  all_time: trades={perf['trades']} win_rate={_fmt(perf['win_rate'], '.2f')} "
            f"net_pnl={_fmt(perf['net_pnl'], '+.2f')} max_dd={_fmt(perf['max_drawdown'], '.2f')}",
        ]
        for position in engine.open_positions():
            lines.append(
                f"    OPEN {position['asset']:<5} {position['side']:<4} qty={_fmt(position['quantity'], '.5f')} "
                f"entry={_fmt(position['entry_price'])}"
            )
        for sig in engine.recent_signals():
            if sig.get("signal"):
                lines.append(
                    f"    LAST SIGNAL {sig['asset']:<5} dir={sig['signal']:+d} "
                    f"p_up={_fmt(sig.get('probability_up'), '.2f')} quality={sig.get('signal_quality')}"
                )
        log.info("paper trading snapshot:\n" + "\n".join(lines))


def persist_l2book_levels(parquet_store: ParquetStore, coin: str, payload: dict, ts_received_ns: int) -> None:
    """One Parquet row per book level (bid or ask) — matches the
    per-level layout in docs/05_parquet_schema.md. Note: unlike a typical
    delta-based L2 feed, Hyperliquid's l2Book messages are full snapshots
    (no `sequence` field to persist — docs/05_parquet_schema.md's original
    "sequence" column doesn't apply here and has been dropped in favor of
    `n_orders`, which Hyperliquid does provide)."""
    ts_exchange_ms = payload.get("ts_exchange_ms")
    for side, levels in (("bid", payload.get("bids") or []), ("ask", payload.get("asks") or [])):
        for price, size, n_orders in levels:
            parquet_store.write_row(
                "hyperliquid_l2",
                coin,
                {
                    "ts_exchange_ms": ts_exchange_ms,
                    "ts_received_ns": ts_received_ns,
                    "asset": coin,
                    "side": side,
                    "price": price,
                    "size": size,
                    "n_orders": n_orders,
                },
                ts_ns=ts_received_ns,
            )


def persist_trades(parquet_store: ParquetStore, coin: str, payload: dict, ts_received_ns: int) -> None:
    """One Parquet row per trade."""
    for t in payload.get("trades") or []:
        parquet_store.write_row(
            "hyperliquid_trades",
            coin,
            {
                "ts_exchange_ms": t.get("ts_exchange_ms"),
                "ts_received_ns": ts_received_ns,
                "asset": coin,
                "side": t.get("side"),
                "price": t.get("price"),
                "size": t.get("size"),
                "tid": t.get("tid"),
            },
            ts_ns=ts_received_ns,
        )


async def market_state_consumer(
    name: str,
    queue: "asyncio.Queue",
    state: dict[str, AssetState],
    engines: dict[str, OrderBookEngine],
    parquet_store: ParquetStore | None = None,
    paper_trading_engine: "PaperTradingEngine | None" = None,
) -> None:
    """Drains the raw queue, updates the lightweight per-asset display
    state, feeds l2Book/trades events into each asset's OrderBookEngine
    (Phase 3), and — if a ParquetStore is given — persists the raw levels/
    trades (Phase 4). SQLite gets a periodic snapshot instead, via
    `sqlite_snapshot_writer`, rather than a write per message.

    Phase 15: if `paper_trading_engine` is given, the same raw event is
    also forwarded to it (`on_market_event()`) — its own per-asset
    `FeatureEngine`/`OrderBookEngine` (owned by `live/trading_loop.py:
    AssetTradingUnit`) is deliberately independent of the `engines` dict
    here (see that module's docstring: "expected to diverge, not
    reconciled")."""
    log = logging.getLogger(name)
    total = 0
    while True:
        item = await queue.get()
        total += 1
        payload = item.get("payload") or {}
        ptype = payload.get("type")
        ts_received_ns = item.get("ts_received", 0)

        coin = payload.get("coin")
        if coin and coin in state:
            asset = state[coin]
            asset.last_update_monotonic = time.monotonic()
            engine = engines.get(coin)

            if ptype == "l2Book":
                asset.best_bid = payload.get("best_bid")
                asset.best_ask = payload.get("best_ask")
                asset.l2_updates += 1
                if engine is not None:
                    engine.on_l2book(payload, ts_received_ns)
                if parquet_store is not None:
                    persist_l2book_levels(parquet_store, coin, payload, ts_received_ns)
                if paper_trading_engine is not None:
                    paper_trading_engine.on_market_event(coin, "l2book", payload, ts_received_ns)
            elif ptype == "trades":
                trades = payload.get("trades") or []
                if trades:
                    last = trades[-1]
                    asset.last_trade_price = last.get("price")
                    asset.last_trade_side = last.get("side")
                    asset.trade_updates += len(trades)
                if engine is not None:
                    engine.on_trades(payload)
                if parquet_store is not None:
                    persist_trades(parquet_store, coin, payload, ts_received_ns)
                if paper_trading_engine is not None:
                    paper_trading_engine.on_market_event(coin, "trades", payload, ts_received_ns)
            elif ptype == "candle":
                candles = payload.get("candles") or []
                if candles:
                    asset.last_candle_close = candles[-1].get("close")
        elif ptype == "unknown":
            log.debug("unhandled hyperliquid channel: %s", payload.get("channel"))

        if total % 200 == 0:
            log.info("processed %d messages total", total)


async def sqlite_snapshot_writer(
    recorder: SQLiteRecorder,
    state: dict[str, AssetState],
    engines: dict[str, OrderBookEngine],
    interval_s: float,
) -> None:
    """Every interval_s, commits one market_data + orderbook_features row
    (+ one trade_features row per configured window) per asset that has
    seen at least one book snapshot. Deliberately periodic rather than
    per-message — SQLite/the Raspberry Pi's I/O don't need (or want) a
    write on every tick (section 63: keep the real-time path light).

    The SQLAlchemy commit is synchronous and briefly blocks the event
    loop; at a handful of rows every few seconds this is a non-issue. If
    profiling on the Pi ever says otherwise, moving this to a thread
    executor is the fix — not a redesign.
    """
    log = logging.getLogger("sqlite_snapshot_writer")
    while True:
        await asyncio.sleep(interval_s)
        now = dt.datetime.now(dt.timezone.utc)
        written = 0
        for symbol, engine in engines.items():
            if engine.current is None:
                continue  # no snapshot yet for this asset — nothing to write
            asset_state = state.get(symbol)
            try:
                recorder.write_asset_snapshot(
                    asset=symbol,
                    exchange="hyperliquid",
                    timestamp=now,
                    last_price=asset_state.last_trade_price if asset_state else None,
                    engine_metrics=engine.metrics(),
                    trade_window_stats=engine.trade_flow.all_window_stats(),
                )
                written += 1
            except Exception:
                log.exception("failed to write SQLite snapshot for %s", symbol)
        if written:
            log.debug("wrote SQLite snapshot for %d asset(s)", written)


async def news_consumer(
    name: str,
    queue: "asyncio.Queue",
    pipeline: NewsPipeline,
    recorder: SQLiteRecorder,
    parquet_store: ParquetStore | None,
    recent_news: "deque",
    paper_trading_engine: "PaperTradingEngine | None" = None,
) -> None:
    """Drains the raw news queue (currently RSS only, via NewsCollector).
    Every raw item is persisted to Parquet as-is for replay/audit
    regardless of relevance (`data/raw/news_raw/`, no `asset=` partition —
    see parquet_store.py's Phase 5 note); only items that match at least
    one configured keyword (`market_relevance > 0`) are scored, written to
    SQLite's `news_events` table, and surfaced in the live display — this
    is what keeps official SEC/Fed feeds' many non-crypto press releases
    from flooding the operational view while still keeping them on disk
    for research."""
    log = logging.getLogger(name)
    total = 0
    relevant = 0
    while True:
        item = await queue.get()
        total += 1
        raw_event = item.get("payload") or {}

        if parquet_store is not None:
            ts_received_ns = item.get("ts_received", 0)
            parquet_store.write_row(
                "news_raw",
                None,
                {
                    "source": raw_event.get("source"),
                    "category": raw_event.get("category"),
                    "url": raw_event.get("url"),
                    "title": raw_event.get("title"),
                    "summary": raw_event.get("summary"),
                    "event_time_iso": raw_event.get("event_time").isoformat() if raw_event.get("event_time") else None,
                    "ingestion_time_iso": raw_event.get("ingestion_time").isoformat()
                    if raw_event.get("ingestion_time")
                    else None,
                },
                ts_ns=ts_received_ns,
            )

        scored = pipeline.process(raw_event)
        if scored is not None and scored["market_relevance"] > 0:
            relevant += 1
            try:
                inserted = recorder.write_news_event(scored)
            except Exception:
                log.exception("failed to write news event to SQLite: %s", scored.get("title"))
                inserted = False
            if inserted and not scored.get("duplicate_of"):
                recent_news.append(scored)
            if paper_trading_engine is not None:
                paper_trading_engine.on_news_event(scored)

        if total % 20 == 0:
            log.info("processed %d news item(s), %d relevant", total, relevant)


async def news_live_display(recent_news: "deque", interval_s: float = 20.0) -> None:
    """Prints the most recently ingested, relevant, non-duplicate news
    items with their current (decayed) impact — the news-side equivalent
    of `live_display()`, so a live run gives the user something concrete
    to eyeball for Phase 5 validation the same way Phase 2-4 did."""
    log = logging.getLogger("news_live_display")
    while True:
        await asyncio.sleep(interval_s)
        if not recent_news:
            continue
        now = dt.datetime.now(dt.timezone.utc)
        lines = []
        for event in list(recent_news)[-5:]:
            decayed = apply_decay(event, now)
            assets = ",".join(event.get("affected_assets") or []) or "-"
            lines.append(
                f"  [{event['event_type']:<20}] {event['source']:<24} assets={assets:<10} "
                f"sent={_fmt(event.get('sentiment'), '+.2f')} imp={_fmt(event.get('importance'), '.2f')} "
                f"cred={_fmt(event.get('credibility'), '.2f')} nov={_fmt(event.get('novelty'), '.2f')} "
                f"impact_now={_fmt(decayed['current_impact'], '+.3f')} "
                f"age={_fmt(decayed['age_s'], '.0f')}s :: {event['title'][:80]}"
            )
        log.info("recent relevant news:\n" + "\n".join(lines))


def _fmt(value, spec: str = ",.2f", na: str = "n/a") -> str:
    if value is None:
        return na
    try:
        return format(value, spec)
    except (ValueError, TypeError):
        return str(value)


async def live_display(
    state: dict[str, AssetState], engines: dict[str, OrderBookEngine], interval_s: float = 2.0
) -> None:
    """Prints a compact two-line-per-asset snapshot every interval_s
    seconds: top-of-book/trade (Phase 2) plus OBI/microprice/liquidity/
    sweep metrics (Phase 3). No screen-clearing/TUI so it behaves the same
    in any terminal, including when logs are piped to a file."""
    log = logging.getLogger("live_display")
    while True:
        await asyncio.sleep(interval_s)
        lines = []
        for symbol, s in state.items():
            if s.last_update_monotonic == 0.0:
                lines.append(f"  {symbol:<5} waiting for data...")
                continue
            trade = f"{_fmt(s.last_trade_price)} ({s.last_trade_side})" if s.last_trade_price is not None else "n/a"
            stale = " [STALE]" if s.age_s() > 15 else ""
            lines.append(
                f"  {symbol:<5} mid={_fmt(s.mid):>12} bid={_fmt(s.best_bid):>12} ask={_fmt(s.best_ask):>12} "
                f"spread={_fmt(s.spread_bps, '.1f')+'bps':>9} last_trade={trade:<20} "
                f"l2={s.l2_updates} trades={s.trade_updates}{stale}"
            )

            engine = engines.get(symbol)
            m = engine.metrics() if engine else {}
            sweep = m.get("last_sweep")
            sweep_str = (
                f"{sweep['side']} vol={_fmt(sweep['volume'], '.3f')} disp={_fmt(sweep['price_displacement_bps'], '.1f')}bps"
                if sweep
                else "none"
            )
            lines.append(
                f"        obi1={_fmt(m.get('obi_1'), '+.3f')} obi5={_fmt(m.get('obi_5'), '+.3f')} "
                f"obi10={_fmt(m.get('obi_10'), '+.3f')} obi25={_fmt(m.get('obi_25'), '+.3f')} "
                f"microprice={_fmt(m.get('microprice'))} "
                f"liq_imb={_fmt(m.get('liquidity_imbalance'), '+.3f')} "
                f"transient_liq={_fmt(m.get('transient_liquidity_score'), '.2f')} "
                f"sweeps={m.get('n_recent_sweeps', 0)} (last: {sweep_str})"
            )
        log.info("live market snapshot:\n" + "\n".join(lines))


async def health_heartbeat(interval_s: int) -> None:
    log = logging.getLogger("health")
    while True:
        await asyncio.sleep(interval_s)
        log.debug("heartbeat ok")


async def run() -> None:
    load_dotenv()
    settings = load_yaml("settings.yaml")
    assets_cfg = load_yaml("assets.yaml")
    sources_cfg = load_yaml("sources.yaml")
    strategy_cfg = load_yaml("strategy.yaml")
    telegram_cfg = load_yaml("telegram.yaml")

    setup_logging(settings["logging"]["level"])
    log = logging.getLogger("main")

    trading_mode = settings["trading_mode"]["default"]
    log.info("starting crypto_radar in %s mode (RESEARCH is the safe default)", trading_mode)

    enabled_assets = [a["symbol"] for a in assets_cfg["assets"] if a.get("enabled")]
    log.info("enabled assets: %s", enabled_assets)

    raw_queue: asyncio.Queue = asyncio.Queue(maxsize=settings["asyncio"]["queue_maxsize"])
    asset_state = {symbol: AssetState(symbol=symbol) for symbol in enabled_assets}
    engines = {symbol: OrderBookEngine(coin=symbol) for symbol in enabled_assets}

    storage_cfg = settings["storage"]
    sqlite_path = PROJECT_ROOT / storage_cfg["sqlite_path"]
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{sqlite_path.as_posix()}"
    recorder = SQLiteRecorder(db_url)  # also creates tables if they don't exist yet (Phase 1 schema)
    log.info("SQLite storage ready: %s", sqlite_path)

    parquet_root = PROJECT_ROOT / storage_cfg["raw_dir"]
    parquet_store = ParquetStore(root=parquet_root, flush_every=storage_cfg["parquet_flush_rows"])
    log.info("Parquet storage ready: %s", parquet_root)

    hl_cfg = settings["asyncio"]["reconnect"]
    collector = HyperliquidWebSocketCollector(
        ws_url="wss://api.hyperliquid.xyz/ws",  # overridden by HYPERLIQUID_WS_URL in real deployments
        assets=enabled_assets,
        out_queue=raw_queue,
        reconnect_policy=ReconnectPolicy(
            base_delay_s=hl_cfg["base_delay_s"],
            max_delay_s=hl_cfg["max_delay_s"],
            jitter=hl_cfg["jitter"],
        ),
        heartbeat_timeout_s=settings["asyncio"]["ws_heartbeat_timeout_s"],
    )

    news_queue: asyncio.Queue = asyncio.Queue(maxsize=settings["asyncio"]["queue_maxsize"])
    news_collector = NewsCollector(sources_cfg, news_queue)
    news_pipeline = NewsPipeline(
        keywords=sources_cfg.get("keywords", []),
        rss_feeds=sources_cfg.get("rss_feeds", []),
        dedup_similarity_threshold=sources_cfg["deduplication"]["title_similarity_threshold"],
        dedup_time_window_s=sources_cfg["deduplication"]["time_window_s"],
        default_half_life_s=sources_cfg["decay"]["default_half_life_s"],
    )
    recent_news: "deque" = deque(maxlen=50)
    n_feeds = len(sources_cfg.get("rss_feeds", []))
    log.info("news radar ready: %d RSS feed(s) configured", n_feeds)

    paper_trading_engine: "PaperTradingEngine | None" = None
    paper_trading_cfg = settings.get("paper_trading")
    if paper_trading_enabled(settings):
        if not paper_trading_cfg:
            raise SystemExit(
                "trading_mode=PAPER and PAPER_TRADING=true but settings.yaml has no paper_trading: block "
                "-- see config/settings.yaml's Phase 15 section"
            )
        paper_trading_engine = build_paper_trading_engine(
            assets=enabled_assets, paper_trading_cfg=paper_trading_cfg, strategy_cfg=strategy_cfg, telegram_cfg=telegram_cfg,
        )
        if paper_trading_engine.skipped_assets:
            log.warning(
                "PAPER TRADING: no promoted model yet for %s (horizon_s=%s threshold_bps=%s) -- run "
                "scripts/train_model.py --promote for these first; trading %d/%d configured asset(s)",
                paper_trading_engine.skipped_assets, paper_trading_cfg["horizon_s"], paper_trading_cfg["threshold_bps"],
                len(paper_trading_engine.units), len(enabled_assets),
            )
        log.info(
            "PAPER TRADING ENABLED (trading_mode=PAPER, PAPER_TRADING=true) -- live assets: %s",
            sorted(paper_trading_engine.units),
        )
    else:
        log.info(
            "paper trading inactive (trading_mode=%s, PAPER_TRADING env=%s)",
            trading_mode, os.getenv("PAPER_TRADING", "false"),
        )

    telegram_application = wire_live_telegram(paper_trading_engine, telegram_cfg, log=log)

    tasks = [
        asyncio.create_task(collector.run(), name="hyperliquid_collector"),
        asyncio.create_task(
            market_state_consumer("market_state", raw_queue, asset_state, engines, parquet_store, paper_trading_engine),
            name="market_state_consumer",
        ),
        asyncio.create_task(live_display(asset_state, engines), name="live_display"),
        asyncio.create_task(health_heartbeat(settings["monitoring"]["check_interval_s"]), name="health_heartbeat"),
        asyncio.create_task(
            sqlite_snapshot_writer(recorder, asset_state, engines, storage_cfg["sqlite_snapshot_interval_s"]),
            name="sqlite_snapshot_writer",
        ),
        asyncio.create_task(news_collector.run(), name="news_collector"),
        asyncio.create_task(
            news_consumer("news_consumer", news_queue, news_pipeline, recorder, parquet_store, recent_news, paper_trading_engine),
            name="news_consumer",
        ),
        asyncio.create_task(news_live_display(recent_news), name="news_live_display"),
    ]

    if paper_trading_engine is not None:
        tasks.append(asyncio.create_task(
            paper_trading_tick_loop(paper_trading_engine, recorder, paper_trading_cfg["tick_interval_s"]),
            name="paper_trading_tick_loop",
        ))
        tasks.append(asyncio.create_task(
            paper_trading_performance_writer(
                paper_trading_engine, recorder, paper_trading_cfg["performance_snapshot_interval_s"],
            ),
            name="paper_trading_performance_writer",
        ))
        tasks.append(asyncio.create_task(paper_trading_display(paper_trading_engine), name="paper_trading_display"))

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown signal received")
        collector.stop()
        news_collector.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows: signal handlers via add_signal_handler are limited; Ctrl+C still raises KeyboardInterrupt

    if telegram_application is not None:
        # Manual lifecycle, not run_polling() -- this process's event loop
        # is already running a dozen other tasks. See telegram_live.py's
        # module docstring for why.
        await telegram_application.initialize()
        await telegram_application.start()
        await telegram_application.updater.start_polling()
        log.info("Telegram polling started")

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        _handle_signal()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if telegram_application is not None:
            await telegram_application.updater.stop()
            await telegram_application.stop()
            await telegram_application.shutdown()
            log.info("Telegram polling stopped cleanly")
        written = parquet_store.flush_all()
        if written:
            log.info("flushed %d pending Parquet part file(s) on shutdown", len(written))
        log.info("crypto_radar stopped cleanly")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
