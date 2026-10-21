"""Phase 7 tests: features/feature_engine.py — the synchronized feature
table (docs/06_features_v1.md) and its target variables (section 28).
Pure computation over hand-built synthetic tick/news sequences, no I/O,
no network. See docs/09_tests_plan.md.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.features.feature_engine import (
    DEFAULT_TARGET_THRESHOLDS_BPS,
    FeatureEngine,
    compute_targets,
    find_forward_indices,
    find_forward_prices,
)

S = 1_000_000_000  # 1 second in nanoseconds


def l2book_event(ts_ns, bids, asks, ts_ms=None):
    return {
        "kind": "l2book",
        "ts_received_ns": ts_ns,
        "payload": {
            "type": "l2Book",
            "coin": "BTC",
            "ts_exchange_ms": ts_ms if ts_ms is not None else ts_ns // 1_000_000,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "bids": bids,
            "asks": asks,
        },
    }


def trades_event(ts_ns, trades):
    """`trades`: list of (side, price, size, tid)."""
    return {
        "kind": "trades",
        "ts_received_ns": ts_ns,
        "payload": {
            "type": "trades",
            "coin": "BTC",
            "trades": [
                {"coin": "BTC", "side": side, "price": price, "size": size,
                 "ts_exchange_ms": ts_ns // 1_000_000, "tid": tid}
                for side, price, size, tid in trades
            ],
        },
    }


def news_event(uid, ingestion_time, *, affected_assets=("BTC",), market_relevance=0.6,
               importance=0.8, sentiment=0.5, novelty=1.0, credibility=0.9):
    return {
        "event_uid": uid,
        "source": "test-source",
        "title": uid,
        "event_time": ingestion_time,
        "ingestion_time": ingestion_time,
        "event_type": "REGULATION",
        "sentiment": sentiment,
        "importance": importance,
        "credibility": credibility,
        "novelty": novelty,
        "market_relevance": market_relevance,
        "affected_assets": list(affected_assets),
    }


def basic_market_events():
    """One book snapshot at t=0 (mid=100.1), a second at t=5s (mid=101.1),
    and one 2-trade buy print at t=5.5s (not a sweep — under
    SweepDetector's default min_trades=3)."""
    return [
        l2book_event(0, [(100.0, 10.0, 1)], [(100.2, 10.0, 1)]),
        l2book_event(5 * S, [(101.0, 10.0, 1)], [(101.2, 10.0, 1)]),
        trades_event(int(5.5 * S), [("B", 101.2, 2.0, 1), ("B", 101.2, 1.0, 2)]),
    ]


# -----------------------------------------------------------------------------
# Grain / basic row shape
# -----------------------------------------------------------------------------


def test_build_rows_produces_one_row_per_sampling_tick():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=10 * S)
    assert len(rows) == 11  # t = 0..10 inclusive
    assert [r["timestamp"] for r in rows] == [
        dt.datetime.fromtimestamp(i, tz=dt.timezone.utc) for i in range(11)
    ]


def test_price_reflects_most_recent_snapshot_at_or_before_each_tick():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=10 * S)
    by_t = {r["timestamp"].second: r["price"] for r in rows}
    assert by_t[0] == pytest.approx(100.1)
    assert by_t[4] == pytest.approx(100.1)  # second snapshot hasn't landed yet
    assert by_t[5] == pytest.approx(101.1)  # lands exactly at t=5s
    assert by_t[10] == pytest.approx(101.1)


def test_obi_and_microprice_delta_present_after_first_snapshot():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=2 * S)
    assert rows[0]["obi_1"] == pytest.approx(0.0)  # symmetric 10/10 book
    assert rows[0]["spread"] is not None


def test_trade_flow_columns_reflect_fed_trades():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0, trade_flow_window_s=10)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=10 * S)
    row6 = next(r for r in rows if r["timestamp"].second == 6)
    assert row6["buy_volume"] == pytest.approx(3.0)
    assert row6["sell_volume"] == pytest.approx(0.0)
    assert row6["trade_imbalance"] == pytest.approx(1.0)
    row4 = next(r for r in rows if r["timestamp"].second == 4)
    assert row4["buy_volume"] == pytest.approx(0.0)  # trade hasn't happened yet


# -----------------------------------------------------------------------------
# Returns / grain honesty
# -----------------------------------------------------------------------------


def test_returns_none_before_enough_history_exists():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0, return_horizons_s=(1, 5))
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=2 * S)
    assert rows[0]["returns_1s"] is None  # no sample 1s before t=0
    assert rows[0]["returns_5s"] is None


def test_returns_5s_matches_hand_computed_value_once_available():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0, return_horizons_s=(5,))
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=5 * S)
    row5 = rows[-1]
    assert row5["timestamp"].second == 5
    expected = (101.1 - 100.1) / 100.1
    assert row5["returns_5s"] == pytest.approx(expected)


