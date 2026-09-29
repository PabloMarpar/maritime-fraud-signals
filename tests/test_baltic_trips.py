"""Unit tests for detect.baltic_trips. All fixtures are synthetic, written to tmp_path (or held
in-memory DuckDB tables); nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from detect import baltic_trips as bt

GATE = bt.GATE_LONGITUDE_DEG
WEST_LON = GATE - 0.5
EAST_LON = GATE + 0.5
MID_LAT = (bt.GATE_LAT_MIN + bt.GATE_LAT_MAX) / 2.0
OUTSIDE_LAT = bt.GATE_LAT_MIN - 1.0


def _ts(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001


def _gate_positions_con(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    """rows: (imo, mmsi, timestamp, latitude, longitude)."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE _gate_positions (imo VARCHAR, mmsi BIGINT, timestamp TIMESTAMP, "
        "latitude DOUBLE, longitude DOUBLE)"
    )
    con.executemany("INSERT INTO _gate_positions VALUES (?, ?, ?, ?, ?)", rows)
    return con


# --------------------------------------------------------------------------------------------
# _month_windows
# --------------------------------------------------------------------------------------------


def test_month_windows_covers_full_calendar_months():
    windows = bt._month_windows(date(2024, 4, 1), date(2024, 6, 30))
    assert windows == [
        (date(2024, 4, 1), date(2024, 4, 30)),
        (date(2024, 5, 1), date(2024, 5, 31)),
        (date(2024, 6, 1), date(2024, 6, 30)),
    ]


def test_month_windows_caps_final_partial_month():
    windows = bt._month_windows(date(2025, 1, 1), date(2025, 2, 26))
    assert windows[-1] == (date(2025, 2, 1), date(2025, 2, 26))


def test_month_windows_never_generates_skipped_window():
    windows = bt._month_windows(date(2024, 4, 1), date(2025, 2, 26))
    assert bt.SKIPPED_WINDOW not in windows


# --------------------------------------------------------------------------------------------
# detect_crossings: the 2h max gap and the latitude band
# --------------------------------------------------------------------------------------------


def test_detect_crossings_eastbound_and_westbound():
    day = date(2024, 4, 5)
    rows = [
        ("IMO1", 111, _ts(day, 8, 0), MID_LAT, WEST_LON),
        ("IMO1", 111, _ts(day, 9, 0), MID_LAT, EAST_LON),  # eastbound crossing, 1h gap
        ("IMO1", 111, _ts(day, 10, 30), MID_LAT, WEST_LON),  # westbound crossing, 1h30m gap
    ]
    con = _gate_positions_con(rows)
    try:
        n = bt.detect_crossings(con)
        crossings = con.execute(
            "SELECT imo, crossing_time, direction FROM _crossings ORDER BY crossing_time"
        ).fetchall()
    finally:
        con.close()
    assert n == 2
    assert crossings == [
        ("IMO1", _ts(day, 9, 0), "E"),
        ("IMO1", _ts(day, 10, 30), "W"),
    ]


def test_detect_crossings_rejects_gap_over_two_hours():
    day = date(2024, 4, 5)
    rows = [
        ("IMO1", 111, _ts(day, 8, 0), MID_LAT, WEST_LON),
        ("IMO1", 111, _ts(day, 11, 1), MID_LAT, EAST_LON),  # 3h01m gap -- too slow
    ]
    con = _gate_positions_con(rows)
    try:
        n = bt.detect_crossings(con)
    finally:
        con.close()
    assert n == 0


def test_detect_crossings_accepts_gap_at_exactly_two_hours():
    day = date(2024, 4, 5)
    rows = [
        ("IMO1", 111, _ts(day, 8, 0), MID_LAT, WEST_LON),
        ("IMO1", 111, _ts(day, 10, 0), MID_LAT, EAST_LON),  # exactly 2h -- boundary, inclusive
    ]
    con = _gate_positions_con(rows)
    try:
        n = bt.detect_crossings(con)
    finally:
        con.close()
    assert n == 1


