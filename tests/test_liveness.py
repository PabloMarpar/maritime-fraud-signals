"""Unit tests for detect.liveness. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from detect import liveness

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
PREV_DAY = date(2024, 6, 4)

BASE = datetime(2024, 6, 5, 0, 0, 0)  # noqa: DTZ001
PREV_BASE = datetime(2024, 6, 4, 0, 0, 0)  # noqa: DTZ001


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


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


def _read_presence(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT cell_lat, cell_lon, cell_hour, mmsi, is_class_a, n_messages, "
            "first_seen, last_seen "
            f"FROM '{out_path.as_posix()}' ORDER BY cell_lat, cell_lon, cell_hour, mmsi"
        ).fetchall()
    finally:
        con.close()


def test_base_station_and_aton_excluded_from_liveness(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000001, _ts(0), 55.05, 12.05, "Class A"),
        (219000002, _ts(0), 55.06, 12.06, "Base Station"),
        (219000003, _ts(0), 55.07, 12.07, "AtoN"),
        (219000004, _ts(0), 55.08, 12.08, "SAR Airborne"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "liveness.parquet"

    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_presence(out_path)
    assert [row[3] for row in result] == [219000001]


def test_presence_is_one_row_per_cell_hour_mmsi(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000005, _ts(0, minute=m), 55.05, 12.05, "Class A") for m in range(0, 50, 5)]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "liveness.parquet"

    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_presence(out_path)
    assert len(result) == 1
    row = result[0]
    assert row[5] == 10  # n_messages
    assert row[6] == _ts(0, minute=0)  # first_seen
    assert row[7] == _ts(0, minute=45)  # last_seen


def test_vessel_crossing_cell_boundary_gets_two_rows(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000006, _ts(0, 0), 55.05, 12.05, "Class A"),
        (219000006, _ts(0, 10), 56.05, 13.05, "Class A"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "liveness.parquet"

    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_presence(out_path)
    cells = {(row[0], row[1]) for row in result}
    assert cells == {(55.0, 12.0), (56.0, 13.0)}


def test_vessel_spanning_hour_boundary_gets_two_rows(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000007, _ts(0, 55), 55.05, 12.05, "Class A"),
        (219000007, _ts(1, 5), 55.06, 12.06, "Class A"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "liveness.parquet"

    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_presence(out_path)
    hours = {row[2] for row in result}
    assert hours == {_ts(0), _ts(1)}


def test_leave_one_out_excludes_own_messages(tmp_path):
    """A vessel alone in a cell during its own silence window must not corroborate itself."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(111, _ts(10), 55.05, 12.05, "Class A")]
    _write_clean_partition(in_root, DAY, rows)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(9),
            window_end=_ts(11),
            exclude_mmsi=111,
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.n_corroborators == 0
    assert verdict.verdict != "receiver_alive"


def test_area_dark_baseline_also_excludes_self(tmp_path):
    """The sole historical occupant of a cell must not become its own area_dark excuse -- and
    the same fixture, queried without excluding the cell's only occupant, must show area_dark
    IS reachable, so the no_evidence result above isn't just because area_dark never fires here.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows_prev = [
        # C_sole (55.0, 12.0): vessel 111 alone, all 24 hours of PREV_DAY.
        *[(111, PREV_BASE + timedelta(hours=h), 55.05, 12.05, "Class A") for h in range(24)],
        # C_multi (56.0, 13.0): three DIFFERENT vessels, 8 hours each -- real multi-vessel traffic.
        *[(222, PREV_BASE + timedelta(hours=h), 56.05, 13.05, "Class A") for h in range(8)],
        *[(333, PREV_BASE + timedelta(hours=h), 56.05, 13.05, "Class A") for h in range(8, 16)],
        *[(444, PREV_BASE + timedelta(hours=h), 56.05, 13.05, "Class A") for h in range(16, 24)],
    ]
    _write_clean_partition(in_root, PREV_DAY, rows_prev)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(PREV_DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        sole_occupant_excluded = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(9),
            window_end=_ts(12),
            exclude_mmsi=111,
            liveness_path=liveness_path,
            as_of=DAY,
        )
        multi_vessel_baseline = liveness.liveness_verdict(
            con,
            cells=[(56.0, 13.0)],
            window_start=_ts(9),
            window_end=_ts(12),
            exclude_mmsi=999,  # never present -- excludes nobody real
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert sole_occupant_excluded.n_baseline_vessels == 0
    assert sole_occupant_excluded.baseline_vessel_hours == 0
    assert sole_occupant_excluded.verdict == "no_evidence"

    assert multi_vessel_baseline.n_baseline_vessels == 3
    assert multi_vessel_baseline.verdict == "area_dark", (
        "the fixture must be capable of area_dark, or the no_evidence result above proves nothing"
    )


def test_single_recurring_vessel_does_not_trigger_area_dark(tmp_path):
    """One vessel reporting every hour for the whole baseline must not, by itself, produce
    enough (vessel-hour) volume to justify area_dark: min_baseline_vessels requires breadth,
    not just a lot of history from one source.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows_prev = [(555, PREV_BASE + timedelta(hours=h), 55.05, 12.05, "Class A") for h in range(24)]
    _write_clean_partition(in_root, PREV_DAY, rows_prev)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(PREV_DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(9),
            window_end=_ts(12),
            exclude_mmsi=999,  # not the recurring vessel -- its history remains in the baseline
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.n_baseline_vessels == 1
    assert verdict.verdict != "area_dark"


def test_receiver_alive_when_another_vessel_heard(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(222, _ts(10), 55.05, 12.05, "Class A")]
    _write_clean_partition(in_root, DAY, rows)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(9),
            window_end=_ts(11),
            exclude_mmsi=111,
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.verdict == "receiver_alive"
    assert verdict.n_corroborators == 1


def test_no_evidence_when_cell_never_occupied(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(222, _ts(10), 55.05, 12.05, "Class A")]
    _write_clean_partition(in_root, DAY, rows)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(10.0, 10.0)],
            window_start=_ts(9),
            window_end=_ts(11),
            exclude_mmsi=999,
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.verdict == "no_evidence"
    assert verdict.n_corroborators == 0
    assert verdict.baseline_vessel_hours == 0


