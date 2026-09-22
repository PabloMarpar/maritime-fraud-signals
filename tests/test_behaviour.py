"""Unit tests for detect.behaviour. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from detect import behaviour
from process.tracks import DEFAULT_GAP_HOURS

DAY = date(2024, 6, 5)


def _ts(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001


def _window_end(day: date = DAY) -> datetime:
    """Matches build_behaviour_events' own end-of-day computation for check 2's knowable_at."""
    return datetime.combine(day, datetime.max.time())


def _valid_imo(prefix: int) -> str:
    """A checksum-valid 7-digit IMO from a 6-digit prefix, same formula as VALID_IMO_SQL."""
    digits = f"{prefix:06d}"
    weights = (7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    return f"{digits}{total % 10}"


VALID_IMO_A = _valid_imo(100000)
VALID_IMO_B = _valid_imo(200000)

ROSTOCK_LAT, ROSTOCK_LON = 55.0, 12.0

# Distinct dummy MMSI used only to populate an anchorage cell's member_mmsis array.
OTHER_MMSI = (900000001, 900000002, 900000003, 900000004, 900000005, 900000006)


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude, cog, sog, draught, destination, imo,
    type_of_mobile) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, cog DOUBLE, sog DOUBLE, draught DOUBLE, destination VARCHAR, "
            "imo VARCHAR, type_of_mobile VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
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


def _write_ports(path: Path, rows: list[tuple[str, float, float]]) -> None:
    """Write a synthetic ports.parquet, same (name, geom) shape as ingest.ports's ST_Read output.

    rows is a list of (name, lat, lon) tuples.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        con.execute("CREATE TABLE ports (name VARCHAR, geom GEOMETRY)")
        if rows:
            con.executemany(
                "INSERT INTO ports VALUES (?, ST_Point(?, ?))",
                [(name, lon, lat) for name, lat, lon in rows],
            )
        con.execute(f"COPY ports TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_empty_ports(path: Path) -> None:
    _write_ports(path, [])


def _write_anchorages(path: Path, rows: list[tuple[float, float, list[int], bool]]) -> None:
    """rows is a list of (center_latitude, center_longitude, member_mmsis, is_coastal) tuples."""
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


def _write_sts(path: Path, rows: list[tuple[int, int, datetime, datetime]]) -> None:
    """rows is a list of (mmsi_a, mmsi_b, start_time, end_time) tuples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE sts (mmsi_a BIGINT, mmsi_b BIGINT, start_time TIMESTAMP, "
            "end_time TIMESTAMP)"
        )
        if rows:
            con.executemany("INSERT INTO sts VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY sts TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_empty_sts(path: Path) -> None:
    _write_sts(path, [])


def _course_points(
    mmsi: int,
    lat: float,
    lon_start: float,
    cog: float,
    destination: str,
    imo: str,
    n: int = 12,
    lon_step: float = -0.02,
    start: datetime | None = None,
    step_minutes: int = 10,
    sog: float = 10.0,
) -> list[tuple]:
    """n position reports moving in longitude only, constant cog/sog/destination/imo/draught=0."""
    start = start if start is not None else _ts(0)
    return [
        (
            mmsi,
            start + timedelta(minutes=step_minutes * i),
            lat,
            lon_start + lon_step * i,
            cog,
            sog,
            0.0,
            destination,
            imo,
            "Class A",
        )
        for i in range(n)
    ]


def _draught_points(
    mmsi: int, lat: float, lon: float, draught: float, imo: str, start: datetime, n: int = 5
) -> list[tuple]:
    """n static-message-bearing pings at a fixed position, constant draught, no destination."""
    return [
        (mmsi, start + timedelta(minutes=10 * i), lat, lon, 0.0, 0.0, draught, "UNKNOWN", imo, "Class A")
        for i in range(n)
    ]


def _voyage_row(
    mmsi: int,
    seq: int,
    start: datetime,
    end: datetime,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    point_count: int,
) -> tuple:
    return (
        mmsi,
        seq,
        f"{mmsi}-{seq}",
        start,
        end,
        start_lat,
        start_lon,
        end_lat,
        end_lon,
        point_count,
        int((end - start).total_seconds()),
    )


def _read_events(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT mmsi, kind, event_time, knowable_at, voyage_id, confidence, evidence_value "
            f"FROM '{out_path.as_posix()}' ORDER BY mmsi, event_time, kind"
        ).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Check 1: destination vs course
# ---------------------------------------------------------------------------


def test_destination_course_mismatch_flagged(tmp_path):
    """A vessel declares ROSTOCK but sails steadily away from it (cog opposite the true bearing)
    for the whole voyage, far enough from the port that approach noise cannot explain it, and
    fast enough that COG is meaningful."""
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"

    mmsi = 111222333
    rows = _course_points(
        mmsi, lat=55.0, lon_start=9.50, cog=270.0, destination=" rostock@@@@",
        imo=VALID_IMO_A,
    )
    _write_clean_partition(in_root, DAY, rows)
    _write_voyages(
        voyages_path,
        [_voyage_row(mmsi, 1, rows[0][1], rows[-1][1], rows[0][2], rows[0][3], rows[-1][2], rows[-1][3], len(rows))],
    )
    _write_ports(ports_path, [("ROSTOCK", ROSTOCK_LAT, ROSTOCK_LON)])
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    events = _read_events(out_path)
    assert len(events) == 1
    e = events[0]
    assert e[0] == mmsi
    assert e[1] == "destination_course_mismatch"
    assert e[5] > 0.0  # confidence
    # knowable_at: voyage end_time + DEFAULT_GAP_HOURS (a voyage boundary is only confirmed once
    # that much silence has elapsed), not end_time alone -- see module docstring.
    end_time = rows[-1][1]
    assert e[3] == end_time + timedelta(hours=DEFAULT_GAP_HOURS)


def test_destination_course_match_not_flagged(tmp_path):
    """Same setup, but cog points toward the declared destination -- must not be flagged."""
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"

    mmsi = 111222444
    rows = _course_points(
        mmsi, lat=55.0, lon_start=9.50, cog=90.0, destination="ROSTOCK", imo=VALID_IMO_A,
    )
    _write_clean_partition(in_root, DAY, rows)
    _write_voyages(
        voyages_path,
        [_voyage_row(mmsi, 1, rows[0][1], rows[-1][1], rows[0][2], rows[0][3], rows[-1][2], rows[-1][3], len(rows))],
    )
    _write_ports(ports_path, [("ROSTOCK", ROSTOCK_LAT, ROSTOCK_LON)])
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


def test_destination_unmatched_not_flagged(tmp_path):
    """A destination with no corresponding port (a free-text activity descriptor, not a place)
    must never produce an event, even with a wildly mismatched course."""
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"

    mmsi = 111222555
    rows = _course_points(
        mmsi, lat=55.0, lon_start=9.50, cog=270.0, destination="FISHING GROUNDS",
        imo=VALID_IMO_A,
    )
    _write_clean_partition(in_root, DAY, rows)
    _write_voyages(
        voyages_path,
        [_voyage_row(mmsi, 1, rows[0][1], rows[-1][1], rows[0][2], rows[0][3], rows[-1][2], rows[-1][3], len(rows))],
    )
    _write_ports(ports_path, [("ROSTOCK", ROSTOCK_LAT, ROSTOCK_LON)])
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


def test_no_valid_imo_excluded(tmp_path):
    """The scope gate: an MMSI that never broadcasts a valid IMO is excluded outright, even with
    an otherwise-textbook course mismatch."""
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"

    mmsi = 111222666
    rows = _course_points(
        mmsi, lat=55.0, lon_start=9.50, cog=270.0, destination="ROSTOCK", imo="",
    )
    _write_clean_partition(in_root, DAY, rows)
    _write_voyages(
        voyages_path,
        [_voyage_row(mmsi, 1, rows[0][1], rows[-1][1], rows[0][2], rows[0][3], rows[-1][2], rows[-1][3], len(rows))],
    )
    _write_ports(ports_path, [("ROSTOCK", ROSTOCK_LAT, ROSTOCK_LON)])
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


def test_stationary_vessel_not_flagged(tmp_path):
    """A near-stationary vessel (sog below MIN_COURSE_CHECK_SOG_KNOTS) must never be flagged, even
    with an otherwise-textbook mismatched cog -- its COG is meaningless noise, not a real heading.
    Regression test for the real-run finding that ~30% of check 1's first-draft events were
    exactly this artefact."""
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"

    mmsi = 111222777
    rows = _course_points(
        mmsi, lat=55.0, lon_start=9.50, cog=270.0, destination="ROSTOCK", imo=VALID_IMO_A,
        sog=0.2,
    )
    _write_clean_partition(in_root, DAY, rows)
    _write_voyages(
        voyages_path,
        [_voyage_row(mmsi, 1, rows[0][1], rows[-1][1], rows[0][2], rows[0][3], rows[-1][2], rows[-1][3], len(rows))],
    )
    _write_ports(ports_path, [("ROSTOCK", ROSTOCK_LAT, ROSTOCK_LON)])
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


# ---------------------------------------------------------------------------
# Check 2: draught vs port calls
# ---------------------------------------------------------------------------


def _draught_change_fixture(tmp_path, mmsi, draught_1, draught_2, lat=57.0, lon=5.0):
    """Two consecutive voyages for one mmsi: draught_1 during the first, draught_2 during the
    second, both at the same position (open water, far from any real coastline). Returns the
    paths and the two voyages' (end_time, start_time) so a test can build overlapping evidence.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"

    v1_rows = _draught_points(mmsi, lat, lon, draught_1, VALID_IMO_B, start=_ts(0))
    v2_rows = _draught_points(mmsi, lat, lon, draught_2, VALID_IMO_B, start=_ts(8))
    _write_clean_partition(in_root, DAY, v1_rows + v2_rows)

    v1 = _voyage_row(mmsi, 1, v1_rows[0][1], v1_rows[-1][1], lat, lon, lat, lon, len(v1_rows))
    v2 = _voyage_row(mmsi, 2, v2_rows[0][1], v2_rows[-1][1], lat, lon, lat, lon, len(v2_rows))
    _write_voyages(voyages_path, [v1, v2])

    return in_root, voyages_path, v1[4], v2[3]  # in_root, voyages_path, v1.end_time, v2.start_time


def test_draught_change_unexplained_flagged(tmp_path):
    """A large draught change between two voyages, with no port/anchorage or STS evidence in the
    gap, must be flagged, with knowable_at = window_end (a whole-window judgement -- see module
    docstring's temporal-leakage section)."""
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"
    mmsi = 444555666

    in_root, voyages_path, _v1_end, _v2_start = _draught_change_fixture(tmp_path, mmsi, 5.0, 10.0)
    _write_empty_ports(ports_path)
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    events = _read_events(out_path)
    assert len(events) == 1
    e = events[0]
    assert e[0] == mmsi
    assert e[1] == "draught_change_unexplained"
    assert e[6] == pytest.approx(5.0)  # evidence_value: new - old
    assert e[3] == _window_end()


def test_draught_change_below_threshold_not_flagged(tmp_path):
    """A small draught change (under MIN_DRAUGHT_CHANGE_M) must never be flagged, evidence or not."""
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"
    mmsi = 444555777

    in_root, voyages_path, _v1_end, _v2_start = _draught_change_fixture(tmp_path, mmsi, 5.0, 6.0)
    _write_empty_ports(ports_path)
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


def test_draught_change_explained_by_anchorage_not_flagged(tmp_path):
    """A large draught change is not flagged if the voyage boundary sits at a known coastal
    anchorage that still qualifies (>= MIN_DISTINCT_VESSELS OTHER vessels) once this mmsi's own
    membership is excluded -- a plausible port call explains the change."""
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"
    mmsi = 444555888

    in_root, voyages_path, _v1_end, _v2_start = _draught_change_fixture(tmp_path, mmsi, 5.0, 10.0)
    _write_empty_ports(ports_path)
    # 5 OTHER vessels plus this mmsi -- still >= MIN_DISTINCT_VESSELS (5) after leave-one-out.
    _write_anchorages(anchorages_path, [(57.0, 5.0, [mmsi, *OTHER_MMSI[:5]], True)])
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


def test_draught_change_anchorage_self_membership_not_exonerating(tmp_path):
    """Leave-one-out regression test: a cell that qualifies as an anchorage ONLY because this
    mmsi itself is counted among its 5 required members must NOT count as evidence -- a vessel
    cannot use its own mooring to exonerate its own draught change. Found by a dedicated review
    pass as a real circularity bug, the same shape detect.sts already guards against for its own
    encounter pairs (P2-1b)."""
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"
    mmsi = 444555990

    in_root, voyages_path, _v1_end, _v2_start = _draught_change_fixture(tmp_path, mmsi, 5.0, 10.0)
    _write_empty_ports(ports_path)
    # Only 4 OTHER vessels plus this mmsi = 5 total (nominally qualifies), but excluding mmsi
    # itself leaves 4 < MIN_DISTINCT_VESSELS (5) -- must NOT count as evidence.
    _write_anchorages(anchorages_path, [(57.0, 5.0, [mmsi, *OTHER_MMSI[:4]], True)])
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    events = _read_events(out_path)
    assert len(events) == 1
    assert events[0][1] == "draught_change_unexplained"


def test_draught_change_explained_by_sts_not_flagged(tmp_path):
    """A large draught change is not flagged if a ship-to-ship transfer for this mmsi overlaps
    the gap between the two voyages."""
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"
    mmsi = 444555999

    in_root, voyages_path, v1_end, v2_start = _draught_change_fixture(tmp_path, mmsi, 5.0, 10.0)
    _write_empty_ports(ports_path)
    _write_empty_anchorages(anchorages_path)
    _write_sts(sts_path, [(mmsi, 999888777, v1_end, v2_start)])

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    assert _read_events(out_path) == []


def test_draught_change_knowable_at_is_window_end_even_with_long_next_voyage(tmp_path):
    """Regression test for the confirmed leak: an earlier version stamped knowable_at as
    next_voyage.start_time, which is impossible since the NEW draught is a median over the whole
    next voyage. This fixture's second voyage runs long after its own start_time to make that
    impossibility structurally visible, not just asserted."""
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"
    mmsi = 444556000

    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    lat, lon = 57.0, 5.0

    v1_rows = _draught_points(mmsi, lat, lon, 5.0, VALID_IMO_B, start=_ts(0))
    # Second voyage's draught pings are spread over many hours, well past its own start_time.
    v2_rows = _draught_points(mmsi, lat, lon, 10.0, VALID_IMO_B, start=_ts(8), n=20)
    _write_clean_partition(in_root, DAY, v1_rows + v2_rows)

    v1 = _voyage_row(mmsi, 1, v1_rows[0][1], v1_rows[-1][1], lat, lon, lat, lon, len(v1_rows))
    v2 = _voyage_row(mmsi, 2, v2_rows[0][1], v2_rows[-1][1], lat, lon, lat, lon, len(v2_rows))
    _write_voyages(voyages_path, [v1, v2])
    _write_empty_ports(ports_path)
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    out_path = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )

    events = _read_events(out_path)
    assert len(events) == 1
    knowable_at = events[0][3]
    assert knowable_at == _window_end()
    assert knowable_at > v2[4]  # strictly after the second voyage's own end_time (index 4)


