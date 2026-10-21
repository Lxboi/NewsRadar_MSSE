"""Rolling train/validation/out-of-sample windows across bull/bear/
sideways/high-vol/low-vol/news-shock regimes. No random splits
(section 45).

Roadmap: PHASE 10. Chronological, expanding-forward windows only — never
a random train/test split. A random split would let a model "train" on
rows that occur after some of its "test" rows in real time, which is
exactly the kind of look-ahead bias section 29 exists to prevent; walk-
forward is what section 45 requires specifically because random k-fold
CV is unsafe for time-ordered market data.

## Window layout

Each window is three consecutive, non-overlapping (after purging, see
below) chronological slices of the same row table:
`train -> validation -> test`. `train` fits a model; `validation` is
where a caller would normally tune anything (entry threshold, which
model, etc.) — this module doesn't do that tuning itself, it just gives
the caller a clean validation slice to do it on; `test` is the genuine
out-of-sample slice, touched only for final evaluation. The window then
advances by `step_s` and repeats, so later windows reuse most of the
data of earlier ones (an expanding-style walk-forward) rather than
throwing history away.

## Purging (section 45's "no random splits" taken seriously)

A training row near the very end of the `train` window still needs a
price up to `horizon_s` seconds in its own future to know its own label
(`features.feature_engine.compute_targets()`'s job) — if that price
happens to fall inside the nominal `validation` window, that's not
future-peeking in the sense section 29 forbids (the model never sees
`validation`'s feature columns, only reuses `validation`'s realized
prices to compute a `train`-row's *own* label, which is required for
supervised learning), but it does mean the boundary is blurry. This
module purges it away regardless: no `train` row within `purge_s`
(default: `horizon_s`) of `train`'s own end is used, and the same purge
is applied at the `validation`/`test` boundary, so every window's
`train`, `validation`, and `test` slices are strictly separated by at
least one full label horizon, not just adjacent.

## Regime labeling — a different granularity than Phase 7's `market_regime`

`classify_window_regime()`'s output is a *window-level* summary (trend
direction bull/bear/sideways + a high/low volatility majority-vote +
whether a news shock occurred anywhere in the window) — this is
deliberately not the same thing as Phase 7's `RegimeDetector`, which
classifies each individual *row* into LOW/NORMAL/HIGH/EXTREME_VOL with no
trend component at all. This module's regime label answers
`docs/08_backtesting_strategy.md` point 3's question directly ("does
this only work in one regime, across bull/bear/sideways/high-vol/low-vol/
news-shock") by aggregating Phase 7's own per-row `market_regime` column
(majority vote, collapsed to low/high) rather than reinventing a second
volatility classifier — the trend and news-shock components are new,
since Phase 7 never computed either.
"""
from __future__ import annotations

import bisect
import datetime as dt

from src.backtest.metrics import compute_metrics

DEFAULT_TREND_THRESHOLD_BPS = 20.0
DEFAULT_NEWS_SHOCK_MIN_ABS_SCORE = 0.5
LOW_VOL_REGIMES = frozenset({"LOW_VOL", "NORMAL_VOL"})
HIGH_VOL_REGIMES = frozenset({"HIGH_VOL", "EXTREME_VOL"})


