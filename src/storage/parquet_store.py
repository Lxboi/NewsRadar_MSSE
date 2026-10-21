"""Parquet writer for raw/processed market data (Phase 4, section 60).

Buffers rows in memory per `(dataset, asset, date)` partition and flushes
to a new part file once the buffer reaches `flush_every` rows, or on an
explicit `flush()`/`flush_all()` call (e.g. at shutdown). Layout matches
docs/05_parquet_schema.md exactly:

    <root>/<dataset>/asset=<SYMBOL>/date=<YYYY-MM-DD>/part-NNNNN.parquet

Deliberately simple — no compaction, no schema registry, no async I/O.
That's the Phase 4 bar ("get data landing on disk reliably"), not a data
warehouse. `main.py` calls `write_row()` synchronously from the same
asyncio task that consumes the raw queue; pyarrow's write is fast enough
at this row/second scale that it hasn't needed a dedicated executor
thread — revisit if profiling ever says otherwise.

Phase 5 addition: `asset` may be `None` for datasets that aren't
per-asset by nature — news isn't tied to a single symbol the way an
order book is, and `docs/05_parquet_schema.md`'s `news_raw`/
`processed/news_events` layout was specified without an `asset=` segment
from the start. When `asset is None`, the partition path drops that
segment instead of writing a literal `asset=None` directory:

    <root>/<dataset>/date=<YYYY-MM-DD>/part-NNNNN.parquet

Existing asset-keyed datasets (`hyperliquid_l2`, `hyperliquid_trades`)
are unaffected — this is purely additive.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


class ParquetStore:
    def __init__(self, root: str | Path, flush_every: int = 500) -> None:
        self.root = Path(root)
        self.flush_every = flush_every
        self._buffers: dict[tuple[str, str | None, str], list[dict]] = defaultdict(list)
        self._part_counter: dict[tuple[str, str | None, str], int] = defaultdict(int)

    def write_row(self, dataset: str, asset: str | None, row: dict, ts_ns: int | None = None) -> None:
        """`ts_ns` (nanoseconds since epoch) determines the date partition;
        defaults to now (UTC) if not given. `asset=None` is valid for
        datasets with no natural per-asset partition (e.g. news) — see the
        module docstring."""
        date_str = self._date_for(ts_ns)
        key = (dataset, asset, date_str)
        self._buffers[key].append(row)
        if len(self._buffers[key]) >= self.flush_every:
            self.flush(key)

    def flush(self, key: tuple[str, str | None, str]) -> Path | None:
        rows = self._buffers.get(key)
        if not rows:
            return None
        dataset, asset, date_str = key
        if asset is None:
            partition_dir = self.root / dataset / f"date={date_str}"
        else:
            partition_dir = self.root / dataset / f"asset={asset}" / f"date={date_str}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        part_idx = self._part_counter[key]
        self._part_counter[key] += 1
        path = partition_dir / f"part-{part_idx:05d}.parquet"

        table = pa.Table.from_pylist(rows)
        pq.write_table(table, path, compression="zstd")

        self._buffers[key] = []
        return path

    def flush_all(self) -> list[Path]:
        written = []
        for key in list(self._buffers.keys()):
            path = self.flush(key)
            if path is not None:
                written.append(path)
        return written

    def pending_row_count(self, key: tuple[str, str | None, str] | None = None) -> int:
        if key is not None:
            return len(self._buffers.get(key, []))
        return sum(len(rows) for rows in self._buffers.values())

    @staticmethod
    def _date_for(ts_ns: int | None) -> str:
        if ts_ns:
            d = dt.datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=dt.timezone.utc)
        else:
            d = dt.datetime.now(dt.timezone.utc)
        return d.strftime("%Y-%m-%d")
