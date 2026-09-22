"""Unit tests for process.partitions. All fixtures are synthetic, written to tmp_path."""

from __future__ import annotations

from datetime import date

from process import partitions

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
DAY3 = date(2024, 6, 7)


def test_daterange_is_inclusive_of_both_ends():
    assert list(partitions.daterange(DAY, DAY3)) == [DAY, DAY2, DAY3]


def test_daterange_single_day():
    assert list(partitions.daterange(DAY, DAY)) == [DAY]


def test_partition_path_shape(tmp_path):
    path = partitions.partition_path(DAY, tmp_path)
    assert path == tmp_path / "date=2024-06-05" / "part-0.parquet"


def test_existing_partitions_finds_only_days_present(tmp_path):
    for day in (DAY, DAY3):
        partition_dir = tmp_path / f"date={day.isoformat()}"
        partition_dir.mkdir(parents=True)
        (partition_dir / "part-0.parquet").touch()

    found = partitions.existing_partitions(DAY, DAY3, tmp_path)

    assert [day for day, _path in found] == [DAY, DAY3]


def test_existing_partitions_warns_on_missing_day(tmp_path, caplog):
    partition_dir = tmp_path / f"date={DAY.isoformat()}"
    partition_dir.mkdir(parents=True)
    (partition_dir / "part-0.parquet").touch()

    with caplog.at_level("WARNING"):
        found = partitions.existing_partitions(DAY, DAY2, tmp_path)

    assert [day for day, _path in found] == [DAY]
    assert "No clean partition for 2024-06-06" in caplog.text


def test_existing_partitions_empty_when_nothing_exists(tmp_path):
    assert partitions.existing_partitions(DAY, DAY3, tmp_path) == []


def test_window_partition_path_shape(tmp_path):
    path = partitions.window_partition_path(DAY, DAY3, tmp_path)
    assert path == tmp_path / "window=2024-06-05_2024-06-07" / "part-0.parquet"


def test_window_partition_path_differs_for_different_windows(tmp_path):
    a = partitions.window_partition_path(DAY, DAY2, tmp_path)
    b = partitions.window_partition_path(DAY, DAY3, tmp_path)
    assert a != b


def test_git_sha_returns_nonempty_string():
    assert isinstance(partitions.git_sha(), str)
    assert partitions.git_sha() != ""


def test_atomic_write_parquet_writes_no_tmp_file_left_behind(tmp_path):
    import duckdb

    con = duckdb.connect()
    try:
        out_path = tmp_path / "window=2024-06-05_2024-06-07" / "part-0.parquet"
        partitions.atomic_write_parquet(con, "SELECT 1 AS x, 2 AS y", out_path)
        assert out_path.exists()
        assert not out_path.with_suffix(".tmp").exists()
        # Not `SELECT *`: DuckDB auto-detects the `window=...` Hive-style directory name and
        # injects an extra `window` column even for a single explicit file path, not just a
        # glob -- see the module docstring note this test guards.
        rows = con.execute(f"SELECT x, y FROM read_parquet('{out_path.as_posix()}')").fetchall()
        assert rows == [(1, 2)]
    finally:
        con.close()


def test_partition_exists_true_for_literal_file(tmp_path):
    path = tmp_path / "part-0.parquet"
    path.touch()
    assert partitions.partition_exists(path) is True


def test_partition_exists_false_for_missing_literal_file(tmp_path):
    assert partitions.partition_exists(tmp_path / "missing.parquet") is False


def test_partition_exists_true_for_glob_with_a_match(tmp_path):
    (tmp_path / "window=2024-06-01_2024-06-10").mkdir()
    (tmp_path / "window=2024-06-01_2024-06-10" / "part-0.parquet").touch()
    pattern = tmp_path / "window=*" / "part-0.parquet"
    assert partitions.partition_exists(pattern) is True


def test_partition_exists_false_for_glob_with_no_match(tmp_path):
    pattern = tmp_path / "window=*" / "part-0.parquet"
    assert partitions.partition_exists(pattern) is False


def test_atomic_write_parquet_creates_parent_dirs(tmp_path):
    import duckdb

    con = duckdb.connect()
    try:
        out_path = tmp_path / "nested" / "deeper" / "part-0.parquet"
        partitions.atomic_write_parquet(con, "SELECT 1 AS x", out_path)
        assert out_path.exists()
    finally:
        con.close()
