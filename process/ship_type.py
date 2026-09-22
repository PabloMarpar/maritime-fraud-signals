"""Per-mmsi ship_type reference, extracted once from clean AIS.

**Why this exists (P3-4/A2's hard blocker).** ``features.panel._build_ship_type`` and
``detect.identity_anomalies._build_ship_types`` each scan clean Parquet partitions directly to
resolve a modal ship_type per mmsi -- the one static-identity fact this project still reads
straight from clean data rather than from a small pre-reduced table. That is exactly what P3-4's
whole point is to stop doing: once clean partitions for a window are discarded (P3-4/A5), neither
caller could be re-run against that window again. This module extracts the raw fact once per
window, ``data/reference/ship_type/window=<start>_<end>/part-0.parquet``, so both callers can be
pointed at it instead.

**Grain: (mmsi, ship_type, type_of_mobile, n_messages), raw and unresolved.** This is deliberately
NOT the already-resolved "modal ship_type per mmsi" -- ``features.panel`` and
``detect.identity_anomalies`` apply different resolution logic on top (the panel counts every
``type_of_mobile`` and does no text normalization; identity_anomalies filters to
:data:`detect.liveness.MOBILE_TYPES` first and normalizes/validates the text via its own
``_normalize_text_sql``/``_valid_text_sql``), and unifying those into one resolution here would be
a silent behaviour change disguised as a storage-layout refactor. Keeping ``type_of_mobile``
unfiltered and ``ship_type`` unnormalized in this table lets each caller reproduce its own prior
logic exactly, as a re-grouping of this table instead of of raw messages -- verified byte-for-byte
against the pre-P3-4/A2 direct-scan behaviour in ``tests/test_ship_type.py`` and the real-window
migration check (see ``docs/DECISIONS.md``).

All of this runs as one DuckDB aggregate query over views; the data is never pulled into Python.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.partitions import (
    atomic_write_parquet,
    existing_partitions,
    git_sha,
    window_partition_path,
)

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
REFERENCE_ROOT = Path("data/reference")
# data/reference/ship_type/window=<start>_<end>/part-0.parquet, one file per window (P3-4/A2) --
# see build_ship_type_reference.
SHIP_TYPE_ROOT = REFERENCE_ROOT / "ship_type"
# Default read-side path for consumers (features.panel, detect.identity_anomalies): every
# window's counts in one glob -- see detect.anchorages.ANCHORAGES_GLOB for why this works as a
# bound read_parquet(?) parameter.
SHIP_TYPE_GLOB = SHIP_TYPE_ROOT / "window=*" / "part-0.parquet"


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition.

    Every module in this project owns its own copy of this small helper rather than importing
    one shared version -- see e.g. features.panel's module docstring.
    """
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def build_ship_type_reference(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_root: Path = SHIP_TYPE_ROOT,
    force: bool = False,
) -> Path:
    """Extract raw (mmsi, ship_type, type_of_mobile) message counts for every clean partition in
    [start, end], writing ``out_root/window=<start>_<end>/part-0.parquet`` (P3-4/A2).

    Idempotent per window: if that exact window's output already exists, this is a no-op unless
    force=True. Returns the output path either way. A different [start, end] lands at a different
    path by construction, so windows accumulate instead of overwriting each other. Raises
    FileNotFoundError if no clean partition exists anywhere in the requested range; a partial
    range with some days missing only warns, see process.partitions.existing_partitions.
    """
    out_path = window_partition_path(start, end, out_root)
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    partitions = existing_partitions(start, end, in_root)
    if not partitions:
        raise FileNotFoundError(
            f"No clean partitions found for {start.isoformat()}..{end.isoformat()} under {in_root}"
        )

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _ship_type_counts AS "
            "SELECT mmsi, ship_type, type_of_mobile, CAST(count(*) AS BIGINT) AS n_messages "
            "FROM (" + _partitions_union_sql(partitions, "mmsi, ship_type, type_of_mobile") + ") "
            "GROUP BY mmsi, ship_type, type_of_mobile"
        )

        (n_rows,) = con.execute("SELECT count(*) FROM _ship_type_counts").fetchone()
        (n_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM _ship_type_counts").fetchone()
        logger.info(
            "Built ship_type reference: %d (mmsi, ship_type, type_of_mobile) row(s), "
            "%d distinct mmsi",
            n_rows,
            n_mmsi,
        )

        built_at = datetime.now(timezone.utc)
        sha = git_sha()
        atomic_write_parquet(
            con,
            "SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{sha}' AS git_sha "
            "FROM _ship_type_counts ORDER BY mmsi, ship_type, type_of_mobile",
            out_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a raw (mmsi, ship_type, type_of_mobile) message-count reference "
        "from clean AIS Parquet partitions, so downstream consumers stop reading clean "
        "partitions directly for this one static fact."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-root",
        default=str(SHIP_TYPE_ROOT),
        help="Root directory for window-partitioned ship_type reference output",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    build_ship_type_reference(
        start, end, in_root=Path(args.in_dir), out_root=Path(args.out_root), force=args.force
    )


if __name__ == "__main__":
    main()
