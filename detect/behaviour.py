"""Detector 5: declared-behaviour contradictions -- destination vs course, draught vs port calls.

**Why this exists.** The first four detectors ask about a vessel's *position* (gaps, spoofing,
ship-to-ship transfers) or its *identity* (identity anomalies). This one asks whether what a
vessel *declares* is consistent with what it *does*: a Message 5 destination that the vessel is
not actually heading toward, or a declared draught (loaded depth) that changes with no port call
or ship-to-ship transfer in between to explain it.

**Scope gate: MMSI that have ever broadcast a valid IMO.** Draught and destination are AIS
Message 5 ("static and voyage related data") fields, broadcast far more reliably and meaningfully
by SOLAS-class vessels (Tanker/Cargo) than by fishing/pleasure craft, which dominate this window's
MMSI count (see ``detect.identity_anomalies``'s own reframing of the same population, ``P1-2``).
This module reuses that precedent rather than re-deriving it: only MMSI with >=1 checksum-valid
IMO (``process.identity.VALID_IMO_SQL``) are candidates for either check. This is also a real
performance necessity -- restricting to ~29% of MMSI up front (6,077 of 21,146 in the pilot
window) keeps both checks' raw-partition scans well under the full 346M rows.

**Check 1: destination vs course** (:func:`check_destination_course_mismatch`). For each candidate
voyage, resolve the modal (most-broadcast) normalized destination text, match it against
``ingest.ports``'s Natural Earth port list by exact normalized name within the data's own
bounding box (plus a generous margin -- ports far outside Danish/Baltic waters are still plausible
destinations, unlike ``detect.spoofing``'s land-mask crop, which only needs the data's own extent).
For every position report at least :data:`MIN_PORT_APPROACH_DISTANCE_M` from the matched port
(closer than that, bearing is dominated by final-approach maneuvering, not the declared course)
AND at least :data:`MIN_COURSE_CHECK_SOG_KNOTS` (a vessel making no real way has no meaningful
COG -- see "A speed floor" below), compute the great-circle bearing to the port and compare it
against the vessel's own COG. A voyage whose median deviation exceeds
:data:`COURSE_MISMATCH_ANGLE_DEG` over at least :data:`MIN_COURSE_SAMPLES` qualifying points is
flagged: the vessel spent most of the voyage heading somewhere other than where it said it was
going.

**A speed floor, added after a dedicated review pass found real validation noise.** The first
version of this check filtered only on COG validity, not speed. A real 30-day run's own COG-valid
points showed 36.5% of the 200 flagged voyages had a median SOG under 0.5kn -- stationary by
``detect.anchorages``'s own definition -- and uniform-random COG jitter on a near-stationary
vessel has an *expected* deviation of exactly 90 degrees against any fixed bearing, which is
exactly this check's flagging threshold: the rule was structurally selecting for the noise it
meant to filter, and the survivors' own high median deviation was evidence of that failure mode,
not evidence against it. Re-running with a :data:`MIN_COURSE_CHECK_SOG_KNOTS` = 2.0kn floor cut
the real count from 200 to 140 -- roughly 30% of the original events were this artifact, not a
declared-behaviour contradiction at all.

**COG, not heading, and why.** The task this detector was scoped from names "destination vs
heading". Heading (``heading`` -- the bow's true compass direction) is only valid (!= 511, the AIS
"not available" sentinel) for 76% of rows in this window; COG (course over ground, the direction
of actual travel) is valid for 91%. COG is also the more meaningful signal for "is this vessel
moving toward its declared destination" -- heading can differ from the direction of travel under
drift or a strong cross-current without being wrong. This is a deliberate substitution, not an
oversight; a future pass could add a parallel heading-based check once its lower coverage is
judged worth the loss in sample size.

**Known limitation: LOCODE-style destinations are not resolved.** Real destination text in this
window is a mix of legible port names (``ESBJERG``, ``ROSTOCK``), free-text activity descriptors
(``FISHING``, ``FISHING GROUNDS``) and UN/LOCODE-style codes with no separator (``NLRTM``,
``PLGDN``, ``DK SKA``) -- about 30% of all non-null destination text is literally the string
``Unknown``. This module has no LOCODE-to-port table and does not attempt to guess one; only
destinations that exactly match a normalized Natural Earth port name (after :func:`_normalize_text_sql`)
participate. This trades recall for not fabricating a fragile abbreviation resolver -- most real
destination broadcasts in this window will simply not produce a check A event, which is a known,
documented gap, not a bug.

**Check 2: draught vs port calls** (:func:`check_draught_change_unexplained`). For each MMSI's
consecutive voyage pair, take the median non-zero declared draught broadcast during each voyage.
A change of :data:`MIN_DRAUGHT_CHANGE_M` or more between the two voyages is a candidate -- draught
should only meaningfully change while cargo is loaded or discharged, at a port/anchorage or during
a ship-to-ship transfer. The gap between the two voyages is checked for either kind of
corroborating evidence:

* the voyage boundary itself (the end position of the earlier voyage, or the start position of the
  later one) falls within :data:`ANCHORAGE_EVIDENCE_RADIUS_M` of a coastal cell in
  ``detect.anchorages``'s empirical mask (``is_coastal = true``), WITH the candidate's own MMSI
  excluded from the cell's member count (leave-one-out -- see "Anchorage evidence" below for why
  this is required, not optional);
* an entry in ``detect.sts``'s ship-to-ship transfer table involving this MMSI overlaps the gap in
  time.

No explanatory evidence in either form: ``draught_change_unexplained``.

**The gap is checked at its two endpoints, not searched.** "Corroborating evidence in the gap"
means exactly two positions -- the earlier voyage's end and the later voyage's start -- tested
against the anchorage mask and against STS episode timestamps. Nothing in between is examined, and
there is no upper bound on gap length: a real 30-day run's flagged gaps have a MEDIAN of 117 hours
(~5 days), a 90th percentile of 307 hours (~13 days), and the flagged position sits a median 27.6km
from the nearest land. The modal flagged event in that run is a vessel that left the Danish
receiver footprint offshore, was gone for days (very plausibly a routine round trip to a foreign
port this project's data simply cannot see, e.g. Rotterdam), and returned with a different
draught. This detector cannot distinguish that from genuine unexplained cargo activity, and does
not claim to -- ``draught_change_unexplained`` is exactly what it says: no evidence was found in
this project's own data, not that none exists. **Declared draught is also a hand-keyed Message 5
field**, routinely stale or mistyped in real AIS -- "unexplained change" has a large benign base
rate before evasion is even on the table. A real 30-day run recorded 764 events; both of these
factors, not just the anchorage mask's own coverage limit below, likely dominate that count more
than any genuine evasive behaviour, and neither should be read as evidence of the latter without a
labelled comparison.

**Anchorage evidence: whole-window provenance and a leave-one-out requirement.** The anchorage
mask (``detect.anchorages``) only creates a cell at all where :data:`detect.anchorages.MIN_DISTINCT_VESSELS`
(5) or more DIFFERENT vessels each dwelled :data:`detect.anchorages.MIN_STATIONARY_HOURS` (3h) or
more there, evaluated over the WHOLE 30-day build, not as of any particular day -- a real run found
14% of coastal cells exist only because a 5th qualifying vessel showed up somewhere else in the
window. Checking a voyage boundary's position against this mask geometrically (rather than reading
``member_mmsis``/window columns directly) avoids pinning a *visit* to the wrong day, but it does
NOT avoid the deeper fact that a cell's very existence as "coastal" is itself a whole-window
artifact -- so this evidence source is not knowable strictly before the fact for any single gap; see
"Temporal leakage" below for how that is handled. Independently of timing, an earlier version of
this evidence check also had a **circularity bug a dedicated review pass caught**: without
excluding the candidate's own MMSI from the cell's member count, a vessel whose OWN mooring is one
of the required 5 could use that same mooring to exonerate its own draught change -- the same shape
of self-exoneration ``docs/DECISIONS.md`` already records ``detect.sts`` needing a leave-one-out
fix for (P2-1b). :func:`_has_anchorage_evidence` now subtracts the candidate MMSI from
``member_mmsis`` before applying :data:`detect.anchorages.MIN_DISTINCT_VESSELS`, mirroring
``detect.sts``'s own ``_ANCHORAGE_MIN_OTHER_VESSELS`` idiom exactly.

**STS evidence borrows a table with no temporal contract of its own.** ``detect.sts`` emits no
``knowable_at`` column -- its hard gates (duration, median SOG, max separation, and the SAME
whole-window anchorage-distance gate discussed above) are whole-episode judgements applied after
segmentation completes, so an episode's status is not knowable at its own ``start_time``. Treating
``sts.parquet``'s ``start_time``/``end_time`` as sufficient to bound evidence, as an earlier
version of this module did, silently borrowed a temporal precision ``detect.sts`` does not
actually provide. See "Temporal leakage" below for how this module now accounts for that instead
of assuming it away.

**Row grain and temporal leakage -- revised after a dedicated review pass found this module's
first draft wrong.** One row per event, mirroring every prior detector. An earlier version of this
docstring claimed both checks were bounded to a single voyage or gap, "never a whole-window
judgement like ``detect.identity_anomalies``'s". That claim was checked against the real run, not
assumed, and did not survive the check:

* **Check 1 IS bounded, confirmed by direct measurement of the real output**
  (``knowable_at < event_time`` returns zero rows; the modal-destination join is strictly scoped
  per ``voyage_id`` via the ``BETWEEN c.start_time AND c.end_time`` join condition, and voyages
  never overlap -- see ``process.tracks``). ``knowable_at`` is ``voyage.end_time + gap_hours``
  (:data:`process.tracks.DEFAULT_GAP_HOURS`), not ``voyage.end_time`` alone: a voyage boundary is
  itself only confirmed once that much silence has elapsed (``process.tracks``'s own definition of
  a voyage boundary), so stamping the boundary's own timestamp as already-known is optimistic by
  up to :data:`process.tracks.DEFAULT_GAP_HOURS`. Also folded into ``knowable_at``: the scope
  gate's own timing (see below).
* **Check 2 is NOT bounded, and the original ``next_voyage.start_time`` claim was actively wrong.**
  Three problems, found by measuring the real run rather than reasoning about the SQL in the
  abstract: (1) the event's own headline number, the NEW draught, is a median over the ENTIRE next
  voyage (:func:`_build_voyage_draughts`), which is not resolved until that voyage's ``end_time``,
  not its ``start_time`` -- every one of 764 real events had ``next_voyage.end_time >
  next_voyage.start_time`` by a median of 42 hours, up to 505; (2) anchorage evidence is a
  whole-window artifact, as described above; (3) STS evidence borrows a table with no temporal
  contract of its own, also described above. All three push the honest ``knowable_at`` for this
  check to ``window_end`` -- the same posture ``detect.identity_anomalies`` already uses for its
  own whole-window judgements (``name_change``/``no_valid_imo``), for the same reason: the claim
  "no evidence exists in this window" cannot be confirmed before the window has been fully
  observed. This makes check 2 more conservative (later-knowable) than check 1, unlike the first
  draft's claim that both were equally gap-bounded.
* **The scope gate is different in kind from either of the above, and does NOT need
  ``window_end``.** ``_build_valid_imo_mmsi`` decides which MMSI are candidates at all from their
  WHOLE-window IMO history, with no time bound -- in principle a day-25 valid-IMO broadcast could
  make a day-3 voyage eligible, using information from the future relative to that voyage. But this
  is a MONOTONE-ACCRUING fact, not an absence claim: once an MMSI has broadcast one valid IMO it
  never stops being a candidate, so later evidence only ever ADDS eligible MMSI, never retracts
  one already established -- the same shape ``detect.identity_anomalies.check_reused_mmsi``
  already argues does NOT need ``window_end`` (a second valid IMO is "fully knowable the instant it
  is seen, and no later evidence can undo it"), unlike ``check_no_valid_imo``'s absence claim,
  which does. The correct treatment is therefore neither "ignore it" (the first draft's bug, though
  empirically inert on the real 30-day run -- zero of 964 events had a first-valid-IMO timestamp
  later than their own ``knowable_at``) nor ``window_end`` (needlessly conservative for a monotone
  fact): each MMSI's ``first_valid_imo_at`` (the earliest valid IMO broadcast observed) is carried
  through ``_candidate_voyages``, and every event's final ``knowable_at`` is
  ``GREATEST(the check's own bound, first_valid_imo_at)`` -- correct by construction rather than
  true only by luck in any one window.

**Confidence is a rule-based heuristic, not a calibrated probability**, same posture as every
other detector here. Every threshold below (``COURSE_MISMATCH_ANGLE_DEG``,
``MIN_PORT_APPROACH_DISTANCE_M``, ``MIN_DRAUGHT_CHANGE_M``, ``ANCHORAGE_EVIDENCE_RADIUS_M``,
``PORT_MATCH_BBOX_MARGIN_DEG``) is a considered but unvalidated default awaiting empirical
calibration once Phase 3's sanctions join gives a comparison point.

One shared DuckDB connection is opened once and reused across both checks, mirroring every prior
detector's pattern of never reconnecting mid-run.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from detect.anchorages import ANCHORAGES_PATH, MIN_DISTINCT_VESSELS
from detect.liveness import MOBILE_TYPES
from detect.sts import STS_PATH
from ingest.ports import PORTS_PATH
from process.identity import VALID_IMO_SQL
from process.partitions import existing_partitions
from process.tracks import DEFAULT_GAP_HOURS, VOYAGES_PATH

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
DETECT_ROOT = Path("data/detect")
BEHAVIOUR_PATH = DETECT_ROOT / "behaviour.parquet"

# Check 1: destination vs course. Unvalidated defaults -- see module docstring.
BBOX_OUTLIER_QUANTILE = 0.001
PORT_MATCH_BBOX_MARGIN_DEG = 10.0
MIN_PORT_APPROACH_DISTANCE_M = 20_000.0
MIN_COURSE_SAMPLES = 10
COURSE_MISMATCH_ANGLE_DEG = 90.0
# A vessel making no real way has no meaningful COG -- see module docstring's "A speed floor".
# Same idea as detect.anchorages.STATIONARY_MAX_SOG_KNOTS (0.5kn), but this is the opposite
# threshold (a floor a moving vessel must clear, not a ceiling a stopped one must stay under), so
# a distinct, higher value: a real-data check found 0.5kn left the artefact largely intact (only
# stationary-by-that-exact-definition vessels excluded), while 2-3kn produced consistent results.
MIN_COURSE_CHECK_SOG_KNOTS = 2.0
# AIS COG "not available" sentinel (360.0 by spec, unlike heading's 511).
COG_UNAVAILABLE = 360.0
DESTINATION_SENTINELS = ("UNKNOWN", "UNDEFINED", "")

# Check 2: draught vs port calls. Unvalidated defaults -- see module docstring.
MIN_DRAUGHT_SAMPLES = 3
MIN_DRAUGHT_CHANGE_M = 2.0
DRAUGHT_CONFIDENCE_SCALE_M = 5.0
ANCHORAGE_EVIDENCE_RADIUS_M = 10_000.0
# Leave-one-out floor for anchorage evidence: same constant detect.sts's own
# _ANCHORAGE_MIN_OTHER_VESSELS reuses, for the same reason -- see module docstring.
_ANCHORAGE_MIN_OTHER_VESSELS = MIN_DISTINCT_VESSELS


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


def _normalize_text_sql(col: str) -> str:
    """SQL expression normalizing an AIS static-data text field for comparison.

    Same convention as detect.identity_anomalies._normalize_text_sql, duplicated here rather than
    imported -- every detector in this project owns its own copy of these small helpers. AIS
    Message 5 pads text to a fixed width with '@'; this strips that padding plus surrounding
    whitespace, collapses internal whitespace runs, and uppercases.
    """
    return f"nullif(upper(regexp_replace(trim({col}, ' @'), '\\s+', ' ', 'g')), '')"


def _mobile_types_sql() -> str:
    return ", ".join(f"'{t}'" for t in MOBILE_TYPES)


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition."""
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