def test_returns_horizon_shorter_than_sampling_interval_is_none_not_fabricated():
    """At 5s sampling, a '1s return' isn't an independently observed
    value — must be None, never a relabeled 5s return (see the module
    docstring's grain-honesty rationale)."""
    fe = FeatureEngine(asset="BTC", sampling_interval_s=5.0, return_horizons_s=(1, 5))
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=10 * S)
    assert all(r["returns_1s"] is None for r in rows)


# -----------------------------------------------------------------------------
# News join — ingestion_time gating, macro/no-asset-tag handling
# -----------------------------------------------------------------------------


def test_news_columns_zeroed_before_any_relevant_news():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=2 * S)
    assert rows[0]["news_score"] == 0.0
    assert rows[0]["news_importance"] == 0.0
    assert rows[0]["news_age"] is None


def test_news_columns_populate_once_ingestion_time_reached_and_not_before():
    ev = news_event("n1", dt.datetime.fromtimestamp(3, tz=dt.timezone.utc))
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [ev], start_ts_ns=0, end_ts_ns=5 * S)
    by_t = {r["timestamp"].second: r for r in rows}

    assert by_t[2]["news_score"] == 0.0  # not yet ingested
    assert by_t[2]["news_age"] is None

    assert by_t[3]["news_importance"] == pytest.approx(0.8)
    assert by_t[3]["news_novelty"] == pytest.approx(1.0)
    assert by_t[3]["news_credibility"] == pytest.approx(0.9)
    assert by_t[3]["news_age"] == pytest.approx(0.0)
    assert by_t[3]["news_score"] == pytest.approx(0.8 * 0.5)  # age 0 -> undecayed

    # Decays (strictly decreases in magnitude) as it ages, never resets.
    assert 0 < by_t[5]["news_score"] < by_t[3]["news_score"]
    assert by_t[5]["news_age"] == pytest.approx(2.0)


def test_news_event_time_is_never_used_for_gating_only_ingestion_time():
    """A news item whose self-reported event_time is far in the past but
    whose ingestion_time is late must not appear early — this is the
    section 29 property the whole join contract exists to guarantee."""
    ev = news_event("backdated", dt.datetime.fromtimestamp(4, tz=dt.timezone.utc))
    ev["event_time"] = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc) - dt.timedelta(days=1)
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [ev], start_ts_ns=0, end_ts_ns=5 * S)
    by_t = {r["timestamp"].second: r for r in rows}
    assert by_t[3]["news_score"] == 0.0  # would be populated by t=3 if event_time leaked in
    assert by_t[4]["news_importance"] == pytest.approx(0.8)


def test_news_not_tagged_to_this_asset_is_ignored():
    ev = news_event("eth-only", dt.datetime.fromtimestamp(1, tz=dt.timezone.utc), affected_assets=["ETH"])
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [ev], start_ts_ns=0, end_ts_ns=3 * S)
    assert all(r["news_score"] == 0.0 for r in rows)


def test_broad_macro_news_with_no_asset_tag_still_applies():
    """No affected_assets but genuinely relevant (e.g. a Fed/macro item) —
    documented decision: treat as market-wide rather than dropping it."""
    ev = news_event("fed-macro", dt.datetime.fromtimestamp(1, tz=dt.timezone.utc),
                     affected_assets=[], market_relevance=0.4)
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [ev], start_ts_ns=0, end_ts_ns=3 * S)
    by_t = {r["timestamp"].second: r for r in rows}
    assert by_t[1]["news_importance"] == pytest.approx(0.8)


def test_zero_relevance_no_asset_tag_news_is_ignored():
    ev = news_event("irrelevant", dt.datetime.fromtimestamp(1, tz=dt.timezone.utc),
                     affected_assets=[], market_relevance=0.0)
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [ev], start_ts_ns=0, end_ts_ns=3 * S)
    assert all(r["news_score"] == 0.0 for r in rows)


# -----------------------------------------------------------------------------
# No-look-ahead property: removing/altering future events must never
# change an earlier row's feature columns.
# -----------------------------------------------------------------------------


def test_feature_rows_unaffected_by_later_market_events():
    events_without_late_trade = basic_market_events()
    events_with_late_trade = events_without_late_trade + [
        trades_event(8 * S, [("A", 95.0, 50.0, 99)])  # big late sell, would move volume/absorption if it leaked back
    ]

    fe_a = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_a = fe_a.build_rows(events_without_late_trade, [], start_ts_ns=0, end_ts_ns=7 * S)

    fe_b = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_b = fe_b.build_rows(events_with_late_trade, [], start_ts_ns=0, end_ts_ns=7 * S)

    assert rows_a == rows_b


