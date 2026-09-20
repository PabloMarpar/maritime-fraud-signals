"""Detector 4: identity anomalies -- flag, name, callsign and MMSI changes.

**Why this exists.** The three earlier detectors (deliberate gaps, position spoofing, ship-to-ship
transfers) all ask *where* a vessel is. This one asks *who it says it is*. A vessel has two
identifiers: the MMSI, a radio-station number that can be reprogrammed with no built-in check, and
the IMO, a permanent hull number with a verifiable check digit. Legal, routine identity changes
(a real re-flagging, a genuine renaming) and evasive ones (broadcasting an unverifiable identity,
borrowing another vessel's papers) look similar in the raw data; this module surfaces the anomaly,
scoring is left as a rule-based heuristic, and separating the two is an analyst's job.

**Scope, already reframed once (see ``docs/DECISIONS.md``).** 71% of all MMSI in this project's
window never pair with a valid IMO -- but that is concentrated in Sailing/Pleasure craft, which
carry no IMO requirement, not a data-quality problem. The anomaly worth flagging is narrower: **a
Tanker or Cargo vessel that never broadcasts a valid IMO** (:func:`check_no_valid_imo`). That
reframing is not reopened here.

**Static data only, no position.** Name, callsign, IMO and ship_type ride in AIS Message 5/24
("static and voyage related data"), not in every position report, so this module reads only those
four fields (plus ``type_of_mobile`` to exclude fixed Base Station/AtoN beacons, see
``detect.liveness.MOBILE_TYPES``) -- no ``voyages.parquet``, no lat/lon.

**Performance: six independent narrow scans, not one shared table.** An earlier version of this
module funneled every check through one materialized table carrying all three normalized text
columns for all 346M messages; real-run timing showed that blew past 5 minutes without finishing
even the first check, because several checks queried it (or views chained on top of it) more than
once, each re-paying for columns the check never used. Each ``_build_*`` now reads only the 2-4
raw columns its own check needs, straight from the Parquet partitions -- mirroring
``process.identity``/``detect.spoofing``'s own per-purpose scans rather than a shared wide one.
Real 30-day run: ~254s total across the six build stages (``value_intervals`` 51s and
``identity_pairs`` 127s are the two heaviest, both grouping on a normalized text column over all
346M rows), then all six checks combined in under 1s once their small, pre-aggregated inputs
exist. Slower than the ~60s this module's own tests once assumed, but the same order of magnitude
as this project's other detectors at this data volume (P2-4's real run took 17.6 minutes) -- see
``docs/DECISIONS.md``.

**Sentinels and text normalization.** Position-report rows fill the static fields with
placeholders (``'Unknown'``, ``'Undefined'``, empty) that this project's own data profiling found
dominate raw distinct-value counts: 86% of MMSI look like they "changed callsign" before filtering
these out, versus a handful once they are. AIS Message 5 also pads text to a fixed width with
``'@'``, so ``'MAERSK'``, ``'MAERSK@@'`` and ``'  maersk '`` are one vessel's name, not three
identities, until normalized (:func:`_normalize_text_sql`). This is the leading suspect for a real
finding in this project's data: of 99 MMSI with exactly two checksum-*valid* names, 60 have them
overlapping in time -- almost certainly two receivers decoding the same padded message
differently, not a genuine flip back and forth, since only 1 of 21,146 MMSI in the same window
holds more than one checksum-valid IMO. See :func:`classify_value_intervals`.

**Temporal leakage -- read this before consuming this table.** ``CLAUDE.md`` is explicit: no
feature may be computed from information that postdates the cutoff it claims to describe. Every
event here carries two timestamps:

* ``event_time`` -- when the anomaly happened.
* ``knowable_at`` -- the earliest instant a detector reading only data up to then could have
  emitted this row *with this kind*. ``features/`` must filter on ``knowable_at``, never
  ``event_time`` -- the same contract ``detect.liveness`` already documents for its verdicts.

Four leaks are easy to miss here specifically -- the first found only by a dedicated review pass,
after an earlier draft of this module got it wrong:

1. **"Clean switch" vs "flapping" is itself a whole-window judgement, not knowable early.**
   Classifying an MMSI's value history requires seeing every interval, so a switch that happens on
   day 5 and never reverts is indistinguishable, before ``window_end``, from one that reverts on
   day 20 and turns out to be flapping. A build cannot retroactively "un-emit" a ``name_change`` it
   already wrote once day 20 arrives, so the only safe answer is that NEITHER kind is knowable
   before the whole window has been scanned: ``name_change``/``name_flapping``/
   ``callsign_change``/``callsign_flapping`` all get ``knowable_at = window_end``, never an
   intra-window timestamp such as the vessel's own last message. This mirrors
   ``detect.liveness``'s own resolution of the identical problem for its verdicts ("not knowable
   until window_end, not as_of").
2. **A pairwise event's ``knowable_at`` belongs to the pair, not to one vessel.** Two MMSI sharing
   an IMO both get ``knowable_at = max(mmsi_a's first valid broadcast, mmsi_b's first valid
   broadcast)`` -- stamping the earlier vessel with its own earlier timestamp would credit that
   vessel's record with a fact that was only true once the second vessel also broadcast it. This
   genuinely IS knowable intra-window (unlike point 1): once both sides have broadcast, the fact
   holds regardless of what happens later -- PROVIDED point 4 below is also respected.
3. **Absence is a whole-window claim too.** ``no_valid_imo`` needs the entire range to confirm a
   vessel never once broadcasts a valid IMO -- a valid IMO arriving on day 25 would undo an
   "absence" staked on an earlier last-seen date -- so it also gets ``knowable_at = window_end``,
   gated on a minimum number of static messages and observed days
   (:data:`MIN_STATIC_MESSAGES_FOR_ABSENCE`, :data:`MIN_OBSERVED_DAYS_FOR_ABSENCE`) so a Tanker
   heard twice isn't flagged as hiding anything. ``reused_mmsi`` is the one exception that does NOT
   wait for window_end: a second distinct valid IMO makes the fact true and fully knowable the
   instant it is seen, and no later evidence can undo it.
4. **A group-size gate must be evaluated as of its own knowable_at, not the window's final size.**
   :data:`MAX_IDENTITY_GROUP_SIZE` caps how many MMSI can plausibly share one real identity before
   it's treated as a slipped-through placeholder instead. Applying that cap using the group's
   EVENTUAL, full-window size would make whether an early, small pair survives depend on how many
   MORE mmsi join the same group later -- a leak in the pair's very existence, not just its
   timestamp. :func:`_expand_pairs` instead counts only members who had already broadcast the
   shared value by that specific pair's own ``knowable_at``.

Every other column (``resolved_ship_type``, flag country names, ``first_seen``/``last_seen``) is
descriptive and may legitimately postdate ``knowable_at``; only ``kind``, ``confidence`` and
``evidence_value`` are under this temporal contract. :func:`build_vessel_links`'s own
``knowable_at`` is a necessary-but-not-sufficient LOWER BOUND, not a precise cutoff -- see its
docstring.

**Row grain.** One row per ``(mmsi, kind, related_mmsi)``, never per vessel. A shared identity
between two MMSI emits two rows, one per vessel, sharing one ``pair_id``, so ``features/`` can
``GROUP BY mmsi`` without exploding pairs -- its aggregation contract is
``count(DISTINCT related_mmsi) FILTER (WHERE kind = ...)`` per kind, never a single
``n_identity_events`` count, because kinds deliberately overlap (``shared_identity`` and
``shared_imo`` are independent evidence; neither subsumes the other). ``flag_change`` is NOT a
separate kind: a cross-flag identity share is always a ``shared_imo`` row with ``cross_mid=true``,
since it is a strict subset of "same IMO, different MMSI" and a separate kind would double-count
it.

**Ship type does not gate the population via a resolved value.** 98 of 21,146 MMSI report more
than one valid ship_type; gating :func:`check_no_valid_imo` on a single "resolved" type would let
that resolution rule silently change who counts. Instead the gate is "ever validly Tanker or
Cargo" (a superset), and the resolved type (mode by message count, deterministic tie-break) is
emitted only as a descriptive column plus a confidence penalty when unstable.

**Known simplification.** :func:`build_vessel_links`'s output cannot distinguish "one hull, two
legitimate radio identities" from "one vessel spoofing another's papers" -- see that function's
docstring.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from detect.liveness import MOBILE_TYPES
from process.identity import VALID_IMO_SQL
from process.mid import country_of, mid_of
from process.partitions import existing_partitions

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
DETECT_ROOT = Path("data/detect")
IDENTITY_ANOMALIES_PATH = DETECT_ROOT / "identity_anomalies.parquet"
IDENTITY_ROOT = Path("data/identity")
VESSEL_LINKS_PATH = IDENTITY_ROOT / "vessel_links.parquet"

# Ship types required by IMO convention to carry a permanent hull number -- see module docstring's
# reframing of the orphaned-MMSI finding. Compared against the uppercased, normalized ship_type
# (see _build_ship_types), so casing here is for readability only.
IMO_REQUIRED_SHIP_TYPES = ("Tanker", "Cargo")

# no_valid_imo evidence gate -- both unvalidated defaults, see module docstring's leakage section.
MIN_STATIC_MESSAGES_FOR_ABSENCE = 20
MIN_OBSERVED_DAYS_FOR_ABSENCE = 3.0

# A checksum-valid IMO or (name, callsign) shared by more than this many MMSI is treated as a
# probable placeholder that slipped the validity filter, not evidence -- see _expand_pairs.
MAX_IDENTITY_GROUP_SIZE = 20

# AIS Message 5/24 sentinel/placeholder values, checked AFTER normalization (_normalize_text_sql),
# so every entry here is already uppercase.
TEXT_SENTINELS = ("UNKNOWN", "UNDEFINED", "N/A", "NONE", "NIL", "0")

# Base rule-based confidence per kind -- unvalidated judgement calls about relative evidential
# strength, not fitted to any labelled outcome (no identity anomaly has ever been confirmed as
# evasion in this project). See identity_confidence.
KIND_BASE_CONFIDENCE: dict[str, float] = {
    "no_valid_imo": 0.5,
    "name_change": 0.5,
    "name_flapping": 0.15,
    "callsign_change": 0.5,
    "callsign_flapping": 0.15,
    "shared_identity": 0.6,
    "shared_imo": 0.8,
    "reused_mmsi": 0.9,
}
CROSS_MID_BONUS = 0.10
SHIP_TYPE_INSTABILITY_PENALTY = 0.15


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
    """SQL expression normalizing an AIS static-data text field for distinct-value comparison.

    AIS Message 5/24 pads text fields to a fixed width with '@'; receivers/decoders trim, case and
    whitespace inconsistently. Without this, one vessel broadcasting 'MAERSK', 'MAERSK@@' and
    '  maersk ' looks like three distinct identities -- see module docstring.
    """
    return f"nullif(upper(regexp_replace(trim({col}, ' @'), '\\s+', ' ', 'g')), '')"


def _valid_text_sql(col: str) -> str:
    """WHERE-clause fragment: col (already normalized) is non-null and not a known sentinel."""
    sentinel_list = ", ".join(f"'{s}'" for s in TEXT_SENTINELS)
    return f"({col} IS NOT NULL AND {col} NOT IN ({sentinel_list}))"


@dataclass(frozen=True)
class ValueInterval:
    """One distinct normalized value an MMSI broadcast for one identity field, and its time span."""

    value: str
    min_ts: datetime
    max_ts: datetime
    n: int


@dataclass(frozen=True)
class ValuePattern:
    """The temporal shape of >=2 ValueInterval for one (mmsi, field): a clean switch or flapping.

    ``transitions`` is only meaningful for ``pattern == "clean_switch"``: one
    ``(old_value, new_value, event_time)`` per consecutive pair by ``min_ts``, where ``event_time``
    is the new value's own ``min_ts``.
    """

    pattern: str  # "clean_switch" | "flapping"
    n_values: int
    n_overlaps: int
    transitions: list[tuple[str, str, datetime]]


def classify_value_intervals(intervals: list[ValueInterval]) -> ValuePattern:
    """Classify >=2 value intervals as a clean temporal switch or as flapping (overlapping).

    Pure function, no ``con`` -- unit-testable standalone, mirroring
    ``detect.gaps.gap_probability`` and ``detect.sts.encounter_confidence``. Intervals are sorted
    by ``min_ts``; two intervals overlap if the later one's ``min_ts`` is at or before the earlier
    one's ``max_ts`` -- touching exactly counts as overlap, not a clean switch. Every pair is
    checked, not just temporally-adjacent ones, so this is exact for any number of values: two
    non-adjacent intervals that happen to overlap still mark the whole group as flapping.
    """
    if len(intervals) < 2:
        raise ValueError("classify_value_intervals requires at least 2 distinct values")
    ordered = sorted(intervals, key=lambda iv: iv.min_ts)
    n_overlaps = sum(
        1
        for i in range(len(ordered))
        for j in range(i + 1, len(ordered))
        if ordered[j].min_ts <= ordered[i].max_ts
    )
    pattern = "flapping" if n_overlaps > 0 else "clean_switch"
    transitions = [(a.value, b.value, b.min_ts) for a, b in itertools.pairwise(ordered)]
    return ValuePattern(
        pattern=pattern, n_values=len(ordered), n_overlaps=n_overlaps, transitions=transitions
    )


def identity_confidence(
    kind: str,
    *,
    cross_mid: bool = False,
    n_distinct_ship_types: int = 1,
) -> tuple[float, dict[str, float]]:
    """Combine a per-kind base rate with two cross-cutting adjustments.

    Pure function, no ``con``. Not a calibrated probability: :data:`KIND_BASE_CONFIDENCE` is a set
    of pre-registered judgement calls about relative evidential strength, not fitted to any
    labelled outcome. ``cross_mid`` only ever applies to ``shared_imo`` (spanning two flag states
    is stronger evidence than a same-flag share); ``n_distinct_ship_types`` only ever applies to
    ``no_valid_imo`` (an MMSI that cannot even hold one ship_type steady is a weaker basis for
    treating "no IMO" as deliberate, versus routine misreporting).
    """
    base = KIND_BASE_CONFIDENCE[kind]
    score_cross_mid = CROSS_MID_BONUS if cross_mid else 0.0
    score_ship_type_instability = (
        -SHIP_TYPE_INSTABILITY_PENALTY if n_distinct_ship_types > 1 else 0.0
    )
    confidence = min(1.0, max(0.0, base + score_cross_mid + score_ship_type_instability))
    components = {
        "base": base,
        "score_cross_mid": score_cross_mid,
        "score_ship_type_instability": score_ship_type_instability,
    }
    return confidence, components


@dataclass(frozen=True)
class IdentityEvent:
    """One detected identity anomaly. One row per event, never per vessel -- see module docstring.

    Most fields are meaningful only for a subset of ``kind`` values -- see the module docstring
    for which. ``confidence``/``evidence_value`` are rule-based heuristics, NOT calibrated
    probabilities. ``event_time``/``knowable_at`` are under this module's temporal-leakage
    contract; every other timestamp-shaped field may legitimately postdate ``knowable_at`` and
    must never be used as a cutoff.
    """

    mmsi: int
    kind: str
    event_time: datetime
    knowable_at: datetime
    related_mmsi: int | None
    pair_id: str | None
    identity_field: str | None
    old_value: str | None
    new_value: str | None
    shared_value: str | None
    mid: int
    related_mid: int | None
    cross_mid: bool | None
    flag_country: str | None
    related_flag_country: str | None
    resolved_ship_type: str | None
    n_distinct_ship_types: int | None
    n_distinct_values: int | None
    n_overlaps: int | None
    observed_span_days: float | None
    n_static_messages: int | None
    first_seen: datetime | None
    last_seen: datetime | None
    confidence: float
    evidence_value: float | None
    detail: str

    @property
    def event_id(self) -> str:
        suffix = f"-{self.related_mmsi}" if self.related_mmsi is not None else ""
        return f"{self.mmsi}-{self.kind}-{self.event_time.strftime('%Y%m%dT%H%M%S')}{suffix}"


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition.

    Extracted because every _build_* below needs this shape but projects a different, deliberately
    narrow column list -- see module docstring's performance note for why narrow matters.
    """
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def _mobile_types_sql() -> str:
    return ", ".join(f"'{t}'" for t in MOBILE_TYPES)