def test_detect_crossings_no_side_change_is_not_a_crossing():
    day = date(2024, 4, 5)
    rows = [
        ("IMO1", 111, _ts(day, 8, 0), MID_LAT, WEST_LON),
        ("IMO1", 111, _ts(day, 9, 0), MID_LAT, WEST_LON + 0.01),  # still west of the gate
    ]
    con = _gate_positions_con(rows)
    try:
        n = bt.detect_crossings(con)
    finally:
        con.close()
    assert n == 0


def test_detect_crossings_different_mmsi_same_imo_still_pairs():
    """'of one IMO (any of its mmsi)' -- a radio-identity change mid-transit must not break the
    crossing pair."""
    day = date(2024, 4, 5)
    rows = [
        ("IMO1", 111, _ts(day, 8, 0), MID_LAT, WEST_LON),
        ("IMO1", 222, _ts(day, 9, 0), MID_LAT, EAST_LON),  # different mmsi, same imo
    ]
    con = _gate_positions_con(rows)
    try:
        n = bt.detect_crossings(con)
        (crossing_mmsi,) = con.execute("SELECT crossing_mmsi FROM _crossings").fetchone()
    finally:
        con.close()
    assert n == 1
    assert crossing_mmsi == 222


def test_gate_positions_extraction_excludes_outside_latitude_band(tmp_path: Path):
    """The latitude-band gate is enforced by the extraction step (every caller of
    detect_crossings pre-filters), not by detect_crossings itself -- see its own docstring."""
    thin_root = tmp_path / "thin"
    day = date(2024, 4, 5)
    partition_dir = thin_root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE thin (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, longitude DOUBLE)"
    )
    con.executemany(
        "INSERT INTO thin VALUES (?, ?, ?, ?)",
        [
            (111, _ts(day, 8, 0), MID_LAT, WEST_LON),  # inside band -- kept
            (111, _ts(day, 9, 0), OUTSIDE_LAT, EAST_LON),  # outside band -- dropped
        ],
    )
    con.execute(f"COPY thin TO '{(partition_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _roster (mmsi BIGINT, imo VARCHAR)"
    )
    con.execute("INSERT INTO _roster VALUES (111, 'IMO1')")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _gate_positions "
        "(imo VARCHAR, mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, longitude DOUBLE)"
    )
    try:
        bt._append_gate_positions_from_thin(con, day, day, thin_root)
        rows = con.execute("SELECT latitude FROM _gate_positions").fetchall()
    finally:
        con.close()
    assert rows == [(MID_LAT,)]


# --------------------------------------------------------------------------------------------
# pair_round_trips: 2-30 day window, next westbound only
# --------------------------------------------------------------------------------------------


