"""Unit tests for process.tracks. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from process import tracks

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    """A naive datetime for a synthetic AIS row, matching test_clean.py's helper."""
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, longitude DOUBLE)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read_points(points_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT mmsi, timestamp, latitude, longitude, voyage_seq, voyage_id "
            f"FROM '{points_path.as_posix()}' ORDER BY mmsi, timestamp"
        ).fetchall()
    finally:
        con.close()


def _read_voyages(voyages_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT mmsi, voyage_seq, voyage_id, start_time, end_time, "
            "start_latitude, start_longitude, end_latitude, end_longitude, "
            "point_count, duration_seconds "
            f"FROM '{voyages_path.as_posix()}' ORDER BY mmsi, voyage_seq"
        ).fetchall()
    finally:
        con.close()


def test_continuous_track_stays_one_voyage(tmp_path):
    """Negative case: closely-spaced points from one vessel must not be split."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000001, _ts(0, 0), 55.0, 12.0),
        (219000001, _ts(0, 10), 55.01, 12.01),
        (219000001, _ts(0, 20), 55.02, 12.02),
        (219000001, _ts(0, 30), 55.03, 12.03),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "tracks"

    points_path, voyages_path = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    points = _read_points(points_path)
    assert len(points) == 4
    assert {row[4] for row in points} == {1}  # voyage_seq
    assert {row[5] for row in points} == {"219000001-1"}  # voyage_id

    voyages = _read_voyages(voyages_path)
    assert len(voyages) == 1


def test_large_time_gap_splits_into_two_voyages(tmp_path):
    """A gap bigger than the threshold starts a new voyage for the same vessel."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000002, _ts(0, 0), 55.0, 12.0),
        (219000002, _ts(0, 10), 55.01, 12.01),
        (219000002, _ts(12, 0), 55.5, 12.5),  # 11h50m later, past the 6h default
        (219000002, _ts(12, 10), 55.51, 12.51),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "tracks"

    points_path, voyages_path = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    points = _read_points(points_path)
    voyage_seqs = [row[4] for row in points]
    assert voyage_seqs == [1, 1, 2, 2]

    voyages = _read_voyages(voyages_path)
    assert len(voyages) == 2
    assert {row[9] for row in voyages} == {2}  # point_count, both voyages have 2 points


def test_gap_below_threshold_does_not_split(tmp_path):
    """Negative case: a gap that exists but stays under the threshold is not a boundary."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000003, _ts(0, 0), 55.0, 12.0),
        (219000003, _ts(3, 0), 55.1, 12.1),  # 3h later, under the 6h default
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "tracks"

    points_path, _voyages_path = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    points = _read_points(points_path)
    assert {row[4] for row in points} == {1}


def test_custom_gap_hours_overrides_default(tmp_path):
    """A caller-supplied gap_hours changes where the boundary falls."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000004, _ts(0, 0), 55.0, 12.0),
        (219000004, _ts(3, 0), 55.1, 12.1),  # 3h later
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "tracks"

    points_path, _voyages_path = tracks.reconstruct_range(
        DAY, DAY, in_root=in_root, out_root=out_root, gap_hours=1.0
    )

    points = _read_points(points_path)
    assert [row[4] for row in points] == [1, 2]


