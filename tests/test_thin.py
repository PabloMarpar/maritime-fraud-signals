"""Unit tests for process.thin. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from process import thin

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """rows is a list of (mmsi, timestamp, latitude, longitude, sog, cog, navigational_status,
    ship_type) tuples -- matching the real clean-partition schema, where the field is named
    ``navigational_status`` (process.thin aliases it to ``nav_status`` on read)."""
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, sog DOUBLE, cog DOUBLE, navigational_status VARCHAR, "
            "ship_type VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read(day_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT mmsi, timestamp, latitude, longitude, sog, cog, nav_status, ship_type "
            f"FROM '{day_path.as_posix()}' ORDER BY mmsi, timestamp"
        ).fetchall()
    finally:
        con.close()


def test_one_position_per_bucket_kept(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
            (111, _ts(10, 2), 55.01, 12.01, 10.0, 90.0, "Under way using engine", "Tanker"),
            (111, _ts(10, 4), 55.02, 12.02, 10.0, 90.0, "Under way using engine", "Tanker"),
        ],
    )

    out_paths = thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root)

    rows = _read(out_paths[0])
    assert rows == [(111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")]


def test_each_bucket_keeps_its_own_earliest_position(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
            (111, _ts(10, 4), 55.02, 12.02, 10.0, 90.0, "Under way using engine", "Tanker"),
            (111, _ts(10, 5), 55.03, 12.03, 10.0, 90.0, "Under way using engine", "Tanker"),
        ],
    )

    out_paths = thin.build_thin_tracks(
        DAY, DAY, in_root=in_root, out_root=out_root, interval_minutes=5
    )

    rows = _read(out_paths[0])
    assert rows == [
        (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
        (111, _ts(10, 5), 55.03, 12.03, 10.0, 90.0, "Under way using engine", "Tanker"),
    ]


def test_distinct_mmsi_bucketed_independently(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
            (111, _ts(10, 1), 55.01, 12.01, 10.0, 90.0, "Under way using engine", "Tanker"),
            (222, _ts(10, 0), 56.0, 13.0, 5.0, 180.0, "At anchor", "Cargo"),
        ],
    )

    out_paths = thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root)

    rows = _read(out_paths[0])
    assert rows == [
        (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
        (222, _ts(10, 0), 56.0, 13.0, 5.0, 180.0, "At anchor", "Cargo"),
    ]


def test_writes_one_partition_per_day(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root, DAY, [(111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")]
    )
    _write_clean_partition(
        in_root,
        DAY2,
        [(111, _ts(9, 0, day=DAY2), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")],
    )

    out_paths = thin.build_thin_tracks(DAY, DAY2, in_root=in_root, out_root=out_root)

    assert [p.parent.name for p in out_paths] == [f"date={DAY.isoformat()}", f"date={DAY2.isoformat()}"]
    assert len(_read(out_paths[0])) == 1
    assert len(_read(out_paths[1])) == 1


def test_missing_day_is_skipped_not_fatal(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root, DAY, [(111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")]
    )
    # DAY2 has no clean partition.

    out_paths = thin.build_thin_tracks(DAY, DAY2, in_root=in_root, out_root=out_root)

    assert len(out_paths) == 1
    assert out_paths[0].parent.name == f"date={DAY.isoformat()}"


def test_raises_when_no_day_has_a_clean_partition(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"

    with pytest.raises(FileNotFoundError, match="clean partitions"):
        thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root)


def test_idempotent_by_default_skips_existing_day(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root, DAY, [(111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")]
    )

    first = thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root)
    first_mtime = first[0].stat().st_mtime_ns

    # Append a second position, then rebuild without force -- the existing day is left untouched.
    _write_clean_partition(
        in_root,
        DAY,
        [
            (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
            (222, _ts(11, 0), 56.0, 13.0, 5.0, 180.0, "At anchor", "Cargo"),
        ],
    )
    second = thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root)

    assert second[0].stat().st_mtime_ns == first_mtime
    assert len(_read(second[0])) == 1


def test_force_rebuilds_existing_day(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root, DAY, [(111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")]
    )
    thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root)

    _write_clean_partition(
        in_root,
        DAY,
        [
            (111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker"),
            (222, _ts(11, 0), 56.0, 13.0, 5.0, 180.0, "At anchor", "Cargo"),
        ],
    )
    out_paths = thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root, force=True)

    assert len(_read(out_paths[0])) == 2


def test_provenance_columns_present(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "tracks" / "thin"
    _write_clean_partition(
        in_root, DAY, [(111, _ts(10, 0), 55.0, 12.0, 10.0, 90.0, "Under way using engine", "Tanker")]
    )

    out_paths = thin.build_thin_tracks(DAY, DAY, in_root=in_root, out_root=out_root, interval_minutes=5)

    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT window_start, window_end, interval_minutes, built_at, git_sha "
            f"FROM '{out_paths[0].as_posix()}' LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == DAY
    assert row[1] == DAY
    assert row[2] == 5
    assert row[3] is not None
    assert row[4] is not None
