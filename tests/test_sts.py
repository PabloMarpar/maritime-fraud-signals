"""Unit tests for detect.sts. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from detect import sts

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
DAY3 = date(2024, 6, 7)
DAY4 = date(2024, 6, 8)

# Open sea, far from the small land polygons any of these tests might use -- deliberately not
# near any real coastline, so "is this cell coastal" is entirely controlled by the synthetic
# anchorages fixture, not by accident.
LAT0, LON0 = 57.0, 5.0


def _minutes(total_minutes: int, day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(minutes=total_minutes)


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude, sog, navigational_status, ship_type,
    type_of_mobile) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, sog DOUBLE, navigational_status VARCHAR, ship_type VARCHAR, "
            "type_of_mobile VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_anchorages(path: Path, rows: list[tuple]) -> None:
    """Write a synthetic anchorages.parquet with just the columns apply_structural_gates reads.

    rows is a list of (center_latitude, center_longitude, member_mmsis, is_coastal) tuples.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE anchorages (center_latitude DOUBLE, center_longitude DOUBLE, "
            "member_mmsis BIGINT[], is_coastal BOOLEAN)"
        )
        if rows:
            con.executemany("INSERT INTO anchorages VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY anchorages TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_empty_anchorages(path: Path) -> None:
    _write_anchorages(path, [])


def _dwell_rows(
    mmsi: int,
    lat: float,
    lon: float,
    start_minute: int,
    duration_hours: float,
    day: date = DAY,
    sog: float = 0.1,
    step_minutes: int = 10,
    nav_status: str = "Under way using engine",
    ship_type: str = "Tanker",
    mobile_type: str = "Class A",
) -> list[tuple]:
    """Dense pings (<= EPISODE_SEPARATION_MINUTES apart) at one position, for one vessel."""
    total_minutes = int(duration_hours * 60)
    rows = []
    t = 0
    while t <= total_minutes:
        rows.append(
            (mmsi, _minutes(start_minute + t, day=day), lat, lon, sog, nav_status, ship_type, mobile_type)
        )
        t += step_minutes
    return rows


def _context_point(
    mmsi: int,
    lat: float,
    lon: float,
    minute: int,
    day: date = DAY,
    sog: float = 10.0,
    nav_status: str = "Under way using engine",
    ship_type: str = "Tanker",
) -> tuple:
    return (mmsi, _minutes(minute, day=day), lat, lon, sog, nav_status, ship_type, "Class A")


def _read_events(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT encounter_id, mmsi_a, mmsi_b, episode_seq, duration_hours, confidence, "
            "approach_state_a, approach_state_b, departure_state_a, departure_state_b "
            f"FROM '{out_path.as_posix()}' ORDER BY start_time"
        ).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Headline discrimination tests
# ---------------------------------------------------------------------------


def test_offshore_rendezvous_is_flagged(tmp_path):
    """Two vessels converge, hold position for 3h, then separate -- no anchorage nearby. Must be
    flagged with high confidence (all four rendezvous sides 'moved')."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = [
        # Approach: far away, fast, 3h before the rendezvous starts (at minute 180).
        _context_point(100, LAT0 - 0.3, LON0 - 0.3, minute=0),
        _context_point(200, LAT0 + 0.3, LON0 + 0.3, minute=0),
    ]
    rows += _dwell_rows(100, LAT0, LON0, start_minute=180, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=180, duration_hours=3.0)
    rows += [
        # Departure: far away, fast, well after the rendezvous ends (at minute ~360).
        _context_point(100, LAT0 - 0.3, LON0 - 0.3, minute=600),
        _context_point(200, LAT0 + 0.3, LON0 + 0.3, minute=600),
    ]
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    events = _read_events(out_path)
    assert len(events) == 1
    e = events[0]
    assert e[4] == pytest.approx(3.0, abs=0.2)  # duration_hours
    assert e[6] == e[7] == e[8] == e[9] == "moved"
    assert e[5] > 0.7, f"expected high confidence for a genuine offshore rendezvous, got {e[5]}"


def test_moored_neighbours_are_not_flagged(tmp_path):
    """The headline case: two vessels at fixed berths, inside a real anchorage (built from 5
    OTHER vessels). Must not be flagged at all."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_anchorages(
        anchorages_path, [(LAT0, LON0, [100, 200, 301, 302, 303, 304, 305], True)]
    )

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=5.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=5.0)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert _read_events(out_path) == []


