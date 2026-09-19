"""Detector 3: ship-to-ship transfers, using the GFW definition.

**Why this exists.** GFW's own encounter definition (``docs/DATA_SOURCES.md``) is: two vessels
within 500m for at least 2 hours at a median speed under 2 knots, at least 10km from a coastal
anchorage. A cheap proxy query over the real 30-day window found 435,715 candidate slow-and-close
vessel pairs -- overwhelmingly false positives. The samples proved it: the top "candidates" were
pairs co-located for 639-647 of the window's 720 hours, i.e. vessels moored at adjacent berths for
nearly the whole month, not transfers. So the detector's real job is not finding co-located
vessels (trivial, and useless on its own) -- it is discriminating a genuine transfer from that
dominant false-positive population. Two things do nearly all of that work:

1. **Episode segmentation** (:func:`segment_episodes`). The proxy above measured a pair's span
   from first to last co-location, so a pair berthing together daily reported one 647-hour
   "encounter". Co-location is instead cut into discrete episodes, separated by gaps of more than
   :data:`EPISODE_SEPARATION_MINUTES` -- the same ``lag()``-flag-running-sum idiom
   ``process.tracks`` uses for voyage segmentation (see that module's ``build_gap_scores``-style
   docstring), reused here with the pair as the partition key and a 10-minute slot in place of a
   raw timestamp. Because slot boundaries are hour-aligned and segmentation runs as ONE global pass
   over the whole window (not per day), a slot -- and therefore an episode -- can never straddle a
   day-partition boundary; there is nothing to stitch.
2. **The coastal-anchorage exclusion** (:func:`apply_structural_gates`), against
   :mod:`detect.anchorages`'s empirically derived mask. A candidate pair's own two MMSI are
   excluded from a cell's member count at query time (arithmetic leave-one-out, not
   ``list_filter``+lambda -- DuckDB's lambda expressions cannot reference a column from an outer
   correlated subquery table, confirmed this session), so the same cell can be an anchorage for
   most pairs and not an anchorage for the two vessels that alone constitute it.

**Output = hard structural gates + a soft confidence score**, so the gated output stays small and
directly comparable against the GFW Events API in the follow-up task (P2-7), while the softer
discriminators (approach/departure movement, co-drift, ship-type pairing, ``navigational_status``,
pair repetition) feed a 0-1 heuristic for analyst triage -- never a calibrated probability, see
:func:`encounter_confidence`. One row per detected episode, never per vessel or vessel-month, per
``docs/DECISIONS.md``.

**Performance.** No per-point spatial join ever happens. Points are first aggregated into
:data:`SLOT_MINUTES`-wide slots (a ~27x reduction measured on real data: 386,284 slot-rows/day from
10.4M points/day), then the pairwise proximity join runs per day partition on the small slot table
(1.3M pair-slots/day measured, 0.26s/day), then episode segmentation runs once, globally, over the
whole window's pair-slots (4s for 9.1M rows measured). A real 30-day run is projected at minutes,
not hours -- the binding constraint is memory (the pair-slot table), not time.

**``ST_Distance_Sphere`` argument order.** This DuckDB build takes each point as
``ST_Point(latitude, longitude)``, the reverse of the standard ``(longitude, latitude)`` order
every other spatial function uses -- found in ``detect.anchorages`` this session and fixed
retroactively in ``detect.spoofing`` too (see ``docs/DECISIONS.md``). Every ``ST_Distance_Sphere``
call in this module uses the ``(latitude, longitude)`` order deliberately, not by accident.

**Known simplification.** The ``navigational_status`` "moored offshore" signal (a vessel reporting
``Moored``/``At anchor`` far from any known anchorage is itself a misreporting signal, not evidence
of a real transfer) uses this episode's own ``distance_to_anchorage_m`` -- already computed for the
hard gate -- as the "how far from a mooring location" proxy, rather than a fresh land-distance
query. Cheaper, and the two are correlated in practice; a dedicated land-distance computation
(``detect.anchorages``'s own technique) would be more precise if this signal turns out to matter.

Every threshold below is a considered but unvalidated default, same posture as every other
detector in this project. **This detector effectively cannot see the Danish Belts**: a 10km
coastal-anchorage limit plus a 10km exclusion radius removes everything within ~20km of shore, and
the Great Belt is narrower than that -- survivors live in the Kattegat, Skagerrak and open Baltic.
Expected and bounds recall against GFW's own set in P2-7; belongs in the README's limitations too.
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

from detect.anchorages import ANCHORAGES_PATH, MIN_DISTINCT_VESSELS
from process.partitions import existing_partitions

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
DETECT_ROOT = Path("data/detect")
STS_PATH = DETECT_ROOT / "sts.parquet"

# Only Class A/B -- a vessel must never be paired with a fixed Base Station/AtoN beacon.
MOBILE_TYPES = ("Class A", "Class B")

SLOT_MINUTES = 10
# >=590m E-W at 58N (the northern edge MAX_ABS_LATITUDE_DEG admits), > MAX_SEPARATION_M, so a 3x3
# neighbourhood of cells is a complete cover for the proximity join.
GRID_DEG = 0.01
# Matches detect.anchorages's own guard against corrupted high-latitude positions, and keeps
# GRID_DEG's 3x3-neighbourhood cover assumption valid (see module docstring).
MAX_ABS_LATITUDE_DEG = 62.0
# Pre-filter for the pairwise join, deliberately wider than MAX_MEDIAN_SOG_KNOTS -- a candidate
# gate, not the GFW structural gate itself (that is applied per-episode, on the pooled median).
CANDIDATE_MAX_SOG_KNOTS = 3.0

# GFW's own structural definition -- hard gates, applied in apply_structural_gates.
MAX_SEPARATION_M = 500.0
MIN_DURATION_HOURS = 2.0
MAX_MEDIAN_SOG_KNOTS = 2.0
MIN_ANCHORAGE_DISTANCE_M = 10_000.0

# A pair's co-location is cut into a new episode when consecutive slots are more than this far
# apart. Unvalidated default: comfortably wider than ordinary AIS reporting cadence, tight enough
# that two vessels ceasing co-location for an hour have, at any realistic speed, gone somewhere.
EPISODE_SEPARATION_MINUTES = 60.0

# Rendezvous-signature look-back/look-ahead around an episode.
CONTEXT_WINDOW_HOURS = 6.0
MIN_APPROACH_DISPLACEMENT_M = 2_000.0
MIN_TRANSIT_SOG_KNOTS = 3.0

# Confidence-score constants (all unvalidated defaults -- see encounter_confidence).
CODRIFT_FULL_CREDIT_M = 500.0
SAME_PLACE_RADIUS_M = 1_000.0
LONG_EPISODE_HOURS = 24.0
VERY_LONG_EPISODE_HOURS = 72.0
W_RENDEZVOUS = 0.35
W_CODRIFT = 0.20
W_SHIP_TYPE = 0.20
W_NAV_STATUS = 0.10
W_REPETITION = 0.10
W_DURATION = 0.05

# A candidate pair's own two MMSI must not count toward an anchorage cell's membership --
# see module docstring. Reused from detect.anchorages: the same "how many OTHER vessels" bar
# the cell needed to become an anchorage in the first place.
_ANCHORAGE_MIN_OTHER_VESSELS = MIN_DISTINCT_VESSELS
# Planar-degree bounding box for the anchorage-distance pre-filter, a strict superset of
# MIN_ANCHORAGE_DISTANCE_M across every latitude this module admits data from -- same technique
# and margin as detect.anchorages's own LAND_PREFILTER_DEG.
_ANCHORAGE_BBOX_LAT_DEG = MIN_ANCHORAGE_DISTANCE_M / 111_320.0 * 1.1
_ANCHORAGE_BBOX_LON_DEG = (
    MIN_ANCHORAGE_DISTANCE_M / (111_320.0 * math.cos(math.radians(MAX_ABS_LATITUDE_DEG))) * 1.1
)

_SERVICE_SHIP_TYPES = frozenset(
    {
        "Tug",
        "Pilot",
        "Port tender",
        "SAR",
        "Law enforcement",
        "Dredging",
        "Anti-pollution",
        "Diving",
        "Towing",
        "Towing long/wide",
    }
)
_MOORED_STATUSES = frozenset({"Moored", "At anchor"})
_UNDEFINED_STATUSES = frozenset({"Undefined", "Unknown", "", "Not defined"})


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


def _ship_type_pair_score(type_a: str | None, type_b: str | None) -> float:
    """Score a ship-type pairing: tanker/tanker highest, any service vessel lowest.

    Service-vessel attendance (tug, pilot, SAR...) was the dominant false-positive population in
    an early prototype over real data -- the pattern this score exists to down-rank.
    """
    a = type_a or "Undefined"
    b = type_b or "Undefined"
    if a in _SERVICE_SHIP_TYPES or b in _SERVICE_SHIP_TYPES:
        return 0.1
    if a == "Tanker" and b == "Tanker":
        return 1.0
    if {a, b} == {"Tanker", "Cargo"}:
        return 0.9
    if a == "Cargo" and b == "Cargo":
        return 0.6
    if "Tanker" in (a, b) or "Cargo" in (a, b):
        return 0.4
    return 0.2


def _movement_state(displacement_m: float | None, max_sog_knots: float | None, n_slots: int) -> str:
    """Tri-state, never a boolean: "moved" / "stationary" / "no_data".

    n_slots == 0 must be distinguished from a genuine zero displacement -- a vessel whose track
    simply begins at the episode is missing evidence, not evidence of never having moved.
    """
    if n_slots == 0:
        return "no_data"
    displacement_m = displacement_m or 0.0
    max_sog_knots = max_sog_knots or 0.0
    if displacement_m >= MIN_APPROACH_DISPLACEMENT_M or max_sog_knots >= MIN_TRANSIT_SOG_KNOTS:
        return "moved"
    return "stationary"


_STATE_SCORE = {"moved": 1.0, "no_data": 0.5, "stationary": 0.0}


def _nav_status_score(nav_status: str | None, distance_to_anchorage_m: float | None) -> float:
    """Score one vessel's navigational_status, with the "moored offshore" inversion.

    Reporting Moored/At anchor far from any known anchorage is evidence of misreporting -- this
    project's actual subject -- not evidence of genuinely being moored, so it scores HIGH, not
    low. Spoofable and weak (see module docstring), so this only ever feeds confidence, never a
    hard gate.
    """
    if nav_status in _MOORED_STATUSES:
        if (distance_to_anchorage_m or 0.0) >= MIN_ANCHORAGE_DISTANCE_M:
            return 1.0
        return 0.0
    if nav_status is None or nav_status in _UNDEFINED_STATUSES:
        return 0.5
    return 1.0


def _duration_score(duration_hours: float) -> float:
    """1.0 up to LONG_EPISODE_HOURS, linear taper to 0.0 at VERY_LONG_EPISODE_HOURS."""
    if duration_hours <= LONG_EPISODE_HOURS:
        return 1.0
    if duration_hours >= VERY_LONG_EPISODE_HOURS:
        return 0.0
    span = VERY_LONG_EPISODE_HOURS - LONG_EPISODE_HOURS
    return 1.0 - (duration_hours - LONG_EPISODE_HOURS) / span


def _repetition_score(pair_same_place_count: int) -> float:
    """1.0 for a one-off encounter, tapering to 0.0 for a pair repeating at the same spot.

    Scored on SAME-PLACE recurrence, not raw pair-episode count, so a pair genuinely meeting twice
    in different places is not punished for it.
    """
    if pair_same_place_count <= 1:
        return 1.0
    if pair_same_place_count == 2:
        return 0.5
    return 0.0


def encounter_confidence(
    approach_state_a: str,
    approach_state_b: str,
    departure_state_a: str,
    departure_state_b: str,
    codrift_m: float,
    ship_type_a: str | None,
    ship_type_b: str | None,
    nav_status_a: str | None,
    nav_status_b: str | None,
    distance_to_anchorage_m: float | None,
    pair_same_place_count: int,
    duration_hours: float,
) -> tuple[float, dict[str, float]]:
    """Combine six rule-based discriminators into a 0-1 heuristic. A pure function, no ``con`` --
    unit-testable without DuckDB, mirroring ``detect.gaps.gap_probability``.

    **Not a calibrated probability.** (1) No confirmed ship-to-ship transfer has ever been
    labelled in this project, so no term here has been fitted to or tested against an outcome.
    (2) The weights are pre-registered judgement calls about relative evidential strength, not
    estimated. (3) The terms are correlated by construction -- rendezvous and co-drift both
    measure "this pair was under way, not berthed" -- so their weighted sum double-counts
    correlated evidence rather than combining independent likelihoods. (4) The discriminators are
    near-binary, so the resulting distribution is bimodal by design and has little resolution in
    the middle of its range. This is an ordering device for analyst triage. P2-7's agreement with
    the GFW Events API must be measured on the hard-gated set, never on a confidence cut.

    All six component scores are returned alongside the combined confidence so a future caller
    (features/, or a re-weighting after P2-7) can recompute without re-running the detector.
    """
    score_rendezvous = sum(
        _STATE_SCORE[s]
        for s in (approach_state_a, approach_state_b, departure_state_a, departure_state_b)
    ) / 4.0
    score_codrift = min(1.0, codrift_m / CODRIFT_FULL_CREDIT_M)
    score_ship_type = _ship_type_pair_score(ship_type_a, ship_type_b)
    score_nav_status = (
        _nav_status_score(nav_status_a, distance_to_anchorage_m)
        + _nav_status_score(nav_status_b, distance_to_anchorage_m)
    ) / 2.0
    score_repetition = _repetition_score(pair_same_place_count)
    score_duration = _duration_score(duration_hours)

    confidence = (
        W_RENDEZVOUS * score_rendezvous
        + W_CODRIFT * score_codrift
        + W_SHIP_TYPE * score_ship_type
        + W_NAV_STATUS * score_nav_status
        + W_REPETITION * score_repetition
        + W_DURATION * score_duration
    )
    components = {
        "score_rendezvous": score_rendezvous,
        "score_codrift": score_codrift,
        "score_ship_type": score_ship_type,
        "score_nav_status": score_nav_status,
        "score_repetition": score_repetition,
        "score_duration": score_duration,
    }
    return confidence, components


@dataclass(frozen=True)
class EncounterEvent:
    """One detected ship-to-ship-transfer candidate episode. One row per episode, never per
    vessel or vessel-month -- see module docstring."""

    encounter_id: str
    mmsi_a: int
    mmsi_b: int
    episode_seq: int
    start_time: datetime
    end_time: datetime
    duration_hours: float
    n_slots: int
    median_separation_m: float
    min_separation_m: float
    max_separation_m: float
    median_sog_a_knots: float
    median_sog_b_knots: float
    median_sog_knots: float
    latitude: float
    longitude: float
    start_latitude: float
    start_longitude: float
    end_latitude: float
    end_longitude: float
    distance_to_anchorage_m: float | None
    ship_type_a: str | None
    ship_type_b: str | None
    nav_status_a: str | None
    nav_status_b: str | None
    approach_state_a: str
    approach_state_b: str
    departure_state_a: str
    departure_state_b: str
    codrift_m: float
    pair_same_place_count: int
    score_rendezvous: float
    score_codrift: float
    score_ship_type: float
    score_nav_status: float
    score_repetition: float
    score_duration: float
    confidence: float  # 0-1, a rule-based heuristic, NOT a calibrated probability -- unvalidated.


def _build_slots(
    con: duckdb.DuckDBPyConnection,
    partitions: list[tuple[date, Path]],
    slot_minutes: int,
    mobile_types: tuple[str, ...],
) -> None:
    """Aggregate clean AIS into (mmsi, slot) rows -- ALL points, not just slow ones, since the
    rendezvous-signature stage needs a vessel's movement around an episode too. Also reduces
    ship_type per mmsi via the most frequent non-Undefined/Unknown value seen. Both accumulated
    per day partition in a Python loop (one scan per file)."""
    mobile_list = ", ".join(f"'{t}'" for t in mobile_types)

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _slots "
        "(mmsi BIGINT, slot TIMESTAMP, latitude DOUBLE, longitude DOUBLE, "
        "median_sog DOUBLE, max_sog DOUBLE, nav_status VARCHAR)"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _ship_type_counts (mmsi BIGINT, ship_type VARCHAR, n INTEGER)"
    )
    for _day, path in partitions:
        con.execute(
            "INSERT INTO _slots "
            "SELECT mmsi, "
            "date_trunc('hour', timestamp) + "
            f"to_minutes(CAST(FLOOR(extract(minute FROM timestamp) / {slot_minutes}) "
            f"* {slot_minutes} AS INTEGER)) AS slot, "
            "median(latitude) AS latitude, median(longitude) AS longitude, "
            "median(sog) AS median_sog, max(sog) AS max_sog, "
            "mode(navigational_status) AS nav_status "
            f"FROM read_parquet('{path.as_posix()}') "
            f"WHERE type_of_mobile IN ({mobile_list}) AND abs(latitude) <= {MAX_ABS_LATITUDE_DEG} "
            "GROUP BY mmsi, slot"
        )
        con.execute(
            "INSERT INTO _ship_type_counts "
            "SELECT mmsi, ship_type, CAST(count(*) AS INTEGER) AS n "
            f"FROM read_parquet('{path.as_posix()}') "
            f"WHERE type_of_mobile IN ({mobile_list}) AND abs(latitude) <= {MAX_ABS_LATITUDE_DEG} "
            "  AND ship_type NOT IN ('Undefined', 'Unknown') "
            "GROUP BY mmsi, ship_type"
        )

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _vessel_types AS "
        "SELECT mmsi, ship_type FROM ("
        "  SELECT mmsi, ship_type, "
        "    row_number() OVER (PARTITION BY mmsi ORDER BY sum(n) DESC) AS rn "
        "  FROM _ship_type_counts GROUP BY mmsi, ship_type"
        ") WHERE rn = 1"
    )


def _build_pair_slots(
    con: duckdb.DuckDBPyConnection,
    partitions: list[tuple[date, Path]],
    grid_deg: float,
    candidate_max_sog_knots: float,
    max_separation_m: float,
) -> None:
    """Grid-bucketed pairwise proximity join, run per day partition off the (already-built)
    ``_slots`` table -- chunking the join, not the Parquet scan, since _slots is small enough to
    stay in memory for the whole window. See module docstring for the measured per-day cost."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _pair_slots "
        "(mmsi_a BIGINT, mmsi_b BIGINT, slot TIMESTAMP, separation_m FLOAT, "
        "mid_latitude FLOAT, mid_longitude FLOAT, sog_a FLOAT, sog_b FLOAT)"
    )
    for day, _path in partitions:
        day_start = f"TIMESTAMP '{day.isoformat()} 00:00:00'"
        day_end = f"TIMESTAMP '{day.isoformat()} 00:00:00' + INTERVAL 1 DAY"
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _slow_day AS "
            "SELECT mmsi, slot, latitude, longitude, median_sog, "
            f"CAST(FLOOR(latitude / {grid_deg}) AS INTEGER) AS cell_x, "
            f"CAST(FLOOR(longitude / {grid_deg}) AS INTEGER) AS cell_y "
            "FROM _slots "
            f"WHERE median_sog < {candidate_max_sog_knots} "
            f"  AND slot >= {day_start} AND slot < {day_end}"
        )
        # ST_Distance_Sphere here takes ST_Point(latitude, longitude) -- see module docstring.
        con.execute(
            "INSERT INTO _pair_slots "
            "WITH neighbours AS ("
            "  SELECT s.*, dx, dy FROM _slow_day s, range(-1, 2) t(dx), range(-1, 2) u(dy)"
            ") "
            "SELECT a.mmsi, b.mmsi, a.slot, "
            "ST_Distance_Sphere(ST_Point(a.latitude, a.longitude), "
            "ST_Point(b.latitude, b.longitude))::FLOAT, "
            "((a.latitude + b.latitude) / 2)::FLOAT, ((a.longitude + b.longitude) / 2)::FLOAT, "
            "a.median_sog::FLOAT, b.median_sog::FLOAT "
            "FROM neighbours a JOIN _slow_day b "
            "  ON a.slot = b.slot AND a.cell_x + a.dx = b.cell_x AND a.cell_y + a.dy = b.cell_y "
            "  AND a.mmsi < b.mmsi "
            "WHERE ST_Distance_Sphere(ST_Point(a.latitude, a.longitude), "
            f"ST_Point(b.latitude, b.longitude)) <= {max_separation_m}"
        )


