"""Phase 4 tests: ParquetStore buffering, flushing, and partition layout.
Uses pytest's tmp_path fixture — no shared state, no network.
See docs/09_tests_plan.md."""
import pyarrow.parquet as pq
import pytest

from src.storage.parquet_store import ParquetStore


def test_write_row_buffers_without_flushing_below_threshold(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=10)
    store.write_row("hyperliquid_trades", "BTC", {"price": 100.0}, ts_ns=1_700_000_000_000_000_000)
    assert store.pending_row_count() == 1
    assert not list(tmp_path.rglob("*.parquet"))  # nothing written yet


def test_flush_writes_a_part_file_with_correct_partition_path(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    ts_ns = 1_700_000_000_000_000_000  # 2023-11-14 in UTC
    store.write_row("hyperliquid_trades", "BTC", {"price": 100.0, "size": 1.0}, ts_ns=ts_ns)
    store.flush_all()

    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1
    path = files[0]
    assert "hyperliquid_trades" in str(path)
    assert "asset=BTC" in str(path)
    assert "date=2023-11-14" in str(path)


def test_auto_flush_at_threshold(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=3)
    for i in range(3):
        store.write_row("hyperliquid_l2", "ETH", {"price": 100.0 + i}, ts_ns=1_700_000_000_000_000_000)
    # Should have auto-flushed exactly once at the 3rd row.
    assert store.pending_row_count() == 0
    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1
    table = pq.read_table(files[0])
    assert table.num_rows == 3


def test_different_assets_and_datasets_get_separate_partitions(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    ts_ns = 1_700_000_000_000_000_000
    store.write_row("hyperliquid_l2", "BTC", {"price": 1.0}, ts_ns=ts_ns)
    store.write_row("hyperliquid_l2", "ETH", {"price": 2.0}, ts_ns=ts_ns)
    store.write_row("hyperliquid_trades", "BTC", {"price": 3.0}, ts_ns=ts_ns)
    store.flush_all()

    files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")}
    assert any("hyperliquid_l2/asset=BTC" in f for f in files)
    assert any("hyperliquid_l2/asset=ETH" in f for f in files)
    assert any("hyperliquid_trades/asset=BTC" in f for f in files)


def test_flush_all_clears_pending_rows(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    store.write_row("hyperliquid_trades", "SOL", {"price": 1.0}, ts_ns=1_700_000_000_000_000_000)
    assert store.pending_row_count() > 0
    store.flush_all()
    assert store.pending_row_count() == 0


def test_flush_with_no_pending_rows_is_a_noop(tmp_path):
    store = ParquetStore(root=tmp_path)
    result = store.flush(("hyperliquid_trades", "BTC", "2023-11-14"))
    assert result is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_part_file_numbering_increments_across_flushes(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=1)
    ts_ns = 1_700_000_000_000_000_000
    store.write_row("hyperliquid_trades", "BTC", {"price": 1.0}, ts_ns=ts_ns)  # auto-flush -> part-00000
    store.write_row("hyperliquid_trades", "BTC", {"price": 2.0}, ts_ns=ts_ns)  # auto-flush -> part-00001

    files = sorted(p.name for p in tmp_path.rglob("*.parquet"))
    assert files == ["part-00000.parquet", "part-00001.parquet"]


def test_write_row_defaults_to_now_without_explicit_ts(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=1)
    # Should not raise, and should partition under *some* today-ish date dir.
    store.write_row("hyperliquid_trades", "BTC", {"price": 1.0})
    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1
    assert "date=" in str(files[0])


# --- Phase 5: asset=None support for non-per-asset datasets (news) ---


def test_write_row_with_asset_none_skips_the_asset_partition_segment(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    ts_ns = 1_700_000_000_000_000_000
    store.write_row("news_raw", None, {"title": "Fed holds rates"}, ts_ns=ts_ns)
    store.flush_all()

    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 1
    path_str = files[0].relative_to(tmp_path).as_posix()
    assert path_str.startswith("news_raw/date=2023-11-14/")
    assert "asset=" not in path_str


def test_asset_none_and_asset_keyed_datasets_coexist(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    ts_ns = 1_700_000_000_000_000_000
    store.write_row("news_raw", None, {"title": "some article"}, ts_ns=ts_ns)
    store.write_row("hyperliquid_trades", "BTC", {"price": 1.0}, ts_ns=ts_ns)
    store.flush_all()

    files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.parquet")}
    assert any(f.startswith("news_raw/date=") and "asset=" not in f for f in files)
    assert any("hyperliquid_trades/asset=BTC" in f for f in files)