def test_feature_rows_unaffected_by_later_news_events():
    early_news = [news_event("n1", dt.datetime.fromtimestamp(2, tz=dt.timezone.utc))]
    late_news = early_news + [news_event("n2", dt.datetime.fromtimestamp(9, tz=dt.timezone.utc), sentiment=-0.9)]

    fe_a = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_a = fe_a.build_rows(basic_market_events(), early_news, start_ts_ns=0, end_ts_ns=8 * S)

    fe_b = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_b = fe_b.build_rows(basic_market_events(), late_news, start_ts_ns=0, end_ts_ns=8 * S)

    assert rows_a == rows_b


def test_shuffling_market_event_order_does_not_change_build_rows_output():
    """build_rows() sorts its inputs defensively — feeding the same event
    set in a different order must reproduce identical output."""
    import random

    events = basic_market_events()
    shuffled = list(events)
    random.Random(7).shuffle(shuffled)

    fe_a = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_a = fe_a.build_rows(events, [], start_ts_ns=0, end_ts_ns=7 * S)

    fe_b = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_b = fe_b.build_rows(shuffled, [], start_ts_ns=0, end_ts_ns=7 * S)

    assert rows_a == rows_b


# -----------------------------------------------------------------------------
# compute_targets — forward labels, kept structurally separate from features
# -----------------------------------------------------------------------------


def test_compute_targets_matches_hand_computed_forward_return():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=6 * S)

    result = compute_targets(rows, horizons_s=(1,), thresholds_bps=(50,))
    row4 = next(r for r in result if r["timestamp"].second == 4)

    base_price = 100.1
    fwd_price = 101.1  # row at t=5s, the first row with timestamp >= t=4s + 1s
    expected_bps = (fwd_price - base_price) / base_price * 10_000

    assert expected_bps > 50  # sanity: this example should trip the threshold
    assert row4["target_1s_up_50bps"] == 1
    assert row4["target_1s_down_50bps"] == 0


def test_default_target_thresholds_cover_above_real_kraken_round_trip_cost():
    # Regression guard, Phase 15 addendum 5: real Kraken Pro Tier 1 taker
    # fees (0.80%) put round_trip_cost_bps at ~166 (see strategy.yaml's
    # costs: block) -- build_feature_table.py's target columns must
    # include at least one threshold above that, or no EV-positive
    # threshold_bps is even representable in a rebuilt features_v1
    # table, silently reproducing the same zero-trades wall the old
    # <=50bps-only defaults caused.
    assert max(DEFAULT_TARGET_THRESHOLDS_BPS) > 166
    # And the original <=50bps values stay, for before/after comparison
    # on the same rebuilt table.
    assert {5, 10, 15, 25, 50}.issubset(set(DEFAULT_TARGET_THRESHOLDS_BPS))


def test_compute_targets_none_when_horizon_exceeds_available_data():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=3 * S)

    result = compute_targets(rows, horizons_s=(30,), thresholds_bps=(5,))
    assert all(r["target_30s_up_5bps"] is None for r in result)
    assert all(r["target_30s_down_5bps"] is None for r in result)


def test_compute_targets_never_mutates_input_rows():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=3 * S)
    original_keys = set(rows[0].keys())

    compute_targets(rows, horizons_s=(1,), thresholds_bps=(5,))

    assert set(rows[0].keys()) == original_keys  # no target_* keys leaked into the originals


def test_compute_targets_does_not_alter_feature_columns():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=6 * S)
    result = compute_targets(rows, horizons_s=(1, 2), thresholds_bps=(5, 50))
    for original, targeted in zip(rows, result):
        for key in original:
            assert targeted[key] == original[key]


def test_compute_targets_truncating_the_tail_only_changes_targets_near_the_cut_not_features():
    fe_full = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows_full = fe_full.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=8 * S)
    targets_full = compute_targets(rows_full, horizons_s=(1,), thresholds_bps=(5,))

    truncated = rows_full[:6]  # drop the last 3 rows (t=6,7,8)
    targets_truncated = compute_targets(truncated, horizons_s=(1,), thresholds_bps=(5,))

    # Every feature column for the surviving rows is untouched by truncation.
    for i in range(len(truncated)):
        for key in rows_full[i]:
            assert targets_truncated[i][key] == rows_full[i][key]
    # Row 4 (t=4s) still has its t=5s neighbor in the truncated set, so its
    # target is unaffected by the truncation.
    assert targets_truncated[4]["target_1s_up_5bps"] == targets_full[4]["target_1s_up_5bps"]


# -----------------------------------------------------------------------------
# find_forward_indices / find_forward_prices (Phase 10 extraction) — the
# same forward-lookup compute_targets() uses internally, now a public
# building block backtest/event_backtester.py also depends on.
# -----------------------------------------------------------------------------