def segment_episodes(con: duckdb.DuckDBPyConnection, episode_separation_minutes: float) -> None:
    """Segment ``_pair_slots`` into ``_episodes``: ONE global pass, the lag()-flag-running-sum
    idiom ``process.tracks`` uses for voyage segmentation, with (mmsi_a, mmsi_b) as the partition
    key and slot in place of a raw timestamp. This is the fix for the 647-hour false-encounter
    artifact -- see module docstring -- and, because slots are hour-aligned and this runs globally
    rather than per day, no episode can straddle a day-partition boundary; there is nothing to
    stitch.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _episodes AS "
        "WITH flagged AS ("
        "  SELECT *, CASE WHEN lag(slot) OVER w IS NULL "
        f"    OR date_diff('minute', lag(slot) OVER w, slot) > {episode_separation_minutes} "
        "    THEN 1 ELSE 0 END AS _new_episode "
        "  FROM _pair_slots WINDOW w AS (PARTITION BY mmsi_a, mmsi_b ORDER BY slot)"
        "), segmented AS ("
        "  SELECT * EXCLUDE (_new_episode), "
        "    CAST(sum(_new_episode) OVER (PARTITION BY mmsi_a, mmsi_b ORDER BY slot "
        "    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS BIGINT) AS episode_seq "
        "  FROM flagged"
        "), pooled AS ("
        "  SELECT mmsi_a, mmsi_b, episode_seq, median(s) AS median_sog_knots "
        "  FROM (SELECT mmsi_a, mmsi_b, episode_seq, unnest([sog_a, sog_b]) AS s FROM segmented) "
        "  GROUP BY mmsi_a, mmsi_b, episode_seq"
        ") "
        "SELECT g.mmsi_a, g.mmsi_b, g.episode_seq, "
        "CAST(g.mmsi_a AS VARCHAR) || '-' || CAST(g.mmsi_b AS VARCHAR) || '-' "
        "  || CAST(g.episode_seq AS VARCHAR) AS encounter_id, "
        "min(g.slot) AS start_time, "
        f"max(g.slot) + to_minutes({int(SLOT_MINUTES)}) AS end_time, "
        f"(date_diff('minute', min(g.slot), max(g.slot)) + {SLOT_MINUTES}) / 60.0 AS duration_hours, "
        "CAST(count(*) AS INTEGER) AS n_slots, "
        "median(g.separation_m) AS median_separation_m, "
        "min(g.separation_m) AS min_separation_m, max(g.separation_m) AS max_separation_m, "
        "median(g.sog_a) AS median_sog_a_knots, median(g.sog_b) AS median_sog_b_knots, "
        "p.median_sog_knots, "
        "median(g.mid_latitude) AS latitude, median(g.mid_longitude) AS longitude, "
        "arg_min(g.mid_latitude, g.slot) AS start_latitude, "
        "arg_min(g.mid_longitude, g.slot) AS start_longitude, "
        "arg_max(g.mid_latitude, g.slot) AS end_latitude, "
        "arg_max(g.mid_longitude, g.slot) AS end_longitude "
        "FROM segmented g JOIN pooled p USING (mmsi_a, mmsi_b, episode_seq) "
        "GROUP BY g.mmsi_a, g.mmsi_b, g.episode_seq, p.median_sog_knots"
    )


def apply_structural_gates(
    con: duckdb.DuckDBPyConnection,
    anchorages_path: Path,
    min_duration_hours: float,
    max_median_sog_knots: float,
    max_separation_m: float,
    min_anchorage_distance_m: float,
) -> None:
    """Apply GFW's four hard structural gates, writing ``_gated``.

    The anchorage-distance leave-one-out is arithmetic (``len(member_mmsis) -
    list_contains(...)::INT - list_contains(...)::INT``), not ``list_filter`` with a lambda --
    DuckDB's lambda expressions cannot reference a column from an outer correlated subquery table
    (confirmed this session; ``list_filter(a.member_mmsis, m -> m <> e.mmsi_a)`` inside a
    correlated subquery raises ``Binder Error: Referenced table "e" not found``). A non-coastal
    anchorage cell (``is_coastal = false``) never excludes anything -- see detect.anchorages.
    A LEFT-join-shaped scalar subquery with ``coalesce(..., 1e9)`` means "no anchorage nearby"
    reads as PASS, not as a NULL-drop.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _episode_anchorage_distance AS "
        "SELECT e.mmsi_a, e.mmsi_b, e.episode_seq, "
        "(SELECT min(ST_Distance_Sphere("
        "   ST_Point(a.center_latitude, a.center_longitude), ST_Point(e.latitude, e.longitude)"
        " )) FROM read_parquet(?) a "
        " WHERE a.is_coastal "
        "   AND len(a.member_mmsis) - list_contains(a.member_mmsis, e.mmsi_a)::INT "
        f"       - list_contains(a.member_mmsis, e.mmsi_b)::INT >= {_ANCHORAGE_MIN_OTHER_VESSELS} "
        f"   AND abs(a.center_latitude - e.latitude) <= {_ANCHORAGE_BBOX_LAT_DEG} "
        f"   AND abs(a.center_longitude - e.longitude) <= {_ANCHORAGE_BBOX_LON_DEG}"
        ") AS distance_to_anchorage_m "
        "FROM _episodes e",
        [str(anchorages_path)],
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _gated AS "
        "SELECT e.*, d.distance_to_anchorage_m "
        "FROM _episodes e JOIN _episode_anchorage_distance d USING (mmsi_a, mmsi_b, episode_seq) "
        f"WHERE e.duration_hours >= {min_duration_hours} "
        f"  AND e.median_sog_knots < {max_median_sog_knots} "
        f"  AND e.max_separation_m <= {max_separation_m} "
        f"  AND coalesce(d.distance_to_anchorage_m, 1e9) >= {min_anchorage_distance_m}"
    )


