"""Unit tests for process.clean. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from process import clean

DAY = date(2024, 6, 5)

# Column order matches the confirmed real schema closely enough for the
# columns the cleaning rules touch; other columns are carried through
# untouched so a couple of representative extras (ship_type) are included
# to confirm pass-through behaviour.
COLUMNS = ["timestamp", "mmsi", "latitude", "longitude", "ship_type"]


def _ts(hour: int, minute: int, second: int, day: date = DAY) -> datetime:
    """A naive datetime for a synthetic AIS row.

    Naive on purpose (no noqa'd repeats needed elsewhere): the raw column is
    a plain DuckDB TIMESTAMP, and the DMA source carries no timezone at all.
    """
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _write_raw_partition(tmp_path: Path, day: date, rows: list[tuple]) -> Path:
    """Write a synthetic raw partition as Parquet via DuckDB, Hive-style."""
    raw_root = tmp_path / "raw" / "ais_dk"
    partition_dir = raw_root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE raw (timestamp TIMESTAMP, mmsi BIGINT, latitude DOUBLE, "
            "longitude DOUBLE, ship_type VARCHAR)"
        )
        con.executemany("INSERT INTO raw VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY raw TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return raw_root


def _read_clean(out_root: Path, day: date) -> list[tuple]:
    parquet_path = out_root / f"date={day.isoformat()}" / "part-0.parquet"
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT * FROM '{parquet_path.as_posix()}' ORDER BY mmsi, timestamp"
        ).fetchall()
    finally:
        con.close()


def test_invalid_mmsi_dropped(tmp_path):
    """MMSI outside 200000000-799999999, or null, is dropped."""
    rows = [
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),  # valid
        (_ts(0, 0, 2), 100000000, 55.1, 12.1, "Cargo"),  # coast station range
        (_ts(0, 0, 3), 900000000, 55.2, 12.2, "Cargo"),  # out of range
        (_ts(0, 0, 4), None, 55.3, 12.3, "Cargo"),  # null
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    mmsis = {row[1] for row in result}
    assert mmsis == {219000001}


def test_mmsi_boundary_values_survive(tmp_path):
    """Negative case: the boundary MMSIs 200000000 and 799999999 are valid, not off-by-one dropped."""
    rows = [
        (_ts(0, 0, 1), 200000000, 55.0, 12.0, "Cargo"),
        (_ts(0, 0, 2), 799999999, 55.1, 12.1, "Cargo"),
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    mmsis = {row[1] for row in result}
    assert mmsis == {200000000, 799999999}


def test_impossible_coordinates_dropped(tmp_path):
    """Out-of-range lat/lon and null island (0, 0) are dropped."""
    rows = [
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),  # valid
        (_ts(0, 0, 2), 219000002, 91.0, 12.0, "Cargo"),  # lat out of range
        (_ts(0, 0, 3), 219000003, 55.0, 181.0, "Cargo"),  # lon out of range
        (_ts(0, 0, 4), 219000004, -91.0, 12.0, "Cargo"),  # lat out of range, negative
        (_ts(0, 0, 5), 219000005, 55.0, -181.0, "Cargo"),  # lon out of range, negative
        (_ts(0, 0, 6), 219000006, 0.0, 0.0, "Cargo"),  # null island
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    mmsis = {row[1] for row in result}
    assert mmsis == {219000001}


def test_coordinate_boundary_values_survive(tmp_path):
    """Negative case: (+/-90, +/-180) are physically valid and must survive; only exact (0, 0) is dropped."""
    rows = [
        (_ts(0, 0, 1), 219000001, 90.0, 180.0, "Cargo"),
        (_ts(0, 0, 2), 219000002, -90.0, -180.0, "Cargo"),
        (_ts(0, 0, 3), 219000003, 0.0, 12.0, "Cargo"),  # lat 0, lon not 0: not null island
        (_ts(0, 0, 4), 219000004, 55.0, 0.0, "Cargo"),  # lon 0, lat not 0: not null island
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    mmsis = {row[1] for row in result}
    assert mmsis == {219000001, 219000002, 219000003, 219000004}


def test_duplicate_messages_dropped_keeps_first(tmp_path):
    """Same MMSI, same (timestamp, lat, lon) reported twice: only the first survives."""
    rows = [
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),  # exact duplicate
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),  # exact duplicate, again
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    assert len(result) == 1
    assert result[0][1] == 219000001


def test_same_position_different_timestamp_is_not_a_duplicate(tmp_path):
    """Negative case: a moored vessel reporting the same position at two different times survives both."""
    rows = [
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),
        (_ts(0, 5, 0), 219000001, 55.0, 12.0, "Cargo"),  # different timestamp
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    assert len(result) == 2


def test_different_mmsi_same_position_and_time_is_not_a_duplicate(tmp_path):
    """Negative case: two different vessels reporting the same position/time are not duplicates of each other."""
    rows = [
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),
        (_ts(0, 0, 1), 219000002, 55.0, 12.0, "Tanker"),
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    assert {row[1] for row in result} == {219000001, 219000002}


def test_well_formed_rows_all_survive(tmp_path):
    """Negative case for all three rules at once: nothing is dropped when nothing is wrong."""
    rows = [
        (_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo"),
        (_ts(0, 0, 2), 219000002, 55.1, 12.1, "Tanker"),
        (_ts(0, 0, 3), 219000003, 55.2, 12.2, "Tanker"),
    ]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    assert len(result) == 3


def test_other_columns_pass_through_untouched(tmp_path):
    """Columns not touched by any rule (e.g. ship_type) survive with their original values."""
    rows = [(_ts(0, 0, 1), 219000001, 55.0, 12.0, "Fishing")]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    result = _read_clean(out_root, DAY)
    assert result[0][4] == "Fishing"


def test_clean_day_is_idempotent_by_default(tmp_path, caplog):
    """Re-running without force must not re-run the cleaning query."""
    rows = [(_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo")]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    first = clean.clean_day(DAY, in_root=raw_root, out_root=out_root)
    first_mtime = first.stat().st_mtime_ns

    second = clean.clean_day(DAY, in_root=raw_root, out_root=out_root)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite the file"


def test_clean_day_force_recleans(tmp_path):
    rows = [(_ts(0, 0, 1), 219000001, 55.0, 12.0, "Cargo")]
    raw_root = _write_raw_partition(tmp_path, DAY, rows)
    out_root = tmp_path / "clean" / "ais_dk"

    clean.clean_day(DAY, in_root=raw_root, out_root=out_root)
    result = clean.clean_day(DAY, in_root=raw_root, out_root=out_root, force=True)

    assert result.exists()


def test_clean_day_raises_when_raw_partition_missing(tmp_path):
    raw_root = tmp_path / "raw" / "ais_dk"
    out_root = tmp_path / "clean" / "ais_dk"

    with pytest.raises(FileNotFoundError):
        clean.clean_day(DAY, in_root=raw_root, out_root=out_root)


def test_clean_range_covers_every_day(tmp_path):
    raw_root = tmp_path / "raw" / "ais_dk"
    out_root = tmp_path / "clean" / "ais_dk"
    for day in (date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)):
        partition_dir = raw_root / f"date={day.isoformat()}"
        partition_dir.mkdir(parents=True)
        con = duckdb.connect()
        try:
            con.execute(
                "CREATE TABLE raw (timestamp TIMESTAMP, mmsi BIGINT, latitude DOUBLE, "
                "longitude DOUBLE, ship_type VARCHAR)"
            )
            con.execute(
                "INSERT INTO raw VALUES (?, ?, ?, ?, ?)",
                [_ts(0, 0, 1, day=day), 219000001, 55.0, 12.0, "Cargo"],
            )
            con.execute(f"COPY raw TO '{(partition_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
        finally:
            con.close()

    results = clean.clean_range(date(2024, 6, 5), date(2024, 6, 7), in_root=raw_root, out_root=out_root)

    assert len(results) == 3
    for path in results:
        assert path.exists()
    assert {p.parent.name for p in results} == {
        "date=2024-06-05",
        "date=2024-06-06",
        "date=2024-06-07",
    }
