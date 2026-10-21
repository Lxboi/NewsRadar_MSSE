"""RSS/Atom feed collector. Configured feeds live in config/sources.yaml.
First and simplest news source to bring online (section 72, Phase 5).

Design notes (section 10's asyncio principles, applied to RSS):

- One polling task per feed, so one dead/slow host never delays the other
  23 (fault isolation). All tasks share an ``asyncio.Semaphore`` so at most
  ``max_concurrent_fetches`` HTTP requests are ever in flight at once —
  this, not feed *count*, is the actual Raspberry Pi 5 4GB consideration
  (a poll is a transient fetch/parse/discard; nothing persists per feed
  except a small bounded "seen" set).
- Each feed's own poll loop reconnects/backs off independently on fetch
  errors (`ReconnectPolicy`, reused from hyperliquid_ws.py) rather than
  taking the whole collector down.
- `robots.txt` is actually checked (not just documented) before the first
  fetch of a feed, honoring `sources.yaml: rate_limits.respect_robots_txt`
  and `never_bypass_anti_bot` (section 20) as real enforcement, not intent.
  Fails open (allows the fetch) if robots.txt itself can't be retrieved —
  most sites don't serve one, and a published RSS/Atom feed is meant to be
  polled by feed readers in the first place.
- `normalize_entry()` is a pure function (mirrors `parse_hyperliquid_message`
  in hyperliquid_ws.py) so feed-entry parsing is unit-testable without any
  network access — see tests/test_rss_collector.py.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import hashlib
import logging
import random
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

import feedparser
import httpx

from src.collectors.hyperliquid_ws import ReconnectPolicy

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "crypto_radar-research-bot/0.5 (personal research project; RESEARCH mode only)"


def _event_uid(source: str, entry_id: str | None, link: str | None, title: str) -> str:
    """Stable dedup key: prefer the feed's own entry id/guid, then the
    article URL, then a source+title fallback for feeds with neither."""
    basis = entry_id or link or f"{source}:{title}"
    return hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()[:32]


def _parse_event_time(entry: dict, fallback: dt.datetime) -> dt.datetime:
    """feedparser normalizes published/updated dates to a UTC time.struct_time
    when it can parse them at all. Falls back to `fallback` (ingestion time)
    rather than dropping the item — a missing/unparseable date shouldn't
    silently discard real news."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return dt.datetime.fromtimestamp(calendar.timegm(struct), tz=dt.timezone.utc)
            except (OverflowError, ValueError, TypeError):
                continue
    return fallback


def normalize_entry(feed_cfg: dict, entry: dict, ingestion_time: dt.datetime) -> dict:
    """Pure function: one feedparser entry + its feed's config -> a raw news
    event dict. Kept separate from the fetch loop so it's unit-testable
    without a live socket."""
    title = (entry.get("title") or "").strip()
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    link = (entry.get("link") or "").strip()
    event_time = _parse_event_time(entry, ingestion_time)
    tags = entry.get("tags") or []
    return {
        "source": feed_cfg["name"],
        "category": feed_cfg.get("category", "crypto_media"),
        "url": link,
        "title": title,
        "summary": summary,
        "event_time": event_time,
        "ingestion_time": ingestion_time,
        "event_uid": _event_uid(feed_cfg["name"], entry.get("id"), link, title),
        "raw_payload": {
            "id": entry.get("id"),
            "author": entry.get("author"),
            "tags": [t.get("term") for t in tags if isinstance(t, dict) and t.get("term")],
        },
    }


class BoundedSeenSet:
    """Fixed-capacity 'seen' tracker so a long-running RSS poller doesn't
    grow memory unbounded on a 4GB Pi — oldest ids are evicted first once
    the cap is hit. A few hundred entries per feed is far more than any of
    these feeds ever show at once, so eviction essentially never causes a
    re-emitted item in practice."""

    def __init__(self, maxlen: int = 300) -> None:
        self._order: "deque[str]" = deque(maxlen=maxlen)
        self._set: set[str] = set()

    def __contains__(self, item: str) -> bool:
        return item in self._set

    def __len__(self) -> int:
        return len(self._set)

    def add(self, item: str) -> None:
        if item in self._set:
            return
        if len(self._order) == self._order.maxlen:
            oldest = self._order.popleft()
            self._set.discard(oldest)
        self._order.append(item)
        self._set.add(item)


