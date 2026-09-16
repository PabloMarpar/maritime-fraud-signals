"""Unit tests for process.identity. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from process import identity

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
DAY3 = date(2024, 6, 7)

# Real IMO numbers (verified check digit) used across tests.
VALID_IMO_A = "9074729"
VALID_IMO_B = "9264386"
VALID_IMO_C = "9806847"
VALID_IMO_D = "2000004"


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, imo) tuples; imo may be None for a missing field.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE clean (mmsi BIGINT, imo VARCHAR)")
        con.executemany("INSERT INTO clean VALUES (?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read_identity(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT mmsi, imo, message_count, first_seen, last_seen, is_orphaned, is_reused "
            f"FROM '{out_path.as_posix()}' ORDER BY mmsi, imo"
        ).fetchall()
    finally:
        con.close()


def test_one_to_one_mmsi_imo_pairing(tmp_path):
    """A clean vessel: one mmsi always reporting the same valid imo."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000001, VALID_IMO_A)] * 5
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    mmsi, imo, count, first_seen, last_seen, is_orphaned, is_reused = result[0]
    assert (mmsi, imo, count) == (219000001, VALID_IMO_A, 5)
    assert first_seen == DAY
    assert last_seen == DAY
    assert is_orphaned is False
    assert is_reused is False


def test_orphaned_mmsi_never_has_valid_imo(tmp_path):
    """An mmsi that never reports a valid imo shows up as a single orphan sentinel row."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000002, None),
        (219000002, ""),
        (219000002, "Unknown"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    mmsi, imo, _count, _first_seen, _last_seen, is_orphaned, is_reused = result[0]
    assert mmsi == 219000002
    assert imo is None
    assert is_orphaned is True
    assert is_reused is False


def test_not_orphaned_negative_case(tmp_path):
    """Negative case: an mmsi with a valid imo must not also be flagged orphaned."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000003, VALID_IMO_A)]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][5] is False  # is_orphaned


def test_reused_mmsi_same_day(tmp_path):
    """Two distinct valid imos reported by the same mmsi on the same day: both flagged reused."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000004, VALID_IMO_A),
        (219000004, VALID_IMO_B),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 2
    imos = {row[1] for row in result}
    assert imos == {VALID_IMO_A, VALID_IMO_B}
    assert all(row[6] is True for row in result)  # is_reused


def test_reused_mmsi_across_days(tmp_path):
    """Two distinct valid imos reported by the same mmsi on different days: still detected."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000005, VALID_IMO_A)])
    _write_clean_partition(in_root, DAY2, [(219000005, VALID_IMO_B)])
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY2, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 2
    assert all(row[6] is True for row in result)  # is_reused


def test_not_reused_negative_case(tmp_path):
    """Negative case: an mmsi with a single distinct valid imo across the range is not reused."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000006, VALID_IMO_A)])
    _write_clean_partition(in_root, DAY2, [(219000006, VALID_IMO_A)])
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY2, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    _mmsi, _imo, count, first_seen, last_seen, _is_orphaned, is_reused = result[0]
    assert count == 2
    assert first_seen == DAY
    assert last_seen == DAY2
    assert is_reused is False


def test_invalid_checksum_not_counted_as_identity(tmp_path):
    """Negative case: right digit count, wrong check digit -- must not create a pairing."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000007, "1234568")]  # 7 digits, checksum fails
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][1] is None  # imo
    assert result[0][5] is True  # is_orphaned


def test_wrong_digit_count_not_counted_as_identity(tmp_path):
    """Negative case: 6-digit and 8-digit imo strings are rejected regardless of arithmetic."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000008, "123456"),
        (219000008, "12345678"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][1] is None
    assert result[0][5] is True


def test_all_zero_imo_not_counted_as_identity(tmp_path):
    """Negative case: '0000000' passes the checksum arithmetic trivially but is a known placeholder."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000009, "0000000")]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][1] is None
    assert result[0][5] is True


def test_well_known_junk_imo_not_counted_as_identity(tmp_path):
    """Negative case: 1193046, a widely-used test/placeholder IMO, fails the checksum and is rejected."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000010, "1193046")]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][1] is None
    assert result[0][5] is True


def test_valid_imo_boundary_survives(tmp_path):
    """Negative case for the validator itself: a well-formed valid imo must survive as an identity pairing."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [(219000011, VALID_IMO_D)]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][1] == VALID_IMO_D
    assert result[0][5] is False


def test_mixed_valid_and_invalid_imo_only_valid_counted(tmp_path):
    """An mmsi reporting a mix of a valid imo and junk values: only the valid one forms a pairing."""
    in_root = tmp_path / "clean" / "ais_dk"
    rows = [
        (219000012, VALID_IMO_C),
        (219000012, "Unknown"),
        (219000012, None),
        (219000012, "0000000"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][1] == VALID_IMO_C
    assert result[0][2] == 1  # message_count: only the valid row contributes
    assert result[0][5] is False


def test_resolve_range_skips_missing_day_with_warning(tmp_path, caplog):
    """A day with no clean partition in the middle of a range is skipped, not fatal."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000013, VALID_IMO_A)])
    # DAY2 deliberately missing.
    _write_clean_partition(in_root, DAY3, [(219000013, VALID_IMO_A)])
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    with caplog.at_level("WARNING"):
        identity.resolve_range(DAY, DAY3, in_root=in_root, out_path=out_path)

    assert any("2024-06-06" in record.message for record in caplog.records)
    result = _read_identity(out_path)
    assert len(result) == 1
    assert result[0][2] == 2  # message_count from the two days that do exist


def test_resolve_range_raises_when_no_partitions_exist(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    with pytest.raises(FileNotFoundError):
        identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)


def test_resolve_range_is_idempotent_by_default(tmp_path):
    """Re-running without force must not rebuild the output."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000014, VALID_IMO_A)])
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    first = identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)
    first_mtime = first.stat().st_mtime_ns

    second = identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite the file"


def test_resolve_range_force_rebuilds(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(in_root, DAY, [(219000015, VALID_IMO_A)])
    out_path = tmp_path / "identity" / "mmsi_imo.parquet"

    identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path)
    result = identity.resolve_range(DAY, DAY, in_root=in_root, out_path=out_path, force=True)

    assert result.exists()
