# News Radar (Phase 5, sections 20-26, 72)

## Source list (section 21)

`config/sources.yaml: rss_feeds` — 24 feeds total, expanded 2026-08-18 at
the user's request ("plus il y a de source mieux c'est... aide moi à en
trouvé un maximum dans le respect des contrainte matériel") after research
into what a Raspberry Pi 5 4GB deployment (section 63) actually constrains:

- **16 crypto media**: CoinDesk, CoinTelegraph, The Block, Decrypt,
  CryptoSlate, The Defiant, NewsBTC, Bitcoin.com News, BeInCrypto,
  AMBCrypto, Cryptonews.com, CoinPedia, DailyCoin, 99Bitcoins, Crypto.News,
  U.Today.
- **8 official regulatory/macro**: SEC (press releases, litigation
  releases, administrative proceedings, trading suspensions) and Federal
  Reserve (press releases, monetary policy, banking regulation, enforcement
  actions) — directly maps to section 21's keyword list (SEC, Fed, FOMC,
  CPI, regulation) and section 24's MACRO/REGULATION categories.

**Explicitly deferred this phase** (user decision, not a hardware
limitation): X/Twitter (no free API tier in 2026, ~$35-50+/month minimum)
and Reddit (free tier exists but now requires an approval application
under Reddit's "Responsible Builder Policy", since June 2026). Google News
RSS was also kept disabled — its unofficial `news.google.com/rss/search`
endpoint's `robots.txt` disallows automated fetching, which would conflict
with this file's own `rate_limits.respect_robots_txt` / `never_bypass_anti_bot`
(section 20, a hard rule). `config/sources.yaml` documents all three
decisions inline; revisit any of them independently of hardware if the
user's calculus changes.

## Why feed count isn't the Pi constraint (section 63)

An RSS poll is a transient fetch/parse/discard — no persistent connection,
no growing per-feed state beyond a few-hundred-entry "seen" set
(`BoundedSeenSet` in `rss_collector.py`, capacity 300, oldest-evicted).
24 feeds polling roughly once a minute is a trivial load next to what a
Pi 5 4GB comfortably handles headless. The two things that *do* matter,
both addressed by design:

1. **Connection bursts** — `rss_polling.max_concurrent_fetches` (default
   5) hard-caps simultaneous HTTP GETs across all 24 feeds via an
   `asyncio.Semaphore`, and `jitter_s` staggers both the first poll and
   every repeat so feeds don't all fire in lockstep.
2. **Heavy NLP/ML dependencies** — `src/news/sentiment.py` is a lexicon
   scan (stdlib string matching), not a transformer model. A proper ML
   sentiment classifier, if ever built, trains on the PC and ships as a
   batch-scored feature later — it does not belong in this collector's
   real-time path (section 63, section 9's "don't multiply dependencies").

## Collection (`src/collectors/rss_collector.py`, `news_collector.py`)

One polling task per feed (section 10's fault-isolation principle — one
dead host never delays the other 23), sharing the semaphore above.
`robots.txt` is actually fetched and checked before a feed's first poll
(`RobotsCache`, fails open if unreachable — most sites don't serve one,
and a published feed is meant to be polled by readers). On persistent
fetch failure, a feed backs off independently via the same
`ReconnectPolicy` `hyperliquid_ws.py` uses, rather than taking the
process down. `NewsCollector` is a thin fan-out wrapper so `main.py`'s
wiring won't need to change if Google News / social get enabled later.

## Scoring pipeline (`src/news/pipeline.py`, sections 22-26)

One `NewsPipeline.process()` call per raw item composes:

| Stage | Module | Output |
|---|---|---|
| Classification | `classifier.py` | `event_type` — `HACK_SECURITY \| REGULATION \| ETF \| LIQUIDATION \| MACRO \| EXCHANGE \| PARTNERSHIP_ADOPTION \| OTHER`, keyword-priority matched |
| Sentiment | `sentiment.py` | `sentiment` in [-1, 1], lexicon-based |
| Relevance | `relevance.py` | `market_relevance` in [0, 1] + `affected_assets` (BTC/ETH/SOL keyword extraction) |
| Credibility | `credibility.py` | `credibility` in [0, 1], looked up from `sources.yaml`'s per-feed value |
| Novelty | `novelty.py` | `novelty` in [0, 1], `1 - max title similarity` to recent history |
| Dedup | `deduplication.py` | `duplicate_of` — canonical event's `event_uid`, or `None` |
| Importance | `pipeline.py` | `CATEGORY_IMPORTANCE_WEIGHT[event_type] * market_relevance`, capped at 1.0 |

**Scope note on `classifier.py`'s category list**: section 24 specifies
*that* a fixed category list exists; the exact keyword-to-category mapping
here is a Phase 5 implementation choice (documented in the module,
reviewable/extendable the same way `rss_feeds` already is), not a verbatim
transcription of the master prompt. Flagged so it's easy to revise once
live article distributions are visible.

## No-look-ahead-bias decision (section 29, flagged as open in
`docs/06_features_v1.md`)

`event_detector.py: apply_decay()` measures age from **`ingestion_time`**,
not `event_time`. A source's stated publish time can be stale, backdated,
or simply wrong; `ingestion_time` is what the system actually observed,
and is what every other look-ahead-safe join in this codebase already
uses. This is now decided and consistent — Phase 7's feature engine should
not need to revisit it.

## Storage

- **Parquet** (`data/raw/news_raw/date=<YYYY-MM-DD>/part-*.parquet`, no
  `asset=` segment — news isn't tied to one symbol): every raw item,
  relevant or not, for full replay/audit. This required a small additive
  change to `ParquetStore.write_row()`/`flush()` to accept `asset=None`
  and skip that path segment (existing asset-keyed datasets unaffected).
- **SQLite** (`news_events` table, schema unchanged from Phase 1): only
  items with `market_relevance > 0` (at least one configured keyword hit)
  — this is what keeps the SEC/Fed feeds' many non-crypto press releases
  from flooding the operational table while keeping them on disk in
  Parquet. Insert is idempotent on `event_uid` (`record_news_event()` in
  `database.py`); `duplicate_of` is resolved from the dedup tracker's
  string `event_uid` to the canonical row's integer `id` at write time.

## Live display

`main.py: news_live_display()` prints the 5 most recent relevant,
non-duplicate items every 20s with their current decayed impact
(`apply_decay()`), mirroring `live_display()`'s role for market data —
something concrete for the user to eyeball during Phase 5 validation.

## Known sandbox limitation (same pattern as Phase 2)

Live RSS fetches were **not** validated from this cloud sandbox — the
sandbox's network proxy returns `403 Forbidden` on outbound HTTPS to
arbitrary hosts (confirmed against `coindesk.com`), the same restriction
that blocked Phase 2's live Hyperliquid WebSocket test. Everything
network-independent was verified here: `pytest` (offline — RSS fetch
logic is tested via `httpx.MockTransport`, no real requests), config
loading, and pipeline wiring smoke-tested end to end in-process. Real
validation happened on the user's machine (below).

## Live validation findings (2026-08-18, user's machine) — fixed

The user ran `python -m src.main` and it worked end to end (24 feeds
polling, items landing in the console and `news_events`), and the run
surfaced three real issues, all fixed the same day:

1. **CoinDesk and U.Today feeds failed every single poll.** Both URLs
   redirect (CoinDesk 308 to the same path without a trailing slash,
   U.Today 301 to `/rss.php`) and httpx does not follow redirects by
   default. Fixed: `RSSCollector.run()` now builds its `httpx.AsyncClient`
   with `follow_redirects=True`.
2. **False-positive relevance/classification from naive substring
   matching.** An SEC litigation release against an unrelated party
   (Gotbit Consulting) was scored `market_relevance > 0`, and another
   (RAD Diversified REIT) was tagged `affected_assets: ["SOL"]` — both
   wrong. Root cause: `kw in text` matched "sec" inside "**Sec**urities"
   and "sol" inside "re**sol**ve"-type words, and the SEC's own
   institutional name, "U.S. Securities and **Exchange** Commission",
   contains the legitimate whole word "Exchange", trivially satisfying
   that keyword on every single item from an SEC feed regardless of
   actual content. Fixed in `relevance.py`: `keyword_matches()` now
   requires a left word-boundary always, and a full word-boundary
   (`EXACT_MATCH_KEYWORDS`) for short ticker/abbreviation keywords (SEC,
   ETH, SOL, BTC, Fed, CPI, FOMC, ETF, CFTC, GDP, "sued") that are prone
   to prefixing unrelated English words — while longer keywords (hack,
   liquidation, regulat...) keep prefix matching so inflected forms still
   count ("hacked", "regulations"). `strip_boilerplate()` additionally
   removes "(U.S.) Securities and Exchange Commission" before matching,
   shared by both `score_relevance()` and `classify_event()` (the latter
   had the same EXCHANGE-via-boilerplate problem). Regression tests use
   the exact real examples from the log
   (`tests/test_news_scoring.py`).
3. **AMBCrypto correctly skipped** — its `robots.txt` disallows
   `/feed/`, and `RobotsCache` correctly refused to poll it. Not a bug;
   included here as confirmation the enforcement actually works against
   a real site, not just mocked tests.
4. **CryptoSlate returns 403 on both its `robots.txt` and its feed.**
   Left as-is deliberately — this looks like anti-bot blocking of a
   non-browser User-Agent, and spoofing a browser UA to get past it would
   be exactly the `never_bypass_anti_bot` violation section 20 forbids.
   The feed backs off with growing delay (up to `max_delay_s`) and
   contributes zero items; the other 23 feeds are unaffected (section
   10's fault isolation working as intended).
