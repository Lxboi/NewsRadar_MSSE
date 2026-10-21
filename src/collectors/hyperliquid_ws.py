"""Hyperliquid WebSocket collector — Phase 2.

Connects to Hyperliquid's public WebSocket API, subscribes to l2Book,
trades and 1m candles for each configured asset, and parses every message
into a small internal shape the rest of the system can consume. Reconnect
with exponential backoff + jitter, an idle-heartbeat watchdog, and a
periodic client ping (required by Hyperliquid — see below) all still apply
per docs/02_asyncio_architecture.md and docs/03_hyperliquid_ws_architecture.md.

Schema confirmed against Hyperliquid's live docs on 2026-08-18
(hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/*):

- Connect: wss://api.hyperliquid.xyz/ws (mainnet)
- Subscribe: {"method": "subscribe", "subscription": {"type": <chan>, "coin": <SYM>, ...}}
- Every server message is an envelope: {"channel": <str>, "data": <...>}
    - "subscriptionResponse" -> ack, data = {"method", "subscription"}
    - "l2Book"   -> data = {"coin", "time", "levels": [[bid WsLevel...], [ask WsLevel...]]}
                    WsLevel = {"px": str, "sz": str, "n": int}
    - "trades"   -> data = [ {"coin","side","px","sz","hash","time","tid","users":[buyer,seller]}, ... ]
    - "candle"   -> data = [ {"t","T","s","i","o","c","h","l","v","n"}, ... ]
    - "pong"     -> data absent; server reply to a client {"method": "ping"}
- Keepalive: server disconnects after 60s with no message; a client "ping"
  every ~20s (well under that) is sent unconditionally, since a quiet
  market can otherwise go 60s with no l2Book/trades update.

This module still doesn't reconstruct a maintained order book (that's
orderbook/orderbook_engine.py, Phase 3) — it only parses each inbound
snapshot/message into a convenient shape and forwards it. Hyperliquid's
l2Book messages are full snapshots per update here (not incremental
deltas), which is why Phase 3 can build a book "from scratch" each message
if it wants to, or maintain a rolling one for history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

PING_INTERVAL_S = 20  # must stay well under Hyperliquid's 60s idle-disconnect window


@dataclass
class ReconnectPolicy:
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    jitter: bool = True

    def delay_for_attempt(self, attempt: int) -> float:
        delay = min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
        if self.jitter:
            delay = delay * (0.5 + random.random())
        return delay


def parse_hyperliquid_message(payload: dict) -> dict | None:
    """Pure function: raw decoded JSON envelope -> internal event dict, or
    None for envelopes we don't act on. Kept separate from the class so it
    can be unit-tested without a live socket (see tests/test_hyperliquid_ws.py)."""
    channel = payload.get("channel")
    data = payload.get("data")

    if channel == "subscriptionResponse":
        return {"type": "subscription_ack", "detail": data}

    if channel == "pong":
        return {"type": "pong"}

    if channel == "l2Book" and isinstance(data, dict):
        levels = data.get("levels") or [[], []]
        bids_raw, asks_raw = (levels + [[], []])[:2]
        bids = [(float(lvl["px"]), float(lvl["sz"]), lvl.get("n")) for lvl in bids_raw]
        asks = [(float(lvl["px"]), float(lvl["sz"]), lvl.get("n")) for lvl in asks_raw]
        return {
            "type": "l2Book",
            "coin": data.get("coin"),
            "ts_exchange_ms": data.get("time"),
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "bids": bids,
            "asks": asks,
        }

    if channel == "trades" and isinstance(data, list):
        trades = [
            {
                "coin": t.get("coin"),
                "side": t.get("side"),
                "price": float(t["px"]),
                "size": float(t["sz"]),
                "ts_exchange_ms": t.get("time"),
                "tid": t.get("tid"),
            }
            for t in data
            if "px" in t and "sz" in t
        ]
        if not trades:
            return None
        return {"type": "trades", "coin": trades[0]["coin"], "trades": trades}

    if channel == "candle" and isinstance(data, list):
        candles = [
            {
                "coin": c.get("s"),
                "interval": c.get("i"),
                "open_ms": c.get("t"),
                "close_ms": c.get("T"),
                "open": c.get("o"),
                "close": c.get("c"),
                "high": c.get("h"),
                "low": c.get("l"),
                "volume": c.get("v"),
                "n_trades": c.get("n"),
            }
            for c in data
        ]
        if not candles:
            return None
        return {"type": "candle", "coin": candles[0]["coin"], "candles": candles}

    # Unknown/unsubscribed channel (e.g. "error") — surfaced but not crashed on.
    return {"type": "unknown", "channel": channel, "data": data}


@dataclass
class HyperliquidWebSocketCollector:
    """Connects to Hyperliquid's WebSocket, subscribes to l2Book/trades/
    candle for each configured asset, and forwards parsed events onto
    ``out_queue``. One instance per process; all assets share one
    connection (Hyperliquid multiplexes subscriptions over one socket)."""

    ws_url: str
    assets: list[str]
    out_queue: "asyncio.Queue"
    reconnect_policy: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    # Must stay comfortably above PING_INTERVAL_S (20s) + a pong round-trip,
    # or the watchdog would force a reconnect on every quiet subscription
    # even though the connection is healthy. This is a socket-liveness
    # timeout, distinct from settings.yaml's asyncio.stale_data_timeout_s
    # (a higher-level "no usable market data -> STOP TRADING" concern wired
    # up in Phase 12's health monitor).
    heartbeat_timeout_s: float = 30.0
    candle_interval: str = "1m"

    _last_message_ts: float = field(default=0.0, init=False, repr=False)
    _stop: bool = field(default=False, init=False, repr=False)

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """Reconnect loop. Never raises — logs and retries with backoff so a
        Hyperliquid outage never takes down the rest of the system
        (section 10)."""
        attempt = 0
        while not self._stop:
            try:
                await self._connect_and_stream()
                attempt = 0  # clean exit (stop() called) resets backoff
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                delay = self.reconnect_policy.delay_for_attempt(attempt)
                logger.warning(
                    "hyperliquid_ws disconnected (%s); reconnecting in %.1fs",
                    exc,
                    delay,
                )
                attempt += 1
                await asyncio.sleep(delay)
            except Exception:
                logger.exception("hyperliquid_ws unexpected error; backing off")
                delay = self.reconnect_policy.delay_for_attempt(attempt)
                attempt += 1
                await asyncio.sleep(delay)

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(self.ws_url, ping_interval=None, ping_timeout=None) as ws:
            # ping_interval=None: we manage our own application-level "ping"
            # method per Hyperliquid's protocol instead of relying on the
            # WebSocket-frame-level ping websockets sends by default.
            logger.info("hyperliquid_ws connected: %s", self.ws_url)
            self._last_message_ts = time.monotonic()
            await self._subscribe(ws)

            watchdog = asyncio.create_task(self._heartbeat_watchdog(ws))
            pinger = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw_message in ws:
                    self._last_message_ts = time.monotonic()
                    await self._handle_message(raw_message)
                    if self._stop:
                        break
            finally:
                watchdog.cancel()
                pinger.cancel()

    async def _subscribe(self, ws) -> None:
        for asset in self.assets:
            for sub in (
                {"type": "l2Book", "coin": asset},
                {"type": "trades", "coin": asset},
                {"type": "candle", "coin": asset, "interval": self.candle_interval},
            ):
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    async def _handle_message(self, raw_message: str) -> None:
        ts_received = time.time_ns()
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("hyperliquid_ws: non-JSON frame dropped")
            return

        event = parse_hyperliquid_message(payload)
        if event is None:
            return

        queue_item = {
            "source": "hyperliquid",
            "ts_received": ts_received,
            "payload": event,
        }
        try:
            self.out_queue.put_nowait(queue_item)
        except asyncio.QueueFull:
            logger.error("raw_queue full — dropping hyperliquid message (backpressure)")

    async def _ping_loop(self, ws) -> None:
        """Hyperliquid disconnects idle sockets after 60s; ping well under
        that so quiet subscriptions (e.g. an illiquid candle interval)
        never trigger an unnecessary reconnect."""
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except ConnectionClosed:
                return

    async def _heartbeat_watchdog(self, ws) -> None:
        """Forces a reconnect if no message has arrived within
        heartbeat_timeout_s, even if the socket hasn't raised an error."""
        while True:
            await asyncio.sleep(self.heartbeat_timeout_s / 3)
            idle_for = time.monotonic() - self._last_message_ts
            if idle_for > self.heartbeat_timeout_s:
                logger.warning(
                    "hyperliquid_ws idle for %.1fs (> %.1fs) — forcing reconnect",
                    idle_for,
                    self.heartbeat_timeout_s,
                )
                await ws.close()
                return
