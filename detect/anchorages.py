"""Empirical coastal-anchorage mask, so detect.sts can tell a ship-to-ship transfer from two
vessels merely moored or anchored near each other.

**Why this exists.** GFW's own ship-to-ship encounter definition (see ``docs/DATA_SOURCES.md``)
excludes anything within 10km of a coastal anchorage -- and a cheap proxy query over the real
30-day window showed exactly why that clause matters: the overwhelming majority of "two slow
vessels within 500m" pairs are neighbours moored at adjacent berths for nearly the entire window
(up to 647 of its 720 hours), not transfers. ``docs/DECISIONS.md`` independently confirms that
population exists -- the ``on_land`` spoofing check's top 10 MMSI, each moored at one exact spot
for the full month. No port/anchorage reference dataset existed in this repo before this module.

**Derived from our own AIS, not downloaded.** This mirrors how GFW built their own anchorage
layer, and captures every real anchorage present in the data -- including small Danish harbours no
global port list holds -- rather than inheriting an external dataset's own coverage gaps. The mask
lives under ``data/coverage/``, not ``data/reference/``: that directory means geometry we
*downloaded* (see :mod:`ingest.landmask`), while this table is derived from the very AIS it will
be used to filter, and that provenance distinction is the entire point of the two safeguards
below. ``ingest.ports`` (Natural Earth's public port list) exists solely as an external sanity
check on this mask, via :func:`compare_to_ports` -- never as an input to building it.

**Two anti-circularity safeguards, both a direct lesson from P2-1b's ``analyst-review`` pass**
(``docs/DECISIONS.md``: that module's first draft had a vessel-hours/distinct-vessels unit
mismatch, and a historical baseline a vessel could exonerate itself with under its own record):

1. **Distinct vessels, never vessel-hours.** A cell only becomes an anchorage if
   :data:`MIN_DISTINCT_VESSELS` or more *different* MMSI were each seen stationary there. One
   vessel moored for the entire window must never manufacture an anchorage by itself.
2. **Coastal requirement.** A cell must be within :data:`COASTAL_MAX_DISTANCE_M` of land
   (:mod:`ingest.landmask`'s polygons) to count as an anchorage at all. A cluster of vessels
   sitting still far from shore is exactly the pattern this mask must never suppress -- it is a
   candidate ship-to-ship hotspot, not noise. Such cells are written out with ``is_coastal =
   false``, never dropped: :mod:`detect.sts` filters on ``is_coastal``, and the build-time
   validation checkpoint inspects every non-coastal cluster by hand (see ``docs/DECISIONS.md``).

**Leave-one-out for a *specific* candidate pair is deliberately NOT done here.** The same cell is
an anchorage for most pairs and not an anchorage for the two vessels that alone constitute it, so
excluding a pair's own MMSI has to happen at query time, downstream in :mod:`detect.sts`, against
this table's :data:`member_mmsis` column -- not baked into this build. This module only records,
for every anchorage cell, which vessels were seen there, so that per-pair leave-one-out is
possible later.

**Method.** A vessel's dwell in a cell is measured per ``(mmsi, cell, day)`` -- never summed
across the window -- so a vessel that briefly revisits the same spot on many different days never
accumulates into "anchored there"; :data:`MIN_STATIONARY_HOURS` applies within a single day.
Cells with enough distinct, sufficiently-dwelling vessels are then checked for coastal proximity
via ``ST_Distance_Sphere(ST_ClosestPoint(land_piece, cell_centre), cell_centre)``, pre-filtered by
a planar ``ST_DWithin`` so the exact spherical distance is only computed against nearby land
pieces (:mod:`ingest.landmask`'s polygons ``ST_Dump``-ed into small pieces first -- the same fix
``detect.spoofing``'s ``check_on_land`` needed at real scale, reused here for the same reason).

Every threshold below is a considered but unvalidated default, same posture as every other
detector in this project.

**Known approximation, not fixed here.** ``ST_ClosestPoint`` finds the nearest point in planar
(degree) space, where a longitude degree is treated as equal to a latitude degree; the true
spherically-nearest point on the same coastline is generally a slightly different point. Worst
case at these latitudes is roughly a 20% overestimate of the true distance for a coastline running
close to 45 degrees to the meridian. This stacks with -- and is much smaller than -- the
ST_Distance_Sphere argument-order fix above; both push a distance estimate up, never down, so
``is_coastal`` errs toward *false* (excluding a cell from anchorage status) rather than toward
wrongly protecting a real transfer site. Acceptable given :data:`COASTAL_MAX_DISTANCE_M` is itself
an unvalidated 10km default, not chased further here; would need a densified boundary or a
locally-scaled coordinate space to remove.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from detect.liveness import MOBILE_TYPES
from ingest.landmask import LAND_PATH
from process.partitions import existing_partitions

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
COVERAGE_ROOT = Path("data/coverage")
ANCHORAGES_PATH = COVERAGE_ROOT / "anchorages.parquet"

# ~1.11km lat, ~0.63km lon at 55.5N. Unvalidated default -- see module docstring.
ANCHORAGE_CELL_DEG = 0.01
# GFW's own "stationary" cut. Unvalidated default.
STATIONARY_MAX_SOG_KNOTS = 0.5
# A vessel must sit still in one cell for this long WITHIN A SINGLE DAY to count as dwelling there
# -- see module docstring. Unvalidated default; measured sensitivity (7-day prototype, at
# min_vessels=5): 1h/3h/6h give 642/549/461 candidate cells.
MIN_STATIONARY_HOURS = 3.0
# DISTINCT mmsi, never vessel-hours -- see module docstring, the direct P2-1b lesson. Unvalidated
# default; measured sensitivity (7-day prototype, at min_stationary_hours=3): 3/5/10 give
# 895/549/276 candidate cells.
MIN_DISTINCT_VESSELS = 5
# GFW's own coastal-anchorage radius. Unvalidated default -- the anti-circularity safeguard.
COASTAL_MAX_DISTANCE_M = 10_000.0
# Planar-degree pre-filter for ST_DWithin, ahead of the exact spherical distance: a strict
# superset of COASTAL_MAX_DISTANCE_M only up to ~63.3N (10km / (111320 * cos(lat)) exceeds 0.2deg
# above that) -- see MAX_ABS_LATITUDE_DEG, which is kept well south of that break-even point so
# this stays a genuine superset everywhere this module admits data, not an approximation of one.
LAND_PREFILTER_DEG = 0.2
# Guards the handful of corrupted high-latitude positions process.clean lets through (up to 89
# degrees on a real day) -- same population detect.spoofing's bounding box had to guard against.
# Deliberately much tighter than that corrupted range (62N, not e.g. 70N): the pilot region is
# Danish/Baltic waters (54-58N), and 62N keeps LAND_PREFILTER_DEG a strict superset of
# COASTAL_MAX_DISTANCE_M (10km / (111320 * cos(62 deg)) =~ 0.19deg, still < 0.2deg) rather than
# silently admitting a 62-70N band where the pre-filter could under-cover and misclassify a
# genuinely coastal cell as non-coastal.
MAX_ABS_LATITUDE_DEG = 62.0
# A vessel's slow pings in one cell on one day are one continuous "stay" only while consecutive
# pings are no more than this far apart; a longer gap starts a new stay (see module docstring's
# Method section on why dwell is the longest single stay, not the day's raw first-to-last span).
# Unvalidated default: comfortably wider than ordinary AIS reporting cadence while stationary
# (Class A typically reports every ~3 minutes when moored/anchored), tight enough that a vessel
# passing through twice in a day with hours between cannot be stitched into one false stay.
MAX_DWELL_GAP_MINUTES = 30.0


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
    land_path: Path,
    cell_deg: float,
    stationary_max_sog_knots: float,
    min_stationary_hours: float,
    min_distinct_vessels: int,
    coastal_max_distance_m: float,
    mobile_types: tuple[str, ...],
    max_dwell_gap_minutes: float = MAX_DWELL_GAP_MINUTES,
) -> None:
    """Build the `anchorages` view: one row per candidate cell, coastal or not.

    Dwell is accumulated per day partition in a Python loop (one scan per file, invariant work
    hoisted before the loop where there is any) -- the same chunking discipline
    detect.spoofing's check_on_land needed at real scale, applied here from the start rather
    than after a slow first run.

    Within one (mmsi, cell, day), a vessel's slow pings are first segmented into "stays"
    (consecutive pings no more than max_dwell_gap_minutes apart -- the lag()-flag-running-sum
    idiom process.tracks.py uses for voyage segmentation, reused here at a finer grain), and
    stationary_hours is the LONGEST single stay's span, not the day's raw first-to-last ping span.
    Two brief, far-apart visits to the same cell on the same day must never be stitched into one
    false all-day "stay" just because both happened to be slow.
    """
    _EPSILON = 1e-9
    mobile_list = ", ".join(f"'{t}'" for t in mobile_types)

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _dwell "
        "(mmsi BIGINT, cell_lat DOUBLE, cell_lon DOUBLE, day DATE, "
        "stationary_hours DOUBLE, n_messages INTEGER)"
    )
    for day, path in partitions:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _day_stays AS "
            "WITH pts AS ("
            "  SELECT mmsi, "
            f"    ROUND(FLOOR(latitude / {cell_deg} + {_EPSILON}) * {cell_deg}, 6) AS cell_lat, "
            f"    ROUND(FLOOR(longitude / {cell_deg} + {_EPSILON}) * {cell_deg}, 6) AS cell_lon, "
            "    timestamp "
            f"  FROM read_parquet('{path.as_posix()}') "
            f"  WHERE type_of_mobile IN ({mobile_list}) "
            f"    AND sog < {stationary_max_sog_knots} "
            f"    AND abs(latitude) <= {MAX_ABS_LATITUDE_DEG}"
            "), gapped AS ("
            "  SELECT *, CASE WHEN lag(timestamp) OVER w IS NULL "
            f"    OR date_diff('minute', lag(timestamp) OVER w, timestamp) > {max_dwell_gap_minutes} "
            "    THEN 1 ELSE 0 END AS _new_stay "
            "  FROM pts WINDOW w AS (PARTITION BY mmsi, cell_lat, cell_lon ORDER BY timestamp)"
            "), staged AS ("
            "  SELECT *, CAST(sum(_new_stay) OVER (PARTITION BY mmsi, cell_lat, cell_lon "
            "    ORDER BY timestamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS BIGINT"
            "  ) AS stay_seq FROM gapped"
            ") "
            "SELECT mmsi, cell_lat, cell_lon, stay_seq, "
            "date_diff('minute', min(timestamp), max(timestamp)) / 60.0 AS stay_hours, "
            "CAST(count(*) AS INTEGER) AS n_messages "
            "FROM staged GROUP BY mmsi, cell_lat, cell_lon, stay_seq"
        )
        con.execute(
            "INSERT INTO _dwell "
            "SELECT mmsi, cell_lat, cell_lon, "
            f"DATE '{day.isoformat()}' AS day, "
            "max(stay_hours) AS stationary_hours, "
            "CAST(sum(n_messages) AS INTEGER) AS n_messages "
            "FROM _day_stays GROUP BY mmsi, cell_lat, cell_lon"
        )

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _cells AS "
        "SELECT cell_lat, cell_lon, "
        f"cell_lat + {cell_deg / 2} AS center_latitude, "
        f"cell_lon + {cell_deg / 2} AS center_longitude, "
        "CAST(count(DISTINCT mmsi) AS INTEGER) AS n_vessels, "
        "CAST(count(*) AS INTEGER) AS n_vessel_days, "
        "sum(stationary_hours) AS total_stationary_hours, "
        "list_sort(list_distinct(list(mmsi))) AS member_mmsis "
        "FROM _dwell "
        f"WHERE stationary_hours >= {min_stationary_hours} "
        "GROUP BY cell_lat, cell_lon "
        f"HAVING count(DISTINCT mmsi) >= {min_distinct_vessels}"
    )

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _land_pieces AS "
        "SELECT (UNNEST(ST_Dump(geom))).geom AS geom FROM read_parquet(?)",
        [str(land_path)],
    )

    # ST_Distance_Sphere in this DuckDB build expects each point as ST_Point(latitude, longitude)
    # -- the reverse of every other spatial function used here (ST_Contains, ST_DWithin,
    # ST_ClosestPoint all take/return the standard ST_Point(longitude, latitude) that land.parquet
    # is stored in). Verified empirically this session: a pure east-west offset of known distance
    # came back inflated by 1/cos(lat) (~1.7x at these latitudes) with the standard argument order,
    # and matched ground truth once swapped. ST_ClosestPoint's result is therefore re-wrapped via
    # ST_Y/ST_X (not re-queried) to flip it into the order ST_Distance_Sphere wants, without
    # touching the land geometry's own (lon, lat) storage that ST_Contains elsewhere depends on.
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _cell_distances AS "
        "SELECT c.*, "
        "(SELECT min(ST_Distance_Sphere("
        "   ST_Point(ST_Y(closest), ST_X(closest)), "
        "   ST_Point(c.center_latitude, c.center_longitude)"
        " )) FROM ("
        "   SELECT ST_ClosestPoint(l.geom, ST_Point(c.center_longitude, c.center_latitude)) AS closest "
        "   FROM _land_pieces l "
        f"   WHERE ST_DWithin(l.geom, ST_Point(c.center_longitude, c.center_latitude), "
        f"     {LAND_PREFILTER_DEG})"
        " )"
        ") AS distance_to_land_m "
        "FROM _cells c"
    )

    con.execute(
        "CREATE OR REPLACE VIEW anchorages AS "
        "SELECT *, "
        f"coalesce(distance_to_land_m, 1e9) <= {coastal_max_distance_m} AS is_coastal "
        "FROM _cell_distances"
    )


def build_anchorages(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    land_path: Path = LAND_PATH,
    out_path: Path = ANCHORAGES_PATH,
    cell_deg: float = ANCHORAGE_CELL_DEG,
    stationary_max_sog_knots: float = STATIONARY_MAX_SOG_KNOTS,
    min_stationary_hours: float = MIN_STATIONARY_HOURS,
    min_distinct_vessels: int = MIN_DISTINCT_VESSELS,
    coastal_max_distance_m: float = COASTAL_MAX_DISTANCE_M,
    mobile_types: tuple[str, ...] = MOBILE_TYPES,
    max_dwell_gap_minutes: float = MAX_DWELL_GAP_MINUTES,
    force: bool = False,
) -> Path:
    """Build the empirical anchorage mask over every clean partition in [start, end] and write out_path.

    Idempotent: if out_path already exists AND was built for this same [start, end], this is a
    no-op unless force=True. Returns out_path either way. A DIFFERENT stored window raises
    ValueError instead of silently reusing a mask built for some other range (e.g. a smaller
    prototype window) -- a stale mask filtering a larger real run would look like a legitimate
    result while actually screening only a fraction of it. Raises FileNotFoundError naming what
    builds each missing input -- no clean partitions in range: process.clean.clean_range; missing
    land_path: ingest.landmask.build_land_mask.

    Non-coastal candidate cells are written with is_coastal=false, not dropped -- see module
    docstring for why. detect.sts is expected to filter on is_coastal itself.
    """
    if out_path.exists() and not force:
        check_con = duckdb.connect()
        try:
            existing_window = check_con.execute(
                "SELECT DISTINCT window_start, window_end FROM read_parquet(?)", [str(out_path)]
            ).fetchall()
        finally:
            check_con.close()
        if existing_window and existing_window[0] != (start, end):
            raise ValueError(
                f"{out_path} was already built for {existing_window[0]}, not "
                f"({start.isoformat()}, {end.isoformat()}); pass force=True to rebuild for this "
                "window"
            )
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    partitions = existing_partitions(start, end, in_root)
    if not partitions:
        raise FileNotFoundError(
            f"No clean partitions found for {start.isoformat()}..{end.isoformat()} under "
            f"{in_root}; run process.clean.clean_range first"
        )
    if not land_path.exists():
        raise FileNotFoundError(
            f"No land mask at {land_path}; run ingest.landmask.build_land_mask first"
        )

    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        _build(
            con,
            partitions,
            land_path,
            cell_deg,
            stationary_max_sog_knots,
            min_stationary_hours,
            min_distinct_vessels,
            coastal_max_distance_m,
            mobile_types,
            max_dwell_gap_minutes,
        )

        (n_cells,) = con.execute("SELECT count(*) FROM anchorages").fetchone()
        (n_coastal,) = con.execute("SELECT count(*) FROM anchorages WHERE is_coastal").fetchone()
        logger.info(
            "Built anchorage mask: %d candidate cell(s), %d coastal (is_coastal=true), "
            "%d non-coastal (kept, not dropped -- see module docstring)",
            n_cells,
            n_coastal,
            n_cells - n_coastal,
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
            "FROM anchorages ORDER BY cell_lat, cell_lon) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


@dataclass(frozen=True)
class PortComparison:
    """Two one-way sanity numbers against a public port list -- see compare_to_ports for why they
    must be read very differently from each other, not as a single precision/recall pair."""

    n_ports: int
    n_ports_near_anchorage: int
    n_anchorages: int
    n_anchorages_near_port: int


def compare_to_ports(
    con: duckdb.DuckDBPyConnection,
    anchorages_path: Path,
    ports_path: Path,
    radius_m: float = 5000.0,
) -> PortComparison:
    """Sanity-check the derived mask against Natural Earth's ports layer.

    Validation only -- never a build-time dependency of build_anchorages, and never a filter.
    Reports two asymmetric numbers, not one score:

    * n_ports_near_anchorage -- how many NE ports have a derived coastal anchorage within
      radius_m. A NE port with none nearby is a red flag: the mask may be missing real traffic.
    * n_anchorages_near_port -- how many derived coastal anchorages have an NE port within
      radius_m. Expected to be LOW, and that is fine: NE's 10m ports layer holds only major world
      ports, not the small Danish harbours this mask is built to catch by design. Read a low
      second number as this mask's added coverage, not as imprecision.
    """
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    (n_ports,) = con.execute("SELECT count(*) FROM read_parquet(?)", [str(ports_path)]).fetchone()
    (n_anchorages,) = con.execute(
        "SELECT count(*) FROM read_parquet(?) WHERE is_coastal", [str(anchorages_path)]
    ).fetchone()
    # See _build's comment on why both points are (latitude, longitude), not the standard
    # (longitude, latitude) -- ST_Distance_Sphere's own argument order in this DuckDB build.
    (n_ports_near_anchorage,) = con.execute(
        "SELECT count(*) FROM read_parquet(?) p WHERE EXISTS ("
        "  SELECT 1 FROM read_parquet(?) a WHERE a.is_coastal "
        "  AND ST_Distance_Sphere(ST_Point(ST_Y(p.geom), ST_X(p.geom)), "
        "        ST_Point(a.center_latitude, a.center_longitude)) <= ?"
        ")",
        [str(ports_path), str(anchorages_path), radius_m],
    ).fetchone()
    (n_anchorages_near_port,) = con.execute(
        "SELECT count(*) FROM read_parquet(?) a WHERE a.is_coastal AND EXISTS ("
        "  SELECT 1 FROM read_parquet(?) p "
        "  WHERE ST_Distance_Sphere(ST_Point(ST_Y(p.geom), ST_X(p.geom)), "
        "        ST_Point(a.center_latitude, a.center_longitude)) <= ?"
        ")",
        [str(anchorages_path), str(ports_path), radius_m],
    ).fetchone()
    return PortComparison(
        n_ports=n_ports,
        n_ports_near_anchorage=n_ports_near_anchorage,
        n_anchorages=n_anchorages,
        n_anchorages_near_port=n_anchorages_near_port,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an empirical coastal-anchorage mask from clean AIS, so detect.sts can "
        "exclude vessels merely moored or anchored near each other."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument("--land-path", default=str(LAND_PATH), help="Path to the land mask table")
    parser.add_argument(
        "--out-path", default=str(ANCHORAGES_PATH), help="Output path for the anchorage mask"
    )
    parser.add_argument(
        "--cell-deg",
        type=float,
        default=ANCHORAGE_CELL_DEG,
        help=f"Grid cell size in degrees (default: {ANCHORAGE_CELL_DEG})",
    )
    parser.add_argument(
        "--min-distinct-vessels",
        type=int,
        default=MIN_DISTINCT_VESSELS,
        help=f"Minimum distinct MMSI to call a cell an anchorage (default: {MIN_DISTINCT_VESSELS})",
    )
    parser.add_argument(
        "--min-stationary-hours",
        type=float,
        default=MIN_STATIONARY_HOURS,
        help=f"Minimum per-day dwell to count (default: {MIN_STATIONARY_HOURS})",
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
    build_anchorages(
        start,
        end,
        in_root=Path(args.in_dir),
        land_path=Path(args.land_path),
        out_path=Path(args.out_path),
        cell_deg=args.cell_deg,
        min_distinct_vessels=args.min_distinct_vessels,
        min_stationary_hours=args.min_stationary_hours,
        force=args.force,
    )


if __name__ == "__main__":
    main()
