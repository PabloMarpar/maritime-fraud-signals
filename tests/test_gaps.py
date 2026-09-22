"""Unit tests for detect.gaps. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from detect import gaps
from detect.liveness import LivenessVerdict

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


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


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude, type_of_mobile) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, type_of_mobile VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _voyage_row(
    mmsi: int,
    seq: int,
    start: datetime,
    end: datetime,
    start_lat: float = 55.0,
    start_lon: float = 12.0,
    end_lat: float = 55.0,
    end_lon: float = 12.0,
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
        2,
        int((end - start).total_seconds()),
    )


# ---------------------------------------------------------------------------
# candidate_gaps
# ---------------------------------------------------------------------------


def test_candidate_gaps_pairs_consecutive_voyages(tmp_path):
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    rows = [
        _voyage_row(111, 1, _ts(0), _ts(1), start_lat=55.0, start_lon=12.0, end_lat=55.1, end_lon=12.1),
        _voyage_row(111, 2, _ts(3), _ts(4), start_lat=55.2, start_lon=12.2, end_lat=55.3, end_lon=12.3),
        _voyage_row(111, 3, _ts(6), _ts(7), start_lat=55.4, start_lon=12.4, end_lat=55.5, end_lon=12.5),
    ]
    _write_voyages(voyages_path, rows)

    con = duckdb.connect()
    try:
        result = gaps.candidate_gaps(con, voyages_path)
    finally:
        con.close()

    assert len(result) == 2

    first, second = sorted(result, key=lambda g: g.gap_start)
    assert first.mmsi == 111
    assert first.prev_voyage_id == "111-1"
    assert first.next_voyage_id == "111-2"
    assert first.gap_start == _ts(1)
    assert first.gap_end == _ts(3)
    assert first.duration_hours == pytest.approx(2.0)
    assert (first.start_latitude, first.start_longitude) == (55.1, 12.1)
    assert (first.end_latitude, first.end_longitude) == (55.2, 12.2)

    assert second.prev_voyage_id == "111-2"
    assert second.next_voyage_id == "111-3"
    assert second.gap_start == _ts(4)
    assert second.gap_end == _ts(6)
    assert second.duration_hours == pytest.approx(2.0)
    assert (second.start_latitude, second.start_longitude) == (55.3, 12.3)
    assert (second.end_latitude, second.end_longitude) == (55.4, 12.4)


def test_single_voyage_produces_no_gaps(tmp_path):
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_voyages(voyages_path, [_voyage_row(222, 1, _ts(0), _ts(1))])

    con = duckdb.connect()
    try:
        result = gaps.candidate_gaps(con, voyages_path)
    finally:
        con.close()

    assert result == []


def test_candidate_gaps_date_range_filtering(tmp_path):
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    rows = [
        # gap_start on DAY (in range)
        _voyage_row(111, 1, _ts(0, day=DAY), _ts(1, day=DAY)),
        _voyage_row(111, 2, _ts(3, day=DAY), _ts(4, day=DAY)),
        # gap_start on DAY2 (out of [DAY, DAY) range if end==DAY, but keep separate mmsi
        # to also exercise the "no successor" filter independently)
        _voyage_row(333, 1, _ts(0, day=DAY2), _ts(1, day=DAY2)),
        _voyage_row(333, 2, _ts(3, day=DAY2), _ts(4, day=DAY2)),
    ]
    _write_voyages(voyages_path, rows)

    con = duckdb.connect()
    try:
        result = gaps.candidate_gaps(con, voyages_path, start=DAY, end=DAY2)
    finally:
        con.close()

    assert len(result) == 1
    assert result[0].mmsi == 111
    assert result[0].gap_start.date() == DAY


# ---------------------------------------------------------------------------
# gap_probability
# ---------------------------------------------------------------------------


def _verdict(kind: str) -> LivenessVerdict:
    return LivenessVerdict(
        verdict=kind,
        n_corroborators=0,
        n_messages=0,
        n_corroborators_class_a=0,
        baseline_vessel_hours=0.0,
        n_baseline_vessels=0,
        baseline_hours_available=0.0,
        expected_corroborators=0.0,
        n_cells=1,
        n_cell_hours_scanned=0,
    )


def test_gap_probability_short_gap_uses_base_rate():
    assert gaps.gap_probability(_verdict("receiver_alive"), 2.0) == 0.9
    assert gaps.gap_probability(_verdict("area_dark"), 2.0) == 0.15
    assert gaps.gap_probability(_verdict("no_evidence"), 2.0) == 0.5


def test_gap_probability_long_gap_clamps_receiver_alive_down():
    assert gaps.gap_probability(_verdict("receiver_alive"), 12.0) == 0.6


def test_gap_probability_long_gap_clamps_area_dark_up():
    assert gaps.gap_probability(_verdict("area_dark"), 12.0) == 0.4


def test_gap_probability_boundary_at_exactly_long_gap_hours_clamps():
    assert gaps.gap_probability(_verdict("receiver_alive"), gaps.LONG_GAP_HOURS) == 0.6


def test_gap_probability_just_under_long_gap_hours_does_not_clamp():
    assert gaps.gap_probability(_verdict("receiver_alive"), gaps.LONG_GAP_HOURS - 0.01) == 0.9
    assert gaps.gap_probability(_verdict("area_dark"), gaps.LONG_GAP_HOURS - 0.01) == 0.15


# ---------------------------------------------------------------------------
# _to_cell
# ---------------------------------------------------------------------------


def test_to_cell_floors_raw_position_to_grid_cell():
    assert gaps._to_cell(55.05, 12.05) == (55.0, 12.0)
    assert gaps._to_cell(55.0, 12.0) == (55.0, 12.0)


def test_to_cell_matches_liveness_grid_cell_values():
    """A gap endpoint's cell must be byte-identical to the cell a vessel there would be recorded
    under in liveness.parquet, or score_gap's join would silently find no evidence anywhere.
    """
    from detect.liveness import cells_within

    assert cells_within([gaps._to_cell(55.05, -12.05)]) == [(55.0, -12.1)]


# ---------------------------------------------------------------------------
# build_gap_scores (end to end)
# ---------------------------------------------------------------------------


def test_build_gap_scores_end_to_end(tmp_path):
    clean_root = tmp_path / "clean" / "ais_dk"
    # 333 heard near the gap endpoints of 111 while 111 is silent -> receiver_alive.
    _write_clean_partition(
        clean_root,
        DAY,
        [
            (333, _ts(2), 55.05, 12.05, "Class A"),
            (111, _ts(0), 55.05, 12.05, "Class A"),
        ],
    )
    liveness_path = tmp_path / "coverage" / "liveness"
    from detect import liveness

    liveness.build_liveness(DAY, DAY, in_root=clean_root, out_root=liveness_path)

    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    rows = [
        _voyage_row(111, 1, _ts(0), _ts(1), start_lat=55.0, start_lon=12.0, end_lat=55.05, end_lon=12.05),
        _voyage_row(111, 2, _ts(3), _ts(4), start_lat=55.05, start_lon=12.05, end_lat=55.1, end_lon=12.1),
        # 999 has only one voyage -> no gap.
        _voyage_row(999, 1, _ts(0), _ts(1)),
    ]
    _write_voyages(voyages_path, rows)

    out_path = tmp_path / "detect" / "gaps.parquet"
    result_path = gaps.build_gap_scores(
        DAY, DAY2, voyages_path=voyages_path, liveness_path=liveness_path, out_path=out_path
    )

    assert result_path == out_path
    con = duckdb.connect()
    try:
        rows_out = con.execute(
            "SELECT mmsi, verdict, probability, built_at, git_sha, window_start, window_end "
            f"FROM '{out_path.as_posix()}'"
        ).fetchall()
    finally:
        con.close()

    assert len(rows_out) == 1
    mmsi, verdict, probability, built_at, git_sha, window_start, window_end = rows_out[0]
    assert mmsi == 111
    assert verdict == "receiver_alive"
    assert 0.0 <= probability <= 1.0
    assert probability == 0.9  # short gap (3h), no clamping
    assert built_at is not None
    assert isinstance(git_sha, str) and git_sha != ""
    assert window_start == DAY
    assert window_end == DAY2


def test_build_gap_scores_is_idempotent_by_default(tmp_path, caplog):
    clean_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(clean_root, DAY, [(111, _ts(0), 55.05, 12.05, "Class A")])
    liveness_path = tmp_path / "coverage" / "liveness"
    from detect import liveness

    liveness.build_liveness(DAY, DAY, in_root=clean_root, out_root=liveness_path)

    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_voyages(
        voyages_path,
        [
            _voyage_row(111, 1, _ts(0), _ts(1)),
            _voyage_row(111, 2, _ts(3), _ts(4)),
        ],
    )

    out_path = tmp_path / "detect" / "gaps.parquet"
    first = gaps.build_gap_scores(
        DAY, DAY2, voyages_path=voyages_path, liveness_path=liveness_path, out_path=out_path
    )
    first_mtime = first.stat().st_mtime_ns

    with caplog.at_level("INFO"):
        second = gaps.build_gap_scores(
            DAY, DAY2, voyages_path=voyages_path, liveness_path=liveness_path, out_path=out_path
        )

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite the file"
    assert any("already exists" in record.message for record in caplog.records)


def test_build_gap_scores_raises_when_voyages_missing(tmp_path):
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness_path.parent.mkdir(parents=True)
    liveness_path.touch()
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    out_path = tmp_path / "detect" / "gaps.parquet"

    with pytest.raises(FileNotFoundError, match="voyages"):
        gaps.build_gap_scores(
            DAY, DAY2, voyages_path=voyages_path, liveness_path=liveness_path, out_path=out_path
        )


def test_build_gap_scores_raises_when_liveness_missing(tmp_path):
    voyages_path = tmp_path / "tracks" / "voyages.parquet"
    _write_voyages(
        voyages_path,
        [
            _voyage_row(111, 1, _ts(0), _ts(1)),
            _voyage_row(111, 2, _ts(3), _ts(4)),
        ],
    )
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    out_path = tmp_path / "detect" / "gaps.parquet"

    with pytest.raises(FileNotFoundError, match="liveness"):
        gaps.build_gap_scores(
            DAY, DAY2, voyages_path=voyages_path, liveness_path=liveness_path, out_path=out_path
        )
