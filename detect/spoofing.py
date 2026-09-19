"""Detector 2: position spoofing -- impossible speeds, positions on land, synthetic circles,
simultaneous positions.

**Why this exists.** ``detect.gaps`` (Detector 1) scores *silences* -- a vessel that stops
reporting. This module scores the opposite failure mode: a vessel that keeps reporting, but whose
reported positions are themselves not physically credible. Four independent checks, each a pure
rule over clean AIS, run against the same window and are merged into one flat table, one row per
detected anomaly (not per vessel) -- matching ``detect.gaps``'s own one-row-per-event precedent,
and set up for Phase 2's features/ aggregation step (not yet built) to roll these up per vessel
later.

**Decoupled from voyage segmentation, except where it can't be.** Three of the four checks
(impossible speed, on land, simultaneous position) read clean AIS directly via this module's own
``CLEAN_ROOT``, with no dependency on ``process.tracks``'s voyage boundaries -- the same reasoning
``detect.coverage``'s module docstring gives for staying decoupled: a voyage boundary is itself
drawn from a time-gap rule, and tangling that into a *different* question (is this position
credible?) would make the two rules interfere for no benefit. Only the synthetic-circle check needs
voyages, because "did this vessel loop in a circle" is a property of a bounded trip, not of a pair
of consecutive points.

**Check 1: impossible speed.** Per mmsi, consecutive clean messages (ordered by timestamp, plain
``lag()``, exactly like ``detect.coverage``'s own pairing) imply a speed via great-circle distance
(``ST_Distance_Sphere``) over the elapsed time. :data:`MAX_PLAUSIBLE_SPEED_KNOTS` (50kn) is a single
global ceiling, not per-ship-type -- there is no calibration data yet to justify a faster threshold
for, say, a fast ferry or a naval vessel, so a genuinely fast vessel with real GPS/timestamp jitter
over a very short interval could trigger a false positive here. An unvalidated default, like every
threshold in this module. Pairs closer together than :data:`MIN_SPEED_CHECK_INTERVAL_SECONDS` (60s,
which includes ``time_diff_seconds == 0`` -- two messages at the same instant are Check 4's job, not
an infinite/undefined speed here) are skipped **not just to avoid noise, but because without this
gate the check is useless in practice**: measured against one real day (2024-06-05), 76,970 of
10,391,420 consecutive pairs (0.74%) exceeded 50kn, and 98.2% of those had a time gap under 10
seconds -- ordinary GPS/positional jitter of a few dozen metres, amplified into an "impossible"
speed by dividing by a near-zero interval. At 60s+ that same day had only 167 flagged pairs, two
orders of magnitude fewer and a plausible rate for a real anomaly signal. This was found the hard
way, from a real run that flagged 31% of the whole 30-day dataset before the gate was added.

**Check 2: positions on land.** Needs :mod:`ingest.landmask`'s pre-built land polygons. That
module's own docstring documents an empirically measured ~200m coastline-generalization error (a
real onshore point in Copenhagen sits outside the raw polygon). To avoid the opposite mistake here
-- flagging a position that is actually at sea because Natural Earth's simplified coastline bulges
out over real water -- the land polygons are eroded inward by :data:`COASTAL_EROSION_DEG` (~1.1km)
before containment is tested, so only positions solidly inland are flagged. This is a per-point
check over every clean message (not per-pair), so a vessel that spends real time at a berth near a
river mouth can produce many "on_land" rows for one stay -- expected, not deduplicated, because
this check does not know about voyages. **Benchmarked the hard way**: an earlier version tested
every point against the raw land polygons directly, including Natural Earth's single
whole-world-coastline multipolygon -- a real 30-day run did not finish in over 80 minutes before
being killed. The fix, in :func:`check_on_land`, crops every land polygon to the data's own bounding
box (plus a small margin) *before* eroding it, so ``ST_Contains`` never tests a point against a
shape sized to the entire globe. This changes nothing about which points get flagged -- the crop
happens well outside any area a Danish AIS point could be in -- only how fast it runs.

**Check 3: synthetic circles.** Some spoofing rigs replay a perfect geometric loop instead of a
real track. Candidate voyages are pre-filtered cheaply from ``voyages.parquet`` alone
(``point_count``/``duration_seconds``, see :data:`MIN_POINTS_FOR_CIRCLE` /
:data:`MIN_VOYAGE_DURATION_FOR_CIRCLE_HOURS``) -- but on real data most voyages pass this filter
(68,431 of 95,692 in the pilot 30-day window; AIS reporting is frequent enough that even a
short-ish voyage accumulates thousands of points), so **this alone does not keep the candidate set
small**. The original version of this check then queried each candidate voyage's own points one at
a time (``mmsi``/``timestamp BETWEEN start_time AND end_time``) and fit a circle in Python with
numpy -- correct, but it never finished a real 30-day run in over 50 minutes (68,431 queries
totalling 345 million points). :func:`check_synthetic_circles` now does the fit itself as bulk,
grouped SQL aggregation instead: see its own docstring for the three-pass design (moment sums ->
Kasa fit + radius-band gate in Python on ~9 numbers per voyage, not the points -> a second pass,
scoped to survivors only, for the exact residual and angular-spread gates). A circle is still fit
with the Kasa algebraic least-squares method (see :func:`_solve_circle`) over a local planar
projection centred on the voyage's own start position -- adequate at voyage scale (a few to tens of
km), not geodesy, and known to be biased for partial arcs or noisy data relative to a full
nonlinear geometric fit; adequate for a flagging heuristic, not precision circle-fitting. Flags
require a low residual-to-radius ratio, a plausible radius band, and a wide angular spread (see the
``CIRCLE_*``/``MIN_CIRCLE_*``/``MAX_CIRCLE_*`` constants) all at once. **Unlike the other three
checks, this event's coordinates are the fitted circle's centre**, converted back from planar to
geographic, not any single observed AIS position.

**Check 4: simultaneous positions.** Two messages from the same mmsi at the exact same timestamp,
far enough apart to be physically impossible for one transponder (see
:data:`SIMULTANEOUS_DISTANCE_THRESHOLD_M`). A self-join on ``(mmsi, timestamp)`` needs a tie-break
to avoid self-pairing or double-counting each pair, using the same ``row_number() OVER ()`` + ``_seq``
idiom ``process.clean`` uses for exactly this "stable row identity over a Parquet scan" problem. The
choice of which of the two points becomes the event's reported position is arbitrary -- the distance
between them is the meaningful evidence, not which one was kept.

**Confidence is a rule-based heuristic, not a calibrated probability**, for all four checks --
unvalidated, exactly like ``detect.gaps``'s own probability table. Every threshold in this module
(``MAX_PLAUSIBLE_SPEED_KNOTS``, ``COASTAL_EROSION_DEG``, the ``CIRCLE_*``/``MIN_CIRCLE_*`` constants,
``SIMULTANEOUS_DISTANCE_THRESHOLD_M``) is a considered but unvalidated default awaiting empirical
calibration, not a tuned constant.

**A fifth real-data bug, found 2026-09-18 while building P2-4's anchorage mask, not by review**:
``ST_Distance_Sphere`` in this DuckDB build takes each point as ``ST_Point(latitude, longitude)``,
the reverse of the standard ``ST_Point(longitude, latitude)`` order every other spatial function
here uses (``ST_Contains``, ``ST_DWithin``, ``ST_ClosestPoint``, and ``ST_Point`` construction
itself, which round-trips ``ST_Point(x, y)`` to WKT ``POINT (x y)`` exactly as given). Verified
with a real-world distance: Copenhagen to Malmo (true ~28.4km) came back as 49.0km with the
standard argument order, 28.45km once swapped -- and a pure east-west offset (where the error is
purely a missing/inverted ``cos(latitude)`` scaling) isolates it unambiguously. **Checks 1 and 4
(``check_impossible_speed``, ``check_simultaneous_positions``) both built their points with the
standard, wrong-for-this-function order**, so every distance, and every implied speed, both checks
ever computed was systematically inflated (worst for east-west separations, roughly unchanged for
north-south ones) -- including the real 30-day counts recorded when P2-3 was first closed. Checks
2 and 3 (``check_on_land``, ``check_synthetic_circles``) do not call ``ST_Distance_Sphere`` (the
former uses ``ST_Contains``, the latter a local planar projection with its own explicit trig), so
neither is affected. Fixed by swapping the argument order at both call sites; see
``docs/DECISIONS.md`` for the fix and the corrected real-run numbers.

One shared DuckDB connection is opened once and reused across all four checks, mirroring
``detect.gaps``'s pattern of never reconnecting mid-run.
"""