def _build_spans(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Materialize `_spans`: per-mmsi first/last message and static-message evidence count.

    Reads only mmsi/timestamp/name -- not imo/callsign/ship_type -- straight from the raw
    partitions; see module docstring's performance note for why each check reads its own narrow
    projection instead of sharing one wide intermediate table.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _spans AS "
        "WITH messages AS ("
        "  SELECT mmsi, timestamp, "
        f"         {_normalize_text_sql('name')} AS name_norm "
        "  FROM (" + _partitions_union_sql(partitions, "mmsi, timestamp, name, type_of_mobile")
        + ") "
        f"  WHERE type_of_mobile IN ({_mobile_types_sql()})"
        ") "
        "SELECT mmsi, "
        "min(timestamp) AS first_seen, "
        "max(timestamp) AS last_seen, "
        "date_diff('second', min(timestamp), max(timestamp)) / 86400.0 AS observed_span_days, "
        f"count(*) FILTER (WHERE {_valid_text_sql('name_norm')}) AS n_static_messages "
        "FROM messages "
        "GROUP BY mmsi"
    )


def _build_value_intervals(
    con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]
) -> None:
    """Materialize `_value_intervals`: per-(mmsi, field) intervals, restricted to MMSI with more
    than one distinct valid value -- the population classify_value_intervals sorts into a clean
    switch or flapping. Reads only mmsi/timestamp/name/callsign.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _value_intervals AS "
        "WITH messages AS ("
        "  SELECT mmsi, timestamp, "
        f"         {_normalize_text_sql('name')} AS name_norm, "
        f"         {_normalize_text_sql('callsign')} AS callsign_norm "
        "  FROM ("
        + _partitions_union_sql(partitions, "mmsi, timestamp, name, callsign, type_of_mobile")
        + ") "
        f"  WHERE type_of_mobile IN ({_mobile_types_sql()})"
        "), "
        "name_intervals AS ("
        "  SELECT mmsi, 'name' AS field, name_norm AS value, "
        "         min(timestamp) AS min_ts, max(timestamp) AS max_ts, count(*) AS n "
        "  FROM messages "
        f"  WHERE {_valid_text_sql('name_norm')} "
        "  GROUP BY mmsi, name_norm"
        "), "
        "callsign_intervals AS ("
        "  SELECT mmsi, 'callsign' AS field, callsign_norm AS value, "
        "         min(timestamp) AS min_ts, max(timestamp) AS max_ts, count(*) AS n "
        "  FROM messages "
        f"  WHERE {_valid_text_sql('callsign_norm')} "
        "  GROUP BY mmsi, callsign_norm"
        "), "
        "combined AS ("
        "  SELECT * FROM name_intervals UNION ALL SELECT * FROM callsign_intervals"
        "), "
        "multi AS ("
        "  SELECT mmsi, field FROM combined GROUP BY mmsi, field HAVING count(*) > 1"
        ") "
        "SELECT c.* FROM combined c JOIN multi m USING (mmsi, field)"
    )


def _build_imo_pairs(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Materialize `_mmsi_imo`: one row per (mmsi, valid imo) actually observed, with its time
    span. Reads only mmsi/timestamp/imo.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _mmsi_imo AS "
        "SELECT mmsi, imo, min(timestamp) AS min_ts, max(timestamp) AS max_ts, count(*) AS n "
        "FROM (" + _partitions_union_sql(partitions, "mmsi, timestamp, imo, type_of_mobile") + ") "
        f"WHERE type_of_mobile IN ({_mobile_types_sql()}) "
        f"AND imo IS NOT NULL AND imo != '' AND {VALID_IMO_SQL} "
        "GROUP BY mmsi, imo"
    )


def _build_identity_pairs(
    con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]
) -> None:
    """Materialize `_identity_pairs`: one row per (mmsi, valid name, valid callsign) combination.
    Reads only mmsi/timestamp/name/callsign -- a second, independent scan from
    _build_value_intervals's, deliberately: each stays a simple, self-contained query rather than
    sharing a table that would force every consumer to pay for columns it doesn't need.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _identity_pairs AS "
        "WITH messages AS ("
        "  SELECT mmsi, timestamp, "
        f"         {_normalize_text_sql('name')} AS name_norm, "
        f"         {_normalize_text_sql('callsign')} AS callsign_norm "
        "  FROM ("
        + _partitions_union_sql(partitions, "mmsi, timestamp, name, callsign, type_of_mobile")
        + ") "
        f"  WHERE type_of_mobile IN ({_mobile_types_sql()})"
        ") "
        "SELECT mmsi, name_norm, callsign_norm, "
        "min(timestamp) AS min_ts, max(timestamp) AS max_ts "
        "FROM messages "
        f"WHERE {_valid_text_sql('name_norm')} AND {_valid_text_sql('callsign_norm')} "
        "GROUP BY mmsi, name_norm, callsign_norm"
    )


