"""SQLite schema for crypto_radar (section 60).

SQLAlchemy 2.0 declarative models for every table the research pipeline
needs from day one. SQLite is the Phase-1/4 storage engine; large
time-series payloads (order book snapshots, tick-level features) belong in
Parquet instead (see docs/05_parquet_schema.md and storage/parquet_store.py)
— SQLite here holds events, features summaries, signals, orders, fills,
positions and performance: things queried by id/time range, not scanned in
bulk.

This module only defines the schema and a thin session/engine factory.
No business logic lives here.

Roadmap: PHASE 4 (storage online), used by every later phase.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    TypeDecorator,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetime that actually round-trips as
    tz-aware, even on SQLite.

    Found during Phase 6's clock-synchronization audit (see
    `docs/11_news_market_sync.md`): SQLite has no native datetime type, so
    plain `DateTime(timezone=True)` silently stores the value and then
    hands it back with `tzinfo=None` on read — every timestamp in this
    schema was affected. That's not a wrong *value* (the wall-clock
    instant is preserved), but it quietly downgrades the safety net: a
    freshly-created `datetime.now(timezone.utc)` compared against a value
    read back from the DB would raise `TypeError: can't compare
    offset-naive and offset-aware datetimes` — or worse, if both happen to
    be naive, compare *silently* with no guarantee they're both really
    UTC. Every timestamp column below uses this type instead of
    `DateTime(timezone=True)` directly so the tz-aware contract holds on
    the way out, not just on the way in.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime passed to a UTCDateTime column — attach "
                "tzinfo=dt.timezone.utc explicitly (section 29: every "
                "timestamp in this system must be UTC and tz-aware)"
            )
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=dt.timezone.utc)


# -----------------------------------------------------------------------------
# News (sections 22-25)
# -----------------------------------------------------------------------------
class NewsEvent(Base):
    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_uid: Mapped[str] = mapped_column(String, unique=True, index=True)  # dedup key

    source: Mapped[str] = mapped_column(String, index=True)
    url: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)

    event_time: Mapped[dt.datetime] = mapped_column(UTCDateTime())       # when it happened / was published
    ingestion_time: Mapped[dt.datetime] = mapped_column(UTCDateTime())   # when WE saw it — used for no-look-ahead joins

    event_type: Mapped[str] = mapped_column(String, index=True)   # section 24 category
    sentiment: Mapped[float] = mapped_column(Float)                # news_score in [-1,1]
    importance: Mapped[float] = mapped_column(Float)               # [0,1]
    credibility: Mapped[float] = mapped_column(Float)               # [0,1]
    novelty: Mapped[float] = mapped_column(Float)                   # [0,1]
    market_relevance: Mapped[float] = mapped_column(Float)          # [0,1]
    affected_assets: Mapped[str] = mapped_column(JSON)              # list[str] as JSON

    duplicate_of: Mapped[int | None] = mapped_column(
        ForeignKey("news_events.id"), nullable=True
    )
    raw_payload: Mapped[dict] = mapped_column(JSON)


# -----------------------------------------------------------------------------
# Market data summaries (tick/L2-level detail lives in Parquet, not here)
# -----------------------------------------------------------------------------
class MarketData(Base):
    __tablename__ = "market_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String, index=True)
    exchange: Mapped[str] = mapped_column(String, index=True)   # hyperliquid | kraken
    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)

    last_price: Mapped[float] = mapped_column(Float)
    mid_price: Mapped[float] = mapped_column(Float)
    best_bid: Mapped[float] = mapped_column(Float)
    best_ask: Mapped[float] = mapped_column(Float)


class OrderbookFeatures(Base):
    __tablename__ = "orderbook_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)

    spread_abs: Mapped[float] = mapped_column(Float)
    spread_bps: Mapped[float] = mapped_column(Float)
    obi_1: Mapped[float] = mapped_column(Float)
    obi_5: Mapped[float] = mapped_column(Float)
    obi_10: Mapped[float] = mapped_column(Float)
    obi_25: Mapped[float] = mapped_column(Float)
    microprice: Mapped[float] = mapped_column(Float)
    microprice_delta: Mapped[float] = mapped_column(Float)
    liquidity_imbalance: Mapped[float] = mapped_column(Float)
    wall_persistence: Mapped[float] = mapped_column(Float)
    transient_liquidity_score: Mapped[float] = mapped_column(Float)


class TradeFeatures(Base):
    __tablename__ = "trade_features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)
    window_s: Mapped[int] = mapped_column(Integer)   # 1/5/10/30/60/180

    buy_volume: Mapped[float] = mapped_column(Float)
    sell_volume: Mapped[float] = mapped_column(Float)
    trade_imbalance: Mapped[float] = mapped_column(Float)
    trade_velocity: Mapped[float] = mapped_column(Float)
    avg_trade_size: Mapped[float] = mapped_column(Float)
    large_trade_freq: Mapped[float] = mapped_column(Float)
    sweep_score: Mapped[float] = mapped_column(Float)
    buy_absorption_score: Mapped[float] = mapped_column(Float)
    sell_absorption_score: Mapped[float] = mapped_column(Float)


# -----------------------------------------------------------------------------
# Signals (sections 33, 57 — log EVERY opportunity, not just notified ones)
# -----------------------------------------------------------------------------
class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime(), index=True)

    horizon_s: Mapped[int] = mapped_column(Integer)
    threshold_bps: Mapped[float] = mapped_column(Float)

    probability_up: Mapped[float] = mapped_column(Float)
    probability_down: Mapped[float] = mapped_column(Float)
    probability_neutral: Mapped[float] = mapped_column(Float)
    expected_move: Mapped[float] = mapped_column(Float)
    expected_value: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)

    signal_quality: Mapped[str] = mapped_column(String, index=True)   # A | B | C | NO_TRADE
    market_regime: Mapped[str] = mapped_column(String)
    model_version: Mapped[str] = mapped_column(String)

    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    features_snapshot: Mapped[dict] = mapped_column(JSON)   # full feature row for reproducibility

    outcome_30s: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_60s: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_180s: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_300s: Mapped[float | None] = mapped_column(Float, nullable=True)


# -----------------------------------------------------------------------------
# Execution (sections 41-44)
# -----------------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    asset: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)          # buy | sell
    order_type: Mapped[str] = mapped_column(String)     # market | limit
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)   # pending|filled|partial|rejected|cancelled
    executor: Mapped[str] = mapped_column(String)        # paper | kraken
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), default=_utcnow)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    fill_price: Mapped[float] = mapped_column(Float)
    fill_quantity: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    slippage_bps: Mapped[float] = mapped_column(Float)
    latency_ms: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime(), default=_utcnow)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[dt.datetime] = mapped_column(UTCDateTime())
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class PerformanceSnapshot(Base):
    __tablename__ = "performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[dt.datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    period: Mapped[str] = mapped_column(String)   # hourly | daily
    trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    max_drawdown: Mapped[float] = mapped_column(Float)
    best_asset: Mapped[str | None] = mapped_column(String, nullable=True)
    best_setup: Mapped[str | None] = mapped_column(String, nullable=True)
    worst_setup: Mapped[str | None] = mapped_column(String, nullable=True)


def get_engine(db_url: str):
    """Create the SQLAlchemy engine. db_url normally comes from .env DATABASE_URL."""
    return create_engine(db_url, future=True)


def get_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(db_url: str) -> None:
    """Create all tables if they don't exist yet. Idempotent, safe to call at startup."""
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)


