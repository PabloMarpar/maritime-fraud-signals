"""Cross-vessel corroboration for AIS silences: was the receiver alive, or was the area dark?

**Why this exists.** ``detect.coverage``'s gap-ratio map answers "how often does this vessel
class, once heard from at all in this cell, keep reporting promptly" -- and documents why that
number cannot safely be read as "the probability a receiver would hear a vessel here": a pair
only forms between two RECEIVED messages, so a stretch of sea with no traffic at all produces no
row (not a low score), and a vessel that deliberately goes dark still contributes a "long" pair
to its *own* cell's denominator, manufacturing the very "bad coverage here" excuse that would
exonerate it. ``detect.coverage`` names the fix as out of scope for that module: "does some
*other* vessel report normally from this cell while this one goes dark?" This module is that
fix.

**Method.** Every mobile AIS transmission (Class A / Class B; fixed Base Station and AtoN
beacons are excluded -- see below) is reduced to one row per (grid cell, hour, mmsi): whether
that vessel was heard in that cell during that hour, how many messages, and the first/last
timestamp within the hour. This is pure observation, no aggregation across vessels or time, so
the *historical baseline* stays free of temporal leakage by construction: it only ever reads
``cell_hour`` strictly before ``as_of``.

**A verdict is not available until ``window_end``, not ``as_of``.** The concurrent-corroboration
count deliberately reads ``[window_start, window_end]`` -- that is the whole point, it is asking
"was anyone else heard *during* the silence" -- so a :class:`LivenessVerdict` describes what could
be known once the silence is over, not what could be known on the day it started. A caller
attaching a verdict to a vessel-month panel under a temporal cutoff ``T`` must stamp it with
``max(window_end, as_of)``, not ``window_start`` or ``as_of`` alone, or it will silently credit
month ``as_of`` with information that only existed after the gap closed.

**The tri-state verdict** (:class:`LivenessVerdict`), for a set of cells and a time window during
which some vessel V was silent:

* ``receiver_alive`` -- another vessel *was* heard in these cells overlapping the window. Coverage
  does not explain V's silence.
* ``area_dark`` -- nobody else was heard, but these cells' pre-window history (excluding V) shows
  enough traffic that a receiver outage is a plausible explanation.
* ``no_evidence`` -- neither corroboration nor a usable baseline exists. The cells are empty or
  unmonitored; no inference can be drawn either way.

**Leave-one-out, including the baseline.** Every query excludes V's own messages -- not just from
the concurrent corroboration count, but from the historical baseline too. Excluding V only from
the corroboration count is not enough: a vessel that is the sole historical occupant of a quiet
corridor would otherwise make that corridor "normally trafficked" by its own record, then be
exonerated as ``area_dark`` for going dark in the corridor only it ever visits -- the same
circularity ``detect.coverage`` was built to escape, one level up. With the baseline also
excluding V, that case correctly falls to ``no_evidence`` (see
``test_area_dark_baseline_also_excludes_self``). ``exclude_mmsi`` accepts a single mmsi or a
sequence of them: an MMSI is a radio identity, not a hull (see ``process.identity``), so a caller
that has already linked V to other MMSIs (a reused MMSI, or a companion vessel under P2-5) should
exclude all of them, or the excluded set's own presence under a different identity could still
build the baseline that exonerates it. This module does not resolve that linkage itself.

**Why a rate, not "normal for this hour-of-day".** Commercial shipping in the pilot region is
close to aperiodic: the diurnal swing in raw message volume across the whole 30-day window is only
~1.2x. Conditioning the baseline on hour-of-day to explain a ~20% effect divides the sample by 24
-- most (cell, hour-of-day) pairs then have too few historical observations to mean anything.
Instead, ``area_dark`` compares the *expected* number of corroborators over the silence window (a
per-cell rate, vessel-hours per hour excluding V, times the window's length) to ``min_expected``.
This threshold is **not** a validated false-negative rate -- an earlier draft of this docstring
claimed a Poisson-derived "~5% chance of seeing nobody when coverage is normal", which does not
survive scrutiny: vessel-hours and distinct vessels are different units, and a *single* vessel
that happens to report continuously accumulates vessel-hours just as fast as several different
vessels passing through, which would let one recurring source single-handedly justify
``area_dark``. ``min_baseline_vessels`` (default 3) is a separate, additional gate on the
*breadth* of the baseline -- the number of distinct vessels behind it, leave-one-out applied --
precisely to block that: ``area_dark`` requires both a high enough expected count **and** that it
come from more than one or two vessels' history. Both thresholds are defaults awaiting the kind of
empirical calibration ``detect.coverage``'s number never got, not derived constants.

**Why the default evidence radius is the exact cell, not a wide ring.** A terrestrial AIS
receiver's footprint (40-70km) is far larger than one 0.1-degree cell (~11km x 6.4km at Danish
latitudes), so corroboration from a neighbouring cell is real evidence a receiver was listening.
:func:`cells_within` supports widening the search with ``ring``, and every verdict reports how
many cells it scanned -- but the default is ``ring=0``. A vessel heard 30km away proves a
receiver was working, not that it could hear V's exact position; how far to look is a judgement
call about route geometry that belongs to the caller (P2-2), not a default this module should
make silently.

**Known limitation, not solved here.** All three verdicts are conditioned on there having been
*some* vessel in the cell to be heard. ``receiver_alive`` conflates "the receiver was working"
with "traffic happened to be there at that moment"; ``area_dark`` conflates "a genuine receiver
outage" with "the sea was genuinely empty right then". The tri-state does not dissolve this -- it
only prevents silently assuming one branch instead of naming the other two.

**``ring`` and the expected-corroborators rate do not scale the same way.** ``base``'s vessel-hour
count sums across every scanned cell, so widening ``ring`` inflates ``expected_corroborators`` for
a single transiting vessel roughly in proportion to how many cells its route touches, while
``live``'s ``n_corroborators`` stays a count of distinct vessels regardless of how many cells they
were seen in. A caller widening ``ring`` should re-examine ``min_expected`` alongside it, not
assume the same default travels.

All of this runs as DuckDB SQL over views and a small parameterised query; the data is never
pulled into Python row by row, except the handful of scalars in a verdict.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import duckdb

from process.partitions import existing_partitions

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
COVERAGE_ROOT = Path("data/coverage")
LIVENESS_PATH = COVERAGE_ROOT / "liveness.parquet"

GRID_SIZE_DEG = 0.1
# Excludes fixed Base Station and AtoN beacons, and SAR Airborne units, which would otherwise
# make their cell look permanently alive regardless of any vessel's presence.
MOBILE_TYPES = ("Class A", "Class B")
MIN_CORROBORATORS = 1
MIN_EXPECTED_CORROBORATORS = 3.0  # unvalidated default -- see module docstring
MIN_BASELINE_VESSELS = 3  # breadth gate: blocks one recurring vessel from justifying area_dark
BASELINE_DAYS = 30


def _git_sha() -> str:
    """Short git commit SHA of the working tree, or "unknown" if it can't be determined.

    Provenance metadata only, never correctness-critical, so any failure (not a git repo, git
    not on PATH, etc.) falls back to a literal string rather than raising.
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
    mobile_types: tuple[str, ...],
) -> None:
    per_day = [
        "SELECT mmsi, timestamp, latitude, longitude, type_of_mobile "
        f"FROM read_parquet('{path.as_posix()}')"
        for _day, path in partitions
    ]
    con.execute(f"CREATE OR REPLACE VIEW all_days AS {' UNION ALL '.join(per_day)}")

    mobile_list = ", ".join(f"'{t}'" for t in mobile_types)
    con.execute(
        "CREATE OR REPLACE VIEW mobile AS "
        "SELECT mmsi, timestamp, latitude, longitude, "
        "type_of_mobile = 'Class A' AS is_class_a "
        "FROM all_days "
        f"WHERE type_of_mobile IN ({mobile_list})"
    )

    # Same epsilon-before-floor, round-after-multiply convention as detect.coverage, so the two
    # artifacts key on byte-identical cell values -- see test_grid_cell_values_match_coverage_module.
    _EPSILON = 1e-9
    con.execute(
        "CREATE OR REPLACE VIEW celled AS "
        "SELECT "
        f"ROUND(FLOOR(latitude / {grid_size_deg} + {_EPSILON}) * {grid_size_deg}, 6) AS cell_lat, "
        f"ROUND(FLOOR(longitude / {grid_size_deg} + {_EPSILON}) * {grid_size_deg}, 6) AS cell_lon, "
        "date_trunc('hour', timestamp) AS cell_hour, "
        "mmsi, is_class_a, timestamp "
        "FROM mobile"
    )

    con.execute(
        "CREATE OR REPLACE VIEW presence AS "
        "SELECT cell_lat, cell_lon, cell_hour, mmsi, "
        "bool_or(is_class_a) AS is_class_a, "
        "CAST(count(*) AS INTEGER) AS n_messages, "
        "min(timestamp) AS first_seen, max(timestamp) AS last_seen "
        "FROM celled "
        "GROUP BY cell_lat, cell_lon, cell_hour, mmsi"
    )


