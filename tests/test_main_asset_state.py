"""Phase 2 tests for the live display's AssetState math (mid/spread) and the
market_state_consumer wiring — no network. See docs/09_tests_plan.md."""
import asyncio
import time

import pytest

from src.main import AssetState, market_state_consumer


def test_mid_and_spread_bps():
    s = AssetState(symbol="BTC", best_bid=100.0, best_ask=101.0)
    assert s.mid == 100.5
    assert s.spread_bps == pytest.approx((1.0 / 100.5) * 10_000)


def test_mid_is_none_without_both_sides():
    s = AssetState(symbol="BTC", best_bid=100.0)
    assert s.mid is None
    assert s.spread_bps is None


def test_age_s_is_infinite_before_first_update():
    s = AssetState(symbol="BTC")
    assert s.age_s() == float("inf")


def test_age_s_after_update():
    s = AssetState(symbol="BTC", last_update_monotonic=time.monotonic())
    assert 0 <= s.age_s() < 1.0


@pytest.mark.asyncio
async def test_market_state_consumer_updates_l2_and_trades():
    queue: asyncio.Queue = asyncio.Queue()
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {}

    await queue.put(
        {
            "source": "hyperliquid",
            "ts_received": 1,
            "payload": {"type": "l2Book", "coin": "BTC", "best_bid": 100.0, "best_ask": 101.0},
        }
    )
    await queue.put(
        {
            "source": "hyperliquid",
            "ts_received": 2,
            "payload": {
                "type": "trades",
                "coin": "BTC",
                "trades": [{"price": 100.5, "side": "B"}],
            },
        }
    )

    task = asyncio.create_task(market_state_consumer("test", queue, state, engines))
    # Let the consumer drain both queued items.
    for _ in range(10):
        if state["BTC"].trade_updates:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert state["BTC"].best_bid == 100.0
    assert state["BTC"].best_ask == 101.0
    assert state["BTC"].last_trade_price == 100.5
    assert state["BTC"].l2_updates == 1
    assert state["BTC"].trade_updates == 1


@pytest.mark.asyncio
async def test_market_state_consumer_ignores_unconfigured_asset():
    queue: asyncio.Queue = asyncio.Queue()
    state = {"BTC": AssetState(symbol="BTC")}
    engines = {}

    await queue.put(
        {
            "source": "hyperliquid",
            "ts_received": 1,
            "payload": {"type": "l2Book", "coin": "DOGE", "best_bid": 1.0, "best_ask": 1.01},
        }
    )

    task = asyncio.create_task(market_state_consumer("test", queue, state, engines))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert state["BTC"].l2_updates == 0  # untouched — DOGE isn't in state
