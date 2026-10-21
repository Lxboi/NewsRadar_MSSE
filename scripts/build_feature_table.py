"""Batch-builds the Phase 7 feature table (docs/06_features_v1.md) from
already-collected raw tick data (Phase 4's `data/raw/hyperliquid_l2`/
`hyperliquid_trades` Parquet) and scored news (Phase 5/6's `news_events`
SQLite table), and writes it to `data/features/features_v1/` as Parquet.

This is deliberately a one-off script (`scripts/`, per README's project
layout), not something wired into `main.py`'s live asyncio loop:
feature-table building is a batch/research step over history someone
already collected, run whenever you want a fresh table for
notebooks/backtesting, not a continuous process. `python -m src.main` is
still what actually collects the raw data and news this script reads.

Usage (from the project root, venv active):

    python -m scripts.build_feature_table --start 2026-08-18 --end 2026-08-18
    python -m scripts.build_feature_table --start 2026-08-18 --end 2026-08-18 --assets BTC ETH
    python -m scripts.build_feature_table --start 2026-08-18 --end 2026-08-18 --sampling-interval-s 5 --no-targets

Requires raw Parquet data to actually exist for the given date range
(i.e. `python -m src.main` must have run and collected something first) —
this script does not fetch anything itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

sys.path.insert(0, str(PROJECT_ROOT))

from src.features.feature_engine import (  # noqa: E402
    DEFAULT_TARGET_HORIZONS_S,
    DEFAULT_TARGET_THRESHOLDS_BPS,
    FeatureEngine,
    compute_targets,
)
from src.features.replay import build_market_event_stream  # noqa: E402
from src.storage.database import (  # noqa: E402
    get_engine,
    get_sessionmaker,
    init_db,
    list_canonical_news_events,
)
from src.storage.parquet_reader import date_range  # noqa: E402
from src.storage.parquet_store import ParquetStore  # noqa: E402

logger = logging.getLogger("build_feature_table")


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def enabled_assets(assets_cfg: dict) -> "list[str]":
    return [a["symbol"] for a in assets_cfg["assets"] if a.get("enabled")]


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="Start date, YYYY-MM-DD (UTC, inclusive).")
    p.add_argument("--end", required=True, help="End date, YYYY-MM-DD (UTC, inclusive).")
    p.add_argument(
        "--assets", nargs="+", default=None,
        help="Assets to build (default: every enabled asset in config/assets.yaml).",
    )
    p.add_argument("--sampling-interval-s", type=float, default=1.0)
    p.add_argument(
        "--no-targets", action="store_true",
        help="Skip target-variable computation (features only, e.g. for a "
        "live/incomplete window where forward returns aren't available yet).",
    )
    return p.parse_args(argv)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    args = parse_args(argv)

    settings = load_yaml("settings.yaml")
    assets_cfg = load_yaml("assets.yaml")
    storage_cfg = settings["storage"]

    start_date = dt.datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = dt.datetime.strptime(args.end, "%Y-%m-%d").date()
    dates = date_range(start_date, end_date)

    start_ts_ns = int(
        dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000
    )
    end_ts_ns = int(
        dt.datetime.combine(end_date, dt.time.max, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000
    )

    assets = args.assets or enabled_assets(assets_cfg)
    logger.info("Building features for %s, %s..%s (sampling=%ss)", assets, args.start, args.end, args.sampling_interval_s)

    raw_root = PROJECT_ROOT / storage_cfg["raw_dir"]
    features_root = PROJECT_ROOT / storage_cfg["features_dir"]
    sqlite_path = PROJECT_ROOT / storage_cfg["sqlite_path"]
    db_url = f"sqlite:///{sqlite_path.as_posix()}"
    init_db(db_url)
    engine = get_engine(db_url)
    Session = get_sessionmaker(engine)

    with Session() as session:
        news_events = list_canonical_news_events(session)
    logger.info("Loaded %d canonical news events from %s", len(news_events), sqlite_path)

    # Default flush_every (500, tuned for Phase 4's per-message raw writes)
    # would roll a new part file every 500 rows -- at 1s sampling that's a
    # new file roughly every 8 minutes, ~170/asset/day. A batch feature
    # build writes far more predictably than the live raw path, so use a
    # much larger buffer to stay closer to docs/05_parquet_schema.md's
    # "roughly 64-256MB per part file" target instead of one tiny file
    # after another.
    feature_store = ParquetStore(root=features_root, flush_every=20_000)

    total_rows = 0
    for asset in assets:
        logger.info("[%s] reading raw ticks from %s ...", asset, raw_root)
        market_events = build_market_event_stream(raw_root, asset, dates)
        logger.info("[%s] %d raw tick events", asset, len(market_events))

        fe = FeatureEngine(asset=asset, sampling_interval_s=args.sampling_interval_s)
        rows = fe.build_rows(market_events, news_events, start_ts_ns, end_ts_ns)
        logger.info("[%s] %d feature rows built", asset, len(rows))

        if not args.no_targets:
            rows = compute_targets(rows, DEFAULT_TARGET_HORIZONS_S, DEFAULT_TARGET_THRESHOLDS_BPS)

        for row in rows:
            out_row = dict(row)
            out_row["timestamp"] = row["timestamp"].isoformat()
            ts_ns = int(row["timestamp"].timestamp() * 1_000_000_000)
            feature_store.write_row("features_v1", asset, out_row, ts_ns=ts_ns)
        total_rows += len(rows)

    written = feature_store.flush_all()
    logger.info("Done: %d total feature rows across %d asset(s), %d part file(s) written to %s",
                total_rows, len(assets), len(written), features_root)


if __name__ == "__main__":
    main()
