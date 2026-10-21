"""Phase 7 tests: parquet_reader.py (read-back counterpart to Phase 4's
ParquetStore writer). Real Parquet I/O against tmp_path, no network.
See docs/09_tests_plan.md."""
import datetime as dt

from src.storage.parquet_reader import date_range, read_partition, read_partitions
from src.storage.parquet_store import ParquetStore


def test_read_partition_round_trips_written_rows(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=2)
    store.write_row("hyperliquid_l2", "BTC", {"ts_received_ns": 1, "price": 100.0}, ts_ns=1)
    store.write_row("hyperliquid_l2", "BTC", {"ts_received_ns": 2, "price": 101.0}, ts_ns=2)
    store.flush_all()

    # ts_ns=1/2 (epoch nanoseconds) both fall on 1970-01-01 — the date
    # ParquetStore actually partitions them under.
    rows = read_partition(tmp_path, "hyperliquid_l2", "1970-01-01", asset="BTC")

    assert len(rows) == 2
    assert {r["ts_received_ns"] for r in rows} == {1, 2}


def test_read_partition_missing_returns_empty_list(tmp_path):
    rows = read_partition(tmp_path, "hyperliquid_l2", "2020-01-01", asset="BTC")
    assert rows == []


def test_read_partition_supports_asset_none_datasets(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=1)
    store.write_row("news_raw", None, {"ts_received_ns": 1, "title": "x"}, ts_ns=1)
    store.flush_all()

    rows = read_partition(tmp_path, "news_raw", "1970-01-01", asset=None)
    assert len(rows) == 1
    assert not (tmp_path / "news_raw" / "asset=None").exists()


def test_read_partitions_concatenates_and_sorts_across_dates(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=1)
    # ts_ns from two different days, written out of order.
    day1_ns = int(dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc).timestamp() * 1e9) + 5
    day2_ns = int(dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc).timestamp() * 1e9) + 3
    store.write_row("hyperliquid_l2", "BTC", {"ts_received_ns": day2_ns}, ts_ns=day2_ns)
    store.write_row("hyperliquid_l2", "BTC", {"ts_received_ns": day1_ns}, ts_ns=day1_ns)
    store.flush_all()

    rows = read_partitions(tmp_path, "hyperliquid_l2", ["2026-08-17", "2026-08-18"], asset="BTC")

    assert [r["ts_received_ns"] for r in rows] == [day1_ns, day2_ns]


def test_read_partitions_sort_key_none_preserves_read_order(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=1)
    store.write_row("hyperliquid_l2", "BTC", {"ts_received_ns": 99}, ts_ns=99)
    store.flush_all()

    rows = read_partitions(
        tmp_path, "hyperliquid_l2", ["1970-01-01"], asset="BTC", sort_key=None,
    )
    assert len(rows) == 1


def test_date_range_inclusive():
    result = date_range(dt.date(2026, 8, 17), dt.date(2026, 8, 19))
    assert result == ["2026-08-17", "2026-08-18", "2026-08-19"]


def test_date_range_single_day():
    result = date_range(dt.date(2026, 8, 17), dt.date(2026, 8, 17))
    assert result == ["2026-08-17"]


def test_date_range_rejects_end_before_start():
    import pytest

    with pytest.raises(ValueError):
        date_range(dt.date(2026, 8, 18), dt.date(2026, 8, 17))
