# Kraken Executor — Implementation Notes (Phase 14)

Sections 41, 42, 65: a live `ExecutionInterface` implementation against
Kraken's Spot REST API — `src/execution/kraken_executor.py` — delivered
as **inert, unactivated scaffolding**, not a live-trading go-live.

## Why "inert scaffolding" and not "wait" or "skip"

After Phase 13 was delivered, the standing session authorization to keep
moving phase-to-phase was deliberately paused: section 5 of the master
prompt gates live Kraken execution on paper-trading validation, which
hadn't happened yet, and that's a materially different kind of decision
than the research/simulation phases built so far (real capital risk, not
just more code). The user was asked directly how to handle it, with three
options offered: wait for real paper-trading history, build
`KrakenExecutor` now as inert/unactivated code, or something else. The
user chose the second option. This phase is that choice, implemented
literally: the real Kraken integration logic exists and is fully
unit-tested, but nothing in this codebase can use it to touch real money
yet — see "Three independent gates" below.

## Scope this phase: spot only, not futures/margin

`config/assets.yaml` already commits to both a `spot_pair` and a
`futures_symbol` per asset, and `ROADMAP.md`'s Phase 14 row names a
"spot-vs-futures comparison (section 42)" as an eventual deliverable.
That comparison needs real data (fees, funding rates, liquidity, actual
fill quality) to be worth anything — building it now would mean guessing
at both venues' behavior with no evidence to compare. `KrakenExecutor`
here implements Kraken's **spot** REST endpoints only (`AddOrder`,
`QueryOrders`, `CancelOrder`, `Balance`, `OpenOrders`). Futures/margin
support and the section 42 comparison itself are explicitly left as a
follow-up once there's paper-trading history to inform the choice —
flagged here rather than silently guessed at or half-built for both
venues.

## Three independent gates, all defaulting to "off"

The same defense-in-depth convention `RiskManager` already uses —
`entry_filter`'s tighter, tunable checks sitting under the account-level
`max_spread_bps`/`max_slippage_bps` hard backstop (Phase 12) — is applied
here to a different question: "can this class ever place a real order or
touch a real account." `KrakenExecutor.live_enabled` is only `True` when
**all three** of the following are explicitly set at construction time;
any one left at its default keeps every method fully inert:

1. `dry_run=False` (default `True`).
2. `confirm_live_trading=True` (default `False`) — deliberately named to
   mirror `config/settings.yaml: trading_mode`'s own section-65
   requirement for "an interactive confirmation at startup" before LIVE
   mode runs at all; the same concept, expressed as a constructor gate
   instead of a startup prompt, since nothing calls this constructor from
   `main.py` yet.
3. Non-empty `api_key`/`api_secret` passed explicitly. `KrakenExecutor`
   never reads `.env` itself — the same "stay config/env-agnostic, let
   `main.py` own the wiring" convention `features/feature_engine.py` and
   `monitoring/performance.py` already use for staying DB-agnostic,
   applied here to secrets.

When inert, every `ExecutionInterface` method still returns a
structurally valid response — `place_order()` returns a real-shaped
dict with `status: "pending"`, `dry_run: True`, and a `gate_reasons` list
naming exactly which gate(s) are blocking it; `get_balance()`/
`get_position()`/`get_open_orders()` return empty-but-correctly-shaped
results — rather than raising or silently no-op'ing. This means
`StrategyEngine`'s optional `executor` slot (Phase 11) can already be
handed a `KrakenExecutor()` today, end to end, with zero risk: every call
is logged to `order_log` (section 43's "every decision is logged," which
applies here specifically *because* it lets the full wiring path be
exercised safely before any real credentials exist) and nothing ever
reaches Kraken's servers until the user deliberately supplies real keys
and flips all three gates.

**Verified directly, not just asserted**: every one of the 6 ways to
satisfy exactly two of the three gates is tested and confirmed to stay
inert (`tests/test_kraken_executor.py`'s parametrized gate-combination
test) — this isn't "the happy path works," it's "every incomplete
combination fails safe."

## Order confirmation: one follow-up query, not a reconciliation loop

Kraken's `AddOrder` is asynchronous — it returns a `txid`, not a
guaranteed fill, even for a market order. `place_order()` makes exactly
one follow-up `QueryOrders` call right after `AddOrder` to read back the
resulting status, and maps Kraken's vocabulary (`closed`, `open`,
`pending`, `canceled`, `expired`) onto `ExecutionInterface`'s four
(`filled`, `partial`, `pending`, `rejected`) — `closed` is further split
into `filled` vs. `partial` by comparing `vol_exec` against the requested
quantity. It deliberately does not poll/loop for a final resolution —
the same "don't build more machinery than there's evidence you need"
call `PaperExecutor`'s deliberately-not-a-full-matching-engine design
already made (Phase 13). A market order still showing `open` after that
one check is reported `pending`, not guessed either direction.