def test_partial_hour_overlap_not_counted(tmp_path):
    """A corroborator present for 3 minutes of an hour bucket must not count for a silence
    window covering a different part of that same hour.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(222, _ts(0, 0), 55.05, 12.05, "Class A"), (222, _ts(0, 3), 55.05, 12.05, "Class A")]
    _write_clean_partition(in_root, DAY, rows)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(0, 30),
            window_end=_ts(0, 59),
            exclude_mmsi=111,
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.n_corroborators == 0
    assert verdict.verdict != "receiver_alive"


def test_baseline_ignores_hours_at_or_after_as_of(tmp_path):
    """Evidence on or after as_of's day must not inflate the baseline used to judge a silence
    on that same day.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows_prev = [(222, PREV_BASE + timedelta(hours=h), 55.05, 12.05, "Class A") for h in (1, 2, 3)]
    rows_same_day = [(222, _ts(20), 55.05, 12.05, "Class A")]
    _write_clean_partition(in_root, PREV_DAY, rows_prev)
    _write_clean_partition(in_root, DAY, rows_same_day)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(PREV_DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(9),
            window_end=_ts(11),
            exclude_mmsi=111,
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.baseline_vessel_hours == 3
    assert verdict.n_cell_hours_scanned == 3, (
        "the hour-20 row on DAY is after window_end and must not appear in any scanned quantity, "
        "including n_cell_hours_scanned"
    )


def test_liveness_verdict_rejects_as_of_after_window_start(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(222, _ts(10), 55.05, 12.05, "Class A")])
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        with pytest.raises(ValueError, match="as_of"):
            liveness.liveness_verdict(
                con,
                cells=[(55.0, 12.0)],
                window_start=_ts(9),
                window_end=_ts(11),
                exclude_mmsi=111,
                liveness_path=liveness_path,
                as_of=DAY2,
            )
    finally:
        con.close()


def test_liveness_verdict_rejects_tz_aware_datetime(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(222, _ts(10), 55.05, 12.05, "Class A")])
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        with pytest.raises(ValueError, match="naive"):
            liveness.liveness_verdict(
                con,
                cells=[(55.0, 12.0)],
                window_start=_ts(9).replace(tzinfo=timezone.utc),
                window_end=_ts(11),
                exclude_mmsi=111,
                liveness_path=liveness_path,
                as_of=DAY,
            )
    finally:
        con.close()