from __future__ import annotations

import argparse
import logging
import math
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np

from ingest.landmask import LAND_PATH
from process.partitions import existing_partitions
from process.tracks import VOYAGES_PATH

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
DETECT_ROOT = Path("data/detect")
SPOOFING_PATH = DETECT_ROOT / "spoofing.parquet"

# Check 1: impossible speed. Unvalidated global ceiling -- see module docstring.
MAX_PLAUSIBLE_SPEED_KNOTS = 50.0
# Pairs closer together in time than this are skipped -- see module docstring for the real-data
# finding that motivated it.
MIN_SPEED_CHECK_INTERVAL_SECONDS = 60.0

# Check 2: positions on land. Unvalidated, sized to ingest.landmask's empirically observed
# coastline-generalization offset (~200m-1km) -- see module docstring.
COASTAL_EROSION_DEG = 0.01  # ~1.1km at these latitudes

# Land polygons are cropped to the data's own bounding box (plus this margin) before erosion and
# containment testing -- see module docstring for why this stopped being optional.
BBOX_MARGIN_DEG = 0.5
# The bounding box is computed from these tail quantiles of the data's own lat/lon, not raw
# min/max -- see module docstring for the real outlier positions (up to 89 degrees latitude) that
# made a min/max bbox nearly useless.
BBOX_OUTLIER_QUANTILE = 0.001
# Tolerance for simplifying the cropped, eroded land shape before the containment join -- an order
# of magnitude finer than COASTAL_EROSION_DEG, so it does not meaningfully change which points are
# flagged, only how many vertices ST_Contains has to test against.
LAND_SIMPLIFY_TOLERANCE_DEG = 0.001

