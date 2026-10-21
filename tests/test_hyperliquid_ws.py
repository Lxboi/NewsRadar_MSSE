"""Phase 2 tests for the Hyperliquid collector — no network calls.

Fixture payloads below match the schema confirmed against Hyperliquid's
live docs on 2026-08-18 (see module docstring in
src/collectors/hyperliquid_ws.py for sources/details). See docs/09_tests_plan.md.
"""
import asyncio

import pytest

from src.collectors.hyperliquid_ws import (
    HyperliquidWebSocketCollector,
    ReconnectPolicy,
    parse_hyperliquid_message,
)


def test_reconnect_policy_respects_bounds():
    policy = ReconnectPolicy(base_delay_s=1, max_delay_s=8, jitter=False)
    assert policy.delay_for_attempt(0) == 1
    assert policy.delay_for_attempt(1) == 2
    assert policy.delay_for_attempt(2) == 4
    assert policy.delay_for_attempt(3) == 8   # capped at max_delay_s
    assert policy.delay_for_attempt(10) == 8  # stays capped


def test_parse_subscription_response():
    raw = {
        "channel": "subscriptionResponse",
        "data": {"method": "subscribe", "subscription": {"type": "trades", "coin": "SOL"}},
    }
    event = parse_hyperliquid_message(raw)
    assert event["type"] == "subscription_ack"


def test_parse_pong():
    assert parse_hyperliquid_message({"channel": "pong"}) == {"type": "pong"}


def test_parse_l2book_extracts_best_bid_ask():
    raw = {
        "channel": "l2Book",
        "data": {
            "coin": "BTC",
            "time": 1234567890000,
            "levels": [
                [
                    {"px": "106217.50", "sz": "1.25", "n": 3},
                    {"px": "106210.00", "sz": "2.50", "n": 5},
                ],
                [
                    {"px": "106220.00", "sz": "0.75", "n": 2},
                    {"px": "106225.50", "sz": "1.10", "n": 4},
                ],
            ],
        },
    }
    event = parse_hyperliquid_message(raw)
    assert event["type"] == "l2Book"
    assert event["coin"] == "BTC"
    assert event["best_bid"] == 106217.50
    assert event["best_ask"] == 106220.00
    assert len(event["bids"]) == 2 and len(event["asks"]) == 2
    assert event["ts_exchange_ms"] == 1234567890000


def test_parse_l2book_handles_empty_side():
    raw = {
        "channel": "l2Book",
        "data": {"coin": "ETH", "time": 1, "levels": [[], []]},
    }
    event = parse_hyperliquid_message(raw)
    assert event["best_bid"] is None
    assert event["best_ask"] is None


def test_parse_trades_batch():
    raw = {
        "channel": "trades",
        "data": [
            {
                "coin": "SOL",
                "side": "B",
                "px": "142.50",
                "sz": "10.0",
                "hash": "0xabc",
                "time": 1234567890000,
                "tid": 987654321,
                "users": ["0x1", "0x2"],
            },
            {
                "coin": "SOL",
                "side": "A",
                "px": "142.55",
                "sz": "5.0",
                "hash": "0xdef",
                "time": 1234567890500,
                "tid": 987654322,
                "users": ["0x3", "0x4"],
            },
        ],
    }
    event = parse_hyperliquid_message(raw)
    assert event["type"] == "trades"
    assert event["coin"] == "SOL"
    assert len(event["trades"]) == 2
    assert event["trades"][-1]["price"] == 142.55
    assert event["trades"][-1]["side"] == "A"


def test_parse_candle_batch():
    raw = {
        "channel": "candle",
        "data": [
            {
                "t": 1234560000000,
                "T": 1234560059999,
                "s": "BTC",
                "i": "1m",
                "o": 106000.0,
                "c": 106217.5,
                "h": 106300.0,
                "l": 105900.0,
                "v": 12.34,
                "n": 421,
            }
        ],
    }
    event = parse_hyperliquid_message(raw)
    assert event["type"] == "candle"
    assert event["coin"] == "BTC"
    assert event["candles"][0]["close"] == 106217.5


def test_parse_unknown_channel_is_surfaced_not_dropped():
    raw = {"channel": "error", "data": {"msg": "something"}}
    event = parse_hyperliquid_message(raw)
    assert event["type"] == "unknown"
    assert event["channel"] == "error"


@pytest.mark.asyncio
async def test_handle_message_enqueues_parsed_l2book_event():
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    collector = HyperliquidWebSocketCollector(
        ws_url="wss://example.invalid", assets=["BTC"], out_queue=queue
    )

    raw = (
        '{"channel":"l2Book","data":{"coin":"BTC","time":1,'
        '"levels":[[{"px":"100","sz":"1","n":1}],[{"px":"101","sz":"1","n":1}]]}}'
    )
    await collector._handle_message(raw)

    item = queue.get_nowait()
    assert item["source"] == "hyperliquid"
    assert item["payload"]["type"] == "l2Book"
    assert item["payload"]["best_bid"] == 100.0
    assert isinstance(item["ts_received"], int) and item["ts_received"] > 0


@pytest.mark.asyncio
async def test_handle_message_drops_malformed_json_without_raising():
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    collector = HyperliquidWebSocketCollector(
        ws_url="wss://example.invalid", assets=["BTC"], out_queue=queue
    )

    await collector._handle_message("not json at all")

    assert queue.empty()


def test_heartbeat_timeout_stays_above_ping_interval():
    """Regression guard: if this ever flips, the socket-level watchdog would
    force a reconnect before our own keepalive ping gets a chance to
    refresh liveness on quiet subscriptions."""
    from src.collectors.hyperliquid_ws import PING_INTERVAL_S

    default_heartbeat_timeout = HyperliquidWebSocketCollector.__dataclass_fields__[
        "heartbeat_timeout_s"
    ].default
    assert default_heartbeat_timeout > PING_INTERVAL_S
