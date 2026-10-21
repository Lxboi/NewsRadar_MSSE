"""Phase 5 tests: RSS collector. All network access is mocked via
httpx.MockTransport, matching the project's "no network required for any
test" bar (see README.md, docs/09_tests_plan.md)."""
import asyncio
import datetime as dt

import httpx
import pytest

from src.collectors.rss_collector import (
    BoundedSeenSet,
    RobotsCache,
    RSSCollector,
    normalize_entry,
)

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Example Feed</title>
  <item>
    <title>Bitcoin ETF sees record inflows</title>
    <link>https://example.com/a</link>
    <guid>guid-a</guid>
    <description>Institutional demand surges.</description>
    <pubDate>Tue, 18 Aug 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Local weather forecast for the weekend</title>
    <link>https://example.com/b</link>
    <guid>guid-b</guid>
    <description>Sunny with a chance of rain.</description>
    <pubDate>Tue, 18 Aug 2026 11:00:00 GMT</pubDate>
  </item>
</channel>
</rss>
"""


# --- normalize_entry (pure function) ---


def test_normalize_entry_extracts_expected_fields():
    import feedparser

    parsed = feedparser.parse(SAMPLE_RSS)
    entry = parsed.entries[0]
    feed_cfg = {"name": "TestFeed", "category": "crypto_media"}
    now = dt.datetime.now(dt.timezone.utc)

    event = normalize_entry(feed_cfg, entry, now)

    assert event["source"] == "TestFeed"
    assert event["title"] == "Bitcoin ETF sees record inflows"
    assert event["url"] == "https://example.com/a"
    assert event["event_time"].year == 2026
    assert event["ingestion_time"] == now
    assert event["event_uid"]  # non-empty, deterministic hash


def test_normalize_entry_uid_is_stable_for_same_entry():
    import feedparser

    parsed = feedparser.parse(SAMPLE_RSS)
    entry = parsed.entries[0]
    feed_cfg = {"name": "TestFeed"}
    now = dt.datetime.now(dt.timezone.utc)
    uid1 = normalize_entry(feed_cfg, entry, now)["event_uid"]
    uid2 = normalize_entry(feed_cfg, entry, now)["event_uid"]
    assert uid1 == uid2


def test_normalize_entry_falls_back_to_ingestion_time_without_pubdate():
    feed_cfg = {"name": "TestFeed"}
    now = dt.datetime.now(dt.timezone.utc)
    event = normalize_entry(feed_cfg, {"title": "No date here", "link": "https://x"}, now)
    assert event["event_time"] == now


# --- BoundedSeenSet ---


def test_bounded_seen_set_contains_and_add():
    seen = BoundedSeenSet(maxlen=3)
    assert "a" not in seen
    seen.add("a")
    assert "a" in seen


def test_bounded_seen_set_evicts_oldest_past_capacity():
    seen = BoundedSeenSet(maxlen=2)
    seen.add("a")
    seen.add("b")
    seen.add("c")
    assert "a" not in seen
    assert "b" in seen
    assert "c" in seen
    assert len(seen) == 2


# --- RobotsCache ---


@pytest.mark.asyncio
async def test_robots_cache_allows_when_no_robots_txt_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        robots = RobotsCache("test-agent", http_timeout_s=5)
        allowed = await robots.allowed(client, "https://example.com/feed.xml")
    assert allowed is True


@pytest.mark.asyncio
async def test_robots_cache_disallows_when_robots_txt_blocks_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /feed.xml\n")
        return httpx.Response(200, content=SAMPLE_RSS)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        robots = RobotsCache("test-agent", http_timeout_s=5)
        allowed = await robots.allowed(client, "https://example.com/feed.xml")
    assert allowed is False


@pytest.mark.asyncio
async def test_robots_cache_fails_open_on_fetch_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        robots = RobotsCache("test-agent", http_timeout_s=5)
        allowed = await robots.allowed(client, "https://example.com/feed.xml")
    assert allowed is True


# --- RSSCollector.poll_feed_once (fetch + parse + enqueue, mocked transport) ---


@pytest.mark.asyncio
async def test_poll_feed_once_enqueues_new_items():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=SAMPLE_RSS)

    transport = httpx.MockTransport(handler)
    queue: asyncio.Queue = asyncio.Queue()
    collector = RSSCollector(feeds=[{"name": "TestFeed", "url": "https://example.com/feed.xml"}], out_queue=queue)

    async with httpx.AsyncClient(transport=transport) as client:
        semaphore = asyncio.Semaphore(1)
        robots = RobotsCache("test-agent", 5)
        new_count = await collector.poll_feed_once(client, semaphore, robots, collector.feeds[0])

    assert new_count == 2
    assert queue.qsize() == 2
    item = queue.get_nowait()
    assert item["source"] == "rss"
    assert item["payload"]["title"] == "Bitcoin ETF sees record inflows"


@pytest.mark.asyncio
async def test_poll_feed_once_skips_already_seen_items_on_repeat_call():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=SAMPLE_RSS)

    transport = httpx.MockTransport(handler)
    queue: asyncio.Queue = asyncio.Queue()
    collector = RSSCollector(feeds=[{"name": "TestFeed", "url": "https://example.com/feed.xml"}], out_queue=queue)

    async with httpx.AsyncClient(transport=transport) as client:
        semaphore = asyncio.Semaphore(1)
        robots = RobotsCache("test-agent", 5)
        first = await collector.poll_feed_once(client, semaphore, robots, collector.feeds[0])
        second = await collector.poll_feed_once(client, semaphore, robots, collector.feeds[0])

    assert first == 2
    assert second == 0  # same items again -> nothing new


@pytest.mark.asyncio
async def test_poll_feed_once_returns_sentinel_when_robots_disallows():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /feed.xml\n")
        return httpx.Response(200, content=SAMPLE_RSS)

    transport = httpx.MockTransport(handler)
    queue: asyncio.Queue = asyncio.Queue()
    collector = RSSCollector(feeds=[{"name": "TestFeed", "url": "https://example.com/feed.xml"}], out_queue=queue)

    async with httpx.AsyncClient(transport=transport) as client:
        semaphore = asyncio.Semaphore(1)
        robots = RobotsCache("test-agent", 5)
        result = await collector.poll_feed_once(client, semaphore, robots, collector.feeds[0])

    assert result == -1
    assert queue.empty()


@pytest.mark.asyncio
async def test_run_with_no_feeds_returns_immediately():
    queue: asyncio.Queue = asyncio.Queue()
    collector = RSSCollector(feeds=[], out_queue=queue)
    await asyncio.wait_for(collector.run(), timeout=2.0)  # should not hang


# --- regression: 2026-08-18 live run found CoinDesk (308) and U.Today
# (301) both 100% failing every poll because httpx defaults to NOT
# following redirects. RSSCollector.run() now builds its client with
# follow_redirects=True; this test exercises that exact behavior end to
# end via poll_feed_once against a MockTransport that redirects once. ---


@pytest.mark.asyncio
async def test_poll_feed_once_follows_redirects_when_client_configured_to():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/rss":
            return httpx.Response(308, headers={"Location": "/rss-canonical"})
        if request.url.path == "/rss-canonical":
            return httpx.Response(200, content=SAMPLE_RSS)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    queue: asyncio.Queue = asyncio.Queue()
    collector = RSSCollector(feeds=[{"name": "TestFeed", "url": "https://example.com/rss"}], out_queue=queue)

    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        semaphore = asyncio.Semaphore(1)
        robots = RobotsCache("test-agent", 5)
        new_count = await collector.poll_feed_once(client, semaphore, robots, collector.feeds[0])

    assert new_count == 2  # redirect was followed instead of raising