def test_find_forward_prices_matches_compute_targets_implied_forward_price():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=6 * S)

    fwd_prices = find_forward_prices(rows, 1)
    row4 = next(i for i, r in enumerate(rows) if r["timestamp"].second == 4)
    # Same fixture test_compute_targets_matches_hand_computed_forward_return()
    # uses: row at t=4s, 1s horizon -> first row at/after t=5s, price 101.1.
    assert fwd_prices[row4] == pytest.approx(101.1)


def test_find_forward_indices_none_at_tail_when_horizon_exceeds_available_data():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=3 * S)

    indices = find_forward_indices(rows, 30)
    assert all(i is None for i in indices)
    assert find_forward_prices(rows, 30) == [None] * len(rows)


def test_find_forward_indices_is_monotonic_non_decreasing():
    # The pointer never needs to move backward as i increases — a
    # regression guard on the two-pointer invariant (each row's forward
    # index is never earlier than an earlier row's forward index).
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    rows = fe.build_rows(basic_market_events(), [], start_ts_ns=0, end_ts_ns=8 * S)

    indices = find_forward_indices(rows, 2)
    resolved = [i for i in indices if i is not None]
    assert resolved == sorted(resolved)


def test_find_forward_indices_empty_rows_returns_empty_list():
    assert find_forward_indices([], 30) == []
    assert find_forward_prices([], 30) == []


def test_compute_targets_uses_find_forward_prices_not_a_second_implementation():
    # Regression guard for the Phase 10 refactor: compute_targets() must
    # delegate to find_forward_prices() rather than carrying its own
    # copy of the two-pointer scan that could silently drift from it.
    import inspect

    import src.features.feature_engine as fe_module

    source = inspect.getsource(fe_module.compute_targets)
    assert "find_forward_prices(" in source


# -----------------------------------------------------------------------------
# Incremental API (Phase 15) — live driving of the same computation build_rows()
# uses, one event/row at a time. See the module docstring's "Incremental API"
# section: build_rows() itself is now a thin loop over these three methods.
# -----------------------------------------------------------------------------


def test_incremental_api_matches_build_rows_exactly():
    # The regression this refactor exists to guarantee: driving the same
    # synthetic event stream through on_market_event()/on_news_event()/
    # build_row() one at a time, on a fresh FeatureEngine, must produce
    # byte-identical rows to build_rows()'s own all-at-once call — a live
    # paper-trading run and an offline batch build over the same window
    # can never silently disagree if there's only one code path.
    market_events = basic_market_events()
    news_events = [news_event("u1", dt.datetime.fromtimestamp(3, tz=dt.timezone.utc))]

    batch = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    batch_rows = batch.build_rows(market_events, news_events, start_ts_ns=0, end_ts_ns=10 * S)

    incremental = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    me_ptr = 0
    news_ptr = 0
    incremental_rows = []
    for i in range(11):
        t_ns = i * S
        while me_ptr < len(market_events) and market_events[me_ptr]["ts_received_ns"] <= t_ns:
            ev = market_events[me_ptr]
            incremental.on_market_event(ev["kind"], ev["payload"], ev["ts_received_ns"])
            me_ptr += 1
        t_dt = dt.datetime.fromtimestamp(t_ns / 1_000_000_000, tz=dt.timezone.utc)
        while news_ptr < len(news_events) and news_events[news_ptr]["ingestion_time"] <= t_dt:
            incremental.on_news_event(news_events[news_ptr])
            news_ptr += 1
        incremental_rows.append(incremental.build_row(t_ns))

    assert incremental_rows == batch_rows


def test_build_row_derives_t_dt_when_not_given():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    fe.on_market_event("l2book", basic_market_events()[0]["payload"], 0)
    row = fe.build_row(3 * S)
    assert row["timestamp"] == dt.datetime.fromtimestamp(3, tz=dt.timezone.utc)


def test_on_news_event_ignores_irrelevant_news_same_as_build_rows():
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    fe.on_news_event(news_event("irrelevant", dt.datetime.fromtimestamp(0, tz=dt.timezone.utc),
                                 affected_assets=(), market_relevance=0.0))
    row = fe.build_row(1 * S)
    assert row["news_score"] == 0.0


def test_liquidity_imbalance_column_present_and_sourced_from_engine_metrics():
    # Regression for the Phase 15 finding: strategy_engine.py/exit.py
    # already read row["liquidity_imbalance"], but no feature-table
    # column ever populated it before this fix.
    fe = FeatureEngine(asset="BTC", sampling_interval_s=1.0)
    ev = basic_market_events()[0]
    fe.on_market_event(ev["kind"], ev["payload"], ev["ts_received_ns"])
    row = fe.build_row(0)
    assert row["liquidity_imbalance"] == fe.engine.metrics()["liquidity_imbalance"]