def score_encounters(con: duckdb.DuckDBPyConnection) -> list[EncounterEvent]:
    """Compute the soft discriminators over the (small) ``_gated`` survivor set, then call
    :func:`encounter_confidence` once per survivor in Python. Requires ``_gated``, ``_slots`` and
    ``_vessel_types`` (see :func:`apply_structural_gates`, :func:`_build_slots`).
    """
    context_minutes = int(CONTEXT_WINDOW_HOURS * 60)
    # ST_Distance_Sphere here takes ST_Point(latitude, longitude) -- see module docstring.
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _context AS "
        "WITH sides AS ("
        "  SELECT encounter_id, mmsi_a AS mmsi, start_time, end_time, latitude, longitude "
        "  FROM _gated "
        "  UNION ALL "
        "  SELECT encounter_id, mmsi_b AS mmsi, start_time, end_time, latitude, longitude "
        "  FROM _gated"
        ") "
        "SELECT s.encounter_id, s.mmsi, "
        "max(CASE WHEN v.slot < s.start_time THEN ST_Distance_Sphere("
        "  ST_Point(v.latitude, v.longitude), ST_Point(s.latitude, s.longitude)) END) "
        "  AS before_displacement_m, "
        "max(CASE WHEN v.slot > s.end_time THEN ST_Distance_Sphere("
        "  ST_Point(v.latitude, v.longitude), ST_Point(s.latitude, s.longitude)) END) "
        "  AS after_displacement_m, "
        "max(CASE WHEN v.slot < s.start_time THEN v.max_sog END) AS before_max_sog, "
        "max(CASE WHEN v.slot > s.end_time THEN v.max_sog END) AS after_max_sog, "
        "CAST(count(*) FILTER (WHERE v.slot < s.start_time) AS INTEGER) AS n_before_slots, "
        "CAST(count(*) FILTER (WHERE v.slot > s.end_time) AS INTEGER) AS n_after_slots, "
        "mode(CASE WHEN v.slot BETWEEN s.start_time AND s.end_time THEN v.nav_status END) "
        "  AS nav_status "
        "FROM sides s "
        "JOIN _slots v ON v.mmsi = s.mmsi "
        f"  AND v.slot BETWEEN s.start_time - to_minutes({context_minutes}) "
        f"                 AND s.end_time + to_minutes({context_minutes}) "
        "GROUP BY s.encounter_id, s.mmsi"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _repetition AS "
        "SELECT a.mmsi_a, a.mmsi_b, a.episode_seq, CAST(count(*) AS INTEGER) AS pair_same_place_count "
        "FROM _gated a JOIN _gated b "
        "  ON a.mmsi_a = b.mmsi_a AND a.mmsi_b = b.mmsi_b "
        "  AND ST_Distance_Sphere(ST_Point(a.latitude, a.longitude), "
        f"        ST_Point(b.latitude, b.longitude)) <= {SAME_PLACE_RADIUS_M} "
        "GROUP BY a.mmsi_a, a.mmsi_b, a.episode_seq"
    )
    rows = con.execute(
        "SELECT g.encounter_id, g.mmsi_a, g.mmsi_b, g.episode_seq, g.start_time, g.end_time, "
        "g.duration_hours, g.n_slots, g.median_separation_m, g.min_separation_m, "
        "g.max_separation_m, g.median_sog_a_knots, g.median_sog_b_knots, g.median_sog_knots, "
        "g.latitude, g.longitude, g.start_latitude, g.start_longitude, "
        "g.end_latitude, g.end_longitude, g.distance_to_anchorage_m, "
        "ta.ship_type, tb.ship_type, "
        "ca.before_displacement_m, ca.before_max_sog, ca.n_before_slots, "
        "ca.after_displacement_m, ca.after_max_sog, ca.n_after_slots, ca.nav_status, "
        "cb.before_displacement_m, cb.before_max_sog, cb.n_before_slots, "
        "cb.after_displacement_m, cb.after_max_sog, cb.n_after_slots, cb.nav_status, "
        "ST_Distance_Sphere(ST_Point(g.start_latitude, g.start_longitude), "
        "  ST_Point(g.end_latitude, g.end_longitude)) AS codrift_m, "
        "r.pair_same_place_count "
        "FROM _gated g "
        "LEFT JOIN _vessel_types ta ON ta.mmsi = g.mmsi_a "
        "LEFT JOIN _vessel_types tb ON tb.mmsi = g.mmsi_b "
        "JOIN _context ca ON ca.encounter_id = g.encounter_id AND ca.mmsi = g.mmsi_a "
        "JOIN _context cb ON cb.encounter_id = g.encounter_id AND cb.mmsi = g.mmsi_b "
        "JOIN _repetition r ON r.mmsi_a = g.mmsi_a AND r.mmsi_b = g.mmsi_b "
        "  AND r.episode_seq = g.episode_seq"
    ).fetchall()

    events = []
    for (
        encounter_id,
        mmsi_a,
        mmsi_b,
        episode_seq,
        start_time,
        end_time,
        duration_hours,
        n_slots,
        median_separation_m,
        min_separation_m,
        max_separation_m,
        median_sog_a_knots,
        median_sog_b_knots,
        median_sog_knots,
        latitude,
        longitude,
        start_latitude,
        start_longitude,
        end_latitude,
        end_longitude,
        distance_to_anchorage_m,
        ship_type_a,
        ship_type_b,
        before_displacement_a_m,
        before_max_sog_a,
        n_before_slots_a,
        after_displacement_a_m,
        after_max_sog_a,
        n_after_slots_a,
        nav_status_a,
        before_displacement_b_m,
        before_max_sog_b,
        n_before_slots_b,
        after_displacement_b_m,
        after_max_sog_b,
        n_after_slots_b,
        nav_status_b,
        codrift_m,
        pair_same_place_count,
    ) in rows:
        approach_state_a = _movement_state(before_displacement_a_m, before_max_sog_a, n_before_slots_a)
        approach_state_b = _movement_state(before_displacement_b_m, before_max_sog_b, n_before_slots_b)
        departure_state_a = _movement_state(after_displacement_a_m, after_max_sog_a, n_after_slots_a)
        departure_state_b = _movement_state(after_displacement_b_m, after_max_sog_b, n_after_slots_b)

        confidence, components = encounter_confidence(
            approach_state_a=approach_state_a,
            approach_state_b=approach_state_b,
            departure_state_a=departure_state_a,
            departure_state_b=departure_state_b,
            codrift_m=codrift_m,
            ship_type_a=ship_type_a,
            ship_type_b=ship_type_b,
            nav_status_a=nav_status_a,
            nav_status_b=nav_status_b,
            distance_to_anchorage_m=distance_to_anchorage_m,
            pair_same_place_count=pair_same_place_count,
            duration_hours=duration_hours,
        )
        events.append(
            EncounterEvent(
                encounter_id=encounter_id,
                mmsi_a=mmsi_a,
                mmsi_b=mmsi_b,
                episode_seq=episode_seq,
                start_time=start_time,
                end_time=end_time,
                duration_hours=duration_hours,
                n_slots=n_slots,
                median_separation_m=median_separation_m,
                min_separation_m=min_separation_m,
                max_separation_m=max_separation_m,
                median_sog_a_knots=median_sog_a_knots,
                median_sog_b_knots=median_sog_b_knots,
                median_sog_knots=median_sog_knots,
                latitude=latitude,
                longitude=longitude,
                start_latitude=start_latitude,
                start_longitude=start_longitude,
                end_latitude=end_latitude,
                end_longitude=end_longitude,
                distance_to_anchorage_m=distance_to_anchorage_m,
                ship_type_a=ship_type_a,
                ship_type_b=ship_type_b,
                nav_status_a=nav_status_a,
                nav_status_b=nav_status_b,
                approach_state_a=approach_state_a,
                approach_state_b=approach_state_b,
                departure_state_a=departure_state_a,
                departure_state_b=departure_state_b,
                codrift_m=codrift_m,
                pair_same_place_count=pair_same_place_count,
                confidence=confidence,
                **components,
            )
        )
    return events


