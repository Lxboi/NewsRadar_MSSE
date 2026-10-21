"""Phase 14 tests: execution/kraken_executor.py — KrakenExecutor, built
as inert, unactivated scaffolding per the user's explicit choice. Every
test proves either (a) the request-building/signing/response-shaping
logic is correct against a stubbed transport, or (b) the three safety
gates genuinely block all network access when not all satisfied.
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from src.execution.execution_interface import ExecutionInterface
from src.execution.kraken_executor import (
    ASSET_CODE_MAP,
    KrakenAPIError,
    KrakenExecutor,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_REJECTED,
)

VALID_SECRET = base64.b64encode(b"a fake but validly-base64-encoded secret").decode()

ASSET_CONFIG = {
    "BTC": {"kraken": {"spot_pair": "XBT/USD"}},
    "ETH": {"kraken": {"spot_pair": "ETH/USD"}},
    "SOL": {"kraken": {"spot_pair": "SOL/USD"}},
}


def raising_transport(url, data, headers):
    raise AssertionError(f"transport should never be called: {url} {data}")


def make_executor(**overrides):
    kwargs = dict(
        api_key="key123",
        api_secret=VALID_SECRET,
        confirm_live_trading=True,
        dry_run=False,
        asset_config=ASSET_CONFIG,
        transport=raising_transport,
    )
    kwargs.update(overrides)
    return KrakenExecutor(**kwargs)


class ScriptedTransport:
    """A duck-typed stand-in for `KrakenExecutor`'s `transport` callable:
    returns each queued `(response, latency_ms)` pair in order, and
    records every call for assertions."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return self._responses.pop(0)


# -- ABC compliance -------------------------------------------------------

def test_kraken_executor_satisfies_the_execution_interface_abc():
    assert issubclass(KrakenExecutor, ExecutionInterface)
    executor = KrakenExecutor()  # fully-defaulted construction must not raise
    assert isinstance(executor, ExecutionInterface)


# -- Gate: default construction is fully inert -----------------------------

def test_default_construction_is_fully_inert():
    executor = KrakenExecutor(transport=raising_transport)
    assert executor.live_enabled is False
    status = executor.explain_gate_status()
    assert status["live_enabled"] is False
    assert len(status["blocking_reasons"]) == 3  # all three gates unsatisfied


def test_inert_place_order_never_calls_transport_and_returns_dry_run_shape():
    executor = KrakenExecutor(asset_config=ASSET_CONFIG, transport=raising_transport)
    result = executor.place_order(asset="BTC", side="buy", quantity=0.01)
    assert result["dry_run"] is True
    assert result["status"] == STATUS_PENDING
    assert result["order_id"].startswith("DRYRUN-")
    assert result["filled_quantity"] == 0.0
    assert len(result["gate_reasons"]) == 3
    assert executor.order_log == [result]  # section 43: still logged even when inert


def test_inert_cancel_order_skips_network():
    executor = KrakenExecutor(transport=raising_transport)
    result = executor.cancel_order("O123-ABC")
    assert result["status"] == "cancel_skipped_dry_run"
    assert result["dry_run"] is True


def test_inert_get_balance_returns_empty_structurally_valid_shape():
    executor = KrakenExecutor(transport=raising_transport)
    balance = executor.get_balance()
    assert balance == {"cash": 0.0, "balances": {}, "raw": None, "dry_run": True,
                        "gate_reasons": balance["gate_reasons"]}


def test_inert_get_position_returns_none():
    executor = KrakenExecutor(transport=raising_transport)
    assert executor.get_position("BTC") is None


def test_inert_get_open_orders_returns_empty_list():
    executor = KrakenExecutor(transport=raising_transport)
    assert executor.get_open_orders() == []


# -- Gate: each individual gate alone is insufficient ----------------------