# Check 3: synthetic circles.
MIN_POINTS_FOR_CIRCLE = 20
MIN_VOYAGE_DURATION_FOR_CIRCLE_HOURS = 1.0
CIRCLE_RESIDUAL_RATIO_MAX = 0.03
MIN_CIRCLE_RADIUS_M = 50.0
MAX_CIRCLE_RADIUS_M = 20_000.0
# Mean resultant length (standard circular-statistics dispersion measure, range [0, 1]): 0 means
# points are spread uniformly around a full 360 degree circle, 1 means they are all bunched in one
# direction. For points spread uniformly over an arc of just 180 degrees the value is 2/pi =~ 0.637,
# so a value at or below this threshold means "at least roughly half the circle was covered" --
# see module docstring for why this replaced a per-point angular-sweep calculation.
MAX_CIRCLE_MEAN_RESULTANT_LENGTH = 0.6
METERS_PER_DEG_LAT = 111_320.0

# Check 4: simultaneous positions.
SIMULTANEOUS_DISTANCE_THRESHOLD_M = 500.0


@dataclass(frozen=True)
class SpoofingEvent:
    """One detected spoofing anomaly. One row per event, not per vessel -- see module docstring."""

    mmsi: int
    kind: str  # "impossible_speed" | "on_land" | "synthetic_circle" | "simultaneous_position"
    event_time: datetime
    latitude: float
    longitude: float
    confidence: float  # 0-1, a rule-based heuristic, NOT a calibrated probability -- unvalidated.
    evidence_value: float  # the check's key metric -- see per-check docstring notes.
    detail: str  # short human-readable note.


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


