"""Unit tests for detect.anchorages. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from detect import anchorages

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)

# Land polygon covering roughly (55.00-55.02 lat, 12.00-12.02 lon). WKT is lon-lat, matching
# tests/test_spoofing.py's _write_land convention.
LAND_POLYGON = "POLYGON((12.0 55.0, 12.0 55.02, 12.02 55.02, 12.02 55.0, 12.0 55.0))"

# Just north of LAND_POLYGON, well within COASTAL_MAX_DISTANCE_M (~556m to the nearest edge).
COASTAL_LAT, COASTAL_LON = 55.025, 12.015

# Far from LAND_POLYGON -- also outside LAND_PREFILTER_DEG, so no distance is even computed.
OFFSHORE_LAT, OFFSHORE_LON = 58.005, 15.005

# Due EAST of LAND_POLYGON's eastern edge (lon=12.02), at mid-height (lat=55.01) so the offset is
# pure longitude, not latitude. True distance ~6.7km (comfortably < COASTAL_MAX_DISTANCE_M): a
# regression test for the ST_Distance_Sphere argument-order bug found this session, which inflated
# exactly this kind of east-west offset by 1/cos(lat) (~1.74x here) -- enough to push a genuinely
# coastal ~6.7km distance over the 10km threshold and misclassify it as non-coastal. A pure
# north-south fixture (like COASTAL_LAT/LON above) cannot catch that bug, since latitude is not
# rescaled by either argument order.
EW_COASTAL_LAT, EW_COASTAL_LON = 55.01, 12.12492


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude, sog, type_of_mobile) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, sog DOUBLE, type_of_mobile VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
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


def _minutes(total_minutes: int, day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(minutes=total_minutes)


def _stationary_rows(
    mmsis: list[int],
    lat: float,
    lon: float,
    day: date = DAY,
    sog: float = 0.1,
    duration_hours: float = 3.0,
    step_minutes: int = 20,
) -> list[tuple]:
    """Dense pings every step_minutes (< MAX_DWELL_GAP_MINUTES) spanning duration_hours, at the
    same (lat, lon) -- exactly at the MIN_STATIONARY_HOURS boundary by default. Must be dense: two
    pings on their own, however far apart, register as two separate zero-length stays under the
    gap-aware dwell logic, not one continuous span -- see
    test_pings_far_apart_within_one_day_do_not_create_a_false_stay."""
    total_minutes = int(duration_hours * 60)
    rows = []
    for mmsi in mmsis:
        t = 0
        while t <= total_minutes:
            rows.append((mmsi, _minutes(t, day=day), lat, lon, sog, "Class A"))
            t += step_minutes
    return rows


def _read_anchorages(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT cell_lat, cell_lon, n_vessels, n_vessel_days, total_stationary_hours, "
            "member_mmsis, distance_to_land_m, is_coastal "
            f"FROM '{out_path.as_posix()}' ORDER BY cell_lat, cell_lon"
        ).fetchall()
    finally:
        con.close()


def test_cell_with_many_distinct_stationary_vessels_is_an_anchorage(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON)
    )

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    rows = _read_anchorages(out_path)
    assert len(rows) == 1
    assert rows[0][2] == 5, "expected 5 distinct vessels"


def test_single_recurring_vessel_does_not_create_an_anchorage(tmp_path):
    """The direct P2-1b lesson in this module's terms: distinct vessels, never vessel-hours."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    # One mmsi, genuinely moored (dense pings) 5h/day for 10 days -- 50 vessel-hours of real
    # dwell, but only 1 distinct vessel.
    for i in range(10):
        day = DAY + timedelta(days=i)
        rows = _stationary_rows([999], COASTAL_LAT, COASTAL_LON, day=day, duration_hours=5.0)
        _write_clean_partition(in_root, day, rows)

    anchorages.build_anchorages(
        DAY, DAY + timedelta(days=9), in_root=in_root, land_path=land_path, out_path=out_path
    )

    assert _read_anchorages(out_path) == []


def test_offshore_cluster_is_recorded_but_not_coastal(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], OFFSHORE_LAT, OFFSHORE_LON)
    )

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    rows = _read_anchorages(out_path)
    assert len(rows) == 1
    assert rows[0][7] is False, "offshore cluster must be recorded, not dropped, but not coastal"
    assert rows[0][6] is None, "no land piece within the planar pre-filter -- distance is NULL"


def test_coastal_cluster_is_marked_coastal(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON)
    )

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    rows = _read_anchorages(out_path)
    assert len(rows) == 1
    assert rows[0][7] is True
    assert rows[0][6] < anchorages.COASTAL_MAX_DISTANCE_M


def test_east_west_coastal_cluster_is_marked_coastal(tmp_path):
    """Regression test for the ST_Distance_Sphere argument-order bug: a pure east-west offset
    of ~6.7km must not be inflated past the 10km threshold."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], EW_COASTAL_LAT, EW_COASTAL_LON)
    )

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    rows = _read_anchorages(out_path)
    assert len(rows) == 1
    assert rows[0][7] is True, "a ~6.7km east-west offset must not be inflated past 10km"
    assert rows[0][6] < 8000, f"distance should be close to the true ~6.7km, got {rows[0][6]}"


def test_moving_vessels_do_not_create_an_anchorage(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root,
        DAY,
        _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON, sog=5.0),
    )

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    assert _read_anchorages(out_path) == []


def test_brief_stops_do_not_create_an_anchorage(tmp_path):
    """A genuine, continuous 1h stay (dense pings, no gap) is still below MIN_STATIONARY_HOURS
    (3.0) -- isolates that threshold from the gap-splitting logic tested elsewhere."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    rows = _stationary_rows(
        [1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON, duration_hours=1.0, step_minutes=10
    )
    _write_clean_partition(in_root, DAY, rows)

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    assert _read_anchorages(out_path) == []


