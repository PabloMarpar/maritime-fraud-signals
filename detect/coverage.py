"""Empirical AIS receive-coverage map: probability of hearing a vessel again soon,
per sea grid cell and vessel class.

**Why this exists.** A gap in a vessel's AIS track can mean two very different
things: the transponder was switched off (evasive), or the vessel simply
sailed through water with poor terrestrial-receiver coverage (routine). The
other Phase 2 detectors -- especially P2-2, deliberate gaps -- cannot tell
these apart from a track alone; they need an independent, empirical baseline
of "how reliably do we normally hear *any* vessel of this class near here?"
against which an observed gap can be compared. This module builds that
baseline. It is deliberately not itself a detector (it makes no fraud
judgement); it is shared infrastructure the detectors lean on.

**Method.** The sea is divided into a flat grid of ``grid_size_deg`` x
``grid_size_deg`` cells (default 0.1 degrees, roughly 11km at Danish
latitudes). A cell is identified by its lower-left corner: ``cell_lat =
floor(latitude / grid_size_deg) * grid_size_deg``, same for longitude --
this floors toward the corner regardless of hemisphere sign (verified
against negative longitudes too, since the flooring logic is written to be
correct in general even though real Danish-waters data is all positive).
The SQL implementation (see ``_build``) adds a tiny epsilon before the floor
and rounds after the multiply, purely to cancel binary floating-point
representation noise around exact decimal boundaries (0.1 has no exact
binary representation) -- not a change to the floor-based definition itself.
For every vessel (``mmsi``), its clean messages are ordered by timestamp
across the *whole* requested date range -- a plain per-mmsi ``lag()``, with
no dependency on ``process.tracks``' voyage segmentation. This is a
deliberate decoupling, not an oversight: coverage is a property of a
*place*, observed through the raw cadence of messages, and voyage boundaries
(which already use a time-gap rule internally) would introduce a circular
dependency between "was this gap long enough to end a voyage" and "was this
gap long enough to suggest AIS was off" -- two different questions the
project wants to be able to answer independently.

Each consecutive pair of messages from the same mmsi (``prev`` -> ``curr``)
is one observation, attributed to the grid cell and vessel class of
``prev`` (the position/class *before* the gap opened -- see below for why
``prev`` over ``curr``). A pair is "short" if the gap between the two
messages is at most ``short_gap_seconds`` (default 1800s / 30 minutes --
comfortably above any legitimate AIS reporting cadence, so an unbroken
silence longer than this is either an outage or a coverage hole, never
routine jitter), otherwise "long". Per (cell, ship_type), the coverage
probability is the fraction of pairs that were short: a cell/class with a
coverage probability near 1.0 reliably hears vessels every few minutes,
so a long gap for that class in that cell is a strong "AIS off" signal; a
cell/class with a low coverage probability has a receive baseline so patchy
that a long gap there is unremarkable. Cells/classes with fewer than
``min_pairs`` observations (default 20) get a NULL ``coverage_probability``
rather than a spuriously precise 0 or 1 -- insufficient data must read as
"unknown", not as a confident estimate.

**ship_type resolution.** AIS static-data messages (which carry ship type)
are, in the general NMEA/raw sense, sent far less often than position
reports, so the design anticipated needing to forward-fill the last known
non-null ``ship_type`` per mmsi. Checked empirically against the real clean
day on disk (2024-06-05, ``SELECT count(*), count(ship_type) ...``):
``ship_type`` is non-null on 100% of clean rows. This is because the DMA's
published historical CSV already carries a resolved ship-type field per
record (joined from the source's own vessel register), not a raw per-message
static broadcast -- so the null rate that would justify a forward-fill (the
task's >20% threshold) does not appear in this data source. Given that, no
forward-fill is implemented here: each pair is attributed to ``prev``'s
``ship_type`` as read directly from the clean partition (prev over curr is
an arbitrary but immaterial choice per the task -- ship_type essentially
never changes within one mmsi's short window, so the two agree in practice).
If a future data source does exhibit a high null rate, forward-filling
``ship_type`` per mmsi (``last_value(... IGNORE NULLS) OVER (PARTITION BY
mmsi ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)``)
before pairing is the documented fallback, not implemented because it is not
needed by the data actually on disk.

**Output shape.** Like ``process.identity`` and ``process.tracks``, this
writes one flat table for the whole requested range, not partitioned by
day, at ``data/coverage/grid.parquet``: a cell's coverage estimate is a
cross-date fact (a coastal cell needs pairs pooled across many days to clear
``min_pairs``), so partitioning by day would fragment a single cell's
evidence across files and turn "how confident is this estimate" into a
multi-file query. There is deliberately no ``build_coverage_day``
counterpart, for the same reason ``process.identity``/``process.tracks``
have none: a single day is just a range of length one.

**Known limitation, not solved here.** This first version builds *one*
static coverage estimate over the whole requested range. If this map is
later used to score AIS gaps for P2-2 inside a supervised/temporal-
validation context (Phase 4), using a map built from dates *after* the day
being scored would leak future information backward across a cutoff --
CLAUDE.md explicitly forbids this ("Never let future information reach a
model trained on the past"). A future revision (deferred "Tramo 2" work in
the project's data-pipeline plan) needs the coverage map broken into
mergeable per-window partials, so that scoring day D only ever uses coverage
evidence from windows ending on or before D. Stated here as a limitation to
design around later, not a TODO to chase now.

**Known limitation, not solved here: selection bias / circularity.** A pair
only exists between two messages that were both actually received (see
"Method" above), which has two consequences that must not be forgotten when
reading ``coverage_probability``. First, the estimator is blind to true
coverage holes: a stretch of sea where no message from *any* vessel is ever
received produces no row at all, not a low probability -- it simply never
enters the denominator, which biases every cell that does have data toward
looking *better* than the full picture. Second, and more seriously for the
detector this map exists to support: a deliberate AIS shutdown -- exactly the
behaviour P2-2 is meant to catch -- still contributes one "long" pair to the
denominator of its *own* cell's estimate, because the message right before
the transponder went dark and the message right after it came back are both
still received and still get paired. In a cell where evasion is common (a
ship-to-ship transfer area, say), this depresses that cell's
``coverage_probability``, which would lead a future P2-2 to read "this cell
has naturally poor coverage" and dismiss the very gaps that are the evasion
signal -- the estimator would be partly measuring the thing it is meant to
help detect. Concretely: ``coverage_probability`` should currently be read as
"how often this vessel class, when heard from at all in this cell, kept
reporting promptly" -- *not* as "the probability a receiver would hear a
vessel here", which is the intuitive but incorrect reading. Fixing this
needs a genuinely different algorithm (cross-vessel corroboration: does some
*other* vessel report normally from this cell while this one goes dark?),
which is out of scope for this module and belongs to whoever builds P2-2.
Until that exists, this map is not yet safe as a direct input to P2-2's gap
classification.

All of this runs as DuckDB SQL over views; the data is never pulled into
Python row by row.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.partitions import existing_partitions

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
COVERAGE_ROOT = Path("data/coverage")
COVERAGE_PATH = COVERAGE_ROOT / "grid.parquet"

GRID_SIZE_DEG = 0.1
SHORT_GAP_SECONDS = 1800.0
MIN_PAIRS = 20


def _git_sha() -> str:
    """Short git commit SHA of the working tree, or "unknown" if it can't be determined.

    Provenance metadata only, never correctness-critical, so any failure
    (not a git repo, git not on PATH, etc.) falls back to a literal string
    rather than raising.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _build(
    con: duckdb.DuckDBPyConnection,
    partitions: list[tuple[date, Path]],
    grid_size_deg: float,
    short_gap_seconds: float,
    min_pairs: int,
) -> None:
    """Build the `grid` view: one row per (cell_lat, cell_lon, ship_type).

    Runs as a handful of DuckDB queries over views; row-level data is never
    pulled into Python.
    """
    per_day = [
        "SELECT mmsi, timestamp, latitude, longitude, ship_type "
        f"FROM read_parquet('{path.as_posix()}')"
        for _day, path in partitions
    ]
    con.execute(f"CREATE OR REPLACE VIEW all_days AS {' UNION ALL '.join(per_day)}")

    # The previous message from the same mmsi, in timestamp order. NULL for
    # each mmsi's very first message in the range, which starts no pair.
    con.execute(
        "CREATE OR REPLACE VIEW ordered AS "
        "SELECT mmsi, timestamp, latitude, longitude, ship_type, "
        "lag(timestamp) OVER w AS prev_timestamp, "
        "lag(latitude) OVER w AS prev_latitude, "
        "lag(longitude) OVER w AS prev_longitude, "
        "lag(ship_type) OVER w AS prev_ship_type "
        "FROM all_days "
        "WINDOW w AS (PARTITION BY mmsi ORDER BY timestamp)"
    )

    # Each pair is attributed to prev's cell and prev's ship_type -- see
    # module docstring for why prev over curr. Two floating-point guards are
    # needed around the floor/multiply, both from binary floating point
    # being unable to represent decimals like 0.1 exactly:
    #  - _EPSILON, added before FLOOR: without it, a coordinate that is
    #    exactly on a grid boundary in decimal (e.g. 12.1 with a 0.1 grid)
    #    can land fractionally *below* it in double precision (12.1 / 0.1 ==
    #    120.99999999999999, not 121.0), so FLOOR would silently place it one
    #    cell short of where it decimally belongs.
    #  - ROUND(..., 6), after the multiply: guards the opposite artifact,
    #    where a clean cell boundary like 55.4 comes back as
    #    55.400000000000006, which would otherwise split one real cell into
    #    two adjacent float-valued groups.
    # 1e-9 is far below any real AIS coordinate precision (typically ~1e-5
    # degrees), so it only cancels representation noise, never a genuine
    # difference in position.
    _EPSILON = 1e-9
    con.execute(
        "CREATE OR REPLACE VIEW pairs AS "
        "SELECT "
        f"ROUND(FLOOR(prev_latitude / {grid_size_deg} + {_EPSILON}) * {grid_size_deg}, 6) "
        "AS cell_lat, "
        f"ROUND(FLOOR(prev_longitude / {grid_size_deg} + {_EPSILON}) * {grid_size_deg}, 6) "
        "AS cell_lon, "
        "prev_ship_type AS ship_type, "
        "date_diff('second', prev_timestamp, timestamp) AS gap_seconds "
        "FROM ordered "
        "WHERE prev_timestamp IS NOT NULL"
    )

    con.execute(
        "CREATE OR REPLACE VIEW flagged AS "
        "SELECT cell_lat, cell_lon, ship_type, "
        f"CASE WHEN gap_seconds <= {short_gap_seconds} THEN 1 ELSE 0 END AS is_short "
        "FROM pairs"
    )

    con.execute(
        "CREATE OR REPLACE VIEW grid AS "
        "SELECT cell_lat, cell_lon, ship_type, "
        "count(*) AS n_pairs, "
        "CAST(sum(is_short) AS BIGINT) AS n_short_pairs, "
        "CASE WHEN count(*) >= "
        f"{min_pairs} "
        "THEN sum(is_short)::DOUBLE / count(*) ELSE NULL END AS coverage_probability "
        "FROM flagged "
        "GROUP BY cell_lat, cell_lon, ship_type"
    )