class RobotsCache:
    """Tiny per-host robots.txt cache so RSSCollector actually enforces
    `sources.yaml: rate_limits.respect_robots_txt` instead of just
    documenting it (section 20 — hard rule, not a suggestion)."""

    def __init__(self, user_agent: str, http_timeout_s: float) -> None:
        self._user_agent = user_agent
        self._timeout = http_timeout_s
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._parsers.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                resp = await client.get(f"{host}/robots.txt", timeout=self._timeout)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])  # no robots.txt served -> nothing explicitly disallowed
            except Exception:
                rp.parse([])  # unreachable -> fail open; don't block legitimate RSS polling
            self._parsers[host] = rp
        try:
            return rp.can_fetch(self._user_agent, url)
        except Exception:
            return True


@dataclass
class RSSCollector:
    """Polls every configured feed independently and forwards normalized
    entries onto ``out_queue``. One instance per process, like
    HyperliquidWebSocketCollector."""

    feeds: "list[dict]"
    out_queue: "asyncio.Queue"
    poll_interval_s: float = 60.0
    jitter_s: float = 15.0
    max_concurrent_fetches: int = 5
    http_timeout_s: float = 10.0
    user_agent: str = DEFAULT_USER_AGENT
    reconnect_policy: ReconnectPolicy = field(
        default_factory=lambda: ReconnectPolicy(base_delay_s=5.0, max_delay_s=300.0)
    )

    _stop: bool = field(default=False, init=False, repr=False)
    _seen: "dict[str, BoundedSeenSet]" = field(default_factory=dict, init=False, repr=False)

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """Never raises — one feed's persistent failure must never take
        down the others or the process (section 10)."""
        if not self.feeds:
            logger.info("rss_collector: no feeds configured, nothing to do")
            return
        semaphore = asyncio.Semaphore(max(1, self.max_concurrent_fetches))
        robots = RobotsCache(self.user_agent, self.http_timeout_s)
        # follow_redirects=True: found via live validation (2026-08-18) —
        # httpx defaults to NOT following redirects, and several real feed
        # URLs 301/308-redirect to a canonical path (coindesk.com's feed
        # redirects to the same path without a trailing slash; u.today/rss
        # redirects to u.today/rss.php). Without this, those feeds fail
        # every single poll instead of just following the redirect once.
        async with httpx.AsyncClient(headers={"User-Agent": self.user_agent}, follow_redirects=True) as client:
            tasks = [
                asyncio.create_task(
                    self._poll_feed_forever(client, semaphore, robots, feed), name=f"rss:{feed['name']}"
                )
                for feed in self.feeds
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def poll_feed_once(self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, robots: RobotsCache, feed: dict) -> int:
        """One fetch+parse+enqueue cycle for a single feed. Returns the
        number of new items enqueued. Split out from the forever-loop so it
        can be exercised directly in tests without sleeping."""
        name = feed["name"]
        seen = self._seen.setdefault(name, BoundedSeenSet())
        async with semaphore:
            if not await robots.allowed(client, feed["url"]):
                logger.warning("rss_collector: robots.txt disallows %s (%s) — skipping", name, feed["url"])
                return -1  # sentinel: caller should stop polling this feed permanently
            resp = await client.get(feed["url"], timeout=self.http_timeout_s)
            resp.raise_for_status()
            content = resp.content
        parsed = await asyncio.to_thread(feedparser.parse, content)
        ingestion_time = dt.datetime.now(dt.timezone.utc)
        new_count = 0
        for entry in parsed.get("entries", []) or []:
            event = normalize_entry(feed, entry, ingestion_time)
            if not event["title"] or event["event_uid"] in seen:
                continue
            seen.add(event["event_uid"])
            try:
                self.out_queue.put_nowait({"source": "rss", "ts_received": time.time_ns(), "payload": event})
                new_count += 1
            except asyncio.QueueFull:
                logger.error("news queue full — dropping item from %s (backpressure)", name)
        return new_count

    async def _poll_feed_forever(
        self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, robots: RobotsCache, feed: dict
    ) -> None:
        name = feed["name"]
        attempt = 0
        # Stagger the first poll across feeds so 24 of them don't all fire
        # at t=0 (thundering herd on startup).
        await asyncio.sleep(random.uniform(0, self.jitter_s))
        while not self._stop:
            try:
                new_count = await self.poll_feed_once(client, semaphore, robots, feed)
                if new_count == -1:
                    return  # robots.txt disallowed this feed — stop polling it
                if new_count:
                    logger.info("rss_collector[%s]: %d new item(s)", name, new_count)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = self.reconnect_policy.delay_for_attempt(attempt)
                attempt += 1
                logger.warning("rss_collector[%s]: fetch failed (%s); retrying in %.0fs", name, exc, delay)
                await asyncio.sleep(delay)
                continue
            sleep_s = max(5.0, self.poll_interval_s + random.uniform(-self.jitter_s, self.jitter_s))
            await asyncio.sleep(sleep_s)