def test_exclude_mmsi_accepts_a_sequence(tmp_path):
    """A vessel that reappears under a second identity must be excludable as a set, not just
    one mmsi at a time -- see the module docstring on leave-one-out and identity linkage.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (111, _ts(10), 55.05, 12.05, "Class A"),
        (222, _ts(10, minute=30), 55.05, 12.05, "Class A"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    liveness_path = tmp_path / "coverage" / "liveness.parquet"
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=liveness_path)

    con = duckdb.connect()
    try:
        verdict = liveness.liveness_verdict(
            con,
            cells=[(55.0, 12.0)],
            window_start=_ts(9),
            window_end=_ts(12),
            exclude_mmsi=[111, 222],
            liveness_path=liveness_path,
            as_of=DAY,
        )
    finally:
        con.close()

    assert verdict.n_corroborators == 0
    assert verdict.verdict != "receiver_alive"


def test_cells_within_ring_zero_returns_input():
    assert liveness.cells_within([(55.0, 12.0)], ring=0) == [(55.0, 12.0)]


def test_cells_within_ring_one_returns_3x3_block():
    result = liveness.cells_within([(55.0, 12.0)], ring=1)
    expected = {
        (round(55.0 + dlat * 0.1, 6), round(12.0 + dlon * 0.1, 6))
        for dlat in (-1, 0, 1)
        for dlon in (-1, 0, 1)
    }
    assert set(result) == expected
    assert len(result) == 9


def test_grid_cell_values_match_coverage_module(tmp_path):
    """The liveness and coverage grids must key on byte-identical cell values for the same raw
    position, or a join between the two artifacts would silently lose rows.
    """
    from detect import coverage

    in_root = tmp_path / "clean" / "ais_dk"
    partition_dir = in_root / f"date={DAY.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, ship_type VARCHAR, type_of_mobile VARCHAR)"
        )
        con.executemany(
            "INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?)",
            [
                (111, _ts(0), 55.05, -12.05, "Cargo", "Class A"),
                (111, _ts(0, minute=10), 55.06, -12.04, "Cargo", "Class A"),
            ],
        )
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()

    cov_path = tmp_path / "coverage" / "grid.parquet"
    live_path = tmp_path / "coverage" / "liveness.parquet"
    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=cov_path, min_pairs=1)
    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=live_path)

    con = duckdb.connect()
    try:
        cov_cells = set(
            con.execute(f"SELECT DISTINCT cell_lat, cell_lon FROM '{cov_path.as_posix()}'").fetchall()
        )
        live_cells = set(
            con.execute(f"SELECT DISTINCT cell_lat, cell_lon FROM '{live_path.as_posix()}'").fetchall()
        )
    finally:
        con.close()

    assert cov_cells and cov_cells == live_cells


def test_liveness_is_idempotent_by_default(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000010, _ts(0), 55.05, 12.05, "Class A")])
    out_path = tmp_path / "coverage" / "liveness.parquet"

    first = liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)
    first_mtime = first.stat().st_mtime_ns

    second = liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite the file"


def test_liveness_force_rebuilds(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000011, _ts(0), 55.05, 12.05, "Class A")])
    out_path = tmp_path / "coverage" / "liveness.parquet"

    liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)
    result = liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path, force=True)

    assert result.exists()


def test_liveness_skips_missing_day_with_warning(tmp_path, caplog):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000012, _ts(0), 55.05, 12.05, "Class A")])
    out_path = tmp_path / "coverage" / "liveness.parquet"

    with caplog.at_level("WARNING"):
        liveness.build_liveness(DAY, DAY2, in_root=in_root, out_path=out_path)

    assert any("2024-06-06" in record.message for record in caplog.records)
    result = _read_presence(out_path)
    assert len(result) == 1


def test_liveness_raises_when_no_partitions_exist(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_path = tmp_path / "coverage" / "liveness.parquet"

    with pytest.raises(FileNotFoundError):
        liveness.build_liveness(DAY, DAY, in_root=in_root, out_path=out_path)


def test_liveness_verdict_raises_when_table_missing(tmp_path):
    con = duckdb.connect()
    try:
        with pytest.raises(FileNotFoundError):
            liveness.liveness_verdict(
                con,
                cells=[(55.0, 12.0)],
                window_start=_ts(9),
                window_end=_ts(11),
                exclude_mmsi=111,
                liveness_path=tmp_path / "coverage" / "liveness.parquet",
                as_of=DAY,
            )
    finally:
        con.close()


def test_liveness_provenance_columns_populated(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000016, _ts(0), 55.05, 12.05, "Class A")])
    out_path = tmp_path / "coverage" / "liveness.parquet"

    before = datetime.now(timezone.utc)
    liveness.build_liveness(DAY, DAY2, in_root=in_root, out_path=out_path)
    after = datetime.now(timezone.utc)

    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT DISTINCT window_start, window_end, built_at, git_sha "
            f"FROM '{out_path.as_posix()}'"
        ).fetchall()
    finally:
        con.close()

    assert len(row) == 1, "provenance columns must be constant across every row of one build"
    window_start, window_end, built_at, git_sha = row[0]
    assert window_start == DAY
    assert window_end == DAY2
    assert before.replace(tzinfo=None) <= built_at <= after.replace(tzinfo=None)
    assert isinstance(git_sha, str) and git_sha != ""