def build_liveness(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_path: Path = LIVENESS_PATH,
    grid_size_deg: float = GRID_SIZE_DEG,
    mobile_types: tuple[str, ...] = MOBILE_TYPES,
    force: bool = False,
) -> Path:
    """Build the cell-hour vessel-presence table for every clean partition in [start, end].

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError if no clean partition exists anywhere in the requested
    range; a partial range with some days missing only warns, see
    process.partitions.existing_partitions. There is deliberately no ``build_liveness_day``, for
    the same reason as ``detect.coverage``'s ``build_coverage_map``: a single day is just a range
    of length one.
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
        _build(con, partitions, grid_size_deg, mobile_types)

        (n_rows,) = con.execute("SELECT count(*) FROM presence").fetchone()
        (n_cells,) = con.execute(
            "SELECT count(DISTINCT cell_lat || ',' || cell_lon) FROM presence"
        ).fetchone()
        (n_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM presence").fetchone()
        logger.info(
            "Built liveness table: %d (cell, hour, mmsi) row(s), %d distinct cell(s), "
            "%d distinct mmsi",
            n_rows,
            n_cells,
            n_mmsi,
        )

        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{git_sha}' AS git_sha "
            "FROM presence ORDER BY cell_lat, cell_lon, cell_hour) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def cells_within(
    cells: Iterable[tuple[float, float]],
    ring: int = 0,
    grid_size_deg: float = GRID_SIZE_DEG,
) -> list[tuple[float, float]]:
    """Expand each (cell_lat, cell_lon) to itself plus its `ring` neighbours in every direction.

    ring=0 returns the cells unchanged (deduplicated); ring=1 returns the 3x3 block around each,
    ring=2 the 5x5 block, and so on. Values are rounded to 6 decimals to match the flooring
    convention used to build both the liveness and coverage grids.
    """
    if ring < 0:
        raise ValueError(f"ring must be >= 0, got {ring}")
    offsets = range(-ring, ring + 1)
    expanded = {
        (round(lat + dlat * grid_size_deg, 6), round(lon + dlon * grid_size_deg, 6))
        for lat, lon in cells
        for dlat in offsets
        for dlon in offsets
    }
    return sorted(expanded)


@dataclass(frozen=True)
class LivenessVerdict:
    """The result of asking "was the receiver alive during this vessel's silence?"."""

    verdict: Literal["receiver_alive", "area_dark", "no_evidence"]
    n_corroborators: int
    n_messages: int
    n_corroborators_class_a: int
    baseline_vessel_hours: float
    n_baseline_vessels: int
    baseline_hours_available: float
    expected_corroborators: float
    n_cells: int
    n_cell_hours_scanned: int


def liveness_verdict(
    con: duckdb.DuckDBPyConnection,
    cells: Sequence[tuple[float, float]],
    window_start: datetime,
    window_end: datetime,
    exclude_mmsi: int | Sequence[int],
    liveness_path: Path = LIVENESS_PATH,
    as_of: date | None = None,
    baseline_days: int = BASELINE_DAYS,
    min_corroborators: int = MIN_CORROBORATORS,
    min_expected: float = MIN_EXPECTED_CORROBORATORS,
    min_baseline_vessels: int = MIN_BASELINE_VESSELS,
) -> LivenessVerdict:
    """Classify a silence by ``exclude_mmsi`` over ``cells`` during [window_start, window_end].

    ``con`` is supplied by the caller (P2-2 will call this once per candidate gap, thousands of
    times per run, and must not pay to open a connection every call). ``as_of`` (default:
    window_start's date) is the anti-leakage cutoff: the historical baseline only ever reads whole
    days strictly before it, never the day the silence itself falls on, so evidence recorded
    during or after the window under scoring cannot leak backward into the baseline that judges it
    -- see ``test_baseline_ignores_hours_at_or_after_as_of``. The corroboration read itself is
    intentionally as of ``window_end``, not ``as_of`` -- see the module docstring for what that
    means for a caller attaching this verdict to a temporally-cut-off panel.

    Every read -- both the concurrent corroboration count and the historical baseline -- excludes
    ``exclude_mmsi``'s own messages first. See the module docstring for why the baseline must be
    leave-one-out too, not only the corroboration count.

    The baseline rate's denominator is the portion of the *nominal* ``baseline_days``-day window
    that ``liveness_path`` was actually built to cover (via its own ``window_start``/``window_end``
    provenance columns), not ``baseline_days`` itself -- a caller asking for a 30-day baseline
    against a table only built for 10 days gets a rate over 10 days of evidence, not a rate
    silently diluted by 20 days that were never observed. See :attr:`LivenessVerdict.
    baseline_hours_available`.
    """
    if not cells:
        raise ValueError("cells must be non-empty")
    if not liveness_path.exists():
        raise FileNotFoundError(f"No liveness table at {liveness_path}; run build_liveness first")
    if window_start.tzinfo is not None or window_end.tzinfo is not None:
        raise ValueError("window_start/window_end must be naive datetimes (UTC assumed)")

    exclude_mmsis = [exclude_mmsi] if isinstance(exclude_mmsi, int) else list(exclude_mmsi)

    as_of_date = as_of if as_of is not None else window_start.date()
    baseline_start = datetime.combine(as_of_date - timedelta(days=baseline_days), datetime.min.time())
    baseline_end = datetime.combine(as_of_date, datetime.min.time())
    if baseline_end > window_start:
        raise ValueError(
            f"as_of ({as_of_date}) must not be after window_start ({window_start}): the "
            "baseline must never include the window it is judging"
        )
    window_hours = (window_end - window_start).total_seconds() / 3600.0
    # Nothing at or after this bound is relevant to either the live or the baseline read, so
    # bounding `obs` here keeps every derived quantity -- including n_cell_hours_scanned -- free
    # of post-window data by construction, not just by omission from the two CTEs that use it.
    obs_upper_bound = window_end + timedelta(hours=1)

    cells_values = ", ".join(f"({lat}, {lon})" for lat, lon in cells)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _liveness_cells AS "
        f"SELECT * FROM (VALUES {cells_values}) AS t(cell_lat, cell_lon)"
    )

    query = (
        "WITH raw AS (SELECT * FROM read_parquet(?)),"
        "bounds AS ("
        "  SELECT min(window_start) AS built_start, max(window_end) AS built_end FROM raw"
        "),"
        "obs AS ("
        "  SELECT r.* FROM raw r"
        "  JOIN _liveness_cells c ON r.cell_lat = c.cell_lat AND r.cell_lon = c.cell_lon"
        "  WHERE NOT list_contains(?, r.mmsi) AND r.cell_hour < ?"
        "),"
        "live AS ("
        "  SELECT count(DISTINCT mmsi) AS n_corroborators,"
        "         coalesce(sum(n_messages), 0)::BIGINT AS n_messages,"
        "         count(DISTINCT CASE WHEN is_class_a THEN mmsi END) AS n_corroborators_class_a"
        "  FROM obs"
        "  WHERE last_seen >= ? AND first_seen <= ?"
        "),"
        "base AS ("
        "  SELECT count(*)::DOUBLE AS vessel_hours, count(DISTINCT mmsi) AS n_vessels"
        "  FROM obs"
        "  WHERE cell_hour >= ? AND cell_hour < ?"
        ")"
        "SELECT live.n_corroborators, live.n_messages, live.n_corroborators_class_a,"
        "       base.vessel_hours, base.n_vessels,"
        "       (SELECT count(DISTINCT cell_hour) FROM obs),"
        "       bounds.built_start, bounds.built_end "
        "FROM live, base, bounds"
    )
    (
        n_corroborators,
        n_messages,
        n_corroborators_class_a,
        baseline_vessel_hours,
        n_baseline_vessels,
        n_cell_hours_scanned,
        built_start,
        built_end,
    ) = con.execute(
        query,
        [
            str(liveness_path),
            exclude_mmsis,
            obs_upper_bound,
            window_start,
            window_end,
            baseline_start,
            baseline_end,
        ],
    ).fetchone()

    if built_start is None or built_end is None:
        baseline_hours_available = 0.0
    else:
        built_start_ts = datetime.combine(built_start, datetime.min.time())
        built_end_ts = datetime.combine(built_end + timedelta(days=1), datetime.min.time())
        overlap_start = max(baseline_start, built_start_ts)
        overlap_end = min(baseline_end, built_end_ts)
        baseline_hours_available = max(0.0, (overlap_end - overlap_start).total_seconds() / 3600.0)

    rate = baseline_vessel_hours / baseline_hours_available if baseline_hours_available > 0 else 0.0
    expected_corroborators = rate * window_hours

    if n_corroborators >= min_corroborators:
        verdict: Literal["receiver_alive", "area_dark", "no_evidence"] = "receiver_alive"
    elif expected_corroborators >= min_expected and n_baseline_vessels >= min_baseline_vessels:
        verdict = "area_dark"
    else:
        verdict = "no_evidence"

    return LivenessVerdict(
        verdict=verdict,
        n_corroborators=n_corroborators,
        n_messages=n_messages,
        n_corroborators_class_a=n_corroborators_class_a,
        baseline_vessel_hours=baseline_vessel_hours,
        n_baseline_vessels=n_baseline_vessels,
        baseline_hours_available=baseline_hours_available,
        expected_corroborators=expected_corroborators,
        n_cells=len(cells),
        n_cell_hours_scanned=n_cell_hours_scanned,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the cell-hour vessel-presence table used for cross-vessel "
        "corroboration of AIS silences, from clean AIS Parquet partitions."
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
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-path", default=str(LIVENESS_PATH), help="Output path for the liveness table"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    build_liveness(
        start,
        end,
        in_root=Path(args.in_dir),
        out_path=Path(args.out_path),
        grid_size_deg=args.grid_size_deg,
        force=args.force,
    )


if __name__ == "__main__":
    main()