# -----------------------------------------------------------------------------
# Insert helpers (Phase 4) — thin wrappers so main.py doesn't need to know
# the ORM model shapes, and so the "what fields come from where" mapping
# lives in one place. None of these commit — pass a Session and commit once
# per batch (see SQLiteRecorder below) rather than once per row, since a
# multi-asset periodic snapshot writer wants one commit per tick, not one
# per row (matters more once this runs on the Raspberry Pi, section 63).
# -----------------------------------------------------------------------------


def record_market_data(
    session,
    *,
    asset: str,
    exchange: str,
    timestamp: dt.datetime,
    last_price: float | None,
    mid_price: float | None,
    best_bid: float | None,
    best_ask: float | None,
) -> None:
    session.add(
        MarketData(
            asset=asset,
            exchange=exchange,
            timestamp=timestamp,
            last_price=last_price or 0.0,
            mid_price=mid_price or 0.0,
            best_bid=best_bid or 0.0,
            best_ask=best_ask or 0.0,
        )
    )


def record_orderbook_features(session, *, asset: str, timestamp: dt.datetime, metrics: dict) -> None:
    """`metrics` is an OrderBookEngine.metrics() dict (src/orderbook/orderbook_engine.py)."""
    session.add(
        OrderbookFeatures(
            asset=asset,
            timestamp=timestamp,
            spread_abs=metrics.get("spread_abs") or 0.0,
            spread_bps=metrics.get("spread_bps") or 0.0,
            obi_1=metrics.get("obi_1") or 0.0,
            obi_5=metrics.get("obi_5") or 0.0,
            obi_10=metrics.get("obi_10") or 0.0,
            obi_25=metrics.get("obi_25") or 0.0,
            microprice=metrics.get("microprice") or 0.0,
            microprice_delta=metrics.get("microprice_delta") or 0.0,
            liquidity_imbalance=metrics.get("liquidity_imbalance") or 0.0,
            wall_persistence=metrics.get("wall_persistence") or 0.0,
            transient_liquidity_score=metrics.get("transient_liquidity_score") or 0.0,
        )
    )


