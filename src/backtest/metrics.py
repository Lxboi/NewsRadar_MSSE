"""Gross/net PnL, fees, slippage, win rate, avg win/loss, expectancy,
profit factor, Sharpe, Sortino, max drawdown, trade count, holding
time, bootstrap CIs, Monte Carlo trade reshuffling (sections 44, 46).

Roadmap: PHASE 10. Pure aggregation over an already-simulated trade list
— every trade here is assumed to already reflect a real, no-look-ahead-
safe fill (that discipline belongs to `event_backtester.py`, which
produces the trade dicts this module consumes). Nothing in this module
runs a strategy or looks at market data; it only computes statistics
over outcomes it's handed.

## Expected trade shape

Every function here takes `trades: list[dict]` shaped like
`storage/database.py: Position` — the same schema a live/paper trading
run would eventually persist, so backtest and live performance
reporting (`monitoring/performance.py`, Phase 13) can share this module
without a translation layer. Required keys: `net_pnl`, `gross_pnl`,
`fees`, `slippage` (all `float`), `opened_at`, `closed_at` (both
`datetime`, tz-aware UTC per section 29's clock discipline). Optional:
`asset`, `side`, `entry_price`, `exit_price`, `quantity`, `exit_reason`
— not used by any calculation here, only useful for later breakdown/
reporting, so their absence never raises.

## Section 46's four extra tools, and what each one actually answers

- `compute_metrics()` — the core numbers section 44 lists (PnL, fees,
  win rate, Sharpe, drawdown, etc.), computed once over the trades as
  they actually occurred, in their actual order.
- `bootstrap_confidence_intervals()` — resamples trades *with*
  replacement to ask "if this backtest were re-run against an
  equally-plausible resample of the same trade population, what range
  of total PnL/Sharpe would come out?" Answers whether the headline
  number is a stable estimate or a small-sample fluke.
- `monte_carlo_reshuffle()` — reorders (not resamples) the *same* set of
  trades to ask "how bad could the drawdown path have been under a
  different, equally likely sequencing of these exact outcomes?" Total
  net PnL is mathematically invariant under reordering (a sum doesn't
  care about order) — reshuffling only stress-tests *path-dependent*
  statistics (drawdown, ruin risk), which is deliberately what this
  function reports on, not PnL.
- `probability_of_ruin()` — bootstrap-resamples entire equity *paths*
  (not just a single aggregate number) to estimate how often a path
  built from this trade population would have breached a ruin threshold
  before finishing.

None of these four claims a strategy is safe to trade live — they're
diagnostics for whether a backtest's headline numbers are the kind of
thing that survives scrutiny, feeding into the same "beats baselines,
survives costs, survives walk-forward, survives stress-testing" chain
`docs/08_backtesting_strategy.md` lays out. PnL/fees/slippage are in
whatever currency unit the trade dicts themselves use (this module
never converts).
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

DEFAULT_INITIAL_EQUITY = 10_000.0
DEFAULT_BOOTSTRAP_RESAMPLES = 1000
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_MONTE_CARLO_SHUFFLES = 1000
DEFAULT_RUIN_THRESHOLD_PCT = 50.0
DEFAULT_RUIN_PATHS = 1000
TRADING_DAYS_PER_YEAR = 365.25  # crypto trades 24/7 — no 252-day equity-market convention here


def _net_pnls(trades: "list[dict]") -> "list[float]":
    return [float(t["net_pnl"]) for t in trades]


def _empty_metrics(initial_equity: float) -> dict:
    return {
        "n_trades": 0,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "total_fees": 0.0,
        "total_slippage": 0.0,
        "win_rate": None,
        "avg_win": None,
        "avg_loss": None,
        "expectancy": None,
        "profit_factor": None,
        "sharpe": None,
        "sharpe_annualized": None,
        "sortino": None,
        "sortino_annualized": None,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "avg_holding_time_s": None,
        "equity_curve": [initial_equity],
    }


def _equity_curve(net_pnls: "list[float]", initial_equity: float) -> "list[float]":
    curve = [initial_equity]
    running = initial_equity
    for pnl in net_pnls:
        running += pnl
        curve.append(running)
    return curve


def _max_drawdown(equity_curve: "list[float]") -> "tuple[float, float]":
    """Returns `(max_drawdown_abs, max_drawdown_pct)`. `max_drawdown_pct`
    is relative to the running peak *at the time of the trough* (the
    standard "how much would you have lost from the local high" framing),
    not relative to `initial_equity` — a drawdown late in a profitable
    run should be measured against the equity actually built up by then,
    not the starting stake."""
    peak = equity_curve[0]
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    for equity in equity_curve[1:]:
        peak = max(peak, equity)
        dd_abs = peak - equity
        if dd_abs > max_dd_abs:
            max_dd_abs = dd_abs
            max_dd_pct = (dd_abs / peak * 100.0) if peak > 0 else 0.0
    return max_dd_abs, max_dd_pct


def _sharpe(returns: "list[float]") -> "float | None":
    if len(returns) < 2:
        return None
    arr = np.asarray(returns, dtype=float)
    std = arr.std(ddof=1)
    if std == 0:
        return None
    return float(arr.mean() / std)


def _sortino(returns: "list[float]") -> "float | None":
    if len(returns) < 2:
        return None
    arr = np.asarray(returns, dtype=float)
    downside = arr[arr < 0]
    if len(downside) < 2:
        # Fewer than 2 losing trades: downside deviation isn't a
        # meaningful sample statistic (a single point has no variance to
        # speak of) — None, not a divide-by-zero or a fabricated 0.
        return None
    downside_std = downside.std(ddof=1)
    if downside_std == 0:
        return None
    return float(arr.mean() / downside_std)


def _trades_per_year(trades: "list[dict]") -> "float | None":
    opened = [t["opened_at"] for t in trades if t.get("opened_at") is not None]
    closed = [t["closed_at"] for t in trades if t.get("closed_at") is not None]
    if not opened or not closed:
        return None
    span_s = (max(closed) - min(opened)).total_seconds()
    if span_s <= 0:
        return None
    span_days = span_s / 86_400.0
    return len(trades) / span_days * TRADING_DAYS_PER_YEAR


def compute_metrics(trades: "list[dict]", *, initial_equity: float = DEFAULT_INITIAL_EQUITY) -> dict:
    """Core section-44 metrics over `trades` in the order given (should
    already be chronological — `event_backtester.py` produces them that
    way). Empty `trades` returns a fully-populated dict of zeros/`None`s
    rather than raising, so a caller sweeping many parameter combinations
    doesn't need a special case for "this combination never traded".

    `sharpe`/`sortino` are **per-trade** (unannualized) by default — mean
    net PnL divided by its own standard deviation, with no calendar
    assumption baked in. `sharpe_annualized`/`sortino_annualized` scale
    those by `sqrt(trades_per_year)`, where `trades_per_year` is measured
    empirically from the actual span between the first trade's open and
    the last trade's close in this exact backtest — not assumed from a
    252-trading-day equity-market convention, since this system's
    holding times are 10s-300s (section 39) and crypto trades 24/7, not
    on an exchange calendar. Both are `None` when the trade count or the
    variance needed to compute them is too small to be meaningful (never
    silently 0, matching every other "insufficient data" convention in
    this codebase — see e.g. `models/calibration.py`'s
    `roc_auc_macro`)."""
    if not trades:
        return _empty_metrics(initial_equity)

    net_pnls = _net_pnls(trades)
    gross_pnl = sum(float(t["gross_pnl"]) for t in trades)
    net_pnl = sum(net_pnls)
    total_fees = sum(float(t["fees"]) for t in trades)
    total_slippage = sum(float(t["slippage"]) for t in trades)

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    decisive = len(wins) + len(losses)  # excludes exact-breakeven trades from the win-rate denominator
    win_rate = (len(wins) / decisive) if decisive else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    expectancy = net_pnl / len(trades)
    gross_loss = -sum(losses)  # positive number
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else None

    equity_curve = _equity_curve(net_pnls, initial_equity)
    max_dd_abs, max_dd_pct = _max_drawdown(equity_curve)

    holding_times = [
        (t["closed_at"] - t["opened_at"]).total_seconds()
        for t in trades
        if t.get("opened_at") is not None and t.get("closed_at") is not None
    ]
    avg_holding_time_s = (sum(holding_times) / len(holding_times)) if holding_times else None

    sharpe = _sharpe(net_pnls)
    sortino = _sortino(net_pnls)
    trades_per_year = _trades_per_year(trades)
    sharpe_annualized = (sharpe * math.sqrt(trades_per_year)) if sharpe is not None and trades_per_year else None
    sortino_annualized = (sortino * math.sqrt(trades_per_year)) if sortino is not None and trades_per_year else None

    return {
        "n_trades": len(trades),
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "total_fees": total_fees,
        "total_slippage": total_slippage,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sharpe_annualized": sharpe_annualized,
        "sortino": sortino,
        "sortino_annualized": sortino_annualized,
        "max_drawdown": max_dd_abs,
        "max_drawdown_pct": max_dd_pct,
        "avg_holding_time_s": avg_holding_time_s,
        "equity_curve": equity_curve,
    }


def bootstrap_confidence_intervals(
    trades: "list[dict]",
    *,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    random_state: "int | np.random.RandomState | None" = None,
) -> dict:
    """Section 46's bootstrap confidence interval: resample `trades`
    *with replacement* (same size as the original) `n_resamples` times,
    recompute total net PnL and per-trade Sharpe on each resample, and
    report the `confidence_level` percentile interval — e.g. the 2.5th
    and 97.5th percentiles for the default 95% level. A wide interval
    (or one that includes/straddles zero net PnL) means the headline
    backtest number could easily have come out very differently on a
    slightly different sample of the same underlying trade-generating
    process — exactly the "don't trust one favorable backtest" concern
    section 4 raises. Raises `ValueError` on an empty `trades` list,
    since there is no population to resample from."""
    if not trades:
        raise ValueError("bootstrap_confidence_intervals() requires at least one trade")
    rng = random_state if isinstance(random_state, np.random.RandomState) else np.random.RandomState(random_state)
    net_pnls = np.asarray(_net_pnls(trades), dtype=float)
    n = len(net_pnls)

    resampled_totals = np.empty(n_resamples)
    resampled_sharpes = []
    for i in range(n_resamples):
        sample = rng.choice(net_pnls, size=n, replace=True)
        resampled_totals[i] = sample.sum()
        sharpe = _sharpe(sample.tolist())
        if sharpe is not None:
            resampled_sharpes.append(sharpe)

    lower_pct = (1.0 - confidence_level) / 2.0 * 100.0
    upper_pct = (1.0 - (1.0 - confidence_level) / 2.0) * 100.0

    net_pnl_ci = (
        float(np.percentile(resampled_totals, lower_pct)),
        float(np.percentile(resampled_totals, upper_pct)),
    )
    sharpe_ci = None
    if len(resampled_sharpes) >= 2:
        sharpe_arr = np.asarray(resampled_sharpes)
        sharpe_ci = (
            float(np.percentile(sharpe_arr, lower_pct)),
            float(np.percentile(sharpe_arr, upper_pct)),
        )

    return {
        "n_resamples": n_resamples,
        "confidence_level": confidence_level,
        "net_pnl_mean": float(resampled_totals.mean()),
        "net_pnl_ci": net_pnl_ci,
        "sharpe_ci": sharpe_ci,
    }


def monte_carlo_reshuffle(
    trades: "list[dict]",
    *,
    n_shuffles: int = DEFAULT_MONTE_CARLO_SHUFFLES,
    initial_equity: float = DEFAULT_INITIAL_EQUITY,
    random_state: "int | np.random.RandomState | None" = None,
) -> dict:
    """Section 46's Monte Carlo trade reshuffling: reorders (never
    resamples — every shuffle uses the exact same set of trade outcomes,
    just in a different sequence) `n_shuffles` times, and recomputes max
    drawdown for each ordering. Total net PnL is **not** reported here —
    it's a sum, so it's mathematically identical for every permutation;
    reshuffling has nothing to say about it. What reshuffling *does*
    stress-test is path-dependent risk: could a different-but-equally-
    likely sequencing of these same outcomes have produced a much worse
    drawdown (e.g. all the losing trades landing consecutively instead of
    spread out)? `percentile_of_actual` is where the trade list's actual,
    as-realized ordering's max drawdown falls within the shuffled
    distribution (0 = the actual sequence was the least-bad ordering seen,
    100 = the actual sequence was the worst-case-so-far ordering) — a low
    percentile is mild evidence the real backtest got a favorable
    ordering, not that the strategy is safe. Raises `ValueError` on an
    empty `trades` list."""
    if not trades:
        raise ValueError("monte_carlo_reshuffle() requires at least one trade")
    rng = random_state if isinstance(random_state, np.random.RandomState) else np.random.RandomState(random_state)
    net_pnls = _net_pnls(trades)

    actual_dd_abs, actual_dd_pct = _max_drawdown(_equity_curve(net_pnls, initial_equity))

    shuffled_dd_abs = np.empty(n_shuffles)
    for i in range(n_shuffles):
        shuffled = net_pnls.copy()
        rng.shuffle(shuffled)
        dd_abs, _ = _max_drawdown(_equity_curve(shuffled, initial_equity))
        shuffled_dd_abs[i] = dd_abs

    percentile_of_actual = float((shuffled_dd_abs <= actual_dd_abs).mean() * 100.0)

    return {
        "n_shuffles": n_shuffles,
        "actual_max_drawdown": actual_dd_abs,
        "actual_max_drawdown_pct": actual_dd_pct,
        "shuffled_max_drawdown_mean": float(shuffled_dd_abs.mean()),
        "shuffled_max_drawdown_std": float(shuffled_dd_abs.std(ddof=1)) if n_shuffles > 1 else 0.0,
        "percentile_of_actual": percentile_of_actual,
        "note": (
            "Total net PnL is order-invariant (a sum) and is not reported here; "
            "only drawdown, which is path-dependent, is meaningfully affected by reshuffling."
        ),
    }


def probability_of_ruin(
    trades: "list[dict]",
    *,
    initial_equity: float = DEFAULT_INITIAL_EQUITY,
    ruin_threshold_pct: float = DEFAULT_RUIN_THRESHOLD_PCT,
    n_paths: int = DEFAULT_RUIN_PATHS,
    random_state: "int | np.random.RandomState | None" = None,
) -> dict:
    """Section 46's probability-of-ruin estimate: bootstrap-resamples
    (with replacement, same length as `trades`) `n_paths` full equity
    *paths* — not just a single aggregate statistic — from this trade
    population, and reports the fraction of paths whose equity ever drops
    below `initial_equity * (1 - ruin_threshold_pct / 100)` at any point
    along the path (not just at the end — a path that recovers after
    breaching the threshold still counts as having hit ruin, since a real
    account can't un-breach a hard stop). This is deliberately a
    resampling-with-replacement simulation, not the reshuffling
    `monte_carlo_reshuffle()` does — ruin risk depends on both the
    ordering *and* how variable the trade-generating process itself might
    plausibly have been, which resampling captures and pure reshuffling
    doesn't. Raises `ValueError` on an empty `trades` list."""
    if not trades:
        raise ValueError("probability_of_ruin() requires at least one trade")
    rng = random_state if isinstance(random_state, np.random.RandomState) else np.random.RandomState(random_state)
    net_pnls = np.asarray(_net_pnls(trades), dtype=float)
    n = len(net_pnls)
    ruin_level = initial_equity * (1.0 - ruin_threshold_pct / 100.0)

    ruined = 0
    for _ in range(n_paths):
        path = rng.choice(net_pnls, size=n, replace=True)
        equity = initial_equity + np.cumsum(path)
        if np.any(equity <= ruin_level):
            ruined += 1

    return {
        "probability_of_ruin": ruined / n_paths,
        "ruin_threshold_pct": ruin_threshold_pct,
        "ruin_equity_level": ruin_level,
        "initial_equity": initial_equity,
        "n_paths": n_paths,
    }
