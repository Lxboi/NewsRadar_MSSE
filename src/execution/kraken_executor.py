"""Live `ExecutionInterface` implementation against Kraken's Spot REST
API (sections 41, 42, 65).

Roadmap: PHASE 14. Built as **inert, unactivated scaffolding** — the
user's explicit choice on 2026-08-19, in response to being asked how to
handle this phase (section 5's own progression gates live execution on
paper-trading validation, which hasn't happened yet). The request-
building, signing, and response-shaping logic below is real and fully
unit-tested against a stubbed transport, but a real network call to
Kraken can never happen unless three independent constructor arguments
all say yes at once at construction time — see `KrakenExecutor`'s class
docstring. Nothing in this codebase constructs a `KrakenExecutor`
anywhere (no caller exists yet in `main.py`), so simply having this file
present changes nothing about what the system currently does.

## Scope this phase: spot only, not futures

`config/assets.yaml` lists both a `spot_pair` and a `futures_symbol` per
asset, and `ROADMAP.md`'s Phase 14 row also names a "spot-vs-futures
comparison (section 42)" as an eventual deliverable. That comparison is
a data-driven question (which venue to actually execute on, informed by
real fee/funding/liquidity numbers once there's paper-trading history to
look at) — not something to guess at by silently picking one or building
both half-heartedly. `KrakenExecutor` here implements Kraken's *spot*
REST endpoints only (`AddOrder`, `CancelOrder`, `QueryOrders`,
`CancelOrder`, `Balance`, `OpenOrders`, all against `spot_pair`).
Futures/margin support and the section 42 comparison itself are left as
an explicit follow-up, flagged here rather than assumed.

## Three independent gates, all defaulting to "off"

The same defense-in-depth convention `RiskManager` already uses for
account-level limits under `entry_filter`'s tighter tunable checks
(Phase 12) is applied here to a different question: "can this class ever
touch real money." `live_enabled` is only `True` when ALL of the
following hold — any one left at its default keeps every method fully
inert:

1. `dry_run=False` passed explicitly (default `True`).
2. `confirm_live_trading=True` passed explicitly (default `False`) —
   mirrors `config/settings.yaml: trading_mode`'s own section-65
   requirement for "an interactive confirmation at startup" before LIVE
   mode is allowed to run at all.
3. Non-empty `api_key`/`api_secret` passed explicitly. This class never
   reads `.env` itself — the same "stay config/env-agnostic, let
   `main.py` do the wiring" convention `features/feature_engine.py` and
   `monitoring/performance.py` already use for staying DB-agnostic,
   applied here to secrets instead of a database handle.

When inert, every `ExecutionInterface` method still returns a
structurally valid response (so calling code — `StrategyEngine`, a
future `main.py` wiring step, a test — can be exercised end to end) with
`dry_run: True` and a `gate_reasons` list explaining exactly which
gate(s) are blocking a real call, rather than raising or silently doing
nothing unexplained. Every attempt, live or inert, is appended to
`self.order_log` — section 43's "every decision is logged" applies here
exactly as it does to `PaperExecutor`, specifically because it lets the
user safely exercise the full "would this have called Kraken" path
before ever supplying real credentials.

## Order confirmation: one follow-up query, not a reconciliation loop

Kraken's `AddOrder` endpoint is asynchronous — it returns a `txid` for
the order, not a guaranteed immediate fill, even for a market order.
`place_order()` here makes exactly one follow-up `QueryOrders` call
right after `AddOrder` to read back the resulting status, and maps
Kraken's status vocabulary (`open`, `closed`, `canceled`, `expired`,
`pending`) onto `ExecutionInterface`'s four (`filled`, `partial`,
`pending`, `rejected`). It deliberately does not loop/poll for a final
resolution — the same "don't build more machinery than there's evidence
you need yet" call `PaperExecutor` made in not building a full matching
engine. A market order still reporting `open`/`pending` after that one
check is reported back as `pending`, not guessed at either direction; a
fuller reconciliation strategy (poll-until-resolved, a background
`try_fill_pending`-style re-check) is left for whoever activates this
against real paper-trading history and can see what actually happens.
**An order status Kraken reports that isn't in the vocabulary above
(e.g. a future API change) is also mapped to `pending`, never guessed as
`filled` or `rejected`** — the same "missing information should never
look more favorable/certain than it is" principle
`strategy/position_sizing.py` (Phase 12) already applies to missing
confidence/volatility inputs, applied here to order state instead.

## `get_position()`'s `avg_price` is always `None` — a documented gap, not a bug

Kraken's Spot API has no cost-basis-bearing "position" endpoint the way
a margin/futures account or `PaperExecutor`'s own ledger does — spot
`Balance` reports only a raw currency amount. Reconstructing an average
entry price would mean walking full trade history (`TradesHistory`),
which is out of scope for inert scaffolding with no real trades to
reconstruct yet. `get_position()` returns the base-asset balance shaped
to match `PaperExecutor`'s `{"asset", "quantity", "avg_price"}` contract
structurally, with `avg_price` explicitly `None` and never fabricated.

## Kraken asset-code mapping: best-effort, with the raw response preserved

Kraken's private `Balance`/`OpenOrders` responses use Kraken's own asset
codes, which do not always match the plain `spot_pair` strings already
committed to in `config/assets.yaml` (a well-known Kraken quirk — BTC's
balance code is historically `XXBT`, not `BTC` or `XBT`). `ASSET_CODE_MAP`
below is a best-effort mapping for BTC/ETH/SOL/USD; every parsed response
also includes the untouched `raw` Kraken payload specifically so a wrong
guess here never loses information — verify the mapping against a real
low-stakes query before trusting `get_balance()`'s parsed `balances` dict.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any, Callable

from src.execution.execution_interface import ExecutionInterface

KRAKEN_API_BASE = "https://api.kraken.com"
KRAKEN_API_VERSION = "0"
KRAKEN_PRIVATE_PATH = f"/{KRAKEN_API_VERSION}/private"

DEFAULT_REQUEST_TIMEOUT_S = 10.0

STATUS_FILLED = "filled"
STATUS_PARTIAL = "partial"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"

# Kraken's own order-status vocabulary (AddOrder/QueryOrders `status` field)
# mapped onto ExecutionInterface's four. Anything Kraken returns that isn't
# a key here falls back to STATUS_PENDING — see the module docstring.
KRAKEN_STATUS_MAP = {
    "closed": STATUS_FILLED,       # further refined to PARTIAL below if vol_exec < vol
    "open": STATUS_PENDING,
    "pending": STATUS_PENDING,
    "canceled": STATUS_REJECTED,
    "expired": STATUS_REJECTED,
}

# Best-effort Kraken private-API asset codes for the BTC/ETH/SOL/USD
# universe config/assets.yaml already commits to. See module docstring.
ASSET_CODE_MAP = {
    "BTC": "XXBT",
    "ETH": "XETH",
    "SOL": "SOL",
    "USD": "ZUSD",
}


class KrakenAPIError(RuntimeError):
    """Raised when Kraken's own response carries a non-empty `error`
    list — never silently swallowed, matching `AlertManager`'s
    `ValueError`-on-unrecognized-input convention (Phase 12) applied
    here to a remote API's own error signal instead of a local one."""


