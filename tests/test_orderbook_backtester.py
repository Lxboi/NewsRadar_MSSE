"""Phase 10 tests: backtest/orderbook_backtester.py — the news-column
stripping ablation and its wrapper around EventBacktester.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.backtest.event_backtester import decisions_from_model
from src.backtest.orderbook_backtester import OrderBookBacktester, strip_news_columns

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def row_with_news(i, price):
    return {
        "asset": "BTC",
        "timestamp": BASE + dt.timedelta(seconds=i),
        "price": price,
        "spread": 2.0,
        "market_regime": "NORMAL_VOL",
        "news_score": 0.8,
        "news_importance": 0.9,
        "news_novelty": 1.0,
        "news_age": 12.0,
        "news_credibility": 0.7,
        "obi_10": 0.3,
    }


def uptrend_rows_with_news(n=100, step=0.1):
    price = 100.0
    rows = []
    for i in range(n):
        price += step
        rows.append(row_with_news(i, price))
    return rows


class ConfidentUpModel:
    def __init__(self, p_up=0.95):
        self.p_up = p_up

    def predict_proba(self, rows):
        return [
            {"probability_up": self.p_up, "probability_down": (1 - self.p_up) / 2, "probability_neutral": (1 - self.p_up) / 2}
            for _ in rows
        ]


def test_strip_news_columns_removes_every_news_prefixed_key():
    rows = uptrend_rows_with_news(5)
    stripped = strip_news_columns(rows)
    for row in stripped:
        assert not any(k.startswith("news_") for k in row)
        with pytest.raises(KeyError):
            _ = row["news_score"]


def test_strip_news_columns_preserves_non_news_keys():
    rows = uptrend_rows_with_news(3)
    stripped = strip_news_columns(rows)
    for original, cleaned in zip(rows, stripped):
        for key in original:
            if not key.startswith("news_"):
                assert cleaned[key] == original[key]


def test_strip_news_columns_does_not_mutate_input():
    rows = uptrend_rows_with_news(3)
    strip_news_columns(rows)
    assert "news_score" in rows[0]


def test_strip_news_columns_empty_list():
    assert strip_news_columns([]) == []


def test_orderbook_backtester_run_produces_trades_from_news_free_rows():
    rows = uptrend_rows_with_news(200, step=0.05)
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=50)
    obt = OrderBookBacktester()
    trades = obt.run(rows, decisions, horizon_s=30)
    assert len(trades) > 0
    assert all(t["gross_pnl"] > 0 for t in trades)  # same uptrend sanity as EventBacktester's own test


def test_orderbook_backtester_same_cost_semantics_as_event_backtester():
    rows = uptrend_rows_with_news(60, step=0.5)
    decisions = decisions_from_model(ConfidentUpModel(0.99), rows, threshold_bps=100)
    obt = OrderBookBacktester(taker_fee_bps=5, assumed_slippage_bps=3)
    trades = obt.run(rows, decisions, horizon_s=20)
    assert trades
    for t in trades:
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"] - t["slippage"])
