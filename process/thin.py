"""Downsampled tracks for the Phase 5 map and manual review -- never for re-detection.

**Why this exists (P3-4/A3).** ``data/clean/`` is the only artifact this project's detectors can
run against, and it is exactly what P3-4/A5 will eventually discard per window to keep disk peak
bounded. The map and manual dossier review (Phase 5) don't need full-resolution clean AIS -- one
position every few minutes is enough to draw a route -- so this module extracts a much smaller,
keep-forever-per-day track table instead: one row per ``(mmsi, N-minute bucket)``, the first
message seen in that bucket, for every day with a clean partition.

**Never for re-detection.** At the default 5-minute resolution, the position jumps
``detect.spoofing`` relies on and the close-approach geometry ``detect.sts`` needs to segment
encounters are both invisible -- a vessel can move several kilometres between kept positions. This
table exists solely for rendering and eyeballing; any detector re-run against a window whose clean
data has since been discarded needs a re-download, not this table.

**Bucketing.** ``time_bucket(INTERVAL 'N minutes', timestamp)`` groups messages, and
``row_number() OVER (PARTITION BY mmsi, bucket ORDER BY timestamp) = 1`` keeps the earliest message
per bucket -- an arbitrary but deterministic choice among messages in the same bucket, not a
midpoint or average.

**Day-partitioned, like ``detect.liveness`` (P3-4/A1), not window-partitioned like P3-4/A2's
detectors.** A thinned day never changes once a later window is built, so per-day partitions
(``THIN_ROOT/date=YYYY-MM-DD/part-0.parquet``) let a later window's build simply fill in new days
without re-touching or overwriting earlier ones -- ``build_thin_tracks`` is idempotent per day, same
contract as ``detect.liveness.build_liveness``.

**Interval is a parameter, not a validated constant.** The plan estimated ~10-25 MB/day at 5
minutes (~2 GB for a 120-day sample); measure against a real day before committing to it for a
multi-year sample, and drop to 10-15 minutes if it exceeds budget -- not done here, this module
only provides the knob.

All of this runs as one DuckDB query per day over views; the data is never pulled into Python row
by row, except the handful of scalars logged per day.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.partitions import daterange, existing_partitions, git_sha, partition_path

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
# data/tracks/thin/date=YYYY-MM-DD/part-0.parquet, one file per day -- see build_thin_tracks.
THIN_ROOT = Path("data/tracks/thin")

DEFAULT_INTERVAL_MINUTES = 5

# Output column names. The clean-partition source column is ``navigational_status`` -- aliased to
# ``nav_status`` on read (_SOURCE_COLUMNS below) to match the shorter name detect.sts already uses
# for the same field.
_THINNED_COLUMNS = "mmsi, timestamp, latitude, longitude, sog, cog, nav_status, ship_type"
_SOURCE_COLUMNS = (
    "mmsi, timestamp, latitude, longitude, sog, cog, navigational_status AS nav_status, ship_type"
)


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition.

    Every module in this project owns its own copy of this small helper rather than importing
    one shared version -- see e.g. features.panel's module docstring.
    """
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def build_thin_tracks(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_root: Path = THIN_ROOT,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    force: bool = False,
) -> list[Path]:
    """Build the downsampled-track table, one partition per day, for every day in [start, end]
    that has a clean partition.

    Idempotent PER DAY: a day whose ``out_root/date=.../part-0.parquet`` already exists is skipped
    unless ``force=True``, mirroring ``detect.liveness.build_liveness`` -- see module docstring. A
    day with no clean partition is logged and skipped, not fatal, but if NO day in the range
    produced an output, this raises FileNotFoundError.

    Returns the list of day-partition paths that exist after this call (both freshly built and
    already-present), ordered by day. Writes are atomic (temp file + ``os.replace``, per
    ``docs/PLAN_P4-0_P3-4.md``'s A0.8).
    """
    written: list[Path] = []
    con = duckdb.connect()
    try:
        for day in daterange(start, end):
            day_out_path = partition_path(day, out_root)
            if day_out_path.exists() and not force:
                logger.info(
                    "%s already exists, skipping (pass force=True / --force to rebuild)",
                    day_out_path,
                )
                written.append(day_out_path)
                continue

            day_partitions = existing_partitions(day, day, in_root)
            if not day_partitions:
                continue

            con.execute(
                "CREATE OR REPLACE TEMP TABLE _thin AS "
                f"SELECT {_THINNED_COLUMNS} FROM ("
                f"  SELECT {_THINNED_COLUMNS}, "
                "    row_number() OVER ("
                "      PARTITION BY mmsi, time_bucket(INTERVAL '"
                f"{interval_minutes} minutes', timestamp) "
                "      ORDER BY timestamp"
                "    ) AS _rn "
                "  FROM (" + _partitions_union_sql(day_partitions, _SOURCE_COLUMNS) + ")"
                ") WHERE _rn = 1"
            )

            (n_rows,) = con.execute("SELECT count(*) FROM _thin").fetchone()
            (n_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM _thin").fetchone()
            logger.info(
                "%s: built thin-track partition, %d row(s) at %d-minute resolution, "
                "%d distinct mmsi",
                day.isoformat(),
                n_rows,
                interval_minutes,
                n_mmsi,
            )

            built_at = datetime.now(timezone.utc)
            sha = git_sha()
            day_out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = day_out_path.with_suffix(".tmp")
            con.execute(
                "COPY (SELECT *, "
                f"DATE '{day.isoformat()}' AS window_start, "
                f"DATE '{day.isoformat()}' AS window_end, "
                f"{interval_minutes} AS interval_minutes, "
                f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
                f"'{sha}' AS git_sha "
                "FROM _thin ORDER BY mmsi, timestamp) "
                f"TO '{tmp_path.as_posix()}' (FORMAT PARQUET)"
            )
            os.replace(tmp_path, day_out_path)
            written.append(day_out_path)
    finally:
        con.close()

    if not written:
        raise FileNotFoundError(
            f"No clean partitions found for {start.isoformat()}..{end.isoformat()} under {in_root}"
        )
    return written


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build downsampled per-day track partitions for the Phase 5 map and manual "
        "review, one row per (mmsi, N-minute bucket). Never used for re-detection."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-root", default=str(THIN_ROOT), help="Root directory for day-partitioned thin output"
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help="Bucket width in minutes (default: %(default)s)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if a day's output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    build_thin_tracks(
        start,
        end,
        in_root=Path(args.in_dir),
        out_root=Path(args.out_root),
        interval_minutes=args.interval_minutes,
        force=args.force,
    )


if __name__ == "__main__":
    main()
