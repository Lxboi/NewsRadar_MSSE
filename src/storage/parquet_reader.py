"""Reads back raw Parquet partitions written by `parquet_store.py`
(Phase 4), for Phase 7's batch feature-table replay (and, later, Phase
10's backtester — both need the same "give me every tick for this asset/
date range, in order" capability).

Deliberately a separate, read-only module rather than a method bolted
onto `ParquetStore`: that class is Phase 4's already-validated writer
(buffered, part-file rolling) and has no business also owning read-back
concerns. Layout matches docs/05_parquet_schema.md exactly:

    <root>/<dataset>/asset=<SYMBOL>/date=<YYYY-MM-DD>/part-NNNNN.parquet
    <root>/<dataset>/date=<YYYY-MM-DD>/part-NNNNN.parquet   (asset=None datasets, e.g. news)

No writing, no state — every function here just reads whatever part
files already exist and hands back plain dicts, sorted by `sort_key` (the
same nanosecond-resolution receipt timestamp every raw row is written
with — see `main.py: persist_l2book_levels`/`persist_trades`).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow.parquet as pq


def read_partition(root: str | Path, dataset: str, date: str, asset: str | None = None) -> list[dict]:
    """Every row from every part file in one `(dataset, asset, date)`
    partition. Empty list (not an error) if the partition doesn't exist —
    a quiet day with no feed activity, or an asset/date with nothing
    collected yet, is a normal, expected case for a research tool."""
    root = Path(root)
    if asset is None:
        partition_dir = root / dataset / f"date={date}"
    else:
        partition_dir = root / dataset / f"asset={asset}" / f"date={date}"
    if not partition_dir.exists():
        return []

    rows: list[dict] = []
    for path in sorted(partition_dir.glob("part-*.parquet")):
        table = pq.read_table(path)
        rows.extend(table.to_pylist())
    return rows


def read_partitions(
    root: str | Path,
    dataset: str,
    dates: "list[str]",
    asset: str | None = None,
    sort_key: str | None = "ts_received_ns",
) -> list[dict]:
    """Same as `read_partition`, across multiple date partitions,
    concatenated and (by default) sorted by `ts_received_ns` so callers get
    one clean chronological stream regardless of how many day-partitions
    or part-files it's spread across. Pass `sort_key=None` to skip
    sorting (e.g. the caller will sort differently, or doesn't care)."""
    rows: list[dict] = []
    for date in dates:
        rows.extend(read_partition(root, dataset, date, asset=asset))
    if sort_key:
        rows.sort(key=lambda r: r.get(sort_key) or 0)
    return rows


def date_range(start: dt.date, end: dt.date) -> list[str]:
    """Inclusive list of `YYYY-MM-DD` strings from `start` to `end` — a
    small convenience since callers will usually have a datetime range,
    not a pre-built date-string list."""
    if end < start:
        raise ValueError("end date must not be before start date")
    days = (end - start).days
    return [(start + dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]
