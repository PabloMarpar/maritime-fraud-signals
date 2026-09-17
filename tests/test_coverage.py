"""Unit tests for detect.coverage. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from detect import coverage

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
DAY3 = date(2024, 6, 7)

BASE = datetime(2024, 6, 5, 0, 0, 0)  # noqa: DTZ001


def _ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _spaced(base: datetime, count: int, step_minutes: float) -> list[datetime]:
    """count timestamps, step_minutes apart, starting at base. Avoids passing an
    out-of-range minute value directly, unlike constructing datetime(..., minute=190).
    """
    return [base + timedelta(minutes=step_minutes * i) for i in range(count)]


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, latitude, longitude, ship_type) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, "
            "longitude DOUBLE, ship_type VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read_grid(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT cell_lat, cell_lon, ship_type, n_pairs, n_short_pairs, coverage_probability "
            f"FROM '{out_path.as_posix()}' ORDER BY cell_lat, cell_lon, ship_type"
        ).fetchall()
    finally:
        con.close()


def test_consistently_short_gaps_give_high_coverage(tmp_path):
    """A vessel reporting every few minutes in one cell -> coverage close to 1.0."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000001, ts, 55.05, 12.05, "Cargo") for ts in _spaced(BASE, 20, 10)
    ]  # 20 messages, 10 min apart -> 19 short pairs
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=10)

    result = _read_grid(out_path)
    assert len(result) == 1
    cell_lat, cell_lon, ship_type, n_pairs, n_short_pairs, prob = result[0]
    assert (cell_lat, cell_lon) == (55.0, 12.0)
    assert ship_type == "Cargo"
    assert n_pairs == 19
    assert n_short_pairs == 19
    assert prob == pytest.approx(1.0)


def test_long_gap_lowers_coverage_in_its_own_cell(tmp_path):
    """A vessel silent for a long stretch while positioned elsewhere: that cell's coverage
    reflects the long pair, distinct from a cell that only ever saw short pairs.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        # Cell A (55.0, 12.0): consistently short gaps.
        *[(219000002, ts, 55.05, 12.05, "Cargo") for ts in _spaced(BASE, 22, 10)],
        # A long silence starts from a different cell, B (56.0, 13.0).
        (219000002, BASE + timedelta(hours=4), 56.05, 13.05, "Cargo"),
        (219000002, BASE + timedelta(hours=10), 56.06, 13.06, "Cargo"),  # 6h later: long gap
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=1)

    result = _read_grid(out_path)
    by_cell = {(r[0], r[1]): r for r in result}
    cell_a = by_cell[(55.0, 12.0)]
    cell_b = by_cell[(56.0, 13.0)]
    assert cell_a[5] == pytest.approx(1.0)  # coverage_probability, all short
    assert cell_b[3] == 1  # n_pairs: just the one long pair
    assert cell_b[4] == 0  # n_short_pairs
    assert cell_b[5] == pytest.approx(0.0)


def test_below_min_pairs_is_null_not_zero_or_one(tmp_path):
    """A (cell, ship_type) with fewer than min_pairs observations: NULL, not a spurious 0/1."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000003, ts, 55.05, 12.05, "Tanker") for ts in _spaced(BASE, 3, 5)
    ]  # only 2 pairs
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=20)

    result = _read_grid(out_path)
    assert len(result) == 1
    _cell_lat, _cell_lon, _ship_type, n_pairs, _n_short, prob = result[0]
    assert n_pairs == 2
    assert prob is None


def test_at_or_above_min_pairs_gets_an_estimate(tmp_path):
    """Negative case for the threshold: exactly min_pairs pairs is enough for a non-NULL estimate."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000004, ts, 55.05, 12.05, "Tanker") for ts in _spaced(BASE, 11, 5)
    ]  # 10 pairs
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=10)

    result = _read_grid(out_path)
    assert len(result) == 1
    assert result[0][3] == 10  # n_pairs
    assert result[0][5] is not None


def test_different_ship_types_in_same_cell_not_pooled(tmp_path):
    """Two vessel classes passing through the same geographic cell get separate rows/estimates."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        *[(219000005, ts, 55.05, 12.05, "Cargo") for ts in _spaced(BASE, 22, 10)],  # short gaps
        # Same cell, different class, with a long gap so its coverage differs from Cargo's.
        (219000006, BASE, 55.06, 12.06, "Fishing"),
        (219000006, BASE + timedelta(hours=8), 55.07, 12.07, "Fishing"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=1)

    result = _read_grid(out_path)
    by_type = {r[2]: r for r in result}
    assert set(by_type) == {"Cargo", "Fishing"}
    assert by_type["Cargo"][0] == 55.0
    assert by_type["Fishing"][0] == 55.0  # same cell
    assert by_type["Cargo"][5] == pytest.approx(1.0)
    assert by_type["Fishing"][5] == pytest.approx(0.0)