def classify_window_regime(
    rows: "list[dict]",
    *,
    trend_threshold_bps: float = DEFAULT_TREND_THRESHOLD_BPS,
    news_shock_min_abs_score: float = DEFAULT_NEWS_SHOCK_MIN_ABS_SCORE,
) -> dict:
    """Window-level regime summary — see module docstring. `trend` is
    `None` if `rows` is empty or the first/last row has no `price`;
    `volatility` is `None` if no row in the window has a `market_regime`
    value to vote with; `news_shock` is `False` (not `None`) when
    unresolvable, since "no shock detected" is the honest read of no
    evidence, matching every other such convention in this codebase."""
    if not rows:
        return {"trend": None, "volatility": None, "news_shock": False, "n_rows": 0}

    first_price = rows[0].get("price")
    last_price = rows[-1].get("price")
    if first_price is None or last_price is None or first_price == 0:
        trend = None
    else:
        window_return_bps = (last_price - first_price) / first_price * 10_000
        if window_return_bps >= trend_threshold_bps:
            trend = "bull"
        elif window_return_bps <= -trend_threshold_bps:
            trend = "bear"
        else:
            trend = "sideways"

    regime_votes = [r["market_regime"] for r in rows if r.get("market_regime") is not None]
    if not regime_votes:
        volatility = None
    else:
        low_votes = sum(1 for r in regime_votes if r in LOW_VOL_REGIMES)
        high_votes = sum(1 for r in regime_votes if r in HIGH_VOL_REGIMES)
        volatility = "high_vol" if high_votes > low_votes else "low_vol"

    news_shock = any(
        r.get("news_score") is not None and abs(r["news_score"]) >= news_shock_min_abs_score
        for r in rows
    )

    return {"trend": trend, "volatility": volatility, "news_shock": news_shock, "n_rows": len(rows)}


