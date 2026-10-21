"""Live/paper performance tracking feeding the hourly/daily Telegram
reports (sections 53-54) and the auto-analysis job (section 70).

Roadmap: PHASE 13. Deliberately reuses `backtest/metrics.py:
compute_metrics()` (Phase 10) rather than a second implementation of
win-rate/PnL/drawdown math — `PerformanceTracker`'s whole job is
answering the same questions Phase 10's backtest analysis already
answers, just windowed over live/paper trades instead of a backtest run.
`record_trade()` reads the exact same closed-position dict shape
`StrategyEngine._close_position()` (Phase 11)/`EventBacktester`'s trade
dicts already produce — the same "one dict shape flows through the
whole system" convention every phase since Phase 10 has followed.

`hourly_snapshot()`/`daily_snapshot()`/`all_time_snapshot()` each return
a dict shaped to match `storage/database.py: PerformanceSnapshot`'s exact
column set (`period`, `trades`, `win_rate`, `gross_pnl`, `fees`,
`slippage`, `net_pnl`, `max_drawdown`, `best_asset`, `best_setup`,
`worst_setup`) — `to_performance_snapshot_row()` is a pure, DB-agnostic
shaping helper (this module never imports SQLAlchemy directly, same
"keep it DB-agnostic" convention `features/feature_engine.py` (Phase 7)
already established), so a caller wires the actual `SQLiteRecorder` call
itself. These same snapshot dicts are also the natural real
implementation behind `notifications/telegram_bot.py`'s (Phase 12)
`performance_provider`/`today_provider` injected callables — e.g.
`lambda: tracker.daily_snapshot(now=dt.datetime.now(dt.timezone.utc))`.

## "best_setup"/"worst_setup" — an interpretive choice

The master prompt doesn't define "setup." Closed-position dicts (from
`StrategyEngine`/`EventBacktester`) carry no `strategy_id`-style field —
the only categorical dimension available to attribute PnL to, beyond
`asset`, is `exit_reason` (which of `strategy/exit.py`'s 7 candidates
closed the trade). `best_setup`/`worst_setup` are computed as the
`exit_reason` with the highest/lowest aggregate `net_pnl` in the window
— an interpretive choice given the available data, not a spec-given
definition, flagged the same way every other interpretive default in
this codebase is. There is deliberately no `worst_asset` computed:
`PerformanceSnapshot`'s own schema only has a `best_asset` column, no
symmetric `worst_asset` one.
"""
from __future__ import annotations

import datetime as dt

from src.backtest.metrics import compute_metrics

DEFAULT_INITIAL_EQUITY = 10_000.0
HOURLY_WINDOW_S = 3_600
DAILY_WINDOW_S = 86_400

PERIOD_HOURLY = "hourly"
PERIOD_DAILY = "daily"
PERIOD_ALL_TIME = "all_time"


def _aggregate_net_pnl_by(trades: "list[dict]", key: str) -> dict:
    totals: "dict[str, float]" = {}
    for trade in trades:
        value = trade.get(key)
        if value is None:
            continue
        totals[value] = totals.get(value, 0.0) + (trade.get("net_pnl") or 0.0)
    return totals


def _best_by(trades: "list[dict]", key: str) -> "str | None":
    totals = _aggregate_net_pnl_by(trades, key)
    return max(totals, key=totals.get) if totals else None


def _worst_by(trades: "list[dict]", key: str) -> "str | None":
    totals = _aggregate_net_pnl_by(trades, key)
    return min(totals, key=totals.get) if totals else None


def to_performance_snapshot_row(snapshot: dict, *, timestamp: "dt.datetime | None" = None) -> dict:
    """Shapes a snapshot dict (as returned by `PerformanceTracker`'s
    `*_snapshot()` methods) into exactly
    `storage/database.py: PerformanceSnapshot`'s column set, ready to
    pass into a caller's own DB-write call. `timestamp` defaults to
    `None` (the ORM column itself defaults to "now" on insert) — pass it
    explicitly for a reproducible/backfilled write."""
    row = {
        "period": snapshot["period"],
        "trades": snapshot["trades"],
        "win_rate": snapshot["win_rate"],
        "gross_pnl": snapshot["gross_pnl"],
        "fees": snapshot["fees"],
        "slippage": snapshot["slippage"],
        "net_pnl": snapshot["net_pnl"],
        "max_drawdown": snapshot["max_drawdown"],
        "best_asset": snapshot["best_asset"],
        "best_setup": snapshot["best_setup"],
        "worst_setup": snapshot["worst_setup"],
    }
    if timestamp is not None:
        row["timestamp"] = timestamp
    return row


class PerformanceTracker:
    """One instance per trading session/account — accumulates closed
    trades indefinitely (`self._trades`); windowed snapshots are computed
    on demand from that same accumulated list, never recomputed
    incrementally, so there's exactly one source of truth for "what
    trades happened" and no risk of a rolling-window accumulator
    silently drifting from the full history."""

    def __init__(self, *, initial_equity: float = DEFAULT_INITIAL_EQUITY) -> None:
        self.initial_equity = initial_equity
        self._trades: "list[dict]" = []

    def record_trade(self, closed_position: dict) -> None:
        self._trades.append(closed_position)

    def trades_since(self, *, now: "dt.datetime", window_s: float) -> "list[dict]":
        """Trades closing within `(now - window_s, now]` — both bounds
        matter: the lower bound is the window itself, and the upper
        bound (`closed_at <= now`) keeps a trade that closes *after*
        `now` from leaking into an earlier snapshot, which a
        lower-bound-only filter would silently allow."""
        cutoff = now - dt.timedelta(seconds=window_s)
        return [
            t for t in self._trades
            if t.get("closed_at") is not None and cutoff <= t["closed_at"] <= now
        ]

    def hourly_snapshot(self, *, now: "dt.datetime") -> dict:
        return self._snapshot(PERIOD_HOURLY, self.trades_since(now=now, window_s=HOURLY_WINDOW_S))

    def daily_snapshot(self, *, now: "dt.datetime") -> dict:
        return self._snapshot(PERIOD_DAILY, self.trades_since(now=now, window_s=DAILY_WINDOW_S))

    def all_time_snapshot(self) -> dict:
        return self._snapshot(PERIOD_ALL_TIME, list(self._trades))

    def _snapshot(self, period: str, trades: "list[dict]") -> dict:
        metrics = compute_metrics(trades, initial_equity=self.initial_equity)
        return {
            "period": period,
            "trades": metrics["n_trades"],
            "win_rate": metrics["win_rate"],
            "gross_pnl": metrics["gross_pnl"],
            "fees": metrics["total_fees"],
            "slippage": metrics["total_slippage"],
            "net_pnl": metrics["net_pnl"],
            "max_drawdown": metrics["max_drawdown"],
            "best_asset": _best_by(trades, "asset"),
            "best_setup": _best_by(trades, "exit_reason"),
            "worst_setup": _worst_by(trades, "exit_reason"),
        }