def _build_all_days(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Create/replace the `all_days` view: mmsi/timestamp/latitude/longitude for every partition."""
    per_day = [
        "SELECT mmsi, timestamp, latitude, longitude "
        f"FROM read_parquet('{path.as_posix()}')"
        for _day, path in partitions
    ]
    con.execute(f"CREATE OR REPLACE VIEW all_days AS {' UNION ALL '.join(per_day)}")


def check_impossible_speed(con: duckdb.DuckDBPyConnection) -> list[SpoofingEvent]:
    """Flag consecutive same-mmsi pairs whose implied speed exceeds MAX_PLAUSIBLE_SPEED_KNOTS.

    Requires the `all_days` view (see :func:`_build_all_days`) and the spatial extension loaded.
    Pairs with ``time_diff_seconds < MIN_SPEED_CHECK_INTERVAL_SECONDS`` are skipped (this includes
    ``== 0``, which is Check 4's job, not this one's) -- see module docstring and
    :data:`MIN_SPEED_CHECK_INTERVAL_SECONDS` for why: ordinary GPS/positional noise, amplified by
    dividing by a very short interval, otherwise dominates the results.
    """
    rows = con.execute(
        "WITH ordered AS ("
        "  SELECT mmsi, timestamp, latitude, longitude, "
        "         lag(timestamp) OVER w AS prev_timestamp, "
        "         lag(latitude) OVER w AS prev_latitude, "
        "         lag(longitude) OVER w AS prev_longitude "
        "  FROM all_days "
        "  WINDOW w AS (PARTITION BY mmsi ORDER BY timestamp)"
        "), "
        "paired AS ("
        "  SELECT mmsi, timestamp, latitude, longitude, "
        "         date_diff('second', prev_timestamp, timestamp) AS time_diff_seconds, "
        # ST_Distance_Sphere in this DuckDB build expects each point as
        # ST_Point(latitude, longitude) -- the reverse of the standard (longitude, latitude)
        # order ST_Point uses everywhere else in this module (ST_Contains, land geometry). Found
        # 2026-09-18: a pure east-west offset of known distance came back inflated by 1/cos(lat)
        # (~1.7x at these latitudes) with the standard order, matched ground truth once swapped.
        # See docs/DECISIONS.md.
        "         ST_Distance_Sphere("
        "           ST_Point(prev_latitude, prev_longitude), ST_Point(latitude, longitude)"
        "         ) AS distance_m "
        "  FROM ordered "
        "  WHERE prev_timestamp IS NOT NULL "
        "    AND date_diff('second', prev_timestamp, timestamp) >= ?"
        ") "
        "SELECT mmsi, timestamp, latitude, longitude, time_diff_seconds, "
        "       (distance_m / time_diff_seconds) * (3600.0 / 1852.0) AS implied_speed_knots "
        "FROM paired "
        "WHERE (distance_m / time_diff_seconds) * (3600.0 / 1852.0) > ?",
        [MIN_SPEED_CHECK_INTERVAL_SECONDS, MAX_PLAUSIBLE_SPEED_KNOTS],
    ).fetchall()

    events = []
    for mmsi, ts, lat, lon, time_diff_seconds, implied_speed_knots in rows:
        confidence = min(
            1.0, (implied_speed_knots - MAX_PLAUSIBLE_SPEED_KNOTS) / MAX_PLAUSIBLE_SPEED_KNOTS
        )
        events.append(
            SpoofingEvent(
                mmsi=mmsi,
                kind="impossible_speed",
                event_time=ts,
                latitude=lat,
                longitude=lon,
                confidence=confidence,
                evidence_value=implied_speed_knots,
                detail=f"implied speed {implied_speed_knots:.1f}kn over {time_diff_seconds:.0f}s",
            )
        )
    return events


def check_on_land(
    con: duckdb.DuckDBPyConnection,
    partitions: list[tuple[date, Path]],
    land_path: Path = LAND_PATH,
) -> list[SpoofingEvent]:
    """Flag every clean position solidly inside an eroded land polygon.

    Requires the `all_days` view (see :func:`_build_all_days`) and the spatial extension loaded.
    See module docstring for why the land polygons are eroded before containment is tested, and for
    why they are cropped to the data's own bounding box first -- one of Natural Earth's 11 features
    is a single multipolygon of the ENTIRE world's coastline, and testing every clean AIS point
    against its full vertex set (most of it thousands of km from Denmark) is what made this check
    impractically slow (confirmed: a real 30-day run did not finish in over 80 minutes before being
    killed). Cropping first, then eroding the much smaller cropped shape, keeps every subsequent
    ST_Contains call cheap without changing which points get flagged inside the data's own extent.

    **The bounding box itself uses tail quantiles, not raw min/max.** A real clean day (2024-06-05)
    contains a handful of wildly corrupted positions -- 963 of 10,429,200 points (0.0092%) outside a
    generous Danish-waters box, with latitude/longitude extremes of (89.16, -84.30) that
    `process.clean`'s existing rules (valid range, not null island) do not catch. A raw min/max bbox
    over that data covers most of the Northern Hemisphere -- 82,680 land-polygon vertices after
    cropping, no faster than not cropping at all. :data:`BBOX_OUTLIER_QUANTILE` (0.1% each tail)
    ignores that handful of outliers while still being data-driven, not hardcoded to Denmark (a
    future working region, e.g. Phase 6's Gibraltar/Ceuta, gets its own bbox automatically). The
    tiny fraction of genuine points outside the resulting box simply never get an on-land verdict --
    an acceptable gap given they are already too corrupted to trust the coordinate at all.

    **A fourth real-data bug, found the same way as the first three (see module docstring): a real
    30-day run of this exact single-day-validated approach still did not finish in over 3.5 hours.**
    The single-day bbox (measured at 68s/day) understated the real 30-day bbox: the DMA's coastal
    receivers see a wider area over a month than on any one day, so the tail-quantile crop over the
    full window pulls in visibly more Baltic/Scandinavian coastline than a single day's crop does.
    Worse, the whole 30-day join was a single monolithic spatial join over the 312M-row union of all
    partitions, so a modest increase in land complexity multiplied against 30x the points with no
    per-day checkpoint. Two independent fixes, both confirmed by direct timing before being trusted
    at scale (not by review alone): (1) the land crop now runs ``ST_Dump`` on the raw multipolygons
    *before* cropping, exploding each landmass/island into its own row with a small, tight bounding
    box, instead of eroding one or two sprawling multi-part geometries -- measured ~2x faster per day
    even alone (68s -> 32s on 2024-06-05); (2) the point-in-polygon join now runs per day partition
    in a Python loop, not once over the full window, so total cost is the well-measured per-day cost
    times the day count (~45-50s/day observed, ~25 min projected for 30 days) instead of an
    unpredictable one-shot join. **Decomposing the land geometry before cropping changes event counts
    by a small amount** (286,440 vs. the pre-fix 277,788 on 2024-06-05, +3.1%) from processing-order
    differences in how adjoining Natural-Earth pieces are cropped and eroded -- within the noise of
    an already-unvalidated heuristic (see below), not chased to exact parity.
    """
    lat_min, lat_max, lon_min, lon_max = con.execute(
        "SELECT quantile_cont(latitude, ?), quantile_cont(latitude, ?), "
        "quantile_cont(longitude, ?), quantile_cont(longitude, ?) FROM all_days",
        [
            BBOX_OUTLIER_QUANTILE,
            1 - BBOX_OUTLIER_QUANTILE,
            BBOX_OUTLIER_QUANTILE,
            1 - BBOX_OUTLIER_QUANTILE,
        ],
    ).fetchone()
    m = BBOX_MARGIN_DEG
    bbox_wkt = (
        f"POLYGON(({lon_min - m} {lat_min - m}, {lon_max + m} {lat_min - m}, "
        f"{lon_max + m} {lat_max + m}, {lon_min - m} {lat_max + m}, {lon_min - m} {lat_min - m}))"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _land_pieces AS "
        "SELECT ST_SimplifyPreserveTopology(ST_Buffer(piece, ?), ?) AS geom FROM ("
        "  SELECT ST_Intersection((UNNEST(ST_Dump(geom))).geom, ST_GeomFromText(?)) AS piece "
        "  FROM read_parquet(?)"
        ") WHERE NOT ST_IsEmpty(piece)",
        [-COASTAL_EROSION_DEG, LAND_SIMPLIFY_TOLERANCE_DEG, bbox_wkt, str(land_path)],
    )
    rows: list[tuple] = []
    for _day, path in partitions:
        rows.extend(
            con.execute(
                "SELECT DISTINCT a.mmsi, a.timestamp, a.latitude, a.longitude "
                f"FROM read_parquet('{path.as_posix()}') a, _land_pieces l "
                "WHERE ST_Contains(l.geom, ST_Point(a.longitude, a.latitude))"
            ).fetchall()
        )

    return [
        SpoofingEvent(
            mmsi=mmsi,
            kind="on_land",
            event_time=ts,
            latitude=lat,
            longitude=lon,
            confidence=0.9,
            evidence_value=1.0,
            detail="position inside eroded land polygon",
        )
        for mmsi, ts, lat, lon in rows
    ]


def _solve_circle(
    n: int, sx: float, sy: float, sxx: float, syy: float, sxy: float, sux: float, suy: float
) -> tuple[float, float, float] | None:
    """Kasa algebraic circle fit from pre-aggregated moment sums (planar x/y in metres).

    ``sux``/``suy`` are ``sum(x*(x^2+y^2))``/``sum(y*(x^2+y^2))``; ``sxx+syy`` is ``sum(x^2+y^2)``.
    Returns ``(center_x, center_y, radius)`` or ``None`` if the normal-equation matrix is singular
    (perfectly collinear points, or fewer than 3 effectively independent points). Known to be
    biased for partial arcs / noisy data relative to a full geometric (nonlinear) fit -- adequate
    for a flagging heuristic, not precision geodesy.
    """
    matrix = np.array([[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]])
    rhs = np.array([sux, suy, sxx + syy])
    try:
        a_coef, b_coef, c_coef = np.linalg.solve(matrix, rhs)
    except np.linalg.LinAlgError:
        return None
    cx, cy = a_coef / 2.0, b_coef / 2.0
    r_sq = c_coef + cx**2 + cy**2
    if r_sq <= 0:
        return None
    return float(cx), float(cy), float(np.sqrt(r_sq))


def check_synthetic_circles(
    con: duckdb.DuckDBPyConnection,
    voyages_path: Path,
    start: date,
    end: date,
) -> list[SpoofingEvent]:
    """Flag voyages whose points fit a near-perfect circle over a wide angular spread.

    Requires the `all_days` view (see :func:`_build_all_days`). Three bulk, grouped SQL passes,
    never one query per voyage -- see module docstring for why an earlier per-voyage-Python-loop
    version of this check was not viable at real scale (68,431 of 95,692 real voyages passed the
    point_count/duration pre-filter alone, comprising 345 million points in total; fetching each
    voyage's points into Python one at a time never finished in over 50 minutes before being
    killed):

    1. Candidate voyages, pre-filtered cheaply from `voyages_path` alone (point_count/
       duration_seconds, start/end within [start, end]) -- unchanged from before, still cheap
       because it never touches raw points.
    2. ONE grouped join between candidates and `all_days` (by mmsi + timestamp BETWEEN start_time
       AND end_time) computing the Kasa fit's moment sums per voyage_id -- sum(x), sum(y),
       sum(x^2), sum(y^2), sum(x*y), sum(x*(x^2+y^2)), sum(y*(x^2+y^2)) -- entirely as SQL
       aggregates. Only these ~9 numbers per voyage (not the underlying points) are pulled into
       Python, where :func:`_solve_circle` solves the 3x3 normal-equation system per voyage
       (cheap, pure linear algebra, no further DB round trips) and the radius band gate
       (:data:`MIN_CIRCLE_RADIUS_M`/:data:`MAX_CIRCLE_RADIUS_M`) is applied immediately -- most
       candidates fail this gate (their fitted "circle" is nothing like the plausible size band),
       so only a small survivor set proceeds to step 3.
    3. A SECOND grouped join, scoped to just the survivors, computing the exact geometric
       residual RMS and the angular spread in one more aggregate pass -- see below for why
       spread is measured as a mean resultant length, not the sequential-unwrap sweep this
       check used before the rewrite.

    **Why mean resultant length, not angular sweep.** The original version unwrapped each point's
    angle around the fitted centre (`numpy.unwrap`) and took `max - min` -- correct, but it needs
    the points in a specific per-voyage array, which is exactly the pattern this rewrite eliminates.
    Mean resultant length (`R = sqrt((sum cos theta)^2 + (sum sin theta)^2) / n`, standard
    circular-statistics dispersion measure) needs only two more SQL sums (`sum(cos(theta))`,
    `sum(sin(theta))`) alongside the residual sum, no ordering or unwrapping required. `R = 0` for
    points spread uniformly over a full 360 degree circle, `R = 2/pi =~ 0.637` for a uniform
    180 degree half-circle -- :data:`MAX_CIRCLE_MEAN_RESULTANT_LENGTH` (0.6) is pinned close to
    that equivalence, not an independent guess. It also tolerates a vessel that briefly reverses
    within the loop better than a naive unwrap would.
    """
    candidates = con.execute(
        "SELECT mmsi, voyage_id, start_time, end_time, start_latitude, start_longitude "
        "FROM read_parquet(?) "
        "WHERE point_count >= ? AND duration_seconds >= ? "
        "AND CAST(start_time AS DATE) >= ? AND CAST(end_time AS DATE) <= ?",
        [
            str(voyages_path),
            MIN_POINTS_FOR_CIRCLE,
            MIN_VOYAGE_DURATION_FOR_CIRCLE_HOURS * 3600,
            start,
            end,
        ],
    ).fetchall()
    if not candidates:
        return []

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _circle_candidates "
        "(mmsi BIGINT, voyage_id VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP, "
        "lat0 DOUBLE, lon0 DOUBLE)"
    )
    con.executemany("INSERT INTO _circle_candidates VALUES (?, ?, ?, ?, ?, ?)", candidates)

    moment_rows = con.execute(
        "WITH pts AS ("
        "  SELECT c.voyage_id, c.mmsi, c.lat0, c.lon0, "
        f"         (a.longitude - c.lon0) * {METERS_PER_DEG_LAT} * cos(radians(c.lat0)) AS x, "
        f"         (a.latitude - c.lat0) * {METERS_PER_DEG_LAT} AS y "
        "  FROM _circle_candidates c "
        "  JOIN all_days a ON a.mmsi = c.mmsi AND a.timestamp BETWEEN c.start_time AND c.end_time"
        ") "
        "SELECT voyage_id, any_value(mmsi), any_value(lat0), any_value(lon0), count(*), "
        "       sum(x), sum(y), sum(x*x), sum(y*y), sum(x*y), "
        "       sum((x*x + y*y) * x), sum((x*x + y*y) * y) "
        "FROM pts GROUP BY voyage_id"
    ).fetchall()

    survivors = []  # (voyage_id, mmsi, lat0, lon0, cx, cy, r)
    for voyage_id, mmsi, lat0, lon0, n, sx, sy, sxx, syy, sxy, sux, suy in moment_rows:
        if n < MIN_POINTS_FOR_CIRCLE:
            continue
        fit = _solve_circle(n, sx, sy, sxx, syy, sxy, sux, suy)
        if fit is None:
            continue
        cx, cy, r = fit
        if MIN_CIRCLE_RADIUS_M <= r <= MAX_CIRCLE_RADIUS_M:
            survivors.append((voyage_id, mmsi, lat0, lon0, cx, cy, r))

    if not survivors:
        return []

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _circle_survivors "
        "(voyage_id VARCHAR, mmsi BIGINT, lat0 DOUBLE, lon0 DOUBLE, "
        "cx DOUBLE, cy DOUBLE, r DOUBLE)"
    )
    con.executemany("INSERT INTO _circle_survivors VALUES (?, ?, ?, ?, ?, ?, ?)", survivors)

    fit_rows = con.execute(
        "WITH pts AS ("
        "  SELECT s.voyage_id, s.r, s.cx, s.cy, "
        f"         (a.longitude - s.lon0) * {METERS_PER_DEG_LAT} * cos(radians(s.lat0)) AS x, "
        f"         (a.latitude - s.lat0) * {METERS_PER_DEG_LAT} AS y "
        "  FROM _circle_survivors s "
        "  JOIN _circle_candidates c ON c.voyage_id = s.voyage_id "
        "  JOIN all_days a ON a.mmsi = c.mmsi AND a.timestamp BETWEEN c.start_time AND c.end_time"
        ") "
        "SELECT voyage_id, any_value(r), count(*), "
        "       sqrt(avg(pow(sqrt(pow(x - cx, 2) + pow(y - cy, 2)) - r, 2))) AS rms, "
        "       sqrt(pow(sum(cos(atan2(y - cy, x - cx))), 2) "
        "            + pow(sum(sin(atan2(y - cy, x - cx))), 2)) / count(*) AS mean_resultant_length "
        "FROM pts GROUP BY voyage_id"
    ).fetchall()

    survivor_by_id = {v[0]: v for v in survivors}
    start_by_id = {voyage_id: start_time for _mmsi, voyage_id, start_time, *_ in candidates}
    events: list[SpoofingEvent] = []
    for voyage_id, r, n_points, rms, mean_resultant_length in fit_rows:
        rms_over_r = float(rms) / r
        if not (
            rms_over_r <= CIRCLE_RESIDUAL_RATIO_MAX
            and mean_resultant_length <= MAX_CIRCLE_MEAN_RESULTANT_LENGTH
        ):
            continue

        _voyage_id, mmsi, lat0, lon0, cx, cy, _r = survivor_by_id[voyage_id]
        voyage_start = start_by_id[voyage_id]

        # Event coordinates are the fitted circle's centre, not any observed position -- see
        # module docstring.
        center_lat = lat0 + cy / METERS_PER_DEG_LAT
        center_lon = lon0 + cx / (METERS_PER_DEG_LAT * math.cos(math.radians(lat0)))
        confidence = max(0.0, 1.0 - rms_over_r / CIRCLE_RESIDUAL_RATIO_MAX)
        events.append(
            SpoofingEvent(
                mmsi=mmsi,
                kind="synthetic_circle",
                event_time=voyage_start,
                latitude=center_lat,
                longitude=center_lon,
                confidence=confidence,
                evidence_value=rms_over_r,
                detail=(
                    f"circle fit r={r:.0f}m, residual/r={rms_over_r:.4f}, "
                    f"mean resultant length={mean_resultant_length:.3f} over {n_points} points"
                ),
            )
        )
    return events


def check_simultaneous_positions(con: duckdb.DuckDBPyConnection) -> list[SpoofingEvent]:
    """Flag same-mmsi, same-timestamp pairs more than SIMULTANEOUS_DISTANCE_THRESHOLD_M apart.

    Requires the `all_days` view (see :func:`_build_all_days`) and the spatial extension loaded.
    The `_seq` tie-break mirrors process.clean's own idiom for a stable row identity over a
    Parquet scan; it avoids both self-pairing and double-counting each pair. Point A's position
    is reported arbitrarily -- see module docstring.
    """
    rows = con.execute(
        "WITH sequenced AS ("
        "  SELECT *, row_number() OVER () AS _seq FROM all_days"
        ") "
        # Both points (latitude, longitude) order -- see check_impossible_speed's comment on
        # ST_Distance_Sphere's reversed argument order in this DuckDB build.
        "SELECT a.mmsi, a.timestamp, a.latitude, a.longitude, "
        "       ST_Distance_Sphere("
        "         ST_Point(a.latitude, a.longitude), ST_Point(b.latitude, b.longitude)"
        "       ) AS distance_m "
        "FROM sequenced a JOIN sequenced b "
        "  ON a.mmsi = b.mmsi AND a.timestamp = b.timestamp AND a._seq < b._seq "
        "WHERE ST_Distance_Sphere("
        "  ST_Point(a.latitude, a.longitude), ST_Point(b.latitude, b.longitude)"
        ") > ?",
        [SIMULTANEOUS_DISTANCE_THRESHOLD_M],
    ).fetchall()

    events = []
    for mmsi, ts, lat, lon, distance_m in rows:
        confidence = min(1.0, distance_m / 5000.0)
        events.append(
            SpoofingEvent(
                mmsi=mmsi,
                kind="simultaneous_position",
                event_time=ts,
                latitude=lat,
                longitude=lon,
                confidence=confidence,
                evidence_value=distance_m,
                detail=f"{distance_m:.0f}m apart at the same timestamp",
            )
        )
    return events


def build_spoofing_events(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    voyages_path: Path = VOYAGES_PATH,
    land_path: Path = LAND_PATH,
    out_path: Path = SPOOFING_PATH,
    force: bool = False,
) -> Path:
    """Run all four spoofing checks over [start, end] (inclusive on both ends) and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError naming what builds each missing input -- no clean
    partitions in range: process.clean.clean_range; missing voyages_path:
    process.tracks.reconstruct_range; missing land_path: ingest.landmask.build_land_mask -- all
    three are required unconditionally, matching detect.gaps's own pattern of requiring every
    declared input even though only some checks use each one.

    Opens exactly one DuckDB connection and reuses it across all four checks, mirroring
    detect.gaps's pattern.
    """
    if out_path.exists() and not force:
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
    if not voyages_path.exists():
        raise FileNotFoundError(
            f"No voyages table at {voyages_path}; run process.tracks.reconstruct_range first"
        )
    if not land_path.exists():
        raise FileNotFoundError(
            f"No land mask at {land_path}; run ingest.landmask.build_land_mask first"
        )

    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        _build_all_days(con, partitions)

        events: list[SpoofingEvent] = []
        for name, run_check in (
            ("impossible_speed", lambda: check_impossible_speed(con)),
            ("on_land", lambda: check_on_land(con, partitions, land_path)),
            ("synthetic_circle", lambda: check_synthetic_circles(con, voyages_path, start, end)),
            ("simultaneous_position", lambda: check_simultaneous_positions(con)),
        ):
            check_start = datetime.now(timezone.utc)
            found = run_check()
            elapsed = (datetime.now(timezone.utc) - check_start).total_seconds()
            logger.info("Check %s: %d event(s) in %.1fs", name, len(found), elapsed)
            events.extend(found)

        counts: dict[str, int] = {}
        for event in events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        logger.info(
            "Detected %d spoofing event(s) in %s..%s: %s",
            len(events),
            start.isoformat(),
            end.isoformat(),
            counts,
        )

        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _spoofing_events ("
            "mmsi BIGINT, kind VARCHAR, event_time TIMESTAMP, latitude DOUBLE, longitude DOUBLE, "
            "confidence DOUBLE, evidence_value DOUBLE, detail VARCHAR)"
        )
        if events:
            con.executemany(
                "INSERT INTO _spoofing_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event.mmsi,
                        event.kind,
                        event.event_time,
                        event.latitude,
                        event.longitude,
                        event.confidence,
                        event.evidence_value,
                        event.detail,
                    )
                    for event in events
                ],
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{git_sha}' AS git_sha "
            "FROM _spoofing_events ORDER BY mmsi, event_time, kind) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect position-spoofing anomalies (impossible speeds, on-land positions, "
        "synthetic circles, simultaneous positions) from clean AIS."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--voyages-path", default=str(VOYAGES_PATH), help="Path to the voyages table"
    )
    parser.add_argument(
        "--land-path", default=str(LAND_PATH), help="Path to the land mask table"
    )
    parser.add_argument(
        "--out-path", default=str(SPOOFING_PATH), help="Output path for the spoofing events"
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
    build_spoofing_events(
        start,
        end,
        in_root=Path(args.in_dir),
        voyages_path=Path(args.voyages_path),
        land_path=Path(args.land_path),
        out_path=Path(args.out_path),
        force=args.force,
    )


if __name__ == "__main__":
    main()
