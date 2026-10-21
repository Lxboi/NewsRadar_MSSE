# Hyperliquid WebSocket Architecture (section 11, Phase 2)

## Role

Hyperliquid is the primary microstructure data source (section 11) — L2
order book, trades, candles. The system builds its own order book engine
from raw messages (Phase 3); Hyperliquid (and Bookmap, optionally, for
human visual validation only) are never a hard runtime dependency of the
signal engine.

## Confirmed schema (checked against Hyperliquid's live docs, 2026-08-18)

**Connect:** `wss://api.hyperliquid.xyz/ws` (mainnet), `wss://api.hyperliquid-testnet.xyz/ws` (testnet).

**Subscribe (client -> server):**

```json
{"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}
{"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}}
{"method": "subscribe", "subscription": {"type": "candle", "coin": "BTC", "interval": "1m"}}
```

`l2Book` accepts optional `nSigFigs`/`nLevels` params to control aggregation
and depth — not set explicitly yet (Hyperliquid defaults apply); revisit once
Phase 3 needs a specific depth/precision.

**Every server message is an envelope** `{"channel": <str>, "data": <...>}`:

| channel | data shape |
|---|---|
| `subscriptionResponse` | `{"method", "subscription"}` — ack of a subscribe call |
| `l2Book` | `{"coin", "time", "levels": [[bid WsLevel...], [ask WsLevel...]]}`, `WsLevel = {"px": str, "sz": str, "n": int}` — **full snapshot each message, not a delta** |
| `trades` | array of `{"coin","side","px","sz","hash","time","tid","users":[buyer,seller]}` |
| `candle` | array of `{"t","T","s","i","o","c","h","l","v","n"}` (open/close ms, symbol, interval, OHLCV, trade count) |
| `pong` | no data — reply to a client `{"method": "ping"}` |

**Keepalive:** the server disconnects a socket after 60s with no message.
The collector sends `{"method": "ping"}` every 20s (`PING_INTERVAL_S` in
`hyperliquid_ws.py`) regardless of subscription activity, so a quiet market
never trips the idle timeout.

Sources: Hyperliquid docs — [Websocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket),
[Subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions),
[Timeouts and heartbeats](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats),
cross-checked against [Dwellir's L2 Book reference](https://www.dwellir.com/docs/hyperliquid/l2-book).
API surfaces change — re-verify before relying on exact optional-parameter names.

## Collector responsibilities (Phase 2, implemented)

`src/collectors/hyperliquid_ws.py` now: connects, subscribes to l2Book +
trades + 1m candle per configured asset, sends the keepalive ping,
reconnects with exponential backoff + jitter on any disconnect, and forces
a reconnect via `_heartbeat_watchdog` if no message arrives for
`heartbeat_timeout_s` (default 30s — deliberately above the 20s ping
interval so a normal ping/pong cycle never falsely trips it). Every inbound
message is parsed by the pure function `parse_hyperliquid_message()` into
one of `l2Book | trades | candle | subscription_ack | pong | unknown`
before being pushed onto the shared queue — see that function's docstring
and `tests/test_hyperliquid_ws.py` for the exact shapes.

## What it still doesn't do

No maintained order book, no OBI/microprice/liquidity/sweep/absorption
computation — those consume this collector's `l2Book`/`trades` events in
`src/orderbook/*` (Phase 3). Since Hyperliquid's `l2Book` messages are full
snapshots (not incremental deltas), Phase 3 can choose to treat each
message as ground truth directly, or maintain rolling history for anything
snapshot data alone can't capture (e.g. exact intra-snapshot order timing).

## Phase 2 validation status

Parsing logic is unit-tested against fixtures matching the confirmed
schema above (`tests/test_hyperliquid_ws.py`) — no network required, and
all pass. **Live connectivity has not been exercised from the delivery
sandbox**: outbound WebSocket connections to `api.hyperliquid.xyz` are
blocked by the sandbox's network proxy (`HTTP 403` at the proxy, confirmed
by attempting a real connection). This is expected — the sandbox only
allowlists package registries and doc sites, not arbitrary market-data
endpoints. **Running `python -m src.main` on your own machine (section 1
of the master prompt: Windows PC) is the actual Phase 2 acceptance test** —
confirm it connects, subscribes, and the live display in the console
updates with real BTC/ETH/SOL prices before moving to Phase 3.
