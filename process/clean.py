"""Clean raw DMA AIS partitions: drop invalid MMSI, impossible coordinates, duplicates.

Reads the raw Hive-partitioned Parquet landed by ``ingest.dma`` (one file per
day at ``data/raw/ais_dk/date=YYYY-MM-DD/part-0.parquet``) and writes a
cleaned partition in the same layout under ``data/clean/ais_dk``. Three rules
are applied, in order, each logged at INFO level with the row count it drops
(mirroring ``ingest.dma._csv_to_parquet``'s logging style):

1. **Invalid MMSI.** A ship station MMSI is a 9-digit number whose first
   digit is 2-7 (ITU Maritime Identification Digits 200-775 are allocated to
   countries for ship stations). MMSIs starting 0/1/8/9 belong to other
   station classes (coast stations, SAR aircraft, aids to navigation, craft
   associated with a parent vessel, etc.) that are out of scope for this
   project, so the valid range is simply 200000000-799999999 inclusive. Null
   or non-numeric MMSI is dropped too, though in practice the raw column is
   already ``BIGINT`` so "non-numeric" only matters if a future source
   ships MMSI as text.
2. **Impossible coordinates.** Latitude must be in [-90, 90] and longitude in
   [-180, 180] -- the boundary values themselves are physically valid (the
   poles, the antimeridian) and must survive. (0, 0) is treated separately:
   it is "null island", the default/error value for a GPS unit with no fix,
   sitting in the Gulf of Guinea far from Denmark, and is dropped even
   though it is technically within range.
3. **Duplicate messages.** The same MMSI reporting an identical
   (timestamp, latitude, longitude) more than once is almost certainly a
   retransmission rather than two independent reports, so only the first
   occurrence (by rowid) is kept. Two genuinely different timestamps for the
   same MMSI/position -- e.g. a moored vessel -- are not duplicates and both
   survive.

All of this runs as a single DuckDB query per partition; the data is never
pulled into Python.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

RAW_ROOT = Path("data/raw/ais_dk")
CLEAN_ROOT = Path("data/clean/ais_dk")

MIN_VALID_MMSI = 200000000
MAX_VALID_MMSI = 799999999


def _daterange(start: date, end: date) -> Iterator[date]:
    """Yield each date from start to end, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _partition_path(day: date, root: Path) -> Path:
    """Hive-style partition path for one day, e.g. .../date=2024-06-05/part-0.parquet."""
    return root / f"date={day.isoformat()}" / "part-0.parquet"


def _clean_partition(con: duckdb.DuckDBPyConnection, raw_path: Path, clean_path: Path) -> int:
    """Apply the three cleaning rules to one raw partition, writing clean_path.

    Logs how many rows each rule drops and returns the final row count. Each
    rule's drop count is measured against the row count surviving the
    previous rule, so the numbers describe a pipeline (rule 2 only sees rows
    that already passed rule 1), not three independent filters over the raw
    file.
    """
    raw_source = raw_path.as_posix()
    con.execute(f"CREATE OR REPLACE VIEW raw AS SELECT * FROM read_parquet('{raw_source}')")
    (raw_count,) = con.execute("SELECT count(*) FROM raw").fetchone()

    # Rule 1: invalid MMSI.
    con.execute(
        "CREATE OR REPLACE VIEW valid_mmsi AS SELECT * FROM raw "
        f"WHERE mmsi IS NOT NULL AND mmsi BETWEEN {MIN_VALID_MMSI} AND {MAX_VALID_MMSI}"
    )
    (mmsi_count,) = con.execute("SELECT count(*) FROM valid_mmsi").fetchone()
    logger.info("Rule 1 (invalid MMSI): dropped %d rows", raw_count - mmsi_count)

    # Rule 2: impossible coordinates, including (0, 0) "null island".
    con.execute(
        "CREATE OR REPLACE VIEW valid_coords AS SELECT * FROM valid_mmsi "
        "WHERE latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180 "
        "AND NOT (latitude = 0 AND longitude = 0)"
    )
    (coords_count,) = con.execute("SELECT count(*) FROM valid_coords").fetchone()
    logger.info("Rule 2 (impossible coordinates): dropped %d rows", mmsi_count - coords_count)

    # Rule 3: duplicate (mmsi, timestamp, latitude, longitude), keep the first
    # occurrence. Parquet scans have no rowid pseudocolumn, so an explicit
    # sequence number stands in for "file order" as the tie-break.
    con.execute(
        "CREATE OR REPLACE VIEW sequenced AS "
        "SELECT *, row_number() OVER () AS _seq FROM valid_coords"
    )
    con.execute(
        "CREATE OR REPLACE VIEW deduped AS "
        "SELECT * EXCLUDE (_seq) FROM sequenced QUALIFY row_number() OVER ("
        "PARTITION BY mmsi, timestamp, latitude, longitude ORDER BY _seq"
        ") = 1"
    )
    (deduped_count,) = con.execute("SELECT count(*) FROM deduped").fetchone()
    logger.info("Rule 3 (duplicate messages): dropped %d rows", coords_count - deduped_count)

    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_target = clean_path.as_posix()
    con.execute(f"COPY (SELECT * FROM deduped) TO '{clean_target}' (FORMAT PARQUET)")
    return deduped_count


def clean_day(
    day: date,
    in_root: Path = RAW_ROOT,
    out_root: Path = CLEAN_ROOT,
    force: bool = False,
) -> Path:
    """Clean one day of raw DMA AIS data and land it as Parquet.

    Idempotent: if the day's clean partition already exists, this is a no-op
    unless force=True. Returns the path to the clean partition's Parquet
    file either way. Raises FileNotFoundError if the raw partition is
    missing.
    """
    raw_path = _partition_path(day, in_root)
    clean_path = _partition_path(day, out_root)
    if clean_path.exists() and not force:
        logger.info(
            "%s already cleaned, skipping (pass force=True / --force to re-clean)",
            day.isoformat(),
        )
        return clean_path

    if not raw_path.exists():
        raise FileNotFoundError(f"No raw partition for {day.isoformat()} at {raw_path}")

    con = duckdb.connect()
    try:
        row_count = _clean_partition(con, raw_path, clean_path)
        logger.info("Cleaned %d rows for %s at %s", row_count, day.isoformat(), clean_path)
    finally:
        con.close()
    return clean_path


def clean_range(
    start: date,
    end: date,
    in_root: Path = RAW_ROOT,
    out_root: Path = CLEAN_ROOT,
    force: bool = False,
) -> list[Path]:
    """Clean DMA AIS data for every day in [start, end], inclusive."""
    return [
        clean_day(day, in_root=in_root, out_root=out_root, force=force)
        for day in _daterange(start, end)
    ]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean raw DMA AIS Parquet partitions.")
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--force", action="store_true", help="Re-clean even if the partition already exists"
    )
    parser.add_argument(
        "--in-dir", default=str(RAW_ROOT), help="Root of the raw Parquet partitions"
    )
    parser.add_argument(
        "--out-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    clean_range(start, end, in_root=Path(args.in_dir), out_root=Path(args.out_dir), force=args.force)


if __name__ == "__main__":
    main()
