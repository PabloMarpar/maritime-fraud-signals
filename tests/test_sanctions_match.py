"""Unit tests for process.sanctions_match. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from process import sanctions_match

# Real IMO numbers (verified check digit), same constants test_identity.py uses.
VALID_IMO_A = "9074729"
VALID_IMO_B = "9264386"
VALID_IMO_C = "9806847"
VALID_IMO_D = "2000004"

DESIGNATION_DATE = date(2024, 1, 15)


def _write_sanctions(path: Path, rows: list[tuple]) -> None:
    """rows: (source, source_id, vessel_name, imo, flag, program, designation_date,
    designation_date_precision). imo may be None or a junk string.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE sanctions (source VARCHAR, source_id VARCHAR, vessel_name VARCHAR, "
            "imo VARCHAR, flag VARCHAR, program VARCHAR, designation_date DATE, "
            "designation_date_precision VARCHAR)"
        )
        con.executemany("INSERT INTO sanctions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY sanctions TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_identity(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, imo, message_count, first_seen, last_seen, is_orphaned, is_reused).
    imo may be None for an orphan sentinel row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE identity (mmsi BIGINT, imo VARCHAR, message_count BIGINT, "
            "first_seen DATE, last_seen DATE, is_orphaned BOOLEAN, is_reused BOOLEAN)"
        )
        con.executemany("INSERT INTO identity VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY identity TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read_matches(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT mmsi, imo, source, source_id, vessel_name, sanctions_flag, program, "
            "designation_date, designation_date_precision, ais_first_seen, ais_last_seen, "
            "ais_message_count, mmsi_is_reused FROM "
            f"'{out_path.as_posix()}' ORDER BY mmsi, imo, source, source_id"
        ).fetchall()
    finally:
        con.close()


def test_clean_one_to_one_match(tmp_path):
    """A sanctioned vessel with one AIS mmsi it was ever seen under: one matching row."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-1", "Baltic Ghost", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000001, VALID_IMO_A, 100, date(2024, 6, 1), date(2024, 6, 30), False, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert len(result) == 1
    mmsi, imo, source, source_id, name, flag, program, ddate, prec, first, last, count, reused = (
        result[0]
    )
    assert mmsi == 219000001
    assert imo == VALID_IMO_A
    assert source == "ofac"
    assert source_id == "SDN-1"
    assert name == "Baltic Ghost"
    assert flag == "Panama"
    assert program == "SDGT"
    assert ddate == DESIGNATION_DATE
    assert prec == "day"
    assert first == date(2024, 6, 1)
    assert last == date(2024, 6, 30)
    assert count == 100
    assert reused is False


def test_sanctioned_imo_with_no_ais_match(tmp_path):
    """A sanctioned vessel never observed in this AIS window produces no output row."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-2", "Never Seen", VALID_IMO_B, "Iran", "SDGT", DESIGNATION_DATE, "day")],
    )
    # Identity table has a completely different vessel, not the sanctioned one.
    _write_identity(
        identity_path,
        [(219000002, VALID_IMO_A, 10, date(2024, 6, 1), date(2024, 6, 2), False, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert result == []


def test_checksum_invalid_sanctions_imo_excluded(tmp_path):
    """A sanctions-list imo that fails the checksum must not join, even against a real vessel."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    junk_imo = "1234568"  # 7 digits, checksum fails (same fixture test_identity.py uses)
    _write_sanctions(
        sanctions_path,
        [("uk", "DPR0001", "Typo Vessel", junk_imo, "Russia", "RUSSIA", DESIGNATION_DATE, "day")],
    )
    # An mmsi that happens to broadcast the exact same (invalid) string must still not match --
    # process.identity never writes it as a pairing in the first place, but assert defensively.
    _write_identity(
        identity_path,
        [(219000003, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 1), False, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert result == []


def test_is_reused_mmsi_match_flagged(tmp_path):
    """A matched mmsi that is itself ambiguous (reused) carries mmsi_is_reused=True through."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-3", "Shared Radio", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [
            (219000004, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, True),
            (219000004, VALID_IMO_C, 5, date(2024, 6, 10), date(2024, 6, 15), False, True),
        ],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert len(result) == 1
    assert result[0][0] == 219000004
    assert result[0][12] is True  # mmsi_is_reused


def test_not_reused_negative_case(tmp_path):
    """Negative case: a matched mmsi with a single distinct imo must not be flagged reused."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-4", "Clean Vessel", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000005, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert len(result) == 1
    assert result[0][12] is False  # mmsi_is_reused


def test_single_imo_matched_by_two_sources_produces_two_rows(tmp_path):
    """One IMO listed by both OFAC and UK: two output rows, one per source record, not collapsed."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [
            ("ofac", "SDN-5", "Corroborated", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day"),
            ("uk", "DPR0002", "Corroborated", VALID_IMO_A, "Panama", "RUSSIA", DESIGNATION_DATE, "day"),
        ],
    )
    _write_identity(
        identity_path,
        [(219000006, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert len(result) == 2
    sources = {row[2] for row in result}
    assert sources == {"ofac", "uk"}
    assert all(row[0] == 219000006 and row[1] == VALID_IMO_A for row in result)


def test_two_distinct_sanctioned_imo_same_mmsi(tmp_path):
    """A reused mmsi whose two distinct valid imos are BOTH independently sanctioned: the
    genuinely confusing ambiguity case -- two output rows, same mmsi, two different imo.
    """
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [
            ("ofac", "SDN-6", "Vessel One", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day"),
            ("ofac", "SDN-7", "Vessel Two", VALID_IMO_B, "Iran", "SDGT", DESIGNATION_DATE, "day"),
        ],
    )
    _write_identity(
        identity_path,
        [
            (219000007, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, True),
            (219000007, VALID_IMO_B, 5, date(2024, 6, 10), date(2024, 6, 15), False, True),
        ],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert len(result) == 2
    mmsis = {row[0] for row in result}
    assert mmsis == {219000007}
    imos = {row[1] for row in result}
    assert imos == {VALID_IMO_A, VALID_IMO_B}


def test_orphan_sentinel_rows_never_match(tmp_path):
    """Negative case: an orphan mmsi sentinel row (imo IS NULL) must never join to anything."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-8", "Some Vessel", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000008, None, 20, date(2024, 6, 1), date(2024, 6, 30), True, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert result == []


def test_sanctions_row_with_no_imo_at_all_is_not_counted_as_checksum_invalid(tmp_path, caplog):
    """A sanctions row with imo IS NULL (never assigned one) is a separate bucket from a
    checksum-invalid typo -- must not double count or crash the join.
    """
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("uk", "DPR0003", "No IMO On File", None, "Russia", "RUSSIA", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000009, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    with caplog.at_level("INFO"):
        sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    result = _read_matches(out_path)
    assert result == []
    assert any("no imo at all" in record.message for record in caplog.records)


def test_match_sanctions_is_idempotent_by_default(tmp_path):
    """Re-running without force must not rebuild the output."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-9", "Idempotent", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000010, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    first = sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)
    first_mtime = first.stat().st_mtime_ns

    second = sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime


def test_match_sanctions_force_rebuilds(tmp_path):
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-10", "Rebuild Me", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000011, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)
    result = sanctions_match.match_sanctions(
        sanctions_path, identity_path, out_path, force=True
    )

    assert result.exists()


def test_raises_when_sanctions_path_missing(tmp_path):
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"
    _write_identity(
        identity_path,
        [(219000012, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    with pytest.raises(FileNotFoundError):
        sanctions_match.match_sanctions(
            tmp_path / "reference" / "sanctions.parquet", identity_path, out_path
        )


def test_raises_when_identity_path_missing(tmp_path):
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"
    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-11", "No Identity Table", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )

    with pytest.raises(FileNotFoundError):
        sanctions_match.match_sanctions(
            sanctions_path, tmp_path / "identity" / "mmsi_imo.parquet", out_path
        )


def test_ambiguous_mmsi_logged_at_warning(tmp_path, caplog):
    """The double-distinct-imo-same-mmsi case must be logged loudly (WARNING), not buried in INFO."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [
            ("ofac", "SDN-12", "Vessel One", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day"),
            ("ofac", "SDN-13", "Vessel Two", VALID_IMO_D, "Iran", "SDGT", DESIGNATION_DATE, "day"),
        ],
    )
    _write_identity(
        identity_path,
        [
            (219000013, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, True),
            (219000013, VALID_IMO_D, 5, date(2024, 6, 10), date(2024, 6, 15), False, True),
        ],
    )

    with caplog.at_level("WARNING"):
        sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    assert any("more than one distinct sanctioned imo" in record.message for record in caplog.records)
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_no_ambiguous_mmsi_negative_case(tmp_path, caplog):
    """Negative case: with no such case in the data, the warning must not fire."""
    sanctions_path = tmp_path / "reference" / "sanctions.parquet"
    identity_path = tmp_path / "identity" / "mmsi_imo.parquet"
    out_path = tmp_path / "identity" / "sanctions_matches.parquet"

    _write_sanctions(
        sanctions_path,
        [("ofac", "SDN-14", "Ordinary", VALID_IMO_A, "Panama", "SDGT", DESIGNATION_DATE, "day")],
    )
    _write_identity(
        identity_path,
        [(219000014, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)],
    )

    with caplog.at_level("WARNING"):
        sanctions_match.match_sanctions(sanctions_path, identity_path, out_path)

    assert not any(record.levelname == "WARNING" for record in caplog.records)
