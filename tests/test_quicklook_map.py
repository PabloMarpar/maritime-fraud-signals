"""Unit tests for report.quicklook_map. All fixtures are synthetic, built in tmp_path."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from report import quicklook_map

DAY = date(2024, 6, 5)

# Columns match ingest.dma's landed schema exactly (see tests/test_dma.py):
# timestamp, type_of_mobile, mmsi, latitude, longitude, navigational_status,
# sog, cog, ship_type — plus the "date" column DuckDB infers from the
# Hive-style partition path itself, so it is not written here explicitly.
ROWS = [
    # (timestamp, type_of_mobile, mmsi, latitude, longitude, navigational_status, sog, cog, ship_type)
    ("2024-06-05 00:00:01", "Class A", 219000001, 55.6761, 12.5683, "Under way using engine", 10.2, 180.5, "Cargo"),
    ("2024-06-05 00:00:02", "Class A", 219000002, 55.7000, 12.6000, "Under way using engine", 7.1, 181.0, "Tanker"),
    ("2024-06-05 00:00:03", "Class A", 219000003, 55.8000, 12.7000, "Moored", 0.0, 0.0, "Tanker"),
    # Invalid: latitude out of range. Must be dropped, not crash the plot.
    ("2024-06-05 00:00:04", "Class A", 219000004, 95.0, 12.7000, "Moored", 0.0, 0.0, "Fishing"),
]

WELL_FORMED_ROW_COUNT = 3  # the fourth row above has an impossible latitude


def _write_partition(tmp_path, rows) -> str:
    """Land rows as a Hive-partitioned Parquet file, mirroring ingest.dma's layout."""
    partition_dir = tmp_path / "ais_dk" / f"date={DAY.isoformat()}"
    partition_dir.mkdir(parents=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            """
            CREATE TABLE raw (
                timestamp VARCHAR,
                type_of_mobile VARCHAR,
                mmsi BIGINT,
                latitude DOUBLE,
                longitude DOUBLE,
                navigational_status VARCHAR,
                sog DOUBLE,
                cog DOUBLE,
                ship_type VARCHAR
            )
            """
        )
        con.executemany("INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        target = parquet_path.as_posix()
        con.execute(f"COPY raw TO '{target}' (FORMAT PARQUET)")
    finally:
        con.close()
    return str(partition_dir)


def test_render_writes_nonempty_png_and_drops_invalid_rows(tmp_path):
    partition_dir = _write_partition(tmp_path, ROWS)
    output_path = tmp_path / "outputs" / "quicklook_2024-06-05.png"

    df, dropped = quicklook_map.load_positions(partition_dir)

    assert dropped == 1
    assert len(df) == WELL_FORMED_ROW_COUNT
    assert 219000004 not in set(df["mmsi"])

    result_path = quicklook_map.render(partition_dir, str(output_path))

    assert result_path == output_path
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_render_accepts_explicit_glob(tmp_path):
    """--input can also be a Parquet glob, not just a partition directory."""
    partition_dir = _write_partition(tmp_path, ROWS)
    glob = f"{partition_dir}/*.parquet"
    output_path = tmp_path / "outputs" / "quicklook_glob.png"

    result_path = quicklook_map.render(glob, str(output_path))

    assert result_path.exists()
    assert result_path.stat().st_size > 0


def test_render_with_no_ship_type_variety_falls_back_to_plain_scatter(tmp_path):
    """Negative case for the colour-by-ship_type path: a single category must not crash."""
    single_type_rows = [
        ("2024-06-05 00:00:01", "Class A", 219000001, 55.6761, 12.5683, "Under way using engine", 10.2, 180.5, "Cargo"),
        ("2024-06-05 00:00:02", "Class A", 219000002, 55.7000, 12.6000, "Under way using engine", 7.1, 181.0, "Cargo"),
    ]
    partition_dir = _write_partition(tmp_path, single_type_rows)
    output_path = tmp_path / "outputs" / "quicklook_single_type.png"

    result_path = quicklook_map.render(partition_dir, str(output_path))

    assert result_path.exists()
    assert result_path.stat().st_size > 0


def test_render_raises_when_all_rows_invalid(tmp_path):
    """Zero valid rows after filtering must raise clearly, not emit a blank/broken PNG."""
    all_invalid_rows = [
        ("2024-06-05 00:00:01", "Class A", 219000001, 95.0, 12.5683, "Under way using engine", 10.2, 180.5, "Cargo"),
        ("2024-06-05 00:00:02", "Class A", 219000002, 55.7000, 200.0, "Under way using engine", 7.1, 181.0, "Tanker"),
    ]
    partition_dir = _write_partition(tmp_path, all_invalid_rows)
    output_path = tmp_path / "outputs" / "quicklook_empty.png"

    with pytest.raises(ValueError, match="No valid positions"):
        quicklook_map.render(partition_dir, str(output_path))

    assert not output_path.exists()