def test_pings_far_apart_within_one_day_do_not_create_a_false_stay(tmp_path):
    """Regression test: two slow pings 22h apart on the same day (e.g. one at the start of the
    day, one moored again at the end) must not be stitched into one false 22h stay just because
    dwell was naively measured as the day's first-to-last ping span."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    rows = []
    for mmsi in (1, 2, 3, 4, 5):
        rows.append((mmsi, _ts(0), COASTAL_LAT, COASTAL_LON, 0.1, "Class A"))
        rows.append((mmsi, _ts(22), COASTAL_LAT, COASTAL_LON, 0.1, "Class A"))
    _write_clean_partition(in_root, DAY, rows)

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    assert _read_anchorages(out_path) == []


def test_dwell_is_measured_per_day_not_summed_across_the_window(tmp_path):
    """A genuine, continuous 1h/day dwell for 5 days must never accumulate into a 5h dwell --
    MIN_STATIONARY_HOURS applies within a single day, not across the whole window."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    for i in range(5):
        day = DAY + timedelta(days=i)
        rows = _stationary_rows(
            [1], COASTAL_LAT, COASTAL_LON, day=day, duration_hours=1.0, step_minutes=10
        )
        _write_clean_partition(in_root, day, rows)

    anchorages.build_anchorages(
        DAY,
        DAY + timedelta(days=4),
        in_root=in_root,
        land_path=land_path,
        out_path=out_path,
        min_distinct_vessels=1,
    )

    assert _read_anchorages(out_path) == []


def test_member_mmsis_lists_each_distinct_vessel_exactly_once(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([5, 3, 1, 4, 2], COASTAL_LAT, COASTAL_LON)
    )
    _write_clean_partition(
        in_root, DAY2, _stationary_rows([1, 2], COASTAL_LAT, COASTAL_LON, day=DAY2)
    )

    anchorages.build_anchorages(DAY, DAY2, in_root=in_root, land_path=land_path, out_path=out_path)

    rows = _read_anchorages(out_path)
    assert len(rows) == 1
    assert rows[0][5] == [1, 2, 3, 4, 5], "member_mmsis must be deduplicated and sorted"
    # n_vessels (distinct MMSI) must not be confused with n_vessel_days (mmsi-day rows) -- the
    # exact P2-1b unit mismatch this module exists to avoid, now pinned at the output-column level.
    assert rows[0][2] == 5, f"n_vessels must be the 5 DISTINCT mmsi, got {rows[0][2]}"
    assert rows[0][3] == 7, f"n_vessel_days must be 5 (day1) + 2 (day2) = 7, got {rows[0][3]}"
    assert len(rows[0][5]) == rows[0][2], "member_mmsis length must match n_vessels exactly"


def test_grid_cell_boundary_value_floors_without_float_error(tmp_path):
    """A position exactly on a cell-boundary multiple of ANCHORAGE_CELL_DEG must floor to that
    exact cell, not the one below it due to floating-point representation error -- the
    epsilon-before-floor convention borrowed from detect.liveness."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    boundary_lat, boundary_lon = 55.02, 12.02  # exact multiples of ANCHORAGE_CELL_DEG (0.01)
    rows = _stationary_rows([1, 2, 3, 4, 5], boundary_lat, boundary_lon)
    _write_clean_partition(in_root, DAY, rows)

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    rows = _read_anchorages(out_path)
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(boundary_lat)
    assert rows[0][1] == pytest.approx(boundary_lon)


def test_build_anchorages_raises_on_window_mismatch_without_force(tmp_path):
    """A mask already built for a DIFFERENT [start, end] must not be silently reused -- e.g. a
    smaller prototype window silently surviving into a larger real run."""
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON)
    )
    _write_clean_partition(
        in_root, DAY2, _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON, day=DAY2)
    )

    anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)

    with pytest.raises(ValueError, match="force=True"):
        anchorages.build_anchorages(
            DAY, DAY2, in_root=in_root, land_path=land_path, out_path=out_path
        )


def test_build_anchorages_is_idempotent_by_default(tmp_path, caplog):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON)
    )

    first = anchorages.build_anchorages(
        DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path
    )
    first_mtime = first.stat().st_mtime_ns

    caplog.clear()
    with caplog.at_level("INFO"):
        second = anchorages.build_anchorages(
            DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path
        )

    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite"
    assert any("already exists" in record.message for record in caplog.records)


def test_build_anchorages_raises_when_clean_partitions_missing(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_land(land_path, LAND_POLYGON)

    with pytest.raises(FileNotFoundError, match="process.clean"):
        anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)


def test_build_anchorages_raises_when_land_missing(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    land_path = tmp_path / "land.parquet"
    out_path = tmp_path / "anchorages.parquet"
    _write_clean_partition(
        in_root, DAY, _stationary_rows([1, 2, 3, 4, 5], COASTAL_LAT, COASTAL_LON)
    )

    with pytest.raises(FileNotFoundError, match="ingest.landmask"):
        anchorages.build_anchorages(DAY, DAY, in_root=in_root, land_path=land_path, out_path=out_path)
