"""Phase 10 tests: backtest/walk_forward.py — window construction
(purging, chronological-only splits), regime classification, and the
end-to-end run() loop against a small deterministic model stand-in
(not a real fitted sklearn model — that integration is covered by the
ad hoc smoke test in docs/15_backtesting.md, this file tests
walk_forward.py's own control flow in isolation).
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.backtest.event_backtester import EventBacktester
from src.backtest.walk_forward import WalkForwardValidator, classify_window_regime

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def make_rows(n, *, price_fn=None, market_regime="LOW_VOL", news_score=0.0):
    rows = []
    price = 100.0
    for i in range(n):
        price = price_fn(i, price) if price_fn else price
        rows.append(
            {
                "asset": "BTC",
                "timestamp": BASE + dt.timedelta(seconds=i),
                "price": price,
                "spread": 1.0,
                "market_regime": market_regime,
                "news_score": news_score,
            }
        )
    return rows


# -- classify_window_regime -------------------------------------------------


def test_classify_window_regime_empty_rows():
    result = classify_window_regime([])
    assert result == {"trend": None, "volatility": None, "news_shock": False, "n_rows": 0}


def test_classify_window_regime_bull_when_return_exceeds_threshold():
    rows = make_rows(10, price_fn=lambda i, p: 100.0 + i * 5)  # large steady rise
    result = classify_window_regime(rows, trend_threshold_bps=20.0)
    assert result["trend"] == "bull"


def test_classify_window_regime_bear_when_return_below_negative_threshold():
    rows = make_rows(10, price_fn=lambda i, p: 100.0 - i * 5)
    result = classify_window_regime(rows, trend_threshold_bps=20.0)
    assert result["trend"] == "bear"


def test_classify_window_regime_sideways_when_return_within_threshold():
    rows = make_rows(10, price_fn=lambda i, p: 100.0 + (0.001 if i % 2 == 0 else -0.001))
    result = classify_window_regime(rows, trend_threshold_bps=20.0)
    assert result["trend"] == "sideways"


def test_classify_window_regime_volatility_majority_vote():
    rows = make_rows(10, market_regime="HIGH_VOL")
    assert classify_window_regime(rows)["volatility"] == "high_vol"
    rows_low = make_rows(10, market_regime="LOW_VOL")
    assert classify_window_regime(rows_low)["volatility"] == "low_vol"


def test_classify_window_regime_news_shock_detected():
    rows = make_rows(5, news_score=0.1)
    rows[2]["news_score"] = 0.9  # one spike above the default 0.5 threshold
    result = classify_window_regime(rows, news_shock_min_abs_score=0.5)
    assert result["news_shock"] is True


def test_classify_window_regime_no_news_shock_below_threshold():
    rows = make_rows(5, news_score=0.1)
    result = classify_window_regime(rows, news_shock_min_abs_score=0.5)
    assert result["news_shock"] is False


# -- WalkForwardValidator.build_windows() ------------------------------------


def test_constructor_rejects_non_positive_window_sizes():
    with pytest.raises(ValueError):
        WalkForwardValidator(train_window_s=0, validation_window_s=10, test_window_s=10, step_s=10)
    with pytest.raises(ValueError):
        WalkForwardValidator(train_window_s=10, validation_window_s=10, test_window_s=10, step_s=-1)


def test_build_windows_empty_rows_returns_empty_list():
    wfv = WalkForwardValidator(train_window_s=100, validation_window_s=50, test_window_s=50, step_s=50)
    assert wfv.build_windows([], horizon_s=10) == []


def test_build_windows_too_short_data_returns_empty_list():
    rows = make_rows(10)
    wfv = WalkForwardValidator(train_window_s=1000, validation_window_s=500, test_window_s=500, step_s=500)
    assert wfv.build_windows(rows, horizon_s=10) == []


def test_build_windows_splits_are_chronological_and_non_overlapping():
    rows = make_rows(1000)
    wfv = WalkForwardValidator(train_window_s=300, validation_window_s=100, test_window_s=100, step_s=200)
    windows = wfv.build_windows(rows, horizon_s=10)
    assert len(windows) > 1
    for w in windows:
        if w["train"] and w["validation"]:
            assert w["train"][-1]["timestamp"] < w["validation"][0]["timestamp"]
        if w["validation"] and w["test"]:
            assert w["validation"][-1]["timestamp"] < w["test"][0]["timestamp"]


def test_build_windows_purges_a_full_horizon_from_trailing_edge_of_train_and_validation():
    rows = make_rows(1000)
    horizon_s = 30
    wfv = WalkForwardValidator(train_window_s=300, validation_window_s=100, test_window_s=100, step_s=500)
    windows = wfv.build_windows(rows, horizon_s=horizon_s)
    w = windows[0]
    assert w["train"][-1]["timestamp"] <= w["train_end"] - dt.timedelta(seconds=horizon_s)
    assert w["validation"][-1]["timestamp"] <= w["validation_end"] - dt.timedelta(seconds=horizon_s)


def test_build_windows_advances_by_step_s_not_by_full_window():
    rows = make_rows(2000)
    wfv = WalkForwardValidator(train_window_s=300, validation_window_s=100, test_window_s=100, step_s=100)
    windows = wfv.build_windows(rows, horizon_s=10)
    assert len(windows) >= 2
    assert windows[1]["train_start"] - windows[0]["train_start"] == dt.timedelta(seconds=100)


# -- WalkForwardValidator.run() ----------------------------------------------


def test_run_empty_rows_returns_zero_windows():
    wfv = WalkForwardValidator(train_window_s=100, validation_window_s=50, test_window_s=50, step_s=50)
    result = wfv.run(
        [], fit_fn=lambda rows: "model", decide_fn=lambda m, rows: [], backtester=EventBacktester(), horizon_s=10
    )
    assert result == {"windows": [], "summary": {"n_windows": 0}}


def test_run_skips_fitting_when_train_slice_is_empty_or_fit_fn_returns_none():
    rows = make_rows(500)
    wfv = WalkForwardValidator(train_window_s=100, validation_window_s=50, test_window_s=50, step_s=100)
    calls = []

    def fit_fn(train_rows):
        calls.append(len(train_rows))
        return None  # simulate "couldn't fit" (e.g. no resolvable targets)

    result = wfv.run(
        rows, fit_fn=fit_fn, decide_fn=lambda m, rows: [], backtester=EventBacktester(), horizon_s=10
    )
    assert result["summary"]["n_windows"] > 0
    assert all(w["validation_metrics"]["n_trades"] == 0 for w in result["windows"])
    assert all(w["test_metrics"]["n_trades"] == 0 for w in result["windows"])
    # fit_fn called exactly once per window (never once per slice) — the
    # same fitted model is reused for both validation and test evaluation.
    assert len(calls) == result["summary"]["n_windows"]


def test_run_produces_trades_with_an_always_long_decide_fn_in_an_uptrend():
    rows = make_rows(600, price_fn=lambda i, p: 100.0 + i * 0.05)
    wfv = WalkForwardValidator(train_window_s=100, validation_window_s=50, test_window_s=50, step_s=100)

    def fit_fn(train_rows):
        return "dummy_model"  # decide_fn below ignores it and always goes long

    def decide_fn(model, eval_rows):
        return [
            {"direction": 1, "probability": 0.99, "expected_value_bps": 200.0}
            for _ in eval_rows
        ]

    bt = EventBacktester()
    result = wfv.run(rows, fit_fn=fit_fn, decide_fn=decide_fn, backtester=bt, horizon_s=10)
    assert result["summary"]["n_windows_with_trades"] > 0
    assert result["summary"]["total_test_net_pnl"] > 0  # steady uptrend, always long, should be net profitable
    for w in result["windows"]:
        assert w["regime"]["trend"] in {"bull", "bear", "sideways", None}


def test_summary_groups_by_trend_label():
    rows = make_rows(600, price_fn=lambda i, p: 100.0 + i * 0.05)
    wfv = WalkForwardValidator(train_window_s=100, validation_window_s=50, test_window_s=50, step_s=100)

    def fit_fn(train_rows):
        return "dummy"

    def decide_fn(model, eval_rows):
        return [{"direction": 1, "probability": 0.99, "expected_value_bps": 200.0} for _ in eval_rows]

    result = wfv.run(rows, fit_fn=fit_fn, decide_fn=decide_fn, backtester=EventBacktester(), horizon_s=10)
    by_trend = result["summary"]["test_net_pnl_by_trend"]
    assert "bull" in by_trend  # steady uptrend should classify every window as bull
    assert sum(v["n_windows"] for v in by_trend.values()) == result["summary"]["n_windows"]