@pytest.mark.parametrize("overrides", [
    {"dry_run": False},                                                   # missing confirm + keys
    {"confirm_live_trading": True},                                       # missing dry_run=False + keys
    {"api_key": "key", "api_secret": VALID_SECRET},                       # missing dry_run=False + confirm
    {"dry_run": False, "confirm_live_trading": True},                     # missing keys
    {"dry_run": False, "api_key": "key", "api_secret": VALID_SECRET},     # missing confirm
    {"confirm_live_trading": True, "api_key": "key", "api_secret": VALID_SECRET},  # missing dry_run=False
])
def test_partial_gate_combinations_stay_inert(overrides):
    executor = KrakenExecutor(transport=raising_transport, **overrides)
    assert executor.live_enabled is False


def test_all_three_gates_together_enable_live_mode():
    executor = make_executor()
    assert executor.live_enabled is True
    assert executor.explain_gate_status() == {"live_enabled": True, "blocking_reasons": []}


def test_empty_string_api_key_still_counts_as_missing():
    executor = KrakenExecutor(api_key="", api_secret=VALID_SECRET, confirm_live_trading=True, dry_run=False)
    assert executor.live_enabled is False


# -- signing / nonce --------------------------------------------------------

def test_sign_is_deterministic_for_identical_inputs():
    executor = make_executor()
    data = {"nonce": 1000, "pair": "XBT/USD"}
    sig1 = executor._sign("/0/private/AddOrder", data, VALID_SECRET)
    sig2 = executor._sign("/0/private/AddOrder", data, VALID_SECRET)
    assert sig1 == sig2


def test_sign_changes_with_nonce():
    executor = make_executor()
    sig_a = executor._sign("/0/private/AddOrder", {"nonce": 1}, VALID_SECRET)
    sig_b = executor._sign("/0/private/AddOrder", {"nonce": 2}, VALID_SECRET)
    assert sig_a != sig_b


def test_sign_rejects_non_base64_secret_with_a_clear_error():
    executor = make_executor()
    with pytest.raises(ValueError, match="base64"):
        executor._sign("/0/private/AddOrder", {"nonce": 1}, "not valid base64 !!!")


def test_nonce_strictly_increases_even_called_rapidly():
    executor = make_executor()
    nonces = [executor._next_nonce() for _ in range(1000)]
    assert all(b > a for a, b in zip(nonces, nonces[1:]))


# -- place_order: live path, status mapping ---------------------------------

def test_place_order_market_buy_fully_filled():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["OABC-123"], "descr": {"order": "buy 0.01 XBTUSD @ market"}}}, 42.0),
        ({"error": [], "result": {"OABC-123": {
            "status": "closed", "vol": "0.01", "vol_exec": "0.01", "price": "65000.0", "fee": "3.25",
        }}}, 11.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="BTC", side="buy", quantity=0.01)

    assert result["order_id"] == "OABC-123"
    assert result["status"] == STATUS_FILLED
    assert result["filled_quantity"] == 0.01
    assert result["avg_fill_price"] == 65000.0
    assert result["fees"] == 3.25
    assert result["latency_ms"] == pytest.approx(53.0)
    assert result["dry_run"] is False

    add_call = transport.calls[0]
    assert add_call["data"]["pair"] == "XBT/USD"
    assert add_call["data"]["type"] == "buy"
    assert add_call["data"]["ordertype"] == "market"
    assert "API-Key" in add_call["headers"] and "API-Sign" in add_call["headers"]


def test_place_order_partial_fill_when_vol_exec_below_requested():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["OXYZ-1"]}}, 10.0),
        ({"error": [], "result": {"OXYZ-1": {
            "status": "closed", "vol": "1.0", "vol_exec": "0.4", "price": "3000.0", "fee": "1.0",
        }}}, 10.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="ETH", side="sell", quantity=1.0)
    assert result["status"] == STATUS_PARTIAL
    assert result["filled_quantity"] == 0.4


