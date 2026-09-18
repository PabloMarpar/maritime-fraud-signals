"""Unit tests for detect.spoofing. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from detect import spoofing

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_voyages(path: Path, rows: list[tuple]) -> None:
    """Write a synthetic voyages.parquet.

    rows is a list of (mmsi, voyage_seq, voyage_id, start_time, end_time, start_latitude,
    start_longitude, end_latitude, end_longitude, point_count, duration_seconds) tuples.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE voyages (mmsi BIGINT, voyage_seq BIGINT, voyage_id VARCHAR, "
            "start_time TIMESTAMP, end_time TIMESTAMP, start_latitude DOUBLE, "
            "start_longitude DOUBLE, end_latitude DOUBLE, end_longitude DOUBLE, "
            "point_count BIGINT, duration_seconds BIGINT)"
        )
        con.executemany("INSERT INTO voyages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY voyages TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_land(path: Path, polygon_wkt: str) -> None:
    """Write a synthetic land.parquet with a single polygon, same shape as ingest.landmask's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        con.execute(
            "CREATE TABLE land AS SELECT 1 AS OGC_FID, "
            f"ST_GeomFromText('{polygon_wkt}') AS geom"
        )
        con.execute(f"COPY land TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_empty_land(path: Path) -> None:
    """A land mask far from every test's other coordinates, so it never spuriously matches."""
    _write_land(path, "POLYGON((170 80, 170 81, 171 81, 171 80, 170 80))")


def _write_empty_voyages(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE voyages (mmsi BIGINT, voyage_seq BIGINT, voyage_id VARCHAR, "
            "start_time TIMESTAMP, end_time TIMESTAMP, start_latitude DOUBLE, "
            "start_longitude DOUBLE, end_latitude DOUBLE, end_longitude DOUBLE, "
            "point_count BIGINT, duration_seconds BIGINT)"
        )
        con.execute(f"COPY voyages TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _circle_points(
    lat0: float,
    lon0: float,
    radius_m: float,
    n: int,
    start: datetime,
    step_seconds: float = 120.0,
) -> list[tuple[datetime, float, float]]:
    """Generate n points evenly spaced around a full 360-degree circle centred at (lat0, lon0)."""
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
    xs = radius_m * np.cos(angles)
    ys = radius_m * np.sin(angles)
    lats = lat0 + ys / spoofing.METERS_PER_DEG_LAT
    lons = lon0 + xs / (spoofing.METERS_PER_DEG_LAT * math.cos(math.radians(lat0)))
    return [
        (start + timedelta(seconds=step_seconds * i), float(lats[i]), float(lons[i]))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Check 1: impossible speed
# ---------------------------------------------------------------------------


def test_impossible_speed_is_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    root = tmp_path / "clean"
    _write_clean_partition(
        root,
        DAY,
        [
            (111, _ts(0, 0, 0), 55.0, 12.0),
            # ~10km away, 60s later -> implied speed ~324kn.
            (111, _ts(0, 1, 0), 55.09, 12.0),
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_impossible_speed(con)

    assert len(events) == 1
    event = events[0]
    assert event.kind == "impossible_speed"
    assert event.mmsi == 111
    assert event.evidence_value == pytest.approx(324.0, rel=0.05)
    assert 0.0 <= event.confidence <= 1.0
    con.close()


def test_plausible_speed_is_not_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    root = tmp_path / "clean"
    # ~15 knots over 10 minutes: 15 * 1852 / 3600 m/s * 600s ~= 4630m.
    _write_clean_partition(
        root,
        DAY,
        [
            (222, _ts(0, 0, 0), 55.0, 12.0),
            (222, _ts(0, 10, 0), 55.0417, 12.0),
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_impossible_speed(con)

    assert events == []
    con.close()


def test_zero_time_diff_is_not_flagged_by_speed_check(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    root = tmp_path / "clean"
    _write_clean_partition(
        root,
        DAY,
        [
            (333, _ts(0, 0, 0), 55.0, 12.0),
            (333, _ts(0, 0, 0), 56.0, 13.0),  # same timestamp, far apart -> check 4's job
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_impossible_speed(con)  # must not crash on division by zero

    assert events == []
    con.close()


# ---------------------------------------------------------------------------
# Check 2: on land
# ---------------------------------------------------------------------------

LAND_POLYGON = "POLYGON((10 55, 10 56, 11 56, 11 55, 10 55))"


def test_position_solidly_inland_is_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    land_path = tmp_path / "land.parquet"
    _write_land(land_path, LAND_POLYGON)
    root = tmp_path / "clean"
    _write_clean_partition(root, DAY, [(444, _ts(0), 55.5, 10.5)])
    partitions = [(DAY, root / "date=2024-06-05" / "part-0.parquet")]
    spoofing._build_all_days(con, partitions)

    events = spoofing.check_on_land(con, partitions, land_path)

    assert len(events) == 1
    assert events[0].kind == "on_land"
    assert events[0].mmsi == 444
    assert events[0].evidence_value == 1.0
    con.close()


def test_position_outside_land_polygon_is_not_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    land_path = tmp_path / "land.parquet"
    _write_land(land_path, LAND_POLYGON)
    root = tmp_path / "clean"
    _write_clean_partition(root, DAY, [(555, _ts(0), 57.3, 11.3)])
    partitions = [(DAY, root / "date=2024-06-05" / "part-0.parquet")]
    spoofing._build_all_days(con, partitions)

    events = spoofing.check_on_land(con, partitions, land_path)

    assert events == []
    con.close()


def test_position_near_boundary_within_erosion_buffer_is_not_flagged(tmp_path):
    """The important test: a point nominally inside the RAW polygon, but within
    COASTAL_EROSION_DEG of its edge, must not be flagged -- the erosion buffer must actually apply.
    """
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    land_path = tmp_path / "land.parquet"
    _write_land(land_path, LAND_POLYGON)
    root = tmp_path / "clean"
    # 0.005 degrees inside the western edge (x=10) -- inside the raw polygon, inside the
    # COASTAL_EROSION_DEG=0.01 buffer, so the eroded polygon does not reach this point.
    _write_clean_partition(root, DAY, [(666, _ts(0), 55.5, 10.005)])
    partitions = [(DAY, root / "date=2024-06-05" / "part-0.parquet")]
    spoofing._build_all_days(con, partitions)

    events = spoofing.check_on_land(con, partitions, land_path)

    assert events == [], "erosion buffer was not applied -- raw-polygon containment used instead"
    con.close()


# ---------------------------------------------------------------------------
# Check 3: synthetic circles
# ---------------------------------------------------------------------------


def test_synthetic_circle_is_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    lat0, lon0 = 55.0, 12.0
    start = _ts(0)
    n = 40
    points = _circle_points(lat0, lon0, 1000.0, n, start)
    end = points[-1][0]

    root = tmp_path / "clean"
    _write_clean_partition(
        root, DAY, [(777, ts, lat, lon) for ts, lat, lon in points]
    )
    voyages_path = tmp_path / "voyages.parquet"
    _write_voyages(
        voyages_path,
        [
            (
                777,
                1,
                "777-1",
                start,
                end,
                lat0,
                lon0,
                points[-1][1],
                points[-1][2],
                n,
                int((end - start).total_seconds()),
            )
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_synthetic_circles(con, voyages_path, DAY, DAY2)

    assert len(events) == 1
    assert events[0].kind == "synthetic_circle"
    assert events[0].mmsi == 777
    assert events[0].evidence_value <= spoofing.CIRCLE_RESIDUAL_RATIO_MAX
    con.close()


def test_straight_line_voyage_is_not_flagged_as_circle(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    lat0, lon0 = 55.0, 12.0
    start = _ts(0)
    n = 40
    points = [
        (start + timedelta(seconds=120 * i), lat0 + 0.001 * i, lon0)
        for i in range(n)
    ]
    end = points[-1][0]

    root = tmp_path / "clean"
    _write_clean_partition(root, DAY, [(888, ts, lat, lon) for ts, lat, lon in points])
    voyages_path = tmp_path / "voyages.parquet"
    _write_voyages(
        voyages_path,
        [
            (
                888,
                1,
                "888-1",
                start,
                end,
                lat0,
                lon0,
                points[-1][1],
                points[-1][2],
                n,
                int((end - start).total_seconds()),
            )
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_synthetic_circles(con, voyages_path, DAY, DAY2)

    assert events == []
    con.close()


def test_voyage_below_min_points_is_prefiltered_even_if_circular(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    lat0, lon0 = 55.0, 12.0
    start = _ts(0)
    n = spoofing.MIN_POINTS_FOR_CIRCLE - 1  # below the pre-filter threshold
    points = _circle_points(lat0, lon0, 1000.0, n, start)
    end = points[-1][0]

    root = tmp_path / "clean"
    _write_clean_partition(root, DAY, [(999, ts, lat, lon) for ts, lat, lon in points])
    voyages_path = tmp_path / "voyages.parquet"
    _write_voyages(
        voyages_path,
        [
            (
                999,
                1,
                "999-1",
                start,
                end,
                lat0,
                lon0,
                points[-1][1],
                points[-1][2],
                n,
                int((end - start).total_seconds()),
            )
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_synthetic_circles(con, voyages_path, DAY, DAY2)

    assert events == [], "voyage below MIN_POINTS_FOR_CIRCLE must be pre-filtered out"
    con.close()


# ---------------------------------------------------------------------------
# Check 4: simultaneous positions
# ---------------------------------------------------------------------------


def test_simultaneous_positions_far_apart_is_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    root = tmp_path / "clean"
    _write_clean_partition(
        root,
        DAY,
        [
            (111, _ts(0), 55.0, 12.0),
            (111, _ts(0), 55.45, 12.0),  # ~50km north, same timestamp
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_simultaneous_positions(con)

    assert len(events) == 1
    assert events[0].kind == "simultaneous_position"
    assert events[0].mmsi == 111
    assert events[0].evidence_value == pytest.approx(50_000, rel=0.05)
    con.close()


def test_simultaneous_positions_close_together_is_not_flagged(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    root = tmp_path / "clean"
    _write_clean_partition(
        root,
        DAY,
        [
            (222, _ts(0), 55.0, 12.0),
            (222, _ts(0), 55.0001, 12.0),  # ~11m north (GPS jitter), same timestamp
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    events = spoofing.check_simultaneous_positions(con)

    assert events == []
    con.close()


def test_simultaneous_positions_not_also_flagged_as_impossible_speed(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    root = tmp_path / "clean"
    _write_clean_partition(
        root,
        DAY,
        [
            (333, _ts(0), 55.0, 12.0),
            (333, _ts(0), 55.45, 12.0),
        ],
    )
    spoofing._build_all_days(con, [(DAY, root / "date=2024-06-05" / "part-0.parquet")])

    speed_events = spoofing.check_impossible_speed(con)
    simultaneous_events = spoofing.check_simultaneous_positions(con)

    assert speed_events == []
    assert len(simultaneous_events) == 1
    con.close()


# ---------------------------------------------------------------------------
# build_spoofing_events (end to end)
# ---------------------------------------------------------------------------


def test_build_spoofing_events_end_to_end(tmp_path):
    clean_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        clean_root,
        DAY,
        [
            # impossible speed pair
            (111, _ts(0, 0, 0), 55.0, 12.0),
            (111, _ts(0, 1, 0), 55.09, 12.0),
            # simultaneous position pair
            (222, _ts(1), 60.0, 20.0),
            (222, _ts(1), 60.45, 20.0),
            # on-land point
            (333, _ts(2), 55.5, 10.5),
        ],
    )
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_empty_voyages(voyages_path)
    land_path = tmp_path / "reference" / "land.parquet"
    _write_land(land_path, LAND_POLYGON)

    out_path = tmp_path / "detect" / "spoofing.parquet"
    result_path = spoofing.build_spoofing_events(
        DAY, DAY2, in_root=clean_root, voyages_path=voyages_path, land_path=land_path,
        out_path=out_path,
    )

    assert result_path == out_path
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT mmsi, kind, built_at, git_sha, window_start, window_end "
            f"FROM '{out_path.as_posix()}' ORDER BY mmsi"
        ).fetchall()
    finally:
        con.close()

    kinds_by_mmsi = {row[0]: row[1] for row in rows}
    assert kinds_by_mmsi[111] == "impossible_speed"
    assert kinds_by_mmsi[222] == "simultaneous_position"
    assert kinds_by_mmsi[333] == "on_land"

    for row in rows:
        _mmsi, _kind, built_at, git_sha, window_start, window_end = row
        assert built_at is not None
        assert isinstance(git_sha, str) and git_sha != ""
        assert window_start == DAY
        assert window_end == DAY2


def test_build_spoofing_events_is_idempotent_by_default(tmp_path, caplog):
    clean_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(clean_root, DAY, [(111, _ts(0), 55.0, 12.0)])
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_empty_voyages(voyages_path)
    land_path = tmp_path / "reference" / "land.parquet"
    _write_empty_land(land_path)

    out_path = tmp_path / "detect" / "spoofing.parquet"
    first = spoofing.build_spoofing_events(
        DAY, DAY2, in_root=clean_root, voyages_path=voyages_path, land_path=land_path,
        out_path=out_path,
    )
    first_mtime = first.stat().st_mtime_ns

    with caplog.at_level("INFO"):
        second = spoofing.build_spoofing_events(
            DAY, DAY2, in_root=clean_root, voyages_path=voyages_path, land_path=land_path,
            out_path=out_path,
        )

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite"
    assert any("already exists" in record.message for record in caplog.records)


def test_build_spoofing_events_raises_when_clean_partitions_missing(tmp_path):
    clean_root = tmp_path / "clean" / "ais_dk"  # never populated
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_empty_voyages(voyages_path)
    land_path = tmp_path / "reference" / "land.parquet"
    _write_empty_land(land_path)
    out_path = tmp_path / "detect" / "spoofing.parquet"

    with pytest.raises(FileNotFoundError, match="clean"):
        spoofing.build_spoofing_events(
            DAY, DAY2, in_root=clean_root, voyages_path=voyages_path, land_path=land_path,
            out_path=out_path,
        )


def test_build_spoofing_events_raises_when_voyages_missing(tmp_path):
    clean_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(clean_root, DAY, [(111, _ts(0), 55.0, 12.0)])
    voyages_path = tmp_path / "tracks" / "voyages.parquet"  # never written
    land_path = tmp_path / "reference" / "land.parquet"
    _write_empty_land(land_path)
    out_path = tmp_path / "detect" / "spoofing.parquet"

    with pytest.raises(FileNotFoundError, match="voyages"):
        spoofing.build_spoofing_events(
            DAY, DAY2, in_root=clean_root, voyages_path=voyages_path, land_path=land_path,
            out_path=out_path,
        )


def test_build_spoofing_events_raises_when_land_missing(tmp_path):
    clean_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(clean_root, DAY, [(111, _ts(0), 55.0, 12.0)])
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_empty_voyages(voyages_path)
    land_path = tmp_path / "reference" / "land.parquet"  # never written
    out_path = tmp_path / "detect" / "spoofing.parquet"

    with pytest.raises(FileNotFoundError, match="land"):
        spoofing.build_spoofing_events(
            DAY, DAY2, in_root=clean_root, voyages_path=voyages_path, land_path=land_path,
            out_path=out_path,
        )