def _build_ship_types(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Materialize `_ship_types`: resolved ship_type per mmsi (mode by message count,
    deterministic tie-break), plus whether the mmsi was EVER validly Tanker/Cargo -- the gate
    check_no_valid_imo uses, deliberately not the resolved type itself (see module docstring).
    Reads only mmsi/ship_type.
    """
    imo_required_sql = ", ".join(f"'{t.upper()}'" for t in IMO_REQUIRED_SHIP_TYPES)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _ship_types AS "
        "WITH messages AS ("
        "  SELECT mmsi, "
        f"         {_normalize_text_sql('ship_type')} AS ship_type_norm "
        "  FROM (" + _partitions_union_sql(partitions, "mmsi, ship_type, type_of_mobile") + ") "
        f"  WHERE type_of_mobile IN ({_mobile_types_sql()})"
        "), "
        "counts AS ("
        "  SELECT mmsi, ship_type_norm, count(*) AS n "
        "  FROM messages "
        f"  WHERE {_valid_text_sql('ship_type_norm')} "
        "  GROUP BY mmsi, ship_type_norm"
        "), "
        "resolved AS ("
        "  SELECT mmsi, ship_type_norm AS resolved_ship_type, "
        "         row_number() OVER (PARTITION BY mmsi ORDER BY n DESC, ship_type_norm ASC) AS rn "
        "  FROM counts"
        "), "
        "agg AS ("
        "  SELECT mmsi, count(*) AS n_distinct_ship_types, "
        f"         bool_or(ship_type_norm IN ({imo_required_sql})) AS ever_imo_required "
        "  FROM counts GROUP BY mmsi"
        ") "
        "SELECT a.mmsi, r.resolved_ship_type, a.n_distinct_ship_types, a.ever_imo_required "
        "FROM agg a JOIN resolved r ON a.mmsi = r.mmsi AND r.rn = 1"
    )


def _build_has_valid_imo(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize `_has_valid_imo`: the set of mmsi that broadcast at least one valid imo, ever.

    Derived from the already-materialized `_mmsi_imo` (which must be built first), not from a
    fresh scan of the raw partitions -- the validity filter has already been paid for once there.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _has_valid_imo AS SELECT DISTINCT mmsi FROM _mmsi_imo"
    )


def check_no_valid_imo(
    con: duckdb.DuckDBPyConnection, window_end: datetime
) -> list[IdentityEvent]:
    """A Tanker or Cargo vessel, well enough observed, that never broadcasts a valid IMO.

    Requires `_spans`, `_ship_types`, `_has_valid_imo`. Gated on evidence (see module docstring),
    not just on the boolean absence -- a Tanker heard twice is not a Tanker hiding its IMO.

    `knowable_at` is `window_end`, not the vessel's own `last_seen`: "never once broadcast a valid
    IMO" is an absence claim over the WHOLE range scanned, so it can only be confirmed once that
    whole range has been observed -- a valid IMO arriving on day 25 would retroactively undo an
    "absence" claim staked on an earlier last_seen. Same reasoning `detect.liveness` already
    applies to its verdicts (see its "not knowable until window_end" docstring section).
    """
    rows = con.execute(
        "SELECT s.mmsi, s.first_seen, s.last_seen, s.observed_span_days, s.n_static_messages, "
        "t.resolved_ship_type, t.n_distinct_ship_types "
        "FROM _spans s "
        "JOIN _ship_types t USING (mmsi) "
        "LEFT JOIN _has_valid_imo v USING (mmsi) "
        "WHERE t.ever_imo_required "
        "AND v.mmsi IS NULL "
        f"AND s.n_static_messages >= {MIN_STATIC_MESSAGES_FOR_ABSENCE} "
        f"AND s.observed_span_days >= {MIN_OBSERVED_DAYS_FOR_ABSENCE}"
    ).fetchall()

    events: list[IdentityEvent] = []
    for (
        mmsi,
        first_seen,
        last_seen,
        observed_span_days,
        n_static_messages,
        resolved_ship_type,
        n_distinct_ship_types,
    ) in rows:
        confidence, _ = identity_confidence(
            "no_valid_imo", n_distinct_ship_types=n_distinct_ship_types
        )
        events.append(
            IdentityEvent(
                mmsi=mmsi,
                kind="no_valid_imo",
                event_time=last_seen,
                knowable_at=window_end,
                related_mmsi=None,
                pair_id=None,
                identity_field="imo",
                old_value=None,
                new_value=None,
                shared_value=None,
                mid=mid_of(mmsi),
                related_mid=None,
                cross_mid=None,
                flag_country=country_of(mmsi),
                related_flag_country=None,
                resolved_ship_type=resolved_ship_type,
                n_distinct_ship_types=n_distinct_ship_types,
                n_distinct_values=None,
                n_overlaps=None,
                observed_span_days=observed_span_days,
                n_static_messages=n_static_messages,
                first_seen=first_seen,
                last_seen=last_seen,
                confidence=confidence,
                evidence_value=observed_span_days,
                detail=(
                    f"{resolved_ship_type} with no valid IMO over {observed_span_days:.1f} "
                    f"observed day(s) ({n_static_messages} static message(s))"
                ),
            )
        )
    return events


def _events_for_value_pattern(
    mmsi: int,
    field: str,
    intervals: list[ValueInterval],
    pattern: ValuePattern,
    window_end: datetime,
) -> list[IdentityEvent]:
    """Turn one MMSI's classified value pattern into `{field}_change` or `{field}_flapping` rows.

    `knowable_at` is `window_end`, not `last_seen`: whether this MMSI's pattern is a clean switch
    or flapping is decided from ALL of its intervals across the whole range, so a value that
    switched cleanly on day 5 and never reverted is indistinguishable, before window_end, from one
    that will revert on day 20 and turn out to be flapping. Stamping a "clean switch" row with an
    early knowable_at would let a consumer filtering on an intra-window cutoff see a name_change
    that a live detector running only up to that cutoff could not yet have safely confirmed.
    """
    first_seen = min(iv.min_ts for iv in intervals)
    last_seen = max(iv.max_ts for iv in intervals)

    if pattern.pattern == "clean_switch":
        kind = f"{field}_change"
        confidence, _ = identity_confidence(kind)
        return [
            IdentityEvent(
                mmsi=mmsi,
                kind=kind,
                event_time=event_time,
                knowable_at=window_end,
                related_mmsi=None,
                pair_id=None,
                identity_field=field,
                old_value=old_value,
                new_value=new_value,
                shared_value=None,
                mid=mid_of(mmsi),
                related_mid=None,
                cross_mid=None,
                flag_country=country_of(mmsi),
                related_flag_country=None,
                resolved_ship_type=None,
                n_distinct_ship_types=None,
                n_distinct_values=pattern.n_values,
                n_overlaps=pattern.n_overlaps,
                observed_span_days=None,
                n_static_messages=None,
                first_seen=first_seen,
                last_seen=last_seen,
                confidence=confidence,
                evidence_value=float(pattern.n_values),
                detail=f"{field} changed from {old_value!r} to {new_value!r}",
            )
            for old_value, new_value, event_time in pattern.transitions
        ]

    kind = f"{field}_flapping"
    confidence, _ = identity_confidence(kind)
    values_joined = " | ".join(iv.value for iv in sorted(intervals, key=lambda iv: iv.min_ts))
    return [
        IdentityEvent(
            mmsi=mmsi,
            kind=kind,
            event_time=last_seen,
            knowable_at=window_end,
            related_mmsi=None,
            pair_id=None,
            identity_field=field,
            old_value=None,
            new_value=None,
            shared_value=values_joined,
            mid=mid_of(mmsi),
            related_mid=None,
            cross_mid=None,
            flag_country=country_of(mmsi),
            related_flag_country=None,
            resolved_ship_type=None,
            n_distinct_ship_types=None,
            n_distinct_values=pattern.n_values,
            n_overlaps=pattern.n_overlaps,
            observed_span_days=None,
            n_static_messages=None,
            first_seen=first_seen,
            last_seen=last_seen,
            confidence=confidence,
            evidence_value=float(pattern.n_overlaps),
            detail=(
                f"{pattern.n_values} distinct {field} values overlapping in time: {values_joined}"
            ),
        )
    ]


def check_value_changes(
    con: duckdb.DuckDBPyConnection, field: str, window_end: datetime
) -> list[IdentityEvent]:
    """`{field}_change`/`{field}_flapping` events for `field` in ("name", "callsign").

    Requires `_value_intervals`.
    """
    rows = con.execute(
        "SELECT mmsi, value, min_ts, max_ts, n FROM _value_intervals "
        "WHERE field = ? ORDER BY mmsi, min_ts",
        [field],
    ).fetchall()
    events: list[IdentityEvent] = []
    for mmsi, group in itertools.groupby(rows, key=lambda r: r[0]):
        intervals = [
            ValueInterval(value=value, min_ts=min_ts, max_ts=max_ts, n=n)
            for _, value, min_ts, max_ts, n in group
        ]
        pattern = classify_value_intervals(intervals)
        events.extend(_events_for_value_pattern(mmsi, field, intervals, pattern, window_end))
    return events


def check_reused_mmsi(con: duckdb.DuckDBPyConnection) -> list[IdentityEvent]:
    """One MMSI paired with more than one distinct valid IMO: two hulls, one radio identity.

    Requires `_mmsi_imo`. Unlike name/callsign, there is no "flapping" variant of this kind: with
    only 1 of 21,146 MMSI in this project's window ever reusing an MMSI, the temporal pattern
    (clean vs interleaved) has no population to be meaningful over, so this emits a single summary
    row per mmsi regardless of ordering.

    Unlike name/callsign changes, this fact does NOT need to wait for window_end: as soon as a
    SECOND distinct valid IMO is seen for this MMSI, "this MMSI has now paired with >=2 distinct
    valid IMOs" is true and fully knowable immediately -- more IMOs arriving later only add more
    evidence, they never retroactively undo this one. So `knowable_at` equals `event_time` here.
    """
    rows = con.execute(
        "SELECT mmsi, imo, min_ts, max_ts FROM _mmsi_imo "
        "WHERE mmsi IN (SELECT mmsi FROM _mmsi_imo GROUP BY mmsi HAVING count(*) > 1) "
        "ORDER BY mmsi, min_ts"
    ).fetchall()

    events: list[IdentityEvent] = []
    for mmsi, group in itertools.groupby(rows, key=lambda r: r[0]):
        ordered = sorted(group, key=lambda r: r[2])
        imos = [r[1] for r in ordered]
        first_seen = ordered[0][2]
        last_seen = max(r[3] for r in ordered)
        event_time = ordered[1][2]
        confidence, _ = identity_confidence("reused_mmsi")
        events.append(
            IdentityEvent(
                mmsi=mmsi,
                kind="reused_mmsi",
                event_time=event_time,
                knowable_at=event_time,
                related_mmsi=None,
                pair_id=None,
                identity_field="imo",
                old_value=imos[0],
                new_value=imos[-1],
                shared_value=None,
                mid=mid_of(mmsi),
                related_mid=None,
                cross_mid=None,
                flag_country=country_of(mmsi),
                related_flag_country=None,
                resolved_ship_type=None,
                n_distinct_ship_types=None,
                n_distinct_values=len(imos),
                n_overlaps=None,
                observed_span_days=None,
                n_static_messages=None,
                first_seen=first_seen,
                last_seen=last_seen,
                confidence=confidence,
                evidence_value=float(len(imos)),
                detail=f"MMSI paired with {len(imos)} distinct valid IMOs: {' | '.join(imos)}",
            )
        )
    return events


def _expand_pairs(
    groups: dict[str, list[tuple[int, datetime]]], kind: str
) -> Iterator[tuple[int, int, str, str, datetime]]:
    """Expand each group of >=2 MMSI sharing one identity value into symmetric pair rows.

    Yields ``(mmsi, related_mmsi, key, pair_id, knowable_at)`` twice per unordered pair -- once
    from each vessel's perspective, so a caller can ``GROUP BY mmsi`` downstream without missing
    either side. ``knowable_at`` is the LATER of the two vessels' first valid broadcast of the
    shared value -- see module docstring's leakage section, point 2.

    The :data:`MAX_IDENTITY_GROUP_SIZE` gate is applied AS OF each pair's own ``knowable_at``, not
    the group's eventual full-window size: a group is sorted by first broadcast, and a pair is
    skipped only once the count of members who had ALREADY broadcast the shared value by that
    pair's own ``knowable_at`` exceeds the cap. Gating on the final size instead would make whether
    an early, small pair survives depend on how many MORE mmsi join the same group later in the
    window -- a temporal leak in the pair's very existence, not just its timestamp. Pairs formed
    before the cap was reached are kept even if the group later grows past it.
    """
    for key, members in groups.items():
        ordered = sorted(members, key=lambda member: member[1])
        skipped_any = False
        for i, (mmsi_a, ts_a) in enumerate(ordered):
            for mmsi_b, ts_b in ordered[i + 1 :]:
                knowable_at = max(ts_a, ts_b)
                size_so_far = sum(1 for _, ts in ordered if ts <= knowable_at)
                if size_so_far > MAX_IDENTITY_GROUP_SIZE:
                    skipped_any = True
                    continue
                pair_id = f"{min(mmsi_a, mmsi_b)}-{max(mmsi_a, mmsi_b)}-{key}"
                yield mmsi_a, mmsi_b, key, pair_id, knowable_at
                yield mmsi_b, mmsi_a, key, pair_id, knowable_at
        if skipped_any:
            logger.warning(
                "%s: group sharing %r exceeds MAX_IDENTITY_GROUP_SIZE=%d MMSI at some point in "
                "the window; pairs formed once that many members had already broadcast the value "
                "were skipped, earlier ones kept",
                kind,
                key,
                MAX_IDENTITY_GROUP_SIZE,
            )


def check_shared_imo(con: duckdb.DuckDBPyConnection) -> list[IdentityEvent]:
    """Same valid IMO broadcast by more than one MMSI: same hull, or one spoofing the other.

    Requires `_mmsi_imo`. `cross_mid=true` marks a pair whose MMSI also span two different flag
    states (see process.mid) -- this IS the flag_change signal; it is deliberately a column here,
    not a separate kind, since it is always a strict subset of "shares an IMO" (see module
    docstring).
    """
    rows = con.execute(
        "SELECT imo, mmsi, min_ts FROM _mmsi_imo "
        "WHERE imo IN (SELECT imo FROM _mmsi_imo GROUP BY imo HAVING count(DISTINCT mmsi) > 1) "
        "ORDER BY imo, mmsi"
    ).fetchall()
    groups: dict[str, list[tuple[int, datetime]]] = {}
    for imo, mmsi, min_ts in rows:
        groups.setdefault(imo, []).append((mmsi, min_ts))

    events: list[IdentityEvent] = []
    for mmsi, related_mmsi, imo, pair_id, knowable_at in _expand_pairs(groups, "shared_imo"):
        cross_mid = mid_of(mmsi) != mid_of(related_mmsi)
        confidence, _ = identity_confidence("shared_imo", cross_mid=cross_mid)
        events.append(
            IdentityEvent(
                mmsi=mmsi,
                kind="shared_imo",
                event_time=knowable_at,
                knowable_at=knowable_at,
                related_mmsi=related_mmsi,
                pair_id=pair_id,
                identity_field="imo",
                old_value=None,
                new_value=None,
                shared_value=imo,
                mid=mid_of(mmsi),
                related_mid=mid_of(related_mmsi),
                cross_mid=cross_mid,
                flag_country=country_of(mmsi),
                related_flag_country=country_of(related_mmsi),
                resolved_ship_type=None,
                n_distinct_ship_types=None,
                n_distinct_values=None,
                n_overlaps=None,
                observed_span_days=None,
                n_static_messages=None,
                first_seen=None,
                last_seen=None,
                confidence=confidence,
                evidence_value=float(cross_mid),
                detail=(
                    f"IMO {imo} shared with MMSI {related_mmsi}"
                    + (" (different flag state)" if cross_mid else "")
                ),
            )
        )
    return events


def check_shared_identity(con: duckdb.DuckDBPyConnection) -> list[IdentityEvent]:
    """Same valid (name, callsign) broadcast by more than one MMSI.

    Requires `_identity_pairs`. Independent evidence from `check_shared_imo`: two MMSI can share a
    declared identity while broadcasting different (or no) IMO, and vice versa -- neither kind
    subsumes the other, so both are kept (see module docstring).
    """
    rows = con.execute(
        "SELECT name_norm, callsign_norm, mmsi, min_ts FROM _identity_pairs "
        "WHERE (name_norm, callsign_norm) IN ("
        "  SELECT name_norm, callsign_norm FROM _identity_pairs "
        "  GROUP BY name_norm, callsign_norm HAVING count(DISTINCT mmsi) > 1"
        ") "
        "ORDER BY name_norm, callsign_norm, mmsi"
    ).fetchall()

    groups: dict[str, list[tuple[int, datetime]]] = {}
    labels: dict[str, tuple[str, str]] = {}
    for name_norm, callsign_norm, mmsi, min_ts in rows:
        key = f"{name_norm}\x1f{callsign_norm}"
        groups.setdefault(key, []).append((mmsi, min_ts))
        labels[key] = (name_norm, callsign_norm)

    events: list[IdentityEvent] = []
    for mmsi, related_mmsi, key, pair_id, knowable_at in _expand_pairs(groups, "shared_identity"):
        name_norm, callsign_norm = labels[key]
        confidence, _ = identity_confidence("shared_identity")
        events.append(
            IdentityEvent(
                mmsi=mmsi,
                kind="shared_identity",
                event_time=knowable_at,
                knowable_at=knowable_at,
                related_mmsi=related_mmsi,
                pair_id=pair_id,
                identity_field="name+callsign",
                old_value=None,
                new_value=None,
                shared_value=f"{name_norm} / {callsign_norm}",
                mid=mid_of(mmsi),
                related_mid=mid_of(related_mmsi),
                cross_mid=None,
                flag_country=country_of(mmsi),
                related_flag_country=country_of(related_mmsi),
                resolved_ship_type=None,
                n_distinct_ship_types=None,
                n_distinct_values=None,
                n_overlaps=None,
                observed_span_days=None,
                n_static_messages=None,
                first_seen=None,
                last_seen=None,
                confidence=confidence,
                evidence_value=None,
                detail=f"Same identity as MMSI {related_mmsi}: '{name_norm}' / '{callsign_norm}'",
            )
        )
    return events


_IDENTITY_EVENTS_DDL = (
    "event_id VARCHAR, mmsi BIGINT, kind VARCHAR, event_time TIMESTAMP, "
    "knowable_at TIMESTAMP, related_mmsi BIGINT, pair_id VARCHAR, "
    "identity_field VARCHAR, old_value VARCHAR, new_value VARCHAR, shared_value VARCHAR, "
    "mid INTEGER, related_mid INTEGER, cross_mid BOOLEAN, flag_country VARCHAR, "
    "related_flag_country VARCHAR, resolved_ship_type VARCHAR, "
    "n_distinct_ship_types INTEGER, n_distinct_values INTEGER, n_overlaps INTEGER, "
    "observed_span_days DOUBLE, n_static_messages BIGINT, first_seen TIMESTAMP, "
    "last_seen TIMESTAMP, confidence DOUBLE, evidence_value DOUBLE, detail VARCHAR"
)


def build_identity_events(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_path: Path = IDENTITY_ANOMALIES_PATH,
    force: bool = False,
) -> Path:
    """Run all eight identity-anomaly checks over [start, end] (inclusive) and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError naming process.clean.clean_range if no clean partition
    exists anywhere in the requested range.

    Reads only AIS static-data fields -- no voyages.parquet, no position -- see module docstring.
    Every event carries both event_time and knowable_at; see module docstring's temporal-leakage
    section for the contract features/ must follow.
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

    con = duckdb.connect()
    try:
        for stage_name, run_stage in (
            ("spans", lambda: _build_spans(con, partitions)),
            ("value_intervals", lambda: _build_value_intervals(con, partitions)),
            ("imo_pairs", lambda: _build_imo_pairs(con, partitions)),
            ("has_valid_imo", lambda: _build_has_valid_imo(con)),
            ("identity_pairs", lambda: _build_identity_pairs(con, partitions)),
            ("ship_types", lambda: _build_ship_types(con, partitions)),
        ):
            stage_start = datetime.now(timezone.utc)
            run_stage()
            elapsed = (datetime.now(timezone.utc) - stage_start).total_seconds()
            logger.info("Stage %s: %.1fs", stage_name, elapsed)

        window_end = datetime.combine(end, datetime.max.time())
        events: list[IdentityEvent] = []
        for name, run_check in (
            ("no_valid_imo", lambda: check_no_valid_imo(con, window_end)),
            ("name_change_or_flapping", lambda: check_value_changes(con, "name", window_end)),
            (
                "callsign_change_or_flapping",
                lambda: check_value_changes(con, "callsign", window_end),
            ),
            ("reused_mmsi", lambda: check_reused_mmsi(con)),
            ("shared_imo", lambda: check_shared_imo(con)),
            ("shared_identity", lambda: check_shared_identity(con)),
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
            "Detected %d identity-anomaly event(s) in %s..%s: %s",
            len(events),
            start.isoformat(),
            end.isoformat(),
            counts,
        )

        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        con.execute(f"CREATE OR REPLACE TEMP TABLE _identity_events ({_IDENTITY_EVENTS_DDL})")
        if events:
            con.executemany(
                "INSERT INTO _identity_events VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event.event_id,
                        event.mmsi,
                        event.kind,
                        event.event_time,
                        event.knowable_at,
                        event.related_mmsi,
                        event.pair_id,
                        event.identity_field,
                        event.old_value,
                        event.new_value,
                        event.shared_value,
                        event.mid,
                        event.related_mid,
                        event.cross_mid,
                        event.flag_country,
                        event.related_flag_country,
                        event.resolved_ship_type,
                        event.n_distinct_ship_types,
                        event.n_distinct_values,
                        event.n_overlaps,
                        event.observed_span_days,
                        event.n_static_messages,
                        event.first_seen,
                        event.last_seen,
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
            "FROM _identity_events ORDER BY mmsi, knowable_at, kind) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


@dataclass(frozen=True)
class VesselLink:
    """One MMSI's membership in a presumed-same-hull group. See build_vessel_links.

    ``knowable_at`` is the EARLIEST instant this mmsi is linked to anything at all (the minimum
    over every edge touching it) -- a necessary-but-not-sufficient lower bound on when the whole
    group's connectivity is knowable, not a precise cutoff. See build_vessel_links's docstring.
    """

    mmsi: int
    vessel_key: str
    link_basis: str
    confidence: float
    knowable_at: datetime


def build_vessel_links(
    events_path: Path = IDENTITY_ANOMALIES_PATH,
    out_path: Path = VESSEL_LINKS_PATH,
    force: bool = False,
) -> Path:
    """Group MMSI presumed to be the same hull, from this module's own shared_imo/shared_identity
    events, via union-find over the pairwise edges.

    A second, independently idempotent builder -- not a side effect of build_identity_events,
    since a builder returning one Path cannot own two outputs without making idempotency
    ambiguous. Idempotent: no-op if out_path exists unless force=True. Raises FileNotFoundError
    naming build_identity_events if events_path is missing.

    **Not a ground truth.** A shared IMO or (name, callsign) is exactly as consistent with "one
    hull, two legitimate radio identities" as with "one vessel spoofing another's papers" -- this
    builder cannot tell the two apart, and does not try to. A consumer feeding vessel_key into
    e.g. detect.liveness.liveness_verdict's exclude_mmsi is choosing to treat the group as one
    vessel for corroboration purposes; that is a conservative modelling choice, not an assertion of
    fact, and could in principle exonerate a spoofer riding on a legitimate vessel's identity
    rather than its victim. link_basis/confidence are carried through so a consumer can decide how
    much to trust a given link.

    **`knowable_at` is a lower bound, not a precise cutoff.** Each mmsi's `knowable_at` is the
    EARLIEST of its own edges' `knowable_at` values (`detect.identity_anomalies` events are
    already under that module's temporal contract). That correctly says a vessel cannot be part of
    ANY group before its own first edge exists -- but a GROUP formed by chaining several edges
    (A-B, then B-C) is only fully connected once ALL of those edges are knowable, which can be
    later than any single edge's own timestamp. A consumer applying a strict cutoff T should treat
    `knowable_at <= T` as necessary, not sufficient, for trusting a multi-hop vessel_key as of T;
    a fully rigorous point-in-time grouping would need to re-run the union-find restricted to
    edges with `knowable_at <= T`, which this builder does not do.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path
    if not events_path.exists():
        raise FileNotFoundError(
            f"No identity-anomalies table at {events_path}; run "
            "detect.identity_anomalies.build_identity_events first"
        )

    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT mmsi, related_mmsi, kind, confidence, knowable_at FROM read_parquet(?) "
            "WHERE kind IN ('shared_imo', 'shared_identity') AND related_mmsi IS NOT NULL",
            [events_path.as_posix()],
        ).fetchall()

        parent: dict[int, int] = {}

        def find(x: int) -> int:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_a] = root_b

        best_edge: dict[int, tuple[str, float]] = {}
        earliest_knowable_at: dict[int, datetime] = {}
        for mmsi, related_mmsi, kind, confidence, knowable_at in rows:
            union(mmsi, related_mmsi)
            if mmsi not in best_edge or confidence > best_edge[mmsi][1]:
                best_edge[mmsi] = (kind, confidence)
            if mmsi not in earliest_knowable_at or knowable_at < earliest_knowable_at[mmsi]:
                earliest_knowable_at[mmsi] = knowable_at

        groups: dict[int, list[int]] = {}
        for mmsi in parent:
            groups.setdefault(find(mmsi), []).append(mmsi)

        links = [
            VesselLink(
                mmsi=mmsi,
                vessel_key=f"vessel-{min(members)}",
                link_basis=best_edge[mmsi][0],
                confidence=best_edge[mmsi][1],
                knowable_at=earliest_knowable_at[mmsi],
            )
            for members in groups.values()
            for mmsi in members
        ]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _vessel_links "
            "(mmsi BIGINT, vessel_key VARCHAR, link_basis VARCHAR, confidence DOUBLE, "
            "knowable_at TIMESTAMP)"
        )
        if links:
            con.executemany(
                "INSERT INTO _vessel_links VALUES (?, ?, ?, ?, ?)",
                [
                    (link.mmsi, link.vessel_key, link.link_basis, link.confidence,
                     link.knowable_at)
                    for link in links
                ],
            )
        con.execute(
            "COPY (SELECT * FROM _vessel_links ORDER BY vessel_key, mmsi) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
        logger.info("Linked %d MMSI into %d vessel group(s)", len(links), len(groups))
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect identity anomalies (missing IMO, name/callsign changes, shared "
        "identities, flag changes) from clean AIS static data."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-path",
        default=str(IDENTITY_ANOMALIES_PATH),
        help="Output path for identity-anomaly events",
    )
    parser.add_argument(
        "--links-out-path",
        default=str(VESSEL_LINKS_PATH),
        help="Output path for the derived vessel-links table",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output(s) already exist"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    events_path = build_identity_events(
        start, end, in_root=Path(args.in_dir), out_path=Path(args.out_path), force=args.force
    )
    build_vessel_links(
        events_path=events_path, out_path=Path(args.links_out_path), force=args.force
    )


if __name__ == "__main__":
    main()