**A status Kraken returns that isn't in the documented vocabulary above
(a future API change) is also mapped to `pending`, never guessed as
`filled` or `rejected`** — even when other fields (like `vol_exec`
matching the requested quantity) might suggest a fill. This extends the
same principle `strategy/position_sizing.py` (Phase 12) already applies
to missing confidence/volatility inputs ("partial information should
never look more favorable than no information") to order-state
uncertainty instead: an unclear resolution should never be reported as
more resolved than it actually is. Covered directly by a dedicated
regression test.

## `get_position()`'s `avg_price` is always `None` — a documented gap, not a bug

Kraken's Spot API has no cost-basis-bearing "position" endpoint the way a
margin/futures account or `PaperExecutor`'s own ledger does (Phase 13) —
spot `Balance` reports only a raw currency amount, nothing about what it
was bought at. Reconstructing an average entry price would mean walking
full trade history (`TradesHistory`), which is out of scope for inert
scaffolding with no real trades yet to reconstruct from. `get_position()`
returns the base-asset balance shaped to match `PaperExecutor`'s
`{"asset", "quantity", "avg_price"}` contract structurally, with
`avg_price` explicitly and permanently `None` rather than a fabricated
number — verified directly by a dedicated test, not just left undefined.

## Kraken asset-code mapping — best-effort, with the raw response always preserved

Kraken's private `Balance`/`OpenOrders` responses use Kraken's own asset
codes, which don't always match the `spot_pair` strings already committed
to in `config/assets.yaml` (a well-known Kraken quirk — BTC's balance
code is historically `XXBT`, not `BTC` or `XBT`). `ASSET_CODE_MAP` is a
best-effort mapping for the BTC/ETH/SOL/USD universe `config/assets.yaml`
already defines. Every parsed response also carries the untouched `raw`
Kraken payload specifically so a wrong guess here never loses information
— this should be verified against one real low-stakes balance query
before anyone trusts `get_balance()`'s parsed `balances` dict for
anything that matters.

## Testability: a duck-typed `transport`, the same convention as `price_lookup`

Every network call goes through `self._transport(url, data, headers) ->
(response_dict, latency_ms)`, injectable exactly the way `PaperExecutor`
accepts a duck-typed `price_lookup` (Phase 13) — tests inject a
`ScriptedTransport` test double that returns canned Kraken JSON in order
and records every call, so the signing/status-mapping/gate logic is
fully verified with zero network access and no real credentials. The
real default (`_httpx_transport`) — used only when no `transport`
override is given — is separately covered by two tests that patch
`httpx.post` directly, confirming the default path signs correctly,
measures real latency, and propagates an HTTP error rather than
swallowing it. No test in this phase ever reaches Kraken's actual API.

## Signing and nonces

`_sign()` implements Kraken's own documented HMAC-SHA512 request-signing
algorithm (`API-Sign` header) — standard REST API authentication, the
same category of mechanism as AWS SigV4, used here only to authenticate
the user's own future API key against their own future account.
`_next_nonce()` floors at "one more than the last nonce issued" rather
than trusting `time.time()` alone, since Kraken requires a strictly
increasing 64-bit nonce per key and successive calls can land within the
same millisecond — verified directly across 1000 rapid calls in the test
suite. A non-base64 `api_secret` (Kraken secrets are always base64)
raises a clear `ValueError` rather than an opaque `binascii` decoding
error several layers down.

## Testing

42 new tests, all in `tests/test_kraken_executor.py`: ABC compliance (1),
gate coverage — default-inert, every inert method's structurally valid
dry-run shape, every incomplete gate-combination stays inert, all-three-
together enables live mode, an empty-string key still counts as missing
(9), signing/nonce determinism and the base64-secret error and 1000-call
nonce monotonicity (4), `place_order()`'s live path across every status
mapping including the "never guess resolved" regression, the no-txid
path, the `KrakenAPIError` propagation path, and slippage computation
with/without a `price_lookup` (12), `cancel_order()`/`get_balance()`/
`get_position()`/`get_open_orders()`'s live paths including the
always-`None`-`avg_price` regression (9), the real default transport
against a mocked `httpx.post`, both the success and HTTP-error paths (2),
and an `ASSET_CODE_MAP` coverage sanity check (1). 641 tests total.

## No live-run this phase, and no `main.py` wiring

Same reasoning as every prior phase's "no live-run" call, with an extra
layer here specifically because this phase's whole point was to stay
inert: no real Kraken API key exists in this sandbox to test against
(`.env`'s `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` are blank by design), and
— deliberately — nothing in `main.py` constructs a `KrakenExecutor`
anywhere. Wiring one in as `StrategyEngine`'s `executor`, and actually
supplying real credentials and flipping the three gates, is left
entirely to the user, once there's real paper-trading history (Phase 13's
`PaperExecutor` run against live data for a meaningful stretch) to
justify it — exactly the check-in this phase's design was meant to honor
rather than quietly bypass.
