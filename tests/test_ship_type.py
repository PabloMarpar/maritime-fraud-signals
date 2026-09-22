"""Unit tests for process.ship_type. All fixtures are synthetic, written to tmp_path; nothing
here touches data/.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from process import ship_type

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """rows is a list of (mmsi, ship_type, type_of_mobile) tuples."""
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, ship_type VARCHAR, type_of_mobile VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read(out_path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT mmsi, ship_type, type_of_mobile, n_messages "
            f"FROM '{out_path.as_posix()}' ORDER BY mmsi, ship_type, type_of_mobile"
        ).fetchall()
    finally:
        con.close()


def test_counts_are_grouped_by_mmsi_ship_type_and_mobile_type(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "reference" / "ship_type"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (111, "Tanker", "Class A"),
            (111, "Tanker", "Class A"),
            (111, "Tanker", "Class A"),
            (111, "Cargo", "Class A"),
        ],
    )

    out_path = ship_type.build_ship_type_reference(DAY, DAY, in_root=in_root, out_root=out_root)

    rows = _read(out_path)
    assert rows == [
        (111, "Cargo", "Class A", 1),
        (111, "Tanker", "Class A", 3),
    ]


def test_raw_unnormalized_and_unfiltered_by_mobile_type(tmp_path):
    """No text normalization and no type_of_mobile filtering happens here -- each consumer
    (features.panel, detect.identity_anomalies) applies its own logic on top, so this reference
    must carry the raw, unresolved facts, including non-mobile broadcast types."""
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "reference" / "ship_type"
    _write_clean_partition(
        in_root,
        DAY,
        [
            (222, "tanker", "Class A"),  # lowercase, distinct from "Tanker"
            (222, "Tanker", "Class A"),
            (222, "Cargo", "Base Station"),  # non-mobile type_of_mobile, kept
        ],
    )

    out_path = ship_type.build_ship_type_reference(DAY, DAY, in_root=in_root, out_root=out_root)

    rows = _read(out_path)
    assert (222, "tanker", "Class A", 1) in rows
    assert (222, "Tanker", "Class A", 1) in rows
    assert (222, "Cargo", "Base Station", 1) in rows


def test_counts_accumulate_across_days_in_one_window(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "reference" / "ship_type"
    _write_clean_partition(in_root, DAY, [(333, "Tanker", "Class A")])
    _write_clean_partition(in_root, DAY2, [(333, "Tanker", "Class A")])

    out_path = ship_type.build_ship_type_reference(DAY, DAY2, in_root=in_root, out_root=out_root)

    rows = _read(out_path)
    assert rows == [(333, "Tanker", "Class A", 2)]


def test_build_ship_type_reference_raises_when_clean_partitions_missing(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "reference" / "ship_type"

    with pytest.raises(FileNotFoundError, match="clean partitions"):
        ship_type.build_ship_type_reference(DAY, DAY, in_root=in_root, out_root=out_root)


def test_build_ship_type_reference_is_idempotent_by_default(tmp_path, caplog):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "reference" / "ship_type"
    _write_clean_partition(in_root, DAY, [(444, "Tanker", "Class A")])

    first = ship_type.build_ship_type_reference(DAY, DAY, in_root=in_root, out_root=out_root)
    first_mtime = first.stat().st_mtime_ns

    caplog.clear()
    with caplog.at_level("INFO"):
        second = ship_type.build_ship_type_reference(DAY, DAY, in_root=in_root, out_root=out_root)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime, "re-running without --force must not rewrite"
    assert any("already exists" in record.message for record in caplog.records)


def test_build_ship_type_reference_different_windows_land_at_different_paths(tmp_path):
    """P3-4/A2: a later window must accumulate next to an earlier one, never overwrite it."""
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "reference" / "ship_type"
    _write_clean_partition(in_root, DAY, [(555, "Tanker", "Class A")])
    _write_clean_partition(in_root, DAY2, [(555, "Tanker", "Class A")])

    first = ship_type.build_ship_type_reference(DAY, DAY, in_root=in_root, out_root=out_root)
    second = ship_type.build_ship_type_reference(DAY, DAY2, in_root=in_root, out_root=out_root)

    assert first != second
    assert first.exists()
    assert second.exists()