def _crossings_con(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    """rows: (imo, crossing_mmsi, crossing_time, latitude, longitude, direction)."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE _crossings (imo VARCHAR, crossing_mmsi BIGINT, crossing_time TIMESTAMP, "
        "latitude DOUBLE, longitude DOUBLE, direction VARCHAR)"
    )
    con.executemany("INSERT INTO _crossings VALUES (?, ?, ?, ?, ?, ?)", rows)
    return con


def test_pair_round_trips_valid_gap():
    east = _ts(date(2024, 4, 1), 8)
    west = east + timedelta(days=10)
    rows = [
        ("IMO1", 111, east, MID_LAT, EAST_LON, "E"),
        ("IMO1", 111, west, MID_LAT, WEST_LON, "W"),
    ]
    con = _crossings_con(rows)
    try:
        n = bt.pair_round_trips(con)
        result = con.execute("SELECT east_time, west_time FROM _round_trips").fetchall()
    finally:
        con.close()
    assert n == 1
    assert result == [(east, west)]


@pytest.mark.parametrize("gap_days", [0, 1])
def test_pair_round_trips_rejects_too_soon(gap_days):
    east = _ts(date(2024, 4, 1), 8)
    west = east + timedelta(days=gap_days)
    rows = [
        ("IMO1", 111, east, MID_LAT, EAST_LON, "E"),
        ("IMO1", 111, west, MID_LAT, WEST_LON, "W"),
    ]
    con = _crossings_con(rows)
    try:
        n = bt.pair_round_trips(con)
    finally:
        con.close()
    assert n == 0


def test_pair_round_trips_rejects_too_late():
    east = _ts(date(2024, 4, 1), 8)
    west = east + timedelta(days=31)
    rows = [
        ("IMO1", 111, east, MID_LAT, EAST_LON, "E"),
        ("IMO1", 111, west, MID_LAT, WEST_LON, "W"),
    ]
    con = _crossings_con(rows)
    try:
        n = bt.pair_round_trips(con)
    finally:
        con.close()
    assert n == 0


def test_pair_round_trips_boundaries_inclusive():
    east = _ts(date(2024, 4, 1), 8)
    for gap_days in (bt.MIN_ROUND_TRIP_DAYS, bt.MAX_ROUND_TRIP_DAYS):
        west = east + timedelta(days=gap_days)
        rows = [
            ("IMO1", 111, east, MID_LAT, EAST_LON, "E"),
            ("IMO1", 111, west, MID_LAT, WEST_LON, "W"),
        ]
        con = _crossings_con(rows)
        try:
            n = bt.pair_round_trips(con)
        finally:
            con.close()
        assert n == 1, f"gap_days={gap_days} should be inclusive"


def test_pair_round_trips_uses_next_westbound_only_not_a_later_qualifying_one():
    """An eastbound crossing must pair with the IMMEDIATE next westbound crossing, even if that
    pairing then fails the 2-30 day gate -- it must NOT fall through to try a later westbound
    crossing that would have qualified."""
    east = _ts(date(2024, 4, 1), 8)
    west_too_soon = east + timedelta(days=1)  # immediate next westbound -- gap=1, fails the gate
    west_would_qualify = east + timedelta(days=10)  # a LATER westbound that would have qualified
    rows = [
        ("IMO1", 111, east, MID_LAT, EAST_LON, "E"),
        ("IMO1", 111, west_too_soon, MID_LAT, WEST_LON, "W"),
        ("IMO1", 111, west_would_qualify, MID_LAT, WEST_LON, "W"),
    ]
    con = _crossings_con(rows)
    try:
        n = bt.pair_round_trips(con)
    finally:
        con.close()
    assert n == 0  # not 1 -- the later westbound must never be reached for this eastbound


def test_pair_round_trips_no_westbound_at_all_produces_nothing():
    rows = [("IMO1", 111, _ts(date(2024, 4, 1), 8), MID_LAT, EAST_LON, "E")]
    con = _crossings_con(rows)
    try:
        n = bt.pair_round_trips(con)
    finally:
        con.close()
    assert n == 0


# --------------------------------------------------------------------------------------------
# compute_trip_draughts: NaN handling
# --------------------------------------------------------------------------------------------


def _round_trip_and_draughts_con(
    east_time: datetime, west_time: datetime, draught_rows: list[tuple]
) -> duckdb.DuckDBPyConnection:
    """draught_rows: (imo, mmsi, timestamp, draught)."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE _round_trips (imo VARCHAR, east_time TIMESTAMP, east_mmsi BIGINT, "
        "east_latitude DOUBLE, east_longitude DOUBLE, west_time TIMESTAMP, west_mmsi BIGINT, "
        "west_latitude DOUBLE, west_longitude DOUBLE)"
    )
    con.execute(
        "INSERT INTO _round_trips VALUES ('IMO1', ?, 111, ?, ?, ?, 111, ?, ?)",
        [east_time, MID_LAT, EAST_LON, west_time, MID_LAT, WEST_LON],
    )
    con.execute(
        "CREATE TABLE _draught_readings (imo VARCHAR, mmsi BIGINT, timestamp TIMESTAMP, "
        "draught DOUBLE)"
    )
    con.executemany("INSERT INTO _draught_readings VALUES (?, ?, ?, ?)", draught_rows)
    return con


def test_compute_trip_draughts_full_data_gives_relative_change():
    east_time = _ts(date(2024, 4, 1), 8)
    west_time = east_time + timedelta(days=10)
    draught_rows = [
        ("IMO1", 111, east_time, 5.0),  # ballast at departure
        ("IMO1", 111, west_time, 15.0),  # laden on return
    ]
    con = _round_trip_and_draughts_con(east_time, west_time, draught_rows)
    try:
        bt.compute_trip_draughts(con)
        row = con.execute(
            "SELECT east_draught, west_draught, max_draught_before_west, rel_change FROM _trips"
        ).fetchone()
    finally:
        con.close()
    east_draught, west_draught, max_before, rel_change = row
    assert east_draught == 5.0
    assert west_draught == 15.0
    assert max_before == 5.0  # only the east reading precedes west_time
    assert rel_change == pytest.approx((15.0 - 5.0) / 5.0)