class KrakenExecutor(ExecutionInterface):
    """See module docstring for the full safety-gate design. `transport`
    is a duck-typed `(url, data, headers) -> (parsed_json_dict,
    latency_ms)` callable — injectable for tests exactly the way
    `PaperExecutor`'s `price_lookup` is, defaulting to a real `httpx`
    POST when not supplied."""

    def __init__(
        self,
        *,
        api_key: "str | None" = None,
        api_secret: "str | None" = None,
        confirm_live_trading: bool = False,
        dry_run: bool = True,
        asset_config: "dict | None" = None,
        price_lookup: "Callable[[str], dict] | None" = None,
        base_url: str = KRAKEN_API_BASE,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        transport: "Callable[[str, dict, dict], tuple[dict, float]] | None" = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.confirm_live_trading = confirm_live_trading
        self.dry_run = dry_run
        self.asset_config = asset_config or {}
        self.price_lookup = price_lookup
        self.base_url = base_url.rstrip("/")
        self.request_timeout_s = request_timeout_s
        self._transport = transport or self._httpx_transport

        self.order_log: "list[dict]" = []
        self._last_nonce = 0

        self.live_enabled = self._compute_live_enabled()
        self._blocking_gate_reasons = self._compute_blocking_gate_reasons()

    # -- gate status ----------------------------------------------------

    def _compute_live_enabled(self) -> bool:
        return bool(
            not self.dry_run
            and self.confirm_live_trading
            and self.api_key
            and self.api_secret
        )

    def _compute_blocking_gate_reasons(self) -> "list[str]":
        reasons = []
        if self.dry_run:
            reasons.append("dry_run=True (must pass dry_run=False)")
        if not self.confirm_live_trading:
            reasons.append("confirm_live_trading=False (must pass confirm_live_trading=True)")
        if not self.api_key or not self.api_secret:
            reasons.append("api_key/api_secret not provided")
        return reasons

    def explain_gate_status(self) -> dict:
        """Self-describing gate state, the same convention
        `RiskManager.state()` (Phase 12) already established — a caller
        (a future `main.py`, a Telegram `/status` handler) can report
        *why* this executor is inert without reaching into internals."""
        return {"live_enabled": self.live_enabled, "blocking_reasons": list(self._blocking_gate_reasons)}

    # -- ExecutionInterface -----------------------------------------------

    def place_order(
        self,
        *,
        asset: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: "float | None" = None,
    ) -> dict:
        pair = self._spot_pair(asset)

        if not self.live_enabled:
            return self._log(self._dry_run_response(
                asset=asset, side=side, quantity=quantity, order_type=order_type,
            ))

        data: "dict[str, Any]" = {
            "pair": pair,
            "type": side,
            "ordertype": order_type,
            "volume": f"{quantity}",
        }
        if order_type == "limit":
            if price is None:
                raise ValueError("a limit order requires an explicit price")
            data["price"] = f"{price}"

        add_result, add_latency_ms = self._private_request("AddOrder", data)
        txids = add_result.get("txid") or []
        order_id = txids[0] if txids else None

        if order_id is None:
            return self._log({
                "order_id": None, "asset": asset, "side": side, "order_type": order_type,
                "requested_quantity": quantity, "status": STATUS_REJECTED, "filled_quantity": 0.0,
                "avg_fill_price": None, "fees": 0.0, "slippage_bps": None,
                "latency_ms": add_latency_ms, "rejected_reason": "kraken_returned_no_txid",
                "dry_run": False, "raw": {"add_order": add_result},
            })

        query_result, query_latency_ms = self._private_request("QueryOrders", {"txid": order_id})
        order_info = (query_result or {}).get(order_id, {})
        status, filled_quantity, avg_fill_price, fees = self._parse_order_info(order_info, quantity)

        slippage_bps = self._compute_slippage_bps(asset, side, avg_fill_price)

        result = {
            "order_id": order_id, "asset": asset, "side": side, "order_type": order_type,
            "requested_quantity": quantity, "status": status, "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price, "fees": fees, "slippage_bps": slippage_bps,
            "latency_ms": add_latency_ms + query_latency_ms, "rejected_reason": None,
            "dry_run": False, "raw": {"add_order": add_result, "query_orders": order_info},
        }
        return self._log(result)

    def cancel_order(self, order_id) -> dict:
        if not self.live_enabled:
            return self._log({
                "order_id": order_id, "status": "cancel_skipped_dry_run",
                "dry_run": True, "gate_reasons": list(self._blocking_gate_reasons),
            })
        result, latency_ms = self._private_request("CancelOrder", {"txid": order_id})
        count = result.get("count", 0)
        status = "cancelled" if count else "cancel_failed"
        return self._log({
            "order_id": order_id, "status": status, "latency_ms": latency_ms,
            "dry_run": False, "raw": result,
        })

    def get_position(self, asset: str) -> "dict | None":
        # See module docstring: Kraken spot has no cost-basis-bearing
        # position endpoint, so this is derived from raw balance.
        if not self.live_enabled:
            return None
        balance = self.get_balance()
        code = ASSET_CODE_MAP.get(asset, asset)
        quantity = balance.get("balances", {}).get(code)
        if quantity is None:
            return None
        return {"asset": asset, "quantity": quantity, "avg_price": None}

    def get_balance(self) -> dict:
        if not self.live_enabled:
            return {"cash": 0.0, "balances": {}, "raw": None, "dry_run": True,
                    "gate_reasons": list(self._blocking_gate_reasons)}
        result, _latency_ms = self._private_request("Balance", {})
        balances = {code: float(amount) for code, amount in result.items()}
        cash = balances.get(ASSET_CODE_MAP["USD"], 0.0)
        return {"cash": cash, "balances": balances, "raw": result, "dry_run": False}

    def get_open_orders(self, asset: "str | None" = None) -> "list[dict]":
        if not self.live_enabled:
            return []
        result, _latency_ms = self._private_request("OpenOrders", {})
        open_orders = (result or {}).get("open", {})
        parsed = []
        for order_id, info in open_orders.items():
            descr = info.get("descr", {})
            pair = descr.get("pair")
            order_asset = self._asset_for_pair(pair) if pair else None
            if asset is not None and order_asset != asset:
                continue
            order_type = descr.get("ordertype", "market")
            parsed.append({
                "order_id": order_id,
                "asset": order_asset,
                "side": descr.get("type"),
                "order_type": order_type,
                "requested_quantity": float(info.get("vol", 0.0)),
                "limit_price": float(descr["price"]) if order_type == "limit" and descr.get("price") else None,
                "status": STATUS_PENDING,
            })
        return parsed

    # -- Kraken REST plumbing --------------------------------------------

    def _spot_pair(self, asset: str) -> str:
        try:
            return self.asset_config[asset]["kraken"]["spot_pair"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"no Kraken spot_pair configured for asset {asset!r} "
                f"(expected asset_config[{asset!r}]['kraken']['spot_pair'], see config/assets.yaml)"
            ) from exc

    def _asset_for_pair(self, pair: str) -> "str | None":
        for asset, cfg in self.asset_config.items():
            if (cfg.get("kraken") or {}).get("spot_pair") == pair:
                return asset
        return None

    def _parse_order_info(self, order_info: dict, requested_quantity: float) -> "tuple[str, float, float | None, float]":
        kraken_status = order_info.get("status")
        status = KRAKEN_STATUS_MAP.get(kraken_status, STATUS_PENDING)

        vol_exec = float(order_info.get("vol_exec", 0.0) or 0.0)
        if status == STATUS_FILLED and vol_exec < requested_quantity:
            status = STATUS_PARTIAL

        avg_fill_price = float(order_info["price"]) if order_info.get("price") not in (None, "0", 0) else None
        fees = float(order_info.get("fee", 0.0) or 0.0)
        return status, vol_exec, avg_fill_price, fees

    def _compute_slippage_bps(self, asset: str, side: str, fill_price: "float | None") -> "float | None":
        if fill_price is None or self.price_lookup is None:
            return None
        quote = self.price_lookup(asset)
        if not quote or quote.get("price") is None:
            return None
        mid_price = quote["price"]
        direction = 1 if side == "buy" else -1
        return direction * (fill_price - mid_price) / mid_price * 10_000

    def _dry_run_response(self, *, asset: str, side: str, quantity: float, order_type: str) -> dict:
        return {
            "order_id": f"DRYRUN-{self._next_nonce()}", "asset": asset, "side": side,
            "order_type": order_type, "requested_quantity": quantity, "status": STATUS_PENDING,
            "filled_quantity": 0.0, "avg_fill_price": None, "fees": 0.0, "slippage_bps": None,
            "latency_ms": None, "rejected_reason": None,
            "dry_run": True, "gate_reasons": list(self._blocking_gate_reasons),
        }

    def _next_nonce(self) -> int:
        # Kraken requires a strictly increasing 64-bit nonce per API key.
        # time.time() alone can repeat across calls faster than 1ms apart,
        # so this floors at "one more than the last nonce issued."
        nonce = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = nonce
        return nonce

    def _sign(self, urlpath: str, data: dict, secret: str) -> str:
        # Kraken's own documented HMAC-SHA512 request-signing algorithm.
        try:
            secret_bytes = base64.b64decode(secret)
        except Exception as exc:
            raise ValueError("api_secret is not valid base64 (Kraken API secrets are base64-encoded)") from exc
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(secret_bytes, message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private_request(self, method_name: str, data: "dict | None") -> "tuple[dict, float]":
        payload = dict(data or {})
        payload["nonce"] = self._next_nonce()
        urlpath = f"{KRAKEN_PRIVATE_PATH}/{method_name}"
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(urlpath, payload, self.api_secret),
        }
        response, latency_ms = self._transport(self.base_url + urlpath, payload, headers)
        errors = response.get("error") or []
        if errors:
            raise KrakenAPIError(f"Kraken {method_name} returned errors: {errors}")
        return response.get("result", {}), latency_ms

    def _httpx_transport(self, url: str, data: dict, headers: dict) -> "tuple[dict, float]":
        import httpx  # imported lazily so a stubbed `transport` never requires httpx to be installed for tests

        start = time.monotonic()
        response = httpx.post(url, data=data, headers=headers, timeout=self.request_timeout_s)
        latency_ms = (time.monotonic() - start) * 1000
        response.raise_for_status()
        return response.json(), latency_ms

    def _log(self, decision: dict) -> dict:
        self.order_log.append(decision)
        return decision