def build_sts_events(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    anchorages_path: Path = ANCHORAGES_PATH,
    out_path: Path = STS_PATH,
    force: bool = False,
) -> Path:
    """Run the full ship-to-ship-transfer pipeline over [start, end] and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError naming what builds each missing input -- no clean
    partitions in range: process.clean.clean_range; missing anchorages_path:
    detect.anchorages.build_anchorages.
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
    if not anchorages_path.exists():
        raise FileNotFoundError(
            f"No anchorage mask at {anchorages_path}; run "
            "detect.anchorages.build_anchorages first"
        )

    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")

        events: list[EncounterEvent] = []
        stage_start = datetime.now(timezone.utc)
        _build_slots(con, partitions, SLOT_MINUTES, MOBILE_TYPES)
        logger.info(
            "Stage slots: %.1fs", (datetime.now(timezone.utc) - stage_start).total_seconds()
        )

        stage_start = datetime.now(timezone.utc)
        _build_pair_slots(con, partitions, GRID_DEG, CANDIDATE_MAX_SOG_KNOTS, MAX_SEPARATION_M)
        (n_pair_slots,) = con.execute("SELECT count(*) FROM _pair_slots").fetchone()
        logger.info(
            "Stage pair_slots: %d row(s) in %.1fs",
            n_pair_slots,
            (datetime.now(timezone.utc) - stage_start).total_seconds(),
        )

        stage_start = datetime.now(timezone.utc)
        segment_episodes(con, EPISODE_SEPARATION_MINUTES)
        (n_episodes,) = con.execute("SELECT count(*) FROM _episodes").fetchone()
        logger.info(
            "Stage episodes: %d row(s) in %.1fs",
            n_episodes,
            (datetime.now(timezone.utc) - stage_start).total_seconds(),
        )

        stage_start = datetime.now(timezone.utc)
        apply_structural_gates(
            con,
            anchorages_path,
            MIN_DURATION_HOURS,
            MAX_MEDIAN_SOG_KNOTS,
            MAX_SEPARATION_M,
            MIN_ANCHORAGE_DISTANCE_M,
        )
        (n_gated,) = con.execute("SELECT count(*) FROM _gated").fetchone()
        logger.info(
            "Stage gated: %d row(s) survive the hard gates in %.1fs",
            n_gated,
            (datetime.now(timezone.utc) - stage_start).total_seconds(),
        )

        stage_start = datetime.now(timezone.utc)
        events = score_encounters(con)
        logger.info(
            "Stage scored: %d event(s) in %.1fs",
            len(events),
            (datetime.now(timezone.utc) - stage_start).total_seconds(),
        )

        mean_confidence = sum(e.confidence for e in events) / len(events) if events else 0.0
        logger.info(
            "Detected %d ship-to-ship-transfer candidate(s) in %s..%s, mean confidence %.3f",
            len(events),
            start.isoformat(),
            end.isoformat(),
            mean_confidence,
        )

        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _sts_events ("
            "encounter_id VARCHAR, mmsi_a BIGINT, mmsi_b BIGINT, episode_seq BIGINT, "
            "start_time TIMESTAMP, end_time TIMESTAMP, duration_hours DOUBLE, n_slots INTEGER, "
            "median_separation_m DOUBLE, min_separation_m DOUBLE, max_separation_m DOUBLE, "
            "median_sog_a_knots DOUBLE, median_sog_b_knots DOUBLE, median_sog_knots DOUBLE, "
            "latitude DOUBLE, longitude DOUBLE, start_latitude DOUBLE, start_longitude DOUBLE, "
            "end_latitude DOUBLE, end_longitude DOUBLE, distance_to_anchorage_m DOUBLE, "
            "ship_type_a VARCHAR, ship_type_b VARCHAR, nav_status_a VARCHAR, nav_status_b VARCHAR, "
            "approach_state_a VARCHAR, approach_state_b VARCHAR, "
            "departure_state_a VARCHAR, departure_state_b VARCHAR, codrift_m DOUBLE, "
            "pair_same_place_count INTEGER, score_rendezvous DOUBLE, score_codrift DOUBLE, "
            "score_ship_type DOUBLE, score_nav_status DOUBLE, score_repetition DOUBLE, "
            "score_duration DOUBLE, confidence DOUBLE)"
        )
        if events:
            con.executemany(
                "INSERT INTO _sts_events VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.encounter_id,
                        e.mmsi_a,
                        e.mmsi_b,
                        e.episode_seq,
                        e.start_time,
                        e.end_time,
                        e.duration_hours,
                        e.n_slots,
                        e.median_separation_m,
                        e.min_separation_m,
                        e.max_separation_m,
                        e.median_sog_a_knots,
                        e.median_sog_b_knots,
                        e.median_sog_knots,
                        e.latitude,
                        e.longitude,
                        e.start_latitude,
                        e.start_longitude,
                        e.end_latitude,
                        e.end_longitude,
                        e.distance_to_anchorage_m,
                        e.ship_type_a,
                        e.ship_type_b,
                        e.nav_status_a,
                        e.nav_status_b,
                        e.approach_state_a,
                        e.approach_state_b,
                        e.departure_state_a,
                        e.departure_state_b,
                        e.codrift_m,
                        e.pair_same_place_count,
                        e.score_rendezvous,
                        e.score_codrift,
                        e.score_ship_type,
                        e.score_nav_status,
                        e.score_repetition,
                        e.score_duration,
                        e.confidence,
                    )
                    for e in events
                ],
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{git_sha}' AS git_sha "
            "FROM _sts_events ORDER BY confidence DESC, start_time) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect ship-to-ship transfer candidates from clean AIS, using GFW's "
        "structural definition as hard gates plus a rule-based confidence score."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--anchorages-path", default=str(ANCHORAGES_PATH), help="Path to the anchorage mask"
    )
    parser.add_argument(
        "--out-path", default=str(STS_PATH), help="Output path for the encounter events"
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
    build_sts_events(
        start,
        end,
        in_root=Path(args.in_dir),
        anchorages_path=Path(args.anchorages_path),
        out_path=Path(args.out_path),
        force=args.force,
    )


if __name__ == "__main__":
    main()