def test_compute_trip_draughts_missing_west_reading_is_null():
    east_time = _ts(date(2024, 4, 1), 8)
    west_time = east_time + timedelta(days=10)
    draught_rows = [("IMO1", 111, east_time, 5.0)]  # nothing near west_time
    con = _round_trip_and_draughts_con(east_time, west_time, draught_rows)
    try:
        bt.compute_trip_draughts(con)
        east_draught, west_draught, rel_change = con.execute(
            "SELECT east_draught, west_draught, rel_change FROM _trips"
        ).fetchone()
    finally:
        con.close()
    assert east_draught == 5.0
    assert west_draught is None
    assert rel_change is None


def test_compute_trip_draughts_reading_outside_twelve_hour_window_is_ignored():
    east_time = _ts(date(2024, 4, 1), 8)
    west_time = east_time + timedelta(days=10)
    draught_rows = [
        ("IMO1", 111, east_time, 5.0),
        # 13h after west_time -- outside the +-12h window, should not count as west_draught
        ("IMO1", 111, west_time + timedelta(hours=13), 15.0),
    ]
    con = _round_trip_and_draughts_con(east_time, west_time, draught_rows)
    try:
        bt.compute_trip_draughts(con)
        west_draught = con.execute("SELECT west_draught FROM _trips").fetchone()[0]
    finally:
        con.close()
    assert west_draught is None


# --------------------------------------------------------------------------------------------
# select_laden_return_threshold
# --------------------------------------------------------------------------------------------


def test_select_laden_return_threshold_bimodal_finds_valley():
    rng = np.random.default_rng(42)
    low_mode = rng.normal(loc=0.02, scale=0.02, size=150)
    high_mode = rng.normal(loc=0.55, scale=0.03, size=150)
    values = np.concatenate([low_mode, high_mode]).tolist()

    threshold, info = bt.select_laden_return_threshold(values)

    assert info["fallback"] is False
    assert info["n"] == 300
    # the valley must sit clearly between the two synthetic modes, not degenerate to an endpoint
    assert 0.15 < threshold < 0.45
    low_center, high_center = info["modes"]
    assert low_center < threshold < high_center


def test_select_laden_return_threshold_unimodal_falls_back():
    rng = np.random.default_rng(7)
    values = rng.normal(loc=0.1, scale=0.03, size=200).tolist()

    threshold, info = bt.select_laden_return_threshold(values)

    assert info["fallback"] is True
    assert threshold == bt.FALLBACK_LADEN_RETURN_THRESHOLD


def test_select_laden_return_threshold_too_few_trips_falls_back():
    threshold, info = bt.select_laden_return_threshold([0.1, 0.2, 0.9])
    assert info["fallback"] is True
    assert info["n"] == 3
    assert threshold == bt.FALLBACK_LADEN_RETURN_THRESHOLD


def test_select_laden_return_threshold_ignores_none_and_nan():
    rng = np.random.default_rng(42)
    low_mode = rng.normal(loc=0.02, scale=0.02, size=150).tolist()
    high_mode = rng.normal(loc=0.55, scale=0.03, size=150).tolist()
    values = low_mode + high_mode + [None, float("nan")] * 5
    _threshold, info = bt.select_laden_return_threshold(values)
    assert info["n"] == 300  # the 10 junk entries are excluded, not counted
    assert info["fallback"] is False


# --------------------------------------------------------------------------------------------
# validate_against_gfw
# --------------------------------------------------------------------------------------------


def test_validate_against_gfw_returns_none_when_port_visits_missing(tmp_path: Path):
    con = duckdb.connect()
    try:
        result = bt.validate_against_gfw(
            con,
            trips_path=tmp_path / "trips.parquet",
            port_visits_path=tmp_path / "does_not_exist.parquet",
        )
    finally:
        con.close()
    assert result is None