def record_trade_features(session, *, asset: str, timestamp: dt.datetime, window_s: int, stats: dict) -> None:
    """`stats` is one entry from TradeFlowTracker.all_window_stats()
    (src/orderbook/trade_flow.py) — i.e. the dict returned by window_stats()."""
    session.add(
        TradeFeatures(
            asset=asset,
            timestamp=timestamp,
            window_s=window_s,
            buy_volume=stats.get("buy_volume") or 0.0,
            sell_volume=stats.get("sell_volume") or 0.0,
            trade_imbalance=stats.get("trade_imbalance") or 0.0,
            trade_velocity=stats.get("trade_velocity") or 0.0,
            avg_trade_size=stats.get("avg_trade_size") or 0.0,
            large_trade_freq=stats.get("large_trade_freq") or 0.0,
            sweep_score=0.0,          # no single scalar sweep score yet — Phase 7 territory
            buy_absorption_score=0.0,  # written separately from orderbook_features'/engine's absorption fields
            sell_absorption_score=0.0,
        )
    )


def record_news_event(session, *, event: dict) -> "NewsEvent | None":
    """Inserts one `NewsEvent` row from a `src/news/pipeline.py`-scored
    event dict. Idempotent on `event_uid`: returns `None` (no insert) if
    an event with the same `event_uid` already exists — safe to call even
    if the same article is re-seen across polls before the in-memory
    per-feed seen-set would have caught it (e.g. right after a restart).

    `event["duplicate_of"]`, if set, is the *canonical event's*
    `event_uid` (a string, per `DeduplicationTracker`) — resolved here to
    the canonical row's integer `id` to match the `NewsEvent.duplicate_of`
    foreign key. If the canonical event was itself never written (e.g. it
    didn't pass the caller's relevance gate), this degrades gracefully to
    `duplicate_of=None` rather than raising — the event is then stored as
    if it were its own canonical, which is the safe direction to fail in
    (never silently drop a relevant article)."""
    existing = session.query(NewsEvent).filter_by(event_uid=event["event_uid"]).first()
    if existing is not None:
        return None

    duplicate_of_id = None
    duplicate_of_uid = event.get("duplicate_of")
    if duplicate_of_uid:
        canonical = session.query(NewsEvent).filter_by(event_uid=duplicate_of_uid).first()
        duplicate_of_id = canonical.id if canonical is not None else None

    news_event = NewsEvent(
        event_uid=event["event_uid"],
        source=event.get("source") or "",
        url=event.get("url") or "",
        title=event.get("title") or "",
        event_time=event["event_time"],
        ingestion_time=event["ingestion_time"],
        event_type=event.get("event_type") or "OTHER",
        sentiment=event.get("sentiment") if event.get("sentiment") is not None else 0.0,
        importance=event.get("importance") if event.get("importance") is not None else 0.0,
        credibility=event.get("credibility") if event.get("credibility") is not None else 0.5,
        novelty=event.get("novelty") if event.get("novelty") is not None else 1.0,
        market_relevance=event.get("market_relevance") if event.get("market_relevance") is not None else 0.0,
        affected_assets=event.get("affected_assets") or [],
        duplicate_of=duplicate_of_id,
        raw_payload=event.get("raw_payload") or {},
    )
    session.add(news_event)
    return news_event


def record_position(session, *, closed_position: dict) -> None:
    """Inserts one `Position` row from a closed-position dict — the exact
    shape `strategy/strategy_engine.py: StrategyEngine._close_position()`
    already produces (Phase 11) and `monitoring/performance.py:
    PerformanceTracker.record_trade()` already accumulates (Phase 13):
    `{"asset", "side", "entry_price", "exit_price", "quantity",
    "opened_at", "closed_at", "exit_reason", "gross_pnl", "fees",
    "slippage", "net_pnl", ...}` (extra keys, e.g. `entry_confidence`,
    are ignored — this table only has columns for the round-trip-trade
    fields, not every bookkeeping detail the in-memory dict carries).

    Roadmap: PHASE 15. `positions`/`performance` (below) existed in the
    schema since Phase 4 but had no write helper until this phase gave
    `live/trading_loop.py` a reason to actually produce closed
    positions/snapshots that need to survive a process restart."""
    session.add(
        Position(
            asset=closed_position["asset"],
            side=closed_position["side"],
            entry_price=closed_position["entry_price"],
            exit_price=closed_position.get("exit_price"),
            quantity=closed_position["quantity"],
            opened_at=closed_position["opened_at"],
            closed_at=closed_position.get("closed_at"),
            exit_reason=closed_position.get("exit_reason"),
            gross_pnl=closed_position.get("gross_pnl"),
            fees=closed_position.get("fees"),
            slippage=closed_position.get("slippage"),
            net_pnl=closed_position.get("net_pnl"),
        )
    )