def test_moored_neighbours_outside_an_anchorage_are_flagged_with_low_confidence(tmp_path):
    """Same geometry as the headline case, but no anchorage nearby: it survives the hard gates
    honestly (GFW would flag it too) but scores low, given explicit stationary context showing
    neither vessel moved before or after (as opposed to no_data, which alone would only pull the
    rendezvous score to a neutral 0.5, not toward 0 -- see test_missing_context_is_no_data)."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = [
        _context_point(100, LAT0, LON0, minute=-180, sog=0.1, nav_status="Moored", ship_type="Cargo"),
        _context_point(200, LAT0, LON0, minute=-180, sog=0.1, nav_status="Moored", ship_type="Cargo"),
    ]
    rows += _dwell_rows(
        100, LAT0, LON0, start_minute=0, duration_hours=5.0, nav_status="Moored", ship_type="Cargo"
    )
    rows += _dwell_rows(
        200, LAT0, LON0, start_minute=0, duration_hours=5.0, nav_status="Moored", ship_type="Cargo"
    )
    rows += [
        _context_point(100, LAT0, LON0, minute=480, sog=0.1, nav_status="Moored", ship_type="Cargo"),
        _context_point(200, LAT0, LON0, minute=480, sog=0.1, nav_status="Moored", ship_type="Cargo"),
    ]
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    events = _read_events(out_path)
    assert len(events) == 1
    assert events[0][6] == events[0][7] == events[0][8] == events[0][9] == "stationary"
    assert events[0][5] < 0.3, f"expected low confidence for stationary neighbours, got {events[0][5]}"


def test_pair_meeting_daily_is_segmented_into_separate_episodes(tmp_path):
    """3h/day for 4 days, separated by ~21h gaps each day, must be 4 episodes -- the direct fix
    for the 647-hour false-encounter artifact."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    for day in (DAY, DAY2, DAY3, DAY4):
        rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0, day=day)
        rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0, day=day)
        _write_clean_partition(in_root, day, rows)

    out_path = sts.build_sts_events(
        DAY, DAY4, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root
    )

    events = _read_events(out_path)
    assert len(events) == 4
    assert {e[3] for e in events} == {1, 2, 3, 4}  # episode_seq
    for e in events:
        assert e[4] == pytest.approx(3.0, abs=0.2)


def test_continuous_colocation_is_one_episode(tmp_path):
    """Co-located for 5h with only small (<EPISODE_SEPARATION_MINUTES) gaps must be ONE episode,
    not several."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=5.0, step_minutes=30)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=5.0, step_minutes=30)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    events = _read_events(out_path)
    assert len(events) == 1
    assert events[0][4] == pytest.approx(5.0, abs=0.2)


def test_episode_crossing_midnight_is_one_event(tmp_path):
    """22:30 -> 01:30 across two day partitions must be one row, not split at the boundary."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    day1_rows = _dwell_rows(100, LAT0, LON0, start_minute=22 * 60 + 30, duration_hours=1.5, day=DAY)
    day1_rows += _dwell_rows(200, LAT0, LON0, start_minute=22 * 60 + 30, duration_hours=1.5, day=DAY)
    _write_clean_partition(in_root, DAY, day1_rows)

    day2_rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=1.5, day=DAY2)
    day2_rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=1.5, day=DAY2)
    _write_clean_partition(in_root, DAY2, day2_rows)

    out_path = sts.build_sts_events(
        DAY, DAY2, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root
    )

    events = _read_events(out_path)
    assert len(events) == 1
    assert events[0][4] == pytest.approx(3.0, abs=0.3)


def test_anchorage_defined_only_by_the_candidate_pair_does_not_exclude_them(tmp_path):
    """The cell's only members are the two candidates themselves: leave-one-out demotes it below
    the distinct-vessel bar, so the encounter IS flagged. The self-exoneration regression."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_anchorages(anchorages_path, [(LAT0, LON0, [100, 200], True)])

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert len(_read_events(out_path)) == 1


def test_non_coastal_cluster_does_not_exclude_an_encounter(tmp_path):
    """An offshore cluster (is_coastal=false) must never exclude a candidate, no matter how many
    distinct vessels it lists."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_anchorages(
        anchorages_path, [(LAT0, LON0, [100, 200, 301, 302, 303, 304, 305], False)]
    )

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert len(_read_events(out_path)) == 1


# ---------------------------------------------------------------------------
# Structural gate thresholds (positive/negative pairs)
# ---------------------------------------------------------------------------


def test_encounter_below_min_duration_is_not_flagged(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=1.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=1.0)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert _read_events(out_path) == []