@dataclass(frozen=True)
class BehaviourEvent:
    """One detected declared-behaviour contradiction. One row per event, not per vessel.

    ``confidence``/``evidence_value`` are rule-based heuristics, NOT calibrated probabilities --
    see module docstring. ``event_time``/``knowable_at`` are under this module's temporal-leakage
    contract, which differs by kind -- check 1 is voyage-bounded, check 2 is a whole-window
    judgement (``knowable_at = window_end``) -- see module docstring's temporal-leakage section.
    """

    mmsi: int
    kind: str  # "destination_course_mismatch" | "draught_change_unexplained"
    event_time: datetime
    knowable_at: datetime
    voyage_id: str
    latitude: float
    longitude: float
    confidence: float
    evidence_value: float  # the check's key metric -- see per-check docstring notes.
    detail: str


def _build_valid_imo_mmsi(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Materialize `_valid_imo_mmsi`: (mmsi, first_valid_imo_at) for every MMSI that ever
    broadcasts a checksum-valid IMO.

    The scope gate for both checks -- see module docstring. `first_valid_imo_at` (the earliest
    valid-IMO broadcast) is carried through so the scope gate's own knowable_at contribution can
    be applied per event, per the module docstring's "the scope gate is different in kind" section
    -- a monotone-accruing fact, not an absence claim, so it needs a per-MMSI timestamp, not
    window_end. Reads only mmsi/timestamp/imo/type_of_mobile.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _valid_imo_mmsi AS "
        "SELECT mmsi, min(timestamp) AS first_valid_imo_at FROM ("
        + _partitions_union_sql(partitions, "mmsi, timestamp, imo, type_of_mobile")
        + ") "
        f"WHERE type_of_mobile IN ({_mobile_types_sql()}) "
        f"AND imo IS NOT NULL AND imo != '' AND {VALID_IMO_SQL} "
        "GROUP BY mmsi"
    )


