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
threshold in this module. Pairs with ``time_diff_seconds == 0`` are skipped by this check on
purpose -- two messages at the same instant are Check 4's job, not an infinite/undefined speed here.

**Check 2: positions on land.** Needs :mod:`ingest.landmask`'s pre-built land polygons. That
module's own docstring documents an empirically measured ~200m coastline-generalization error (a
real onshore point in Copenhagen sits outside the raw polygon). To avoid the opposite mistake here
-- flagging a position that is actually at sea because Natural Earth's simplified coastline bulges
out over real water -- the land polygons are eroded inward by :data:`COASTAL_EROSION_DEG` (~1.1km)
before containment is tested, so only positions solidly inland are flagged. This is a per-point
check over every clean message (not per-pair), so a vessel that spends real time at a berth near a
river mouth can produce many "on_land" rows for one stay -- expected, not deduplicated, because
this check does not know about voyages. **Not yet benchmarked**: the spatial join's performance over
a full multi-million-row 30-day window has not been measured. Flagged here explicitly as a thing to
check before a real run, not silently assumed fast; no bounding-box pre-filtering has been added,
since that would be optimizing a problem that has not actually been observed yet.

**Check 3: synthetic circles.** Some spoofing rigs replay a perfect geometric loop instead of a
real track. Candidate voyages are pre-filtered cheaply from ``voyages.parquet`` alone
(``point_count``/``duration_seconds``, see :data:`MIN_POINTS_FOR_CIRCLE` /
:data:`MIN_VOYAGE_DURATION_FOR_CIRCLE_HOURS``) before any raw points are touched -- most voyages are
short and never reach this stage. Surviving voyages' own points are re-queried from clean AIS by
``mmsi``/``timestamp BETWEEN start_time AND end_time`` (simpler than going through
``process.tracks.attach_voyage_ids`` for exactly one voyage's membership, which is already known
from its own start/end). A circle is fit in Python with a Kasa algebraic least-squares fit (see
:func:`_fit_circle`) over a local planar projection centred on the voyage's own start position --
adequate at voyage scale (a few to tens of km), not geodesy. The Kasa fit is known to be biased for
partial arcs or noisy data relative to a full nonlinear geometric fit; adequate for a flagging
heuristic, not precision circle-fitting. Flags require a low residual-to-radius ratio, a plausible
radius band, and a wide angular sweep (see the four ``CIRCLE_*``/``MIN_CIRCLE_*`` constants) all at
once. **Unlike the other three checks, this event's coordinates are the fitted circle's centre**,
converted back from planar to geographic, not any single observed AIS position.

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

# Check 2: positions on land. Unvalidated, sized to ingest.landmask's empirically observed
# coastline-generalization offset (~200m-1km) -- see module docstring.
COASTAL_EROSION_DEG = 0.01  # ~1.1km at these latitudes

# Check 3: synthetic circles.
MIN_POINTS_FOR_CIRCLE = 20
MIN_VOYAGE_DURATION_FOR_CIRCLE_HOURS = 1.0
CIRCLE_RESIDUAL_RATIO_MAX = 0.03
MIN_CIRCLE_RADIUS_M = 50.0
MAX_CIRCLE_RADIUS_M = 20_000.0
MIN_CIRCLE_ANGULAR_SWEEP_DEG = 180.0
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
    Pairs with time_diff_seconds == 0 are skipped -- that is Check 4's job, not this one's; skipping
    them here also avoids a division by zero.
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
        "         ST_Distance_Sphere("
        "           ST_Point(prev_longitude, prev_latitude), ST_Point(longitude, latitude)"
        "         ) AS distance_m "
        "  FROM ordered "
        "  WHERE prev_timestamp IS NOT NULL "
        "    AND date_diff('second', prev_timestamp, timestamp) > 0"
        ") "
        "SELECT mmsi, timestamp, latitude, longitude, time_diff_seconds, "
        "       (distance_m / time_diff_seconds) * (3600.0 / 1852.0) AS implied_speed_knots "
        "FROM paired "
        "WHERE (distance_m / time_diff_seconds) * (3600.0 / 1852.0) > ?",
        [MAX_PLAUSIBLE_SPEED_KNOTS],
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


def check_on_land(con: duckdb.DuckDBPyConnection, land_path: Path = LAND_PATH) -> list[SpoofingEvent]:
    """Flag every clean position solidly inside an eroded land polygon.

    Requires the `all_days` view (see :func:`_build_all_days`) and the spatial extension loaded.
    See module docstring for why the land polygons are eroded before containment is tested, and for
    the un-benchmarked performance caveat on this join.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _land_eroded AS "
        f"SELECT ST_Buffer(geom, -{COASTAL_EROSION_DEG}) AS geom FROM read_parquet('{land_path.as_posix()}')"
    )
    rows = con.execute(
        "SELECT a.mmsi, a.timestamp, a.latitude, a.longitude "
        "FROM all_days a, _land_eroded l "
        "WHERE ST_Contains(l.geom, ST_Point(a.longitude, a.latitude))"
    ).fetchall()

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


def _project_planar(
    lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float
) -> tuple[np.ndarray, np.ndarray]:
    """Local planar approximation of (lat, lon) around (lat0, lon0), in metres.

    Adequate at voyage scale (a few to tens of km); not a general-purpose projection.
    """
    x = (lon - lon0) * METERS_PER_DEG_LAT * np.cos(np.radians(lat0))
    y = (lat - lat0) * METERS_PER_DEG_LAT
    return x, y


def _fit_circle(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Kasa algebraic circle fit. Returns (center_x, center_y, radius) in the same planar units
    as x/y. Known to be biased for partial arcs / noisy data relative to a full geometric
    (nonlinear) fit -- adequate for a flagging heuristic, not precision geodesy.
    """
    A = np.column_stack([x, y, np.ones_like(x)])
    b = x**2 + y**2
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    a_coef, b_coef, c_coef = sol
    cx, cy = a_coef / 2.0, b_coef / 2.0
    r = float(np.sqrt(c_coef + cx**2 + cy**2))
    return float(cx), float(cy), r


def check_synthetic_circles(
    con: duckdb.DuckDBPyConnection,
    voyages_path: Path,
    start: date,
    end: date,
) -> list[SpoofingEvent]:
    """Flag voyages whose points fit a near-perfect circle over a wide angular sweep.

    Requires the `all_days` view (see :func:`_build_all_days`). Candidate voyages are pre-filtered
    cheaply from voyages_path alone (point_count/duration_seconds, and start/end falling in
    [start, end]) before any raw points are pulled -- see module docstring.
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

    events: list[SpoofingEvent] = []
    for mmsi, _voyage_id, voyage_start, voyage_end, lat0, lon0 in candidates:
        points = con.execute(
            "SELECT timestamp, latitude, longitude FROM all_days "
            "WHERE mmsi = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            [mmsi, voyage_start, voyage_end],
        ).fetchall()
        if len(points) < MIN_POINTS_FOR_CIRCLE:
            continue

        lats = np.array([p[1] for p in points], dtype=float)
        lons = np.array([p[2] for p in points], dtype=float)
        x, y = _project_planar(lats, lons, lat0, lon0)
        cx, cy, r = _fit_circle(x, y)
        if r <= 0:
            continue

        residuals = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) - r
        rms_over_r = float(np.sqrt(np.mean(residuals**2)) / r)
        angles = np.unwrap(np.arctan2(y - cy, x - cx))
        angular_sweep_deg = float(np.degrees(angles.max() - angles.min()))

        if not (
            rms_over_r <= CIRCLE_RESIDUAL_RATIO_MAX
            and MIN_CIRCLE_RADIUS_M <= r <= MAX_CIRCLE_RADIUS_M
            and angular_sweep_deg >= MIN_CIRCLE_ANGULAR_SWEEP_DEG
        ):
            continue

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
                    f"sweep={angular_sweep_deg:.0f}deg over {len(points)} points"
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
        "SELECT a.mmsi, a.timestamp, a.latitude, a.longitude, "
        "       ST_Distance_Sphere("
        "         ST_Point(a.longitude, a.latitude), ST_Point(b.longitude, b.latitude)"
        "       ) AS distance_m "
        "FROM sequenced a JOIN sequenced b "
        "  ON a.mmsi = b.mmsi AND a.timestamp = b.timestamp AND a._seq < b._seq "
        "WHERE ST_Distance_Sphere("
        "  ST_Point(a.longitude, a.latitude), ST_Point(b.longitude, b.latitude)"
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
        events.extend(check_impossible_speed(con))
        events.extend(check_on_land(con, land_path))
        events.extend(check_synthetic_circles(con, voyages_path, start, end))
        events.extend(check_simultaneous_positions(con))

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
