"""Reconstruct per-vessel tracks and segment them into voyages.

A **track** is simply the time-ordered sequence of one MMSI's cleaned
positions. A **voyage** is a contiguous stretch of that track: a new voyage
starts wherever there is a real break in continuous reporting, not merely
because a calendar day ended. The signal used here is the standard, simplest
one in AIS analytics: a **time-gap threshold**. If the interval between two
consecutive messages from the same MMSI exceeds ``gap_hours`` (a parameter,
default :data:`DEFAULT_GAP_HOURS` = 6 hours -- long enough that routine
reporting jitter or a short patch of poor coverage never splits a voyage,
short enough that a vessel genuinely idle in port or dark for the better
part of a day does get a new voyage boundary), the later message starts a
new voyage. This is deliberately the only signal used: a secondary signal
built from ``navigational_status`` transitions ("moored" / "at anchor") was
considered but dropped as unnecessary complexity for a structural baseline --
see the module docstring of ``detect`` (Phase 2) for where the richer,
*classifying* version of this problem (is a given gap suspicious or
ordinary?) actually lives. This module does not attempt that classification;
it only splits tracks on the plain distance/time rule.

**Known limitation, not solved here.** A reused MMSI (``process.identity``'s
``is_reused``) can make two physically different vessels appear as one
continuous track, since this module keys everything by MMSI alone. Detecting
and correcting for that is out of scope for P1-3.

**Output shape.** Like ``process.identity``, both outputs are flat, whole-
range tables under ``data/tracks/``, not Hive-partitioned by day: a voyage
is inherently cross-date (it can start on one day and end on the next), so
partitioning by date would risk splitting a single voyage's points across
two files and would make "how many points in this voyage" a multi-file
query. For the same reason there is no ``reconstruct_day`` counterpart to
``reconstruct_range`` -- a single day is just a range of length one.

1. ``data/tracks/points.parquet`` -- every input position, unchanged, plus
   ``voyage_seq`` (1-based, restarting at 1 for each MMSI) and ``voyage_id``
   (``"{mmsi}-{voyage_seq}"``), ordered by (mmsi, timestamp).
2. ``data/tracks/voyages.parquet`` -- one row per (mmsi, voyage_seq): start
   and end time, start and end position, point count and duration.

All of this runs as DuckDB window/aggregate queries over views; the data is
never pulled into Python.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
TRACKS_ROOT = Path("data/tracks")
POINTS_PATH = TRACKS_ROOT / "points.parquet"
VOYAGES_PATH = TRACKS_ROOT / "voyages.parquet"

# Default voyage-boundary gap: see module docstring for the reasoning.
# Callers (library or CLI) may override this per run.
DEFAULT_GAP_HOURS = 6.0


def _daterange(start: date, end: date) -> Iterator[date]:
    """Yield each date from start to end, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _partition_path(day: date, root: Path) -> Path:
    """Hive-style partition path for one day, e.g. .../date=2024-06-05/part-0.parquet."""
    return root / f"date={day.isoformat()}" / "part-0.parquet"


def _existing_partitions(start: date, end: date, in_root: Path) -> list[tuple[date, Path]]:
    """Return (day, path) for every day in [start, end] whose clean partition exists.

    Mirrors ``process.identity``'s helper of the same name: a missing day is
    logged and skipped, not fatal, since reconstructing tracks over a range
    is meant to work with whatever has been cleaned so far.
    """
    found = []
    for day in _daterange(start, end):
        path = _partition_path(day, in_root)
        if path.exists():
            found.append((day, path))
        else:
            logger.warning("No clean partition for %s at %s, skipping", day.isoformat(), path)
    return found