def test_pair_above_max_median_speed_is_not_flagged(tmp_path):
    """Both vessels at 2.5kn -- passes the 3kn candidate pre-filter but fails the 2kn gate."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0, sog=2.5)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0, sog=2.5)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert _read_events(out_path) == []


def test_pair_beyond_max_separation_is_not_flagged(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0 + 0.02, LON0, start_minute=0, duration_hours=3.0)  # ~2.2km
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert _read_events(out_path) == []


def test_pair_straddling_a_grid_cell_boundary_is_flagged(tmp_path):
    """Two vessels ~300m apart, straddling a 0.01deg cell boundary -- proves the 3x3 neighbour
    expansion, not just a same-cell join."""
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    boundary_lat = 57.00  # an exact multiple of GRID_DEG (0.01)
    rows = _dwell_rows(100, boundary_lat - 0.0013, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, boundary_lat + 0.0013, LON0, start_minute=0, duration_hours=3.0)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert len(_read_events(out_path)) == 1


def test_base_station_is_never_paired_with_a_vessel(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0, mobile_type="Base Station")
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    assert _read_events(out_path) == []


def test_each_pair_appears_once_not_twice(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0)
    _write_clean_partition(in_root, DAY, rows)

    out_path = sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)

    events = _read_events(out_path)
    assert len(events) == 1
    assert events[0][1] == 100 and events[0][2] == 200  # mmsi_a < mmsi_b, canonical order


# ---------------------------------------------------------------------------
# Pure-function confidence tests (no DuckDB)
# ---------------------------------------------------------------------------


def test_tanker_pair_scores_higher_than_tug_attendance():
    tanker_score = sts._ship_type_pair_score("Tanker", "Tanker")
    tug_score = sts._ship_type_pair_score("Tanker", "Tug")
    assert tanker_score > tug_score


def test_missing_context_is_no_data_not_stationary():
    assert sts._movement_state(None, None, n_slots=0) == "no_data"
    assert sts._movement_state(0.0, 0.0, n_slots=1) == "stationary"


def test_moored_status_offshore_inverts_to_a_positive_signal():
    near_score = sts._nav_status_score("Moored", distance_to_anchorage_m=0.0)
    far_score = sts._nav_status_score("Moored", distance_to_anchorage_m=50_000.0)
    assert far_score > near_score
    assert far_score == 1.0
    assert near_score == 0.0


def test_repeated_pair_at_the_same_spot_scores_lower_than_a_one_off():
    one_off = sts._repetition_score(1)
    repeated = sts._repetition_score(4)
    assert one_off > repeated


def test_codrifting_pair_scores_higher_confidence_than_static_pair():
    static_confidence, _ = sts.encounter_confidence(
        approach_state_a="stationary",
        approach_state_b="stationary",
        departure_state_a="stationary",
        departure_state_b="stationary",
        codrift_m=0.0,
        ship_type_a="Cargo",
        ship_type_b="Cargo",
        nav_status_a="Moored",
        nav_status_b="Moored",
        distance_to_anchorage_m=0.0,
        pair_same_place_count=1,
        duration_hours=3.0,
    )
    drifting_confidence, _ = sts.encounter_confidence(
        approach_state_a="moved",
        approach_state_b="moved",
        departure_state_a="moved",
        departure_state_b="moved",
        codrift_m=500.0,
        ship_type_a="Tanker",
        ship_type_b="Tanker",
        nav_status_a="Under way using engine",
        nav_status_b="Under way using engine",
        distance_to_anchorage_m=None,
        pair_same_place_count=1,
        duration_hours=3.0,
    )
    assert drifting_confidence > static_confidence


# ---------------------------------------------------------------------------
# Builder: idempotency and required-input errors
# ---------------------------------------------------------------------------


def test_build_sts_events_is_idempotent_by_default(tmp_path, caplog):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)
    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0)
    _write_clean_partition(in_root, DAY, rows)

    first = sts.build_sts_events(
        DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root
    )
    first_mtime = first.stat().st_mtime_ns

    caplog.clear()
    with caplog.at_level("INFO"):
        second = sts.build_sts_events(
            DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root
        )

    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite"
    assert any("already exists" in record.message for record in caplog.records)


def test_build_sts_events_raises_when_clean_partitions_missing(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    _write_empty_anchorages(anchorages_path)

    with pytest.raises(FileNotFoundError, match="process.clean"):
        sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)


def test_build_sts_events_raises_when_anchorages_missing(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    anchorages_path = tmp_path / "anchorages.parquet"
    out_root = tmp_path / "sts"
    rows = _dwell_rows(100, LAT0, LON0, start_minute=0, duration_hours=3.0)
    rows += _dwell_rows(200, LAT0, LON0, start_minute=0, duration_hours=3.0)
    _write_clean_partition(in_root, DAY, rows)

    with pytest.raises(FileNotFoundError, match="detect.anchorages"):
        sts.build_sts_events(DAY, DAY, in_root=in_root, anchorages_path=anchorages_path, out_root=out_root)