def record_performance_snapshot(session, *, row: dict) -> None:
    """Inserts one `PerformanceSnapshot` row from
    `monitoring/performance.py: to_performance_snapshot_row()`'s output
    — already shaped to this table's exact column set, so this is a
    thin `session.add(PerformanceSnapshot(**row))` wrapper, kept as its
    own function only to match every other `record_*()` helper's
    "session in, nothing committed" convention in this module."""
    session.add(PerformanceSnapshot(**row))


def list_canonical_news_events(session) -> "list[dict]":
    """All non-duplicate (`duplicate_of is None`) news events, ordered by
    `ingestion_time` ascending, shaped as plain dicts for
    `features/feature_engine.py` (Phase 7) to consume without needing a
    live SQLAlchemy session or ORM objects — feature building is a batch/
    offline step over already-collected data, not something that should
    carry a DB dependency into its core logic. Duplicates are excluded
    here (not just left with a `duplicate_of` pointer) because a feature
    row should count one real-world event once, not once per re-reported
    duplicate across sources."""
    rows = (
        session.query(NewsEvent)
        .filter(NewsEvent.duplicate_of.is_(None))
        .order_by(NewsEvent.ingestion_time.asc())
        .all()
    )
    return [
        {
            "event_uid": r.event_uid,
            "source": r.source,
            "title": r.title,
            "event_time": r.event_time,
            "ingestion_time": r.ingestion_time,
            "event_type": r.event_type,
            "sentiment": r.sentiment,
            "importance": r.importance,
            "credibility": r.credibility,
            "novelty": r.novelty,
            "market_relevance": r.market_relevance,
            "affected_assets": r.affected_assets or [],
        }
        for r in rows
    ]


class SQLiteRecorder:
    """Convenience wrapper main.py uses for periodic snapshot writes: one
    Session, one commit per call to `flush_snapshot()`, rather than a
    session per row."""

    def __init__(self, db_url: str) -> None:
        init_db(db_url)
        self.engine = get_engine(db_url)
        self.Session = get_sessionmaker(self.engine)

    def write_asset_snapshot(
        self,
        *,
        asset: str,
        exchange: str,
        timestamp: dt.datetime,
        last_price: float | None,
        engine_metrics: dict,
        trade_window_stats: dict[str, dict] | None = None,
    ) -> None:
        """One market_data row, one orderbook_features row, and (if given)
        one trade_features row per configured window — all in a single
        transaction."""
        with self.Session() as session:
            record_market_data(
                session,
                asset=asset,
                exchange=exchange,
                timestamp=timestamp,
                last_price=last_price,
                mid_price=engine_metrics.get("mid"),
                best_bid=engine_metrics.get("best_bid"),
                best_ask=engine_metrics.get("best_ask"),
            )
            record_orderbook_features(session, asset=asset, timestamp=timestamp, metrics=engine_metrics)
            for window_label, stats in (trade_window_stats or {}).items():
                record_trade_features(
                    session, asset=asset, timestamp=timestamp, window_s=stats.get("window_s", 0), stats=stats
                )
            session.commit()

    def write_news_event(self, event: dict) -> bool:
        """One commit per call. Returns True if a new row was inserted,
        False if `event_uid` already existed (idempotent re-ingestion)."""
        with self.Session() as session:
            result = record_news_event(session, event=event)
            session.commit()
            return result is not None

    def write_position(self, closed_position: dict) -> None:
        """One commit per call — a single closed position (Phase 15's
        `live/trading_loop.py` calls this once per `POSITION_CLOSED`
        action, not batched, since closes are already infrequent
        relative to `write_asset_snapshot()`'s periodic tick)."""
        with self.Session() as session:
            record_position(session, closed_position=closed_position)
            session.commit()

    def write_performance_snapshot(self, row: dict) -> None:
        """One commit per call. `row` is
        `monitoring/performance.py: to_performance_snapshot_row()`'s
        output — pass it straight through."""
        with self.Session() as session:
            record_performance_snapshot(session, row=row)
            session.commit()