def _reconstruct(
    con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]], gap_hours: float
) -> None:
    """Build the `points` and `voyages` views from the given clean partitions.

    Runs as a handful of DuckDB queries over views; row-level data is never
    pulled into Python.
    """
    per_day = [f"SELECT * FROM read_parquet('{path.as_posix()}')" for _day, path in partitions]
    con.execute(f"CREATE OR REPLACE VIEW all_days AS {' UNION ALL BY NAME '.join(per_day)}")

    gap_seconds = gap_hours * 3600

    # Distance-in-time to the previous message from the same MMSI. NULL for
    # each MMSI's very first message, which by definition starts a voyage.
    con.execute(
        "CREATE OR REPLACE VIEW ordered AS "
        "SELECT *, lag(timestamp) OVER (PARTITION BY mmsi ORDER BY timestamp) AS _prev_ts "
        "FROM all_days"
    )

    con.execute(
        "CREATE OR REPLACE VIEW flagged AS "
        "SELECT *, CASE WHEN _prev_ts IS NULL "
        f"OR date_diff('second', _prev_ts, timestamp) > {gap_seconds} "
        "THEN 1 ELSE 0 END AS _new_voyage "
        "FROM ordered"
    )

    # A running count of voyage-start flags per MMSI, in timestamp order, is
    # exactly a 1-based voyage sequence number. sum() over an INTEGER column
    # widens to HUGEINT by default, which Parquet has no native type for and
    # DuckDB would silently round-trip as DOUBLE; CAST back to BIGINT so the
    # column stays a clean integer on disk.
    con.execute(
        "CREATE OR REPLACE VIEW segmented AS "
        "SELECT * EXCLUDE (_prev_ts, _new_voyage), "
        "CAST(sum(_new_voyage) OVER (PARTITION BY mmsi ORDER BY timestamp "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS BIGINT) AS voyage_seq "
        "FROM flagged"
    )

    con.execute(
        "CREATE OR REPLACE VIEW points AS "
        "SELECT *, CAST(mmsi AS VARCHAR) || '-' || CAST(voyage_seq AS VARCHAR) AS voyage_id "
        "FROM segmented"
    )

    con.execute(
        "CREATE OR REPLACE VIEW voyages AS "
        "SELECT mmsi, voyage_seq, voyage_id, "
        "min(timestamp) AS start_time, max(timestamp) AS end_time, "
        "arg_min(latitude, timestamp) AS start_latitude, "
        "arg_min(longitude, timestamp) AS start_longitude, "
        "arg_max(latitude, timestamp) AS end_latitude, "
        "arg_max(longitude, timestamp) AS end_longitude, "
        "count(*) AS point_count, "
        "date_diff('second', min(timestamp), max(timestamp)) AS duration_seconds "
        "FROM points "
        "GROUP BY mmsi, voyage_seq, voyage_id"
    )


def reconstruct_range(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_root: Path = TRACKS_ROOT,
    gap_hours: float = DEFAULT_GAP_HOURS,
    force: bool = False,
) -> tuple[Path, Path]:
    """Reconstruct tracks and segment voyages for every clean partition in [start, end].

    Idempotent: if both output files already exist, this is a no-op unless
    force=True. Returns (points_path, voyages_path) either way. Raises
    FileNotFoundError if no clean partition exists anywhere in the requested
    range; a partial range with some days missing only warns, see
    _existing_partitions. Because of the cross-date design (see module
    docstring), there is deliberately no ``reconstruct_day``.
    """
    points_path = out_root / "points.parquet"
    voyages_path = out_root / "voyages.parquet"
    if points_path.exists() and voyages_path.exists() and not force:
        logger.info(
            "%s and %s already exist, skipping (pass force=True / --force to rebuild)",
            points_path,
            voyages_path,
        )
        return points_path, voyages_path

    partitions = _existing_partitions(start, end, in_root)
    if not partitions:
        raise FileNotFoundError(
            f"No clean partitions found for {start.isoformat()}..{end.isoformat()} under {in_root}"
        )

    con = duckdb.connect()
    try:
        _reconstruct(con, partitions, gap_hours)

        (n_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM points").fetchone()
        (n_voyages,) = con.execute("SELECT count(*) FROM voyages").fetchone()
        (n_points,) = con.execute("SELECT count(*) FROM points").fetchone()
        avg_points = n_points / n_voyages if n_voyages else 0.0
        logger.info(
            "Reconstructed %d voyage(s) for %d distinct MMSI over %d day(s) "
            "(gap threshold %.1fh): %.1f points/voyage on average",
            n_voyages,
            n_mmsi,
            len(partitions),
            gap_hours,
            avg_points,
        )

        out_root.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT * FROM points ORDER BY mmsi, timestamp) "
            f"TO '{points_path.as_posix()}' (FORMAT PARQUET)"
        )
        con.execute(
            "COPY (SELECT * FROM voyages ORDER BY mmsi, voyage_seq) "
            f"TO '{voyages_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return points_path, voyages_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconstruct per-vessel tracks and segment them into voyages."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--gap-hours",
        type=float,
        default=DEFAULT_GAP_HOURS,
        help=f"Time-gap threshold in hours that starts a new voyage (default: {DEFAULT_GAP_HOURS})",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the outputs already exist"
    )
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-dir", default=str(TRACKS_ROOT), help="Root for the points/voyages output tables"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    reconstruct_range(
        start,
        end,
        in_root=Path(args.in_dir),
        out_root=Path(args.out_dir),
        gap_hours=args.gap_hours,
        force=args.force,
    )


if __name__ == "__main__":
    main()