class WalkForwardValidator:
    """See module docstring. Stateless across `run()` calls — one
    instance can be reused for multiple row tables/models."""

    def __init__(
        self,
        *,
        train_window_s: float,
        validation_window_s: float,
        test_window_s: float,
        step_s: float,
        purge_s: "float | None" = None,
    ) -> None:
        if train_window_s <= 0 or validation_window_s <= 0 or test_window_s <= 0 or step_s <= 0:
            raise ValueError("all window/step sizes must be positive")
        self.train_window_s = train_window_s
        self.validation_window_s = validation_window_s
        self.test_window_s = test_window_s
        self.step_s = step_s
        self.purge_s = purge_s

    def build_windows(self, rows: "list[dict]", *, horizon_s: int) -> "list[dict]":
        """Every window as `{"train", "validation", "test"}` row-list
        slices, plus the window's `train_start`/`train_end`/
        `validation_end`/`test_end` boundaries. `rows` must be sorted
        ascending by `timestamp` (as every Phase 7 row table already is).
        Returns `[]` if `rows` doesn't span enough time for even one full
        `train + validation + test` window."""
        if not rows:
            return []
        purge_s = self.purge_s if self.purge_s is not None else horizon_s
        timestamps = [r["timestamp"] for r in rows]
        data_end = timestamps[-1]

        windows = []
        train_start = timestamps[0]
        while True:
            train_end = train_start + dt.timedelta(seconds=self.train_window_s)
            validation_end = train_end + dt.timedelta(seconds=self.validation_window_s)
            test_end = validation_end + dt.timedelta(seconds=self.test_window_s)
            if test_end > data_end:
                break

            purge_delta = dt.timedelta(seconds=purge_s)
            train_lo = bisect.bisect_left(timestamps, train_start)
            train_hi = bisect.bisect_left(timestamps, train_end - purge_delta)
            val_lo = bisect.bisect_left(timestamps, train_end)
            val_hi = bisect.bisect_left(timestamps, validation_end - purge_delta)
            test_lo = bisect.bisect_left(timestamps, validation_end)
            test_hi = bisect.bisect_left(timestamps, test_end)

            windows.append(
                {
                    "train_start": train_start,
                    "train_end": train_end,
                    "validation_end": validation_end,
                    "test_end": test_end,
                    "train": rows[train_lo:train_hi],
                    "validation": rows[val_lo:val_hi],
                    "test": rows[test_lo:test_hi],
                }
            )
            train_start = train_start + dt.timedelta(seconds=self.step_s)

        return windows

    def run(
        self,
        rows: "list[dict]",
        *,
        fit_fn,
        decide_fn,
        backtester,
        horizon_s: int,
        trend_threshold_bps: float = DEFAULT_TREND_THRESHOLD_BPS,
        news_shock_min_abs_score: float = DEFAULT_NEWS_SHOCK_MIN_ABS_SCORE,
    ) -> dict:
        """Runs every window through `fit_fn(train_rows) -> model`,
        `decide_fn(model, eval_rows) -> decisions`, then
        `backtester.run(eval_rows, decisions, horizon_s=horizon_s)` for
        both the `validation` and `test` slices, and
        `metrics.compute_metrics()` over each resulting trade list.
        `fit_fn`/`decide_fn` are caller-supplied so this module stays
        agnostic to whether the model is a Phase 9
        `LogisticDirectionModel`/`TreeDirectionModel` or a Phase 8
        baseline wrapped to expose the same shape (see
        `event_backtester.decisions_from_model()`/`decisions_from_baseline()`).
        A window whose `train` slice is empty skips fitting for that
        window (empty-trade metrics for both `validation`/`test`, not a
        crash) — this can legitimately happen at the very start of a
        sparse historical period.

        Returns `{"windows": [...], "summary": {...}}`. Each window
        result includes its regime label (`classify_window_regime()`,
        computed over the `test` slice — the genuinely out-of-sample
        data — falling back to `validation` if `test` is empty) so the
        caller can see whether performance holds across regimes or is
        concentrated in one, per `docs/08_backtesting_strategy.md` point
        3. `summary` aggregates `test`-slice net PnL/Sharpe/win-rate
        across all windows, plus a per-trend-label breakdown, without
        claiming any single number is "the" result — a strategy is not
        validated by one aggregate figure any more than by one backtest
        (section 4)."""
        windows = self.build_windows(rows, horizon_s=horizon_s)
        if not windows:
            return {"windows": [], "summary": {"n_windows": 0}}

        results = []
        for w in windows:
            # Fit once per window, not once per slice: validation and test
            # are two evaluations of the *same* trained model, and a real
            # model fit (Phase 9's LogisticDirectionModel/TreeDirectionModel)
            # is expensive enough that fitting it twice per window for no
            # reason would double this function's cost for nothing.
            model = fit_fn(w["train"]) if w["train"] else None
            val_trades = self._run_slice(model, w["validation"], decide_fn, backtester, horizon_s)
            test_trades = self._run_slice(model, w["test"], decide_fn, backtester, horizon_s)

            regime_source = w["test"] if w["test"] else w["validation"]
            regime = classify_window_regime(
                regime_source,
                trend_threshold_bps=trend_threshold_bps,
                news_shock_min_abs_score=news_shock_min_abs_score,
            )

            results.append(
                {
                    "train_start": w["train_start"],
                    "train_end": w["train_end"],
                    "validation_end": w["validation_end"],
                    "test_end": w["test_end"],
                    "n_train_rows": len(w["train"]),
                    "n_validation_rows": len(w["validation"]),
                    "n_test_rows": len(w["test"]),
                    "regime": regime,
                    "validation_metrics": compute_metrics(val_trades),
                    "test_metrics": compute_metrics(test_trades),
                }
            )

        return {"windows": results, "summary": _summarize(results)}

    @staticmethod
    def _run_slice(model, eval_rows, decide_fn, backtester, horizon_s) -> "list[dict]":
        if model is None or not eval_rows:
            return []
        decisions = decide_fn(model, eval_rows)
        return backtester.run(eval_rows, decisions, horizon_s=horizon_s)


def _summarize(results: "list[dict]") -> dict:
    n_windows = len(results)
    test_net_pnls = [r["test_metrics"]["net_pnl"] for r in results]
    n_windows_with_trades = sum(1 for r in results if r["test_metrics"]["n_trades"] > 0)

    by_trend: "dict[str, list[float]]" = {}
    for r in results:
        trend = r["regime"]["trend"] or "unknown"
        by_trend.setdefault(trend, []).append(r["test_metrics"]["net_pnl"])

    return {
        "n_windows": n_windows,
        "n_windows_with_trades": n_windows_with_trades,
        "total_test_net_pnl": sum(test_net_pnls),
        "mean_test_net_pnl_per_window": (sum(test_net_pnls) / n_windows) if n_windows else None,
        "test_net_pnl_by_trend": {
            trend: {"n_windows": len(pnls), "total_net_pnl": sum(pnls), "mean_net_pnl": sum(pnls) / len(pnls)}
            for trend, pnls in by_trend.items()
        },
    }