def test_idempotent_skips_existing_output(tmp_path):
    """A second call with the same out_path is a no-op unless force=True."""
    in_root = tmp_path / "clean" / "ais_dk"
    voyages_path = tmp_path / "voyages.parquet"
    ports_path = tmp_path / "ports.parquet"
    anchorages_path = tmp_path / "anchorages.parquet"
    sts_path = tmp_path / "sts.parquet"
    out_root = tmp_path / "behaviour"

    mmsi = 111222999
    rows = _course_points(
        mmsi, lat=55.0, lon_start=9.50, cog=270.0, destination="ROSTOCK", imo=VALID_IMO_A,
    )
    _write_clean_partition(in_root, DAY, rows)
    _write_voyages(
        voyages_path,
        [_voyage_row(mmsi, 1, rows[0][1], rows[-1][1], rows[0][2], rows[0][3], rows[-1][2], rows[-1][3], len(rows))],
    )
    _write_ports(ports_path, [("ROSTOCK", ROSTOCK_LAT, ROSTOCK_LON)])
    _write_empty_anchorages(anchorages_path)
    _write_empty_sts(sts_path)

    result1 = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )
    mtime1 = result1.stat().st_mtime
    result2 = behaviour.build_behaviour_events(
        DAY, DAY, in_root=in_root, voyages_path=voyages_path, ports_path=ports_path,
        anchorages_path=anchorages_path, sts_path=sts_path, out_root=out_root,
    )
    assert result2 == result1
    assert result2.stat().st_mtime == mtime1


def test_missing_voyages_raises(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root, DAY, [(1, _ts(0), 55.0, 12.0, 90.0, 10.0, 0.0, "UNKNOWN", "", "Class A")]
    )
    with pytest.raises(FileNotFoundError, match="voyages"):
        behaviour.build_behaviour_events(
            DAY, DAY, in_root=in_root, voyages_path=tmp_path / "missing_voyages.parquet",
            ports_path=tmp_path / "ports.parquet", anchorages_path=tmp_path / "anchorages.parquet",
            sts_path=tmp_path / "sts.parquet", out_root=tmp_path / "behaviour",
        )