def test_place_order_open_status_maps_to_pending():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["O1"]}}, 5.0),
        ({"error": [], "result": {"O1": {"status": "open", "vol": "1.0", "vol_exec": "0.0"}}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="SOL", side="buy", quantity=1.0)
    assert result["status"] == STATUS_PENDING


@pytest.mark.parametrize("kraken_status", ["canceled", "expired"])
def test_place_order_canceled_or_expired_maps_to_rejected(kraken_status):
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["O2"]}}, 5.0),
        ({"error": [], "result": {"O2": {"status": kraken_status, "vol": "1.0", "vol_exec": "0.0"}}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="SOL", side="buy", quantity=1.0)
    assert result["status"] == STATUS_REJECTED


def test_place_order_unrecognized_kraken_status_falls_back_to_pending_never_guessed_filled():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["O3"]}}, 5.0),
        ({"error": [], "result": {"O3": {"status": "some_future_status_kraken_invents", "vol": "1.0", "vol_exec": "1.0"}}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="SOL", side="buy", quantity=1.0)
    assert result["status"] == STATUS_PENDING  # never assumed filled just because vol_exec matches


def test_place_order_no_txid_returned_is_reported_rejected_not_raised():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": []}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="BTC", side="buy", quantity=0.01)
    assert result["status"] == STATUS_REJECTED
    assert result["rejected_reason"] == "kraken_returned_no_txid"


def test_place_order_limit_requires_a_price():
    executor = make_executor()
    with pytest.raises(ValueError, match="price"):
        executor.place_order(asset="BTC", side="buy", quantity=0.01, order_type="limit")


def test_place_order_limit_sends_price_field():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["O4"]}}, 5.0),
        ({"error": [], "result": {"O4": {"status": "open", "vol": "0.01", "vol_exec": "0.0"}}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    executor.place_order(asset="BTC", side="buy", quantity=0.01, order_type="limit", price=64000.0)
    assert transport.calls[0]["data"]["price"] == "64000.0"


def test_place_order_unknown_asset_raises_value_error_before_any_network_call():
    executor = make_executor(transport=raising_transport)
    with pytest.raises(ValueError, match="XRP"):
        executor.place_order(asset="XRP", side="buy", quantity=1.0)


def test_place_order_kraken_error_response_raises_kraken_api_error():
    transport = ScriptedTransport([
        ({"error": ["EOrder:Insufficient funds"], "result": {}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    with pytest.raises(KrakenAPIError, match="Insufficient funds"):
        executor.place_order(asset="BTC", side="buy", quantity=100.0)


def test_place_order_computes_slippage_bps_when_price_lookup_given():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["O5"]}}, 5.0),
        ({"error": [], "result": {"O5": {"status": "closed", "vol": "1.0", "vol_exec": "1.0", "price": "101.0", "fee": "0.1"}}}, 5.0),
    ])
    executor = make_executor(transport=transport, price_lookup=lambda asset: {"price": 100.0, "spread_bps": 2.0})
    result = executor.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["slippage_bps"] == pytest.approx((101.0 - 100.0) / 100.0 * 10_000)


def test_place_order_slippage_bps_is_none_without_price_lookup():
    transport = ScriptedTransport([
        ({"error": [], "result": {"txid": ["O6"]}}, 5.0),
        ({"error": [], "result": {"O6": {"status": "closed", "vol": "1.0", "vol_exec": "1.0", "price": "101.0", "fee": "0.1"}}}, 5.0),
    ])
    executor = make_executor(transport=transport)
    result = executor.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["slippage_bps"] is None


# -- cancel_order, get_balance, get_position, get_open_orders: live path ---

def test_cancel_order_live_success():
    transport = ScriptedTransport([({"error": [], "result": {"count": 1}}, 8.0)])
    executor = make_executor(transport=transport)
    result = executor.cancel_order("OABC-123")
    assert result["status"] == "cancelled"
    assert transport.calls[0]["data"]["txid"] == "OABC-123"


def test_cancel_order_live_failure_when_count_zero():
    transport = ScriptedTransport([({"error": [], "result": {"count": 0}}, 8.0)])
    executor = make_executor(transport=transport)
    result = executor.cancel_order("unknown-id")
    assert result["status"] == "cancel_failed"


def test_get_balance_live_parses_known_asset_codes_and_preserves_raw():
    transport = ScriptedTransport([
        ({"error": [], "result": {"ZUSD": "1234.56", "XXBT": "0.5"}}, 6.0),
    ])
    executor = make_executor(transport=transport)
    balance = executor.get_balance()
    assert balance["cash"] == 1234.56
    assert balance["balances"] == {"ZUSD": 1234.56, "XXBT": 0.5}
    assert balance["raw"] == {"ZUSD": "1234.56", "XXBT": "0.5"}
    assert balance["dry_run"] is False


def test_get_position_returns_quantity_with_avg_price_always_none():
    transport = ScriptedTransport([
        ({"error": [], "result": {"XXBT": "0.75"}}, 6.0),
    ])
    executor = make_executor(transport=transport)
    position = executor.get_position("BTC")
    assert position == {"asset": "BTC", "quantity": 0.75, "avg_price": None}


def test_get_position_returns_none_when_asset_has_no_recorded_balance():
    transport = ScriptedTransport([({"error": [], "result": {}}, 6.0)])
    executor = make_executor(transport=transport)
    assert executor.get_position("BTC") is None


def test_get_open_orders_parses_and_filters_by_asset():
    transport = ScriptedTransport([
        ({"error": [], "result": {"open": {
            "O1": {"descr": {"pair": "XBT/USD", "type": "buy", "ordertype": "limit", "price": "64000.0"}, "vol": "0.01"},
            "O2": {"descr": {"pair": "ETH/USD", "type": "sell", "ordertype": "market"}, "vol": "1.0"},
        }}}, 9.0),
    ])
    executor = make_executor(transport=transport)
    all_orders = executor.get_open_orders()
    assert {o["order_id"] for o in all_orders} == {"O1", "O2"}

    transport2 = ScriptedTransport([
        ({"error": [], "result": {"open": {
            "O1": {"descr": {"pair": "XBT/USD", "type": "buy", "ordertype": "limit", "price": "64000.0"}, "vol": "0.01"},
            "O2": {"descr": {"pair": "ETH/USD", "type": "sell", "ordertype": "market"}, "vol": "1.0"},
        }}}, 9.0),
    ])
    executor2 = make_executor(transport=transport2)
    btc_only = executor2.get_open_orders(asset="BTC")
    assert len(btc_only) == 1
    assert btc_only[0]["asset"] == "BTC"
    assert btc_only[0]["limit_price"] == 64000.0


def test_get_open_orders_live_but_empty():
    transport = ScriptedTransport([({"error": [], "result": {"open": {}}}, 3.0)])
    executor = make_executor(transport=transport)
    assert executor.get_open_orders() == []


# -- ASSET_CODE_MAP sanity --------------------------------------------------

def test_asset_code_map_covers_the_configured_universe():
    assert set(ASSET_CODE_MAP.keys()) >= {"BTC", "ETH", "SOL", "USD"}


# -- the real (default) transport, exercised against a mocked httpx.post ---
# Every test above injects `transport` directly (the same duck-typed-
# dependency convention `PaperExecutor.price_lookup` uses) to keep the
# signing/status-mapping logic testable with zero network. This test
# covers the one remaining code path those tests don't: `_httpx_transport`
# itself — the default that runs when no `transport` override is given.

def test_default_httpx_transport_signs_measures_latency_and_parses_response():
    secret = base64.b64encode(b"fake secret").decode()
    executor = KrakenExecutor(
        api_key="k", api_secret=secret, confirm_live_trading=True, dry_run=False,
        asset_config=ASSET_CONFIG,
    )
    fake_response = MagicMock()
    fake_response.json.return_value = {"error": [], "result": {"count": 1}}
    fake_response.raise_for_status.return_value = None

    with patch("httpx.post", return_value=fake_response) as mock_post:
        result = executor.cancel_order("O123")

    assert result["status"] == "cancelled"
    assert isinstance(result["latency_ms"], float) and result["latency_ms"] >= 0
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["data"]["txid"] == "O123"
    assert "API-Key" in call_kwargs["headers"] and "API-Sign" in call_kwargs["headers"]


def test_default_httpx_transport_raises_on_http_error_status():
    secret = base64.b64encode(b"fake secret").decode()
    executor = KrakenExecutor(
        api_key="k", api_secret=secret, confirm_live_trading=True, dry_run=False,
        asset_config=ASSET_CONFIG,
    )
    fake_response = MagicMock()
    fake_response.raise_for_status.side_effect = RuntimeError("HTTP 500")

    with patch("httpx.post", return_value=fake_response):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            executor.cancel_order("O123")
