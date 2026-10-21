"""Generic news aggregator: fans out to configured news APIs / financial
and crypto media, respecting rate limits and ToS (section 20).

Phase 5 (2026-08-18): RSS is the only source actually wired up. The user
was asked explicitly whether to pursue X/Twitter (no free API tier in
2026, ~$35-50+/month minimum) and Reddit (free tier exists but now gated
behind a "Responsible Builder Policy" approval application since June
2026) — both were deferred, not a hardware limitation. `GoogleNewsCollector`
and `SocialCollector` remain Phase-1 stubs; `config/sources.yaml` keeps
`google_news.enabled` / `social.enabled` at `false`. This class exists so
`main.py`'s wiring doesn't need to change when/if those get enabled later
— it already fans out to "every enabled source", there's just one today.
"""
from __future__ import annotations

import asyncio

from src.collectors.rss_collector import RSSCollector


class NewsCollector:
    """See module docstring."""

    def __init__(self, sources_cfg: dict, out_queue: "asyncio.Queue") -> None:
        self.out_queue = out_queue
        polling_cfg = sources_cfg.get("rss_polling", {})
        self.rss_collector = RSSCollector(
            feeds=sources_cfg.get("rss_feeds", []),
            out_queue=out_queue,
            poll_interval_s=polling_cfg.get("poll_interval_s", 60.0),
            jitter_s=polling_cfg.get("jitter_s", 15.0),
            max_concurrent_fetches=polling_cfg.get("max_concurrent_fetches", 5),
            http_timeout_s=polling_cfg.get("http_timeout_s", 10.0),
            user_agent=polling_cfg.get("user_agent", "crypto_radar-research-bot/0.5"),
        )
        # Google News / social are intentionally not instantiated here:
        # both are `enabled: false` in config/sources.yaml, and their
        # collector classes are still Phase-1 NotImplementedError stubs.

    def stop(self) -> None:
        self.rss_collector.stop()

    async def run(self) -> None:
        await self.rss_collector.run()