def _write_parquet(path: Path, columns_sql: str, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({columns_sql})")
    con.executemany(f"INSERT INTO t VALUES ({', '.join(['?'] * len(rows[0]))})", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


def test_validate_against_gfw_computes_share(tmp_path: Path):
    trips_path = tmp_path / "trips.parquet"
    east1, west1 = _ts(date(2024, 4, 1), 0), _ts(date(2024, 4, 10), 0)
    east2, west2 = _ts(date(2024, 5, 1), 0), _ts(date(2024, 5, 10), 0)
    _write_parquet(
        trips_path,
        "imo VARCHAR, east_time TIMESTAMP, west_time TIMESTAMP, is_laden_return BOOLEAN",
        [
            ("IMO1", east1, west1, True),  # has a matching RUS visit
            ("IMO2", east2, west2, True),  # no matching visit
            ("IMO3", east2, west2, False),  # not a laden return -- excluded from the sample
        ],
    )
    port_visits_path = tmp_path / "port_visits.parquet"
    _write_parquet(
        port_visits_path,
        'imo VARCHAR, "start" TIMESTAMP, "end" TIMESTAMP, confidence INTEGER, start_anchorage_flag VARCHAR',
        [
            ("IMO1", east1 + timedelta(days=1), east1 + timedelta(days=2), 4, "RUS"),
            ("IMO2", east2 + timedelta(days=1), east2 + timedelta(days=2), 2, "RUS"),  # confidence too low
        ],
    )
    con = duckdb.connect()
    try:
        result = bt.validate_against_gfw(
            con, trips_path=trips_path, port_visits_path=port_visits_path, seed=1
        )
    finally:
        con.close()
    assert result == {"n_sampled": 2, "n_with_rus_visit": 1, "share": 0.5}


def test_validate_against_gfw_no_laden_returns(tmp_path: Path):
    trips_path = tmp_path / "trips_no_laden.parquet"
    east, west = _ts(date(2024, 4, 1), 0), _ts(date(2024, 4, 10), 0)
    _write_parquet(
        trips_path,
        "imo VARCHAR, east_time TIMESTAMP, west_time TIMESTAMP, is_laden_return BOOLEAN",
        [("IMO1", east, west, False)],
    )
    port_visits_path = tmp_path / "port_visits_unused.parquet"
    _write_parquet(
        port_visits_path,
        'imo VARCHAR, "start" TIMESTAMP, "end" TIMESTAMP, confidence INTEGER, start_anchorage_flag VARCHAR',
        [("IMO1", east, west, 4, "RUS")],
    )
    con = duckdb.connect()
    try:
        result = bt.validate_against_gfw(
            con, trips_path=trips_path, port_visits_path=port_visits_path
        )
    finally:
        con.close()
    assert result == {"n_sampled": 0, "n_with_rus_visit": 0, "share": None}


# --------------------------------------------------------------------------------------------
# Full pipeline: build_baltic_trips end-to-end with synthetic clean/thin/identity/ship_type data
# --------------------------------------------------------------------------------------------


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """rows: (mmsi, timestamp, latitude, longitude, draught, ship_type)."""
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
        "longitude DOUBLE, draught DOUBLE, ship_type VARCHAR)"
    )
    con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY clean TO '{(partition_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()


def _write_thin_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """rows: (mmsi, timestamp, latitude, longitude)."""
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE thin (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, longitude DOUBLE)"
    )
    con.executemany("INSERT INTO thin VALUES (?, ?, ?, ?)", rows)
    con.execute(f"COPY thin TO '{(partition_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()


def _write_identity_window(root: Path, window_start: date, window_end: date, rows: list[tuple]) -> None:
    """rows: (mmsi, imo, message_count, first_seen, last_seen, is_orphaned, is_reused)."""
    out_dir = root / f"window={window_start.isoformat()}_{window_end.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE identity (mmsi BIGINT, imo VARCHAR, message_count BIGINT, "
        "first_seen DATE, last_seen DATE, is_orphaned BOOLEAN, is_reused BOOLEAN)"
    )
    con.executemany("INSERT INTO identity VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY identity TO '{(out_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()


def _write_ship_type_window(root: Path, window_start: date, window_end: date, rows: list[tuple]) -> None:
    """rows: (mmsi, ship_type, type_of_mobile, n_messages)."""
    out_dir = root / f"window={window_start.isoformat()}_{window_end.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE st (mmsi BIGINT, ship_type VARCHAR, type_of_mobile VARCHAR, n_messages BIGINT)"
    )
    con.executemany("INSERT INTO st VALUES (?, ?, ?, ?)", rows)
    con.execute(f"COPY st TO '{(out_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()


@pytest.fixture
def small_archive(tmp_path: Path):
    """A minimal two-chunk archive (March 2024 + April 2024) with one Tanker (IMO 9074729,
    mmsi 111) making one clean round trip, entirely inside April so the thin-track path is
    exercised end-to-end. March is present but contributes no crossings for this vessel (used
    to exercise the March-specific roster path without adding a second trip).
    """
    clean_root = tmp_path / "clean"
    thin_root = tmp_path / "thin"
    identity_root = tmp_path / "identity"
    ship_type_root = tmp_path / "ship_type"

    march_day = date(2024, 3, 15)
    _write_clean_partition(
        clean_root, march_day, [(111, _ts(march_day, 12), MID_LAT, WEST_LON, 5.0, "Tanker")]
    )

    april_start, april_end = date(2024, 4, 1), date(2024, 4, 30)
    _write_identity_window(
        identity_root, april_start, april_end,
        [(111, "9074729", 100, april_start, april_end, False, False)],
    )
    _write_ship_type_window(
        ship_type_root, april_start, april_end, [(111, "Tanker", "Class A", 100)]
    )

    east_day = date(2024, 4, 5)
    west_day = date(2024, 4, 15)
    _write_thin_partition(
        thin_root, east_day,
        [(111, _ts(east_day, 8), MID_LAT, WEST_LON), (111, _ts(east_day, 9), MID_LAT, EAST_LON)],
    )
    _write_thin_partition(
        thin_root, west_day,
        [(111, _ts(west_day, 8), MID_LAT, EAST_LON), (111, _ts(west_day, 9), MID_LAT, WEST_LON)],
    )
    # Draught readings for both crossings, from clean data (thin has no draught column).
    _write_clean_partition(
        clean_root, east_day,
        [(111, _ts(east_day, 9), MID_LAT, EAST_LON, 5.0, "Tanker")],  # ballast at eastbound crossing
    )
    _write_clean_partition(
        clean_root, west_day,
        [(111, _ts(west_day, 9), MID_LAT, WEST_LON, 15.0, "Tanker")],  # laden at westbound crossing
    )

    return {
        "clean_root": clean_root,
        "thin_root": thin_root,
        "identity_root": identity_root,
        "ship_type_root": ship_type_root,
    }


def test_build_baltic_trips_end_to_end(tmp_path: Path, small_archive: dict):
    out_path = tmp_path / "out" / "part-0.parquet"
    bt.build_baltic_trips(
        in_root=small_archive["clean_root"],
        thin_root=small_archive["thin_root"],
        identity_root=small_archive["identity_root"],
        ship_type_root=small_archive["ship_type_root"],
        out_path=out_path,
        laden_return_threshold=0.20,
        first_monthly_window_start=date(2024, 4, 1),
        last_archive_day=date(2024, 4, 30),
    )
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT imo, east_draught, west_draught, rel_change, is_laden_return "
            "FROM read_parquet(?)",
            [str(out_path)],
        ).fetchall()
    finally:
        con.close()
    assert rows == [("9074729", 5.0, 15.0, pytest.approx(2.0), True)]


def test_build_baltic_trips_is_idempotent(tmp_path: Path, small_archive: dict):
    out_path = tmp_path / "out" / "part-0.parquet"
    kwargs = {
        "in_root": small_archive["clean_root"],
        "thin_root": small_archive["thin_root"],
        "identity_root": small_archive["identity_root"],
        "ship_type_root": small_archive["ship_type_root"],
        "out_path": out_path,
        "first_monthly_window_start": date(2024, 4, 1),
        "last_archive_day": date(2024, 4, 30),
    }
    bt.build_baltic_trips(**kwargs)
    mtime_1 = out_path.stat().st_mtime_ns
    bt.build_baltic_trips(**kwargs)  # force=False by default -- must not rebuild
    assert out_path.stat().st_mtime_ns == mtime_1