def test_grid_cell_boundary_flooring(tmp_path):
    """Hand-picked lat/lon values on/near 0.1-degree boundaries resolve to the expected cell,
    including negative longitudes.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        # Exactly on a boundary: 55.1 belongs to the 55.1 cell, not 55.0.
        (219000007, _ts(0, 0), 55.1, 12.1, "Cargo"),
        (219000007, _ts(0, 5), 55.14, 12.14, "Cargo"),
        # Negative longitude, just under a boundary: -12.35 floors to the -12.4 cell.
        (219000008, _ts(0, 0), 40.0, -12.35, "Cargo"),
        (219000008, _ts(0, 5), 40.02, -12.32, "Cargo"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=1)

    result = _read_grid(out_path)
    cells = {(r[0], r[1]) for r in result}
    assert (55.1, 12.1) in cells
    assert (40.0, -12.4) in cells


def test_null_ship_type_handled_without_crash(tmp_path):
    """A row with a null ship_type (no forward-fill implemented, see module docstring) still
    produces a pair; it just forms its own NULL-keyed group rather than crashing or merging
    into a named class.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000015, _ts(0, 0), 55.05, 12.05, None),
        (219000015, _ts(0, 5), 55.06, 12.06, None),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=1)

    result = _read_grid(out_path)
    assert len(result) == 1
    assert result[0][2] is None  # ship_type
    assert result[0][3] == 1  # n_pairs


def test_coverage_map_skips_missing_day_with_warning(tmp_path, caplog):
    """A day with no clean partition in the middle of a range is skipped, not fatal."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root, DAY, [(219000009, _ts(0, 0), 55.05, 12.05, "Cargo")]
    )
    # DAY2 deliberately missing.
    _write_clean_partition(
        in_root, DAY3, [(219000009, _ts(0, 0, day=DAY3), 55.06, 12.06, "Cargo")]
    )
    out_path = tmp_path / "coverage" / "grid.parquet"

    with caplog.at_level("WARNING"):
        coverage.build_coverage_map(DAY, DAY3, in_root=in_root, out_path=out_path, min_pairs=1)

    assert any("2024-06-06" in record.message for record in caplog.records)
    result = _read_grid(out_path)
    assert len(result) == 1
    assert result[0][3] == 1  # one pair, spanning the two days that do exist


def test_coverage_map_raises_when_no_partitions_exist(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_path = tmp_path / "coverage" / "grid.parquet"

    with pytest.raises(FileNotFoundError):
        coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path)


def test_coverage_map_is_idempotent_by_default(tmp_path):
    """Re-running without force must not rebuild the output."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (219000010, _ts(0, 0), 55.05, 12.05, "Cargo"),
            (219000010, _ts(0, 5), 55.06, 12.06, "Cargo"),
        ],
    )
    out_path = tmp_path / "coverage" / "grid.parquet"

    first = coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path)
    first_mtime = first.stat().st_mtime_ns

    second = coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite the file"


def test_coverage_map_force_rebuilds(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (219000011, _ts(0, 0), 55.05, 12.05, "Cargo"),
            (219000011, _ts(0, 5), 55.06, 12.06, "Cargo"),
        ],
    )
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path)
    result = coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, force=True)

    assert result.exists()


def test_single_message_mmsi_contributes_no_pairs(tmp_path):
    """Negative case: a lone message from an mmsi has no 'prev', so it contributes zero pairs
    and must not crash or appear as a spurious cell.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000012, _ts(0, 0), 55.05, 12.05, "Cargo")]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY, in_root=in_root, out_path=out_path, min_pairs=1)

    result = _read_grid(out_path)
    assert result == []


def test_custom_grid_size_and_short_gap_thresholds(tmp_path):
    """Caller-supplied grid_size_deg and short_gap_seconds change the output."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000013, _ts(0, 0), 55.25, 12.25, "Cargo"),
        (219000013, _ts(0, 20), 55.26, 12.26, "Cargo"),  # 20 min gap
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "coverage" / "grid.parquet"

    # A coarser 0.5-degree grid puts both points in the same (55.0, 12.0) cell,
    # and a strict 10-minute short-gap threshold makes the 20-minute pair "long".
    coverage.build_coverage_map(
        DAY,
        DAY,
        in_root=in_root,
        out_path=out_path,
        grid_size_deg=0.5,
        short_gap_seconds=600,
        min_pairs=1,
    )

    result = _read_grid(out_path)
    assert len(result) == 1
    assert (result[0][0], result[0][1]) == (55.0, 12.0)
    assert result[0][5] == pytest.approx(0.0)  # the one pair is "long" under the strict threshold


def test_provenance_columns_populated(tmp_path):
    """window_start/window_end match the call's start/end, git_sha is a non-empty string in
    this (git) repo, and built_at is a real timestamp -- see module docstring, finding A.
    """
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (219000016, _ts(0, 0), 55.05, 12.05, "Cargo"),
            (219000016, _ts(0, 5), 55.06, 12.06, "Cargo"),
        ],
    )
    out_path = tmp_path / "coverage" / "grid.parquet"

    before = datetime.now(timezone.utc)
    coverage.build_coverage_map(DAY, DAY2, in_root=in_root, out_path=out_path, min_pairs=1)
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


def test_coverage_spans_a_day_boundary(tmp_path):
    """A gap across midnight is still one pair, decoupled from any per-day partition boundary."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root, DAY, [(219000014, _ts(23, 55, day=DAY), 55.05, 12.05, "Cargo")]
    )
    _write_clean_partition(
        in_root, DAY2, [(219000014, _ts(0, 5, day=DAY2), 55.06, 12.06, "Cargo")]
    )
    out_path = tmp_path / "coverage" / "grid.parquet"

    coverage.build_coverage_map(DAY, DAY2, in_root=in_root, out_path=out_path, min_pairs=1)

    result = _read_grid(out_path)
    assert len(result) == 1
    assert result[0][3] == 1  # n_pairs
    assert result[0][5] == pytest.approx(1.0)  # 10-minute gap is short