def _build_candidate_voyages(
    con: duckdb.DuckDBPyConnection, voyages_path: Path, start: date, end: date
) -> None:
    """Materialize `_candidate_voyages`: voyages for valid-IMO MMSI only, within [start, end].

    Requires `_valid_imo_mmsi`. Both checks build on this same small table -- a JOIN against the
    scope gate, not a raw-partition scan, since voyages.parquet is already tiny relative to the
    clean partitions it was built from. Carries `first_valid_imo_at` through from the scope gate
    so both checks can fold it into their own knowable_at without a second join.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _candidate_voyages AS "
        "SELECT v.mmsi, v.voyage_seq, v.voyage_id, v.start_time, v.end_time, "
        "v.start_latitude, v.start_longitude, v.end_latitude, v.end_longitude, "
        "m.first_valid_imo_at "
        "FROM read_parquet(?) v "
        "JOIN _valid_imo_mmsi m ON m.mmsi = v.mmsi "
        "WHERE CAST(v.start_time AS DATE) >= ? AND CAST(v.end_time AS DATE) <= ?",
        [str(voyages_path), start, end],
    )


def _build_matched_ports(con: duckdb.DuckDBPyConnection, ports_path: Path) -> int:
    """Materialize `_matched_ports`: (name_norm, port_latitude, port_longitude), one row per
    distinct normalized name, restricted to the candidate voyages' own bounding box (plus a
    generous margin -- see module docstring). Returns the row count.

    Requires `_candidate_voyages` and the spatial extension loaded. Bbox is derived from voyage
    endpoints (a small table), not a fresh scan of raw points -- cheap, and adequate since voyage
    endpoints span the same operating area as the underlying positions. A name collision (two
    ports sharing a normalized name within the box) keeps only one, deterministically -- a known,
    accepted imprecision at this bbox scale. Returns 0 with an empty `_matched_ports` (rather than
    erroring) if `_candidate_voyages` itself is empty -- a real possibility for a short window or
    one with no valid-IMO MMSI at all, not just a test-fixture edge case.
    """
    (n_voyages,) = con.execute("SELECT count(*) FROM _candidate_voyages").fetchone()
    if n_voyages == 0:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _matched_ports "
            "(name_norm VARCHAR, port_latitude DOUBLE, port_longitude DOUBLE)"
        )
        return 0

    lat_min, lat_max, lon_min, lon_max = con.execute(
        "SELECT quantile_cont(lat, ?), quantile_cont(lat, ?), "
        "quantile_cont(lon, ?), quantile_cont(lon, ?) FROM ("
        "  SELECT start_latitude AS lat, start_longitude AS lon FROM _candidate_voyages "
        "  UNION ALL SELECT end_latitude, end_longitude FROM _candidate_voyages"
        ")",
        [
            BBOX_OUTLIER_QUANTILE,
            1 - BBOX_OUTLIER_QUANTILE,
            BBOX_OUTLIER_QUANTILE,
            1 - BBOX_OUTLIER_QUANTILE,
        ],
    ).fetchone()
    m = PORT_MATCH_BBOX_MARGIN_DEG
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _matched_ports AS "
        "WITH in_box AS ("
        f"  SELECT {_normalize_text_sql('name')} AS name_norm, "
        # Explicit CAST: a GEOMETRY column round-trips through Parquet as BLOB when the table
        # that produced it was empty (no geometry value to carry CRS metadata) -- found by this
        # module's own empty-ports-fixture test. ingest.ports's real output always has rows, so
        # this never bites in production, but the cast keeps this query correct regardless.
        "         ST_Y(CAST(geom AS GEOMETRY)) AS port_latitude, "
        "         ST_X(CAST(geom AS GEOMETRY)) AS port_longitude "
        "  FROM read_parquet(?) "
        "  WHERE ST_Y(CAST(geom AS GEOMETRY)) BETWEEN ? AND ? "
        "    AND ST_X(CAST(geom AS GEOMETRY)) BETWEEN ? AND ?"
        "), ranked AS ("
        "  SELECT *, row_number() OVER (PARTITION BY name_norm ORDER BY port_latitude) AS rn "
        "  FROM in_box WHERE name_norm IS NOT NULL"
        ") "
        "SELECT name_norm, port_latitude, port_longitude FROM ranked WHERE rn = 1",
        [str(ports_path), lat_min - m, lat_max + m, lon_min - m, lon_max + m],
    )
    (n,) = con.execute("SELECT count(*) FROM _matched_ports").fetchone()
    return n


def check_destination_course_mismatch(
    con: duckdb.DuckDBPyConnection,
    partitions: list[tuple[date, Path]],
) -> list[BehaviourEvent]:
    """Flag voyages whose declared destination resolves to a port the vessel was not heading to.

    Requires `_candidate_voyages` and `_matched_ports`. Two narrow, grouped passes, mirroring
    detect.spoofing.check_synthetic_circles's two-pass survivor-narrowing shape -- pass 1 resolves
    each voyage's modal destination and matches it to a port (cheap: one text column); pass 2
    computes course deviation only for the much smaller set of voyages that actually matched
    (lat/lon/cog, a costlier per-point calculation). See module docstring for the bearing/COG
    method and the COG-not-heading substitution.
    """
    sentinel_list_sql = ", ".join(f"'{s}'" for s in DESTINATION_SENTINELS if s)
    dest_rows = con.execute(
        "WITH pts AS ("
        "  SELECT c.voyage_id, "
        f"         {_normalize_text_sql('a.destination')} AS destination_norm "
        "  FROM _candidate_voyages c "
        "  JOIN (" + _partitions_union_sql(partitions, "mmsi, timestamp, destination, type_of_mobile")
        + f") a ON a.mmsi = c.mmsi AND a.timestamp BETWEEN c.start_time AND c.end_time "
        f"    AND a.type_of_mobile IN ({_mobile_types_sql()})"
        "), counts AS ("
        "  SELECT voyage_id, destination_norm, count(*) AS n FROM pts "
        f"  WHERE destination_norm NOT IN ({sentinel_list_sql}) "
        "    AND destination_norm IS NOT NULL "
        "  GROUP BY voyage_id, destination_norm"
        "), resolved AS ("
        "  SELECT voyage_id, destination_norm, "
        "         row_number() OVER (PARTITION BY voyage_id ORDER BY n DESC, destination_norm) AS rn "
        "  FROM counts"
        ") "
        "SELECT r.voyage_id, r.destination_norm, p.port_latitude, p.port_longitude "
        "FROM resolved r JOIN _matched_ports p ON p.name_norm = r.destination_norm "
        "WHERE r.rn = 1"
    ).fetchall()
    if not dest_rows:
        return []

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _matched_voyages "
        "(voyage_id VARCHAR, destination_norm VARCHAR, port_latitude DOUBLE, port_longitude DOUBLE)"
    )
    con.executemany("INSERT INTO _matched_voyages VALUES (?, ?, ?, ?)", dest_rows)

    course_rows = con.execute(
        "WITH pts AS ("
        "  SELECT mv.voyage_id, mv.destination_norm, mv.port_latitude, mv.port_longitude, "
        "         a.latitude, a.longitude, a.cog, "
        "         degrees(atan2("
        "           sin(radians(mv.port_longitude - a.longitude)) * cos(radians(mv.port_latitude)), "
        "           cos(radians(a.latitude)) * sin(radians(mv.port_latitude)) "
        "           - sin(radians(a.latitude)) * cos(radians(mv.port_latitude)) "
        "             * cos(radians(mv.port_longitude - a.longitude))"
        "         )) AS bearing_raw, "
        # (latitude, longitude) argument order -- this DuckDB build's ST_Distance_Sphere quirk,
        # documented in detect.spoofing/detect.anchorages.
        "         ST_Distance_Sphere("
        "           ST_Point(a.latitude, a.longitude), ST_Point(mv.port_latitude, mv.port_longitude)"
        "         ) AS distance_to_port_m "
        "  FROM _matched_voyages mv "
        "  JOIN _candidate_voyages c ON c.voyage_id = mv.voyage_id "
        "  JOIN ("
        + _partitions_union_sql(partitions, "mmsi, timestamp, latitude, longitude, cog, sog, type_of_mobile")
        + f") a ON a.mmsi = c.mmsi AND a.timestamp BETWEEN c.start_time AND c.end_time "
        f"    AND a.type_of_mobile IN ({_mobile_types_sql()}) "
        f"    AND a.cog >= 0 AND a.cog < {COG_UNAVAILABLE} "
        # A vessel not making real way has no meaningful COG -- see module docstring's "A speed
        # floor", added after a real-run review found ~30% of check 1's events were exactly this
        # artefact (stationary-vessel COG jitter against a fixed bearing).
        f"    AND a.sog >= {MIN_COURSE_CHECK_SOG_KNOTS}"
        "), filtered AS ("
        "  SELECT voyage_id, destination_norm, "
        "         CASE WHEN bearing_raw < 0 THEN bearing_raw + 360 ELSE bearing_raw END AS bearing_deg, "
        "         cog, distance_to_port_m "
        "  FROM pts "
        f"  WHERE distance_to_port_m >= {MIN_PORT_APPROACH_DISTANCE_M}"
        "), deviated AS ("
        "  SELECT voyage_id, destination_norm, "
        "         LEAST(ABS(bearing_deg - cog), 360 - ABS(bearing_deg - cog)) AS deviation_deg "
        "  FROM filtered"
        ") "
        "SELECT voyage_id, any_value(destination_norm), "
        "       median(deviation_deg) AS median_deviation_deg, count(*) AS n "
        "FROM deviated GROUP BY voyage_id "
        f"HAVING count(*) >= {MIN_COURSE_SAMPLES} AND median(deviation_deg) >= {COURSE_MISMATCH_ANGLE_DEG}"
    ).fetchall()
    if not course_rows:
        return []

    voyage_meta = {
        v[0]: v  # voyage_id -> full row
        for v in con.execute(
            "SELECT voyage_id, mmsi, end_time, end_latitude, end_longitude, first_valid_imo_at "
            "FROM _candidate_voyages"
        ).fetchall()
    }

    events: list[BehaviourEvent] = []
    for voyage_id, destination_norm, median_deviation_deg, n in course_rows:
        meta = voyage_meta.get(voyage_id)
        if meta is None:
            continue
        _voyage_id, mmsi, end_time, end_lat, end_lon, first_valid_imo_at = meta
        # knowable_at: a voyage boundary is only confirmed once DEFAULT_GAP_HOURS of silence have
        # elapsed (process.tracks's own definition), so end_time alone is optimistic; folded with
        # the scope gate's own timing (first_valid_imo_at) per the module docstring's temporal-
        # leakage section -- monotone-accruing, so GREATEST is correct, not window_end.
        knowable_at = max(end_time + timedelta(hours=DEFAULT_GAP_HOURS), first_valid_imo_at)
        confidence = min(
            1.0, (median_deviation_deg - COURSE_MISMATCH_ANGLE_DEG) / (180.0 - COURSE_MISMATCH_ANGLE_DEG)
        )
        events.append(
            BehaviourEvent(
                mmsi=mmsi,
                kind="destination_course_mismatch",
                event_time=end_time,
                knowable_at=knowable_at,
                voyage_id=voyage_id,
                latitude=end_lat,
                longitude=end_lon,
                confidence=confidence,
                evidence_value=median_deviation_deg,
                detail=(
                    f"declared destination {destination_norm!r} matched to a nearby port; "
                    f"median course deviation {median_deviation_deg:.0f} deg over {n} sample(s)"
                ),
            )
        )
    return events


def _build_voyage_draughts(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Materialize `_voyage_draughts`: median non-zero declared draught per candidate voyage.

    Requires `_candidate_voyages`. Reads only mmsi/timestamp/draught, restricted to the same
    scope gate via the BETWEEN join to `_candidate_voyages` (already restricted to valid-IMO MMSI).
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _voyage_draughts AS "
        "WITH pts AS ("
        "  SELECT c.mmsi, c.voyage_seq, c.voyage_id, a.draught "
        "  FROM _candidate_voyages c "
        "  JOIN (" + _partitions_union_sql(partitions, "mmsi, timestamp, draught, type_of_mobile")
        + f") a ON a.mmsi = c.mmsi AND a.timestamp BETWEEN c.start_time AND c.end_time "
        f"    AND a.type_of_mobile IN ({_mobile_types_sql()}) AND a.draught > 0"
        ") "
        "SELECT mmsi, voyage_seq, voyage_id, median(draught) AS median_draught, count(*) AS n "
        "FROM pts GROUP BY mmsi, voyage_seq, voyage_id "
        f"HAVING count(*) >= {MIN_DRAUGHT_SAMPLES}"
    )


def _has_anchorage_evidence(
    con: duckdb.DuckDBPyConnection,
    anchorages_path: Path,
    mmsi: int,
    lat_a: float,
    lon_a: float,
    lat_b: float,
    lon_b: float,
) -> bool:
    """True if either voyage-boundary position lies within ANCHORAGE_EVIDENCE_RADIUS_M of a
    coastal anchorage cell that still qualifies as one with `mmsi` itself excluded from the
    member count (leave-one-out) -- see module docstring's "Anchorage evidence" section for why
    this is required: without it, a vessel that is itself one of the required
    :data:`_ANCHORAGE_MIN_OTHER_VESSELS` members could use its own mooring to exonerate its own
    draught change. Mirrors detect.sts's own `_ANCHORAGE_MIN_OTHER_VESSELS` leave-one-out idiom
    exactly, applied here to a single candidate MMSI instead of a pair.
    """
    (found,) = con.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM read_parquet(?) WHERE is_coastal "
        f"    AND len(member_mmsis) - list_contains(member_mmsis, ?)::INT >= {_ANCHORAGE_MIN_OTHER_VESSELS} "
        "    AND ("
        "      ST_Distance_Sphere(ST_Point(?, ?), ST_Point(center_latitude, center_longitude)) <= ? "
        "      OR ST_Distance_Sphere(ST_Point(?, ?), ST_Point(center_latitude, center_longitude)) <= ?"
        "    )"
        ")",
        [
            str(anchorages_path),
            mmsi,
            lat_a, lon_a, ANCHORAGE_EVIDENCE_RADIUS_M,
            lat_b, lon_b, ANCHORAGE_EVIDENCE_RADIUS_M,
        ],
    ).fetchone()
    return bool(found)


def _has_sts_evidence(
    con: duckdb.DuckDBPyConnection, sts_path: Path, mmsi: int, gap_start: datetime, gap_end: datetime
) -> bool:
    """True if a ship-to-ship transfer episode involving mmsi overlaps [gap_start, gap_end]."""
    (found,) = con.execute(
        "SELECT EXISTS ("
        "  SELECT 1 FROM read_parquet(?) "
        "  WHERE (mmsi_a = ? OR mmsi_b = ?) AND start_time <= ? AND end_time >= ?"
        ")",
        [str(sts_path), mmsi, mmsi, gap_end, gap_start],
    ).fetchone()
    return bool(found)


def check_draught_change_unexplained(
    con: duckdb.DuckDBPyConnection,
    anchorages_path: Path,
    sts_path: Path,
    window_end: datetime,
) -> list[BehaviourEvent]:
    """Flag voyage-to-voyage draught changes with no port/anchorage or STS evidence in the gap.

    Requires `_candidate_voyages` and `_voyage_draughts`. Candidate pairs are found via a `LEAD`
    window over voyage_seq per mmsi, mirroring detect.gaps.candidate_gaps -- then, for each
    candidate whose draught change clears MIN_DRAUGHT_CHANGE_M, two small existence queries (one
    per evidence kind) are run per candidate, the same per-candidate-query idiom detect.gaps uses
    for liveness_verdict. The candidate set is expected to be small (see module docstring: 43.5%
    of valid-IMO MMSI show >1 distinct draught value in the pilot window, and most of those changes
    will be under the threshold or explained), so this does not need bulk SQL the way the
    much-larger-volume checks elsewhere in this project do.

    `knowable_at = window_end` for every event -- NOT `next_voyage.start_time` (an earlier version
    of this function's claim, found wrong by a dedicated review pass; see module docstring's
    temporal-leakage section for the three reasons: the new draught needs the whole next voyage to
    resolve, anchorage evidence is a whole-window artifact, and STS evidence borrows a table with
    no temporal contract of its own).
    """
    rows = con.execute(
        "WITH paired AS ("
        "  SELECT c.mmsi, c.voyage_seq, c.voyage_id, c.end_time, c.end_latitude, c.end_longitude, "
        "         lead(c.voyage_id) OVER w AS next_voyage_id, "
        "         lead(c.start_time) OVER w AS next_start_time, "
        "         lead(c.start_latitude) OVER w AS next_start_latitude, "
        "         lead(c.start_longitude) OVER w AS next_start_longitude, "
        "         lead(c.voyage_seq) OVER w AS next_voyage_seq "
        "  FROM _candidate_voyages c "
        "  WINDOW w AS (PARTITION BY c.mmsi ORDER BY c.voyage_seq)"
        ") "
        "SELECT p.mmsi, p.voyage_id, p.next_voyage_id, p.end_time, p.end_latitude, p.end_longitude, "
        "       p.next_start_time, p.next_start_latitude, p.next_start_longitude, "
        "       d1.median_draught AS old_draught, d2.median_draught AS new_draught "
        "FROM paired p "
        "JOIN _voyage_draughts d1 ON d1.mmsi = p.mmsi AND d1.voyage_seq = p.voyage_seq "
        "JOIN _voyage_draughts d2 ON d2.mmsi = p.mmsi AND d2.voyage_seq = p.next_voyage_seq "
        "WHERE p.next_voyage_id IS NOT NULL "
        f"AND abs(d2.median_draught - d1.median_draught) >= {MIN_DRAUGHT_CHANGE_M}"
    ).fetchall()

    events: list[BehaviourEvent] = []
    for (
        mmsi,
        voyage_id,
        next_voyage_id,
        end_time,
        end_lat,
        end_lon,
        next_start_time,
        next_start_lat,
        next_start_lon,
        old_draught,
        new_draught,
    ) in rows:
        if _has_anchorage_evidence(
            con, anchorages_path, mmsi, end_lat, end_lon, next_start_lat, next_start_lon
        ):
            continue
        if _has_sts_evidence(con, sts_path, mmsi, end_time, next_start_time):
            continue

        change = new_draught - old_draught
        confidence = min(1.0, abs(change) / DRAUGHT_CONFIDENCE_SCALE_M)
        events.append(
            BehaviourEvent(
                mmsi=mmsi,
                kind="draught_change_unexplained",
                event_time=end_time,
                knowable_at=window_end,
                voyage_id=voyage_id,
                latitude=end_lat,
                longitude=end_lon,
                confidence=confidence,
                evidence_value=change,
                detail=(
                    f"draught changed {old_draught:.1f}m -> {new_draught:.1f}m between voyage "
                    f"{voyage_id} and {next_voyage_id} with no port/anchorage or STS evidence "
                    "in the gap"
                ),
            )
        )
    return events


_BEHAVIOUR_EVENTS_DDL = (
    "mmsi BIGINT, kind VARCHAR, event_time TIMESTAMP, knowable_at TIMESTAMP, voyage_id VARCHAR, "
    "latitude DOUBLE, longitude DOUBLE, confidence DOUBLE, evidence_value DOUBLE, detail VARCHAR"
)


def build_behaviour_events(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    voyages_path: Path = VOYAGES_PATH,
    ports_path: Path = PORTS_PATH,
    anchorages_path: Path = ANCHORAGES_PATH,
    sts_path: Path = STS_PATH,
    out_path: Path = BEHAVIOUR_PATH,
    force: bool = False,
) -> Path:
    """Run both declared-behaviour checks over [start, end] (inclusive) and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError naming what builds each missing input -- no clean
    partitions in range: process.clean.clean_range; missing voyages_path:
    process.tracks.reconstruct_range; missing ports_path: ingest.ports.build_ports; missing
    anchorages_path: detect.anchorages.build_anchorages; missing sts_path: detect.sts.build_sts_events.

    Opens exactly one DuckDB connection and reuses it across both checks.
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
    if not ports_path.exists():
        raise FileNotFoundError(f"No ports table at {ports_path}; run ingest.ports.build_ports first")
    if not anchorages_path.exists():
        raise FileNotFoundError(
            f"No anchorage mask at {anchorages_path}; run detect.anchorages.build_anchorages first"
        )
    if not sts_path.exists():
        raise FileNotFoundError(f"No STS table at {sts_path}; run detect.sts.build_sts_events first")

    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")

        for stage_name, run_stage in (
            ("valid_imo_mmsi", lambda: _build_valid_imo_mmsi(con, partitions)),
            ("candidate_voyages", lambda: _build_candidate_voyages(con, voyages_path, start, end)),
        ):
            stage_start = datetime.now(timezone.utc)
            run_stage()
            elapsed = (datetime.now(timezone.utc) - stage_start).total_seconds()
            logger.info("Stage %s: %.1fs", stage_name, elapsed)

        n_matched_ports = _build_matched_ports(con, ports_path)
        logger.info("Matched port reference: %d port(s) in region", n_matched_ports)
        _build_voyage_draughts(con, partitions)

        # Same end-of-day convention as detect.identity_anomalies's window_end, for the same
        # reason: check 2's knowable_at is a whole-window judgement -- see module docstring.
        window_end = datetime.combine(end, datetime.max.time())

        events: list[BehaviourEvent] = []
        for name, run_check in (
            (
                "destination_course_mismatch",
                lambda: check_destination_course_mismatch(con, partitions),
            ),
            (
                "draught_change_unexplained",
                lambda: check_draught_change_unexplained(con, anchorages_path, sts_path, window_end),
            ),
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
            "Detected %d behaviour-contradiction event(s) in %s..%s: %s",
            len(events),
            start.isoformat(),
            end.isoformat(),
            counts,
        )

        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        con.execute(f"CREATE OR REPLACE TEMP TABLE _behaviour_events ({_BEHAVIOUR_EVENTS_DDL})")
        if events:
            con.executemany(
                "INSERT INTO _behaviour_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event.mmsi,
                        event.kind,
                        event.event_time,
                        event.knowable_at,
                        event.voyage_id,
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
            "FROM _behaviour_events ORDER BY mmsi, knowable_at, kind) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect declared-behaviour contradictions (destination vs course, draught vs "
        "port calls) from clean AIS."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--voyages-path", default=str(VOYAGES_PATH), help="Path to the voyages table"
    )
    parser.add_argument("--ports-path", default=str(PORTS_PATH), help="Path to the ports table")
    parser.add_argument(
        "--anchorages-path", default=str(ANCHORAGES_PATH), help="Path to the anchorage mask"
    )
    parser.add_argument("--sts-path", default=str(STS_PATH), help="Path to the STS events table")
    parser.add_argument(
        "--out-path", default=str(BEHAVIOUR_PATH), help="Output path for the behaviour events"
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
    build_behaviour_events(
        start,
        end,
        in_root=Path(args.in_dir),
        voyages_path=Path(args.voyages_path),
        ports_path=Path(args.ports_path),
        anchorages_path=Path(args.anchorages_path),
        sts_path=Path(args.sts_path),
        out_path=Path(args.out_path),
        force=args.force,
    )


if __name__ == "__main__":
    main()