def test_multiple_vessels_segmented_independently(tmp_path):
    """Different MMSIs must not influence each other's voyage boundaries or numbering."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000005, _ts(0, 0), 55.0, 12.0),
        (219000005, _ts(0, 10), 55.01, 12.01),
        (219000005, _ts(12, 0), 55.5, 12.5),  # splits vessel A into 2 voyages
        (219000006, _ts(1, 0), 56.0, 13.0),
        (219000006, _ts(1, 10), 56.01, 13.01),  # vessel B stays continuous
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "tracks"

    points_path, voyages_path = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    points = _read_points(points_path)
    vessel_a = [row for row in points if row[0] == 219000005]
    vessel_b = [row for row in points if row[0] == 219000006]
    assert [row[4] for row in vessel_a] == [1, 1, 2]
    assert [row[4] for row in vessel_b] == [1, 1]

    voyages = _read_voyages(voyages_path)
    assert len(voyages) == 3  # 2 for vessel A, 1 for vessel B
    a_voyages = {row[1] for row in voyages if row[0] == 219000005}
    b_voyages = {row[1] for row in voyages if row[0] == 219000006}
    assert a_voyages == {1, 2}
    assert b_voyages == {1}


def test_voyage_summary_matches_input_exactly(tmp_path):
    """start/end time, position and point_count of a simple single voyage must match the input."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000007, _ts(0, 0), 55.0, 12.0),
        (219000007, _ts(0, 10), 55.1, 12.1),
        (219000007, _ts(0, 20), 55.2, 12.2),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "tracks"

    _points_path, voyages_path = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    voyages = _read_voyages(voyages_path)
    assert len(voyages) == 1
    (
        mmsi,
        voyage_seq,
        voyage_id,
        start_time,
        end_time,
        start_lat,
        start_lon,
        end_lat,
        end_lon,
        point_count,
        duration_seconds,
    ) = voyages[0]
    assert mmsi == 219000007
    assert voyage_seq == 1
    assert voyage_id == "219000007-1"
    assert start_time == _ts(0, 0)
    assert end_time == _ts(0, 20)
    assert (start_lat, start_lon) == (55.0, 12.0)
    assert (end_lat, end_lon) == (55.2, 12.2)
    assert point_count == 3
    assert duration_seconds == 1200  # 20 minutes


def test_voyage_spans_a_day_boundary(tmp_path):
    """A voyage continuing across midnight is not artificially split by the day partition."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000008, _ts(23, 0, day=DAY), 55.0, 12.0)])
    _write_clean_partition(in_root, DAY2, [(219000008, _ts(1, 0, day=DAY2), 55.1, 12.1)])
    out_root = tmp_path / "tracks"

    _points_path, voyages_path = tracks.reconstruct_range(DAY, DAY2, in_root=in_root, out_root=out_root)

    voyages = _read_voyages(voyages_path)
    assert len(voyages) == 1
    assert voyages[0][9] == 2  # point_count


def test_reconstruct_range_skips_missing_day_with_warning(tmp_path, caplog):
    """A day with no clean partition in the middle of a range is skipped, not fatal."""
    in_root = tmp_path / "clean" / "ais_dk"
    day3 = date(2024, 6, 7)
    _write_clean_partition(in_root, DAY, [(219000009, _ts(0, 0), 55.0, 12.0)])
    # DAY2 deliberately missing.
    _write_clean_partition(in_root, day3, [(219000009, _ts(0, 0, day=day3), 55.0, 12.0)])
    out_root = tmp_path / "tracks"

    with caplog.at_level("WARNING"):
        tracks.reconstruct_range(DAY, day3, in_root=in_root, out_root=out_root)

    assert any("2024-06-06" in record.message for record in caplog.records)


def test_reconstruct_range_raises_when_no_partitions_exist(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks"

    with pytest.raises(FileNotFoundError):
        tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)


def test_reconstruct_range_is_idempotent_by_default(tmp_path):
    """Re-running without force must not rebuild the outputs."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000010, _ts(0, 0), 55.0, 12.0)])
    out_root = tmp_path / "tracks"

    points_first, voyages_first = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)
    points_mtime = points_first.stat().st_mtime_ns
    voyages_mtime = voyages_first.stat().st_mtime_ns

    points_second, voyages_second = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    assert points_second == points_first
    assert voyages_second == voyages_first
    assert points_second.stat().st_mtime_ns == points_mtime, "re-running without --force must not rewrite the file"
    assert voyages_second.stat().st_mtime_ns == voyages_mtime, "re-running without --force must not rewrite the file"


def test_reconstruct_range_force_rebuilds(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000011, _ts(0, 0), 55.0, 12.0)])
    out_root = tmp_path / "tracks"

    tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)
    points_path, voyages_path = tracks.reconstruct_range(
        DAY, DAY, in_root=in_root, out_root=out_root, force=True
    )

    assert points_path.exists()
    assert voyages_path.exists()


def test_single_point_voyage_has_zero_duration(tmp_path):
    """Negative case: a lone message forms a valid one-point voyage, not an error."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000012, _ts(5, 0), 55.0, 12.0)])
    out_root = tmp_path / "tracks"

    _points_path, voyages_path = tracks.reconstruct_range(DAY, DAY, in_root=in_root, out_root=out_root)

    voyages = _read_voyages(voyages_path)
    assert len(voyages) == 1
    assert voyages[0][9] == 1  # point_count
    assert voyages[0][10] == 0  # duration_seconds