def build_coverage_map(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_path: Path = COVERAGE_PATH,
    grid_size_deg: float = GRID_SIZE_DEG,
    short_gap_seconds: float = SHORT_GAP_SECONDS,
    min_pairs: int = MIN_PAIRS,
    force: bool = False,
) -> Path:
    """Build the empirical coverage map for every clean partition in [start, end].

    Idempotent: if out_path already exists, this is a no-op unless
    force=True. Returns out_path either way. Raises FileNotFoundError if no
    clean partition exists anywhere in the requested range (an empty result
    would silently look like "nothing to see here" rather than "nothing was
    read"); a partial range with some days missing only warns, see
    process.partitions.existing_partitions. Because of the cross-date design
    (see module docstring), there is deliberately no ``build_coverage_day``.
    """
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
        _build(con, partitions, grid_size_deg, short_gap_seconds, min_pairs)

        (n_cells,) = con.execute(
            "SELECT count(DISTINCT cell_lat || ',' || cell_lon) FROM grid"
        ).fetchone()
        (n_usable,) = con.execute(
            "SELECT count(*) FROM grid WHERE coverage_probability IS NOT NULL"
        ).fetchone()
        (n_below_threshold,) = con.execute(
            "SELECT count(*) FROM grid WHERE coverage_probability IS NULL"
        ).fetchone()
        (mean_coverage,) = con.execute(
            "SELECT avg(coverage_probability) FROM grid WHERE coverage_probability IS NOT NULL"
        ).fetchone()
        logger.info(
            "Built coverage map: %d distinct cell(s), %d (cell, ship_type) group(s) with a "
            "usable estimate (n_pairs >= %d), %d below the min_pairs threshold (NULL), "
            "mean coverage_probability where defined: %s",
            n_cells,
            n_usable,
            min_pairs,
            n_below_threshold,
            f"{mean_coverage:.3f}" if mean_coverage is not None else "n/a",
        )

        # Provenance columns: the same value repeated on every row of this
        # build, not per-cell. Cheap, and makes every row self-describing --
        # see module docstring, finding A -- so a future caller pointed at a
        # possibly-stale out_path (build_coverage_map is a no-op if it
        # already exists, see below) can tell what window and code version
        # actually produced it, without having to trust the filename alone.
        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{git_sha}' AS git_sha "
            "FROM grid ORDER BY cell_lat, cell_lon, ship_type) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the empirical AIS coverage map (receive probability per sea "
        "grid cell and vessel class) from clean AIS Parquet partitions."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--grid-size-deg",
        type=float,
        default=GRID_SIZE_DEG,
        help=f"Grid cell size in degrees (default: {GRID_SIZE_DEG})",
    )
    parser.add_argument(
        "--short-gap-seconds",
        type=float,
        default=SHORT_GAP_SECONDS,
        help=f"Gaps at or under this many seconds count as 'short' (default: {SHORT_GAP_SECONDS})",
    )
    parser.add_argument(
        "--min-pairs",
        type=int,
        default=MIN_PAIRS,
        help=f"Minimum pairs for a (cell, ship_type) estimate to be non-NULL (default: {MIN_PAIRS})",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-path", default=str(COVERAGE_PATH), help="Output path for the coverage map"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    build_coverage_map(
        start,
        end,
        in_root=Path(args.in_dir),
        out_path=Path(args.out_path),
        grid_size_deg=args.grid_size_deg,
        short_gap_seconds=args.short_gap_seconds,
        min_pairs=args.min_pairs,
        force=args.force,
    )


if __name__ == "__main__":
    main()
