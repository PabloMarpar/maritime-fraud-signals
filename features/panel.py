"""**INCOMPLETE FIX PASS, 2026-09-22 -- READ BEFORE TRUSTING ANYTHING BELOW ABOUT ``label_``
PREFIXES.** A post-analyst-review fix pass was interrupted mid-edit (session rate limit) after
rewriting this docstring to describe the target end-state (five ``label_``-prefixed columns, a
guard test) but BEFORE actually renaming the columns in the SQL below or in
``tests/test_panel.py``. As of right now the real output columns are still ``is_sanctioned_ever``
/ ``earliest_designation_date`` / ``is_sanctioned_as_of_window_end`` /
``is_sanctioned_after_window_end`` / ``n_sanctions_sources`` (UNPREFIXED, no ``label_`` prefix),
and no guard test exists yet. See ``docs/STATE.md``'s "In progress" section for the full list of
fixes analyst-review found and which of them (if any) actually landed. Do not assume this
docstring's prose matches the code until that note says otherwise.

Aggregate every detector, the identity table and the sanctions join into one flat
vessel-month feature/label panel -- the input Phase 4's models will train on.

**Grain: one row per (mmsi, year_month).** ``year_month`` is a DATE truncated to the first of the
calendar month (DuckDB's ``date_trunc('month', ...)`` convention), not a string, so it sorts and
filters like any other date. Right now a single call with ``window_start=2024-06-01,
window_end=2024-06-30`` produces exactly one month, 2024-06 -- but the aggregation is written
generically off the actual ``window_start``/``window_end`` arguments (:func:`_months_spanned`
truncates them to the calendar month(s) they touch), not hardcoded to June, per this project's
disk-budget notes on sampling short windows around future validation cutoffs (``docs/STATE.md``).
**This build only ever produces months for which the given window actually has data** -- it does
not, and must not, synthesize a month from data that does not exist. In the present single-window
case this reduces to exactly the single-month behaviour described below; a hypothetical future
multi-month window would reuse the SAME whole-window detector tables for every month it spans,
filtering each month's aggregates more strictly the earlier the month is -- see the temporal-cutoff
section below for why that is only a safe design for identity_anomalies/behaviour today.

**Population.** Every distinct ``mmsi`` in ``mmsi_imo_path`` (already the complete roster for the
window -- not re-derived from raw clean partitions, see ``process.identity``).

**BLOCKER for Phase 4 -- read before training anything on this panel.** ``imo IS NULL`` for 77.5%
of the panel (16,265 of 21,146 rows, real 2024-06 build, verified against the actual Parquet
directly, not copied from a prior write-up). These are "orphaned" MMSI (``is_orphaned=true``) --
the sanctions join in this module is by ``imo`` only (see "Label columns" below), so an orphaned
row can *never* be positively labelled: it is not a confirmed negative, it is **unlabelled**,
because whether that physical vessel is sanctioned is genuinely unknowable from this panel's own
join key. Training on the whole panel with orphaned rows folded in as negatives, with
``is_orphaned`` itself available as a feature, lets a model split once on ``is_orphaned`` and
capture 77% of the negative class for free, then again on ``ship_type='Tanker'``, and report a
meaningless ~0.97 AUC that is not prediction -- it is the naive baseline
("tanker over 15 years old under a flag of convenience", ``CLAUDE.md``) rediscovering its own
population-membership criterion. Real numbers (verified directly against
``data/processed/vessel_month_panel.parquet``): whole panel 21,146 rows / 147 forward-positive
(0.70%); restricted to ``imo IS NOT NULL`` 4,881 rows / 147 positive (3.01%); restricted further to
``imo IS NOT NULL AND ship_type='Tanker'`` 894 rows / 135 positive (15.1%). ``is_orphaned`` is 0%
among positives and 77.5% among negatives -- a feature that trivially predicts the labelling
restriction itself, not vessel risk. **Phase 4's modelling population MUST be restricted to
``imo IS NOT NULL`` rows.** See ``docs/STATE.md``'s open questions for the standing entry.

**Static/identity features per mmsi** (constant across every year_month row for that mmsi, since
they are derived from the whole-window identity table, not a per-month slice): ``imo`` (the
representative valid IMO, NULL if orphaned -- see "Representative IMO" below), ``is_orphaned``,
``is_reused``, ``total_message_count`` (summed across the mmsi's row(s) in ``mmsi_imo_path``),
``flag_country`` (``process.mid.country_of(mmsi)``, applied over the small distinct-mmsi roster,
never the raw message table -- cosmetic/best-effort, see ``process.mid``'s own docstring: a missing
MID mapping yields NULL, not an error), ``ship_type`` (the modal non-null ``ship_type`` broadcast
by this mmsi across the window's clean partitions, resolved by one aggregate GROUP BY + row_number
query, never a per-row Python scan), ``voyage_count`` (count of this mmsi's voyages in
``voyages_path`` within the window). **These are correct for the current single-month panel but are
computed over the WHOLE window regardless of which month a row represents -- a real, confirmed,
larger P4-3 blocker than the detector ``knowable_at`` proxy issue below (a future month's identity,
including the representative ``imo`` itself, could silently determine a past month's label). Not
fixed here; see ``docs/STATE.md``'s open questions before building a multi-month panel.**

**Representative IMO for a reused mmsi.** ``process.identity`` already records, per mmsi, every
distinct valid IMO it was ever paired with in the window, plus ``is_reused=true`` when there is more
than one. This panel needs exactly one ``imo`` value per mmsi-month (it is a join key for the
sanctions label below), so for a reused mmsi the IMO with the latest ``last_seen`` is chosen as the
representative -- an arbitrary but documented and inspectable tie-break, not a hidden one:
``is_reused`` is carried through as its own column precisely so this choice is visible rather than
silently collapsing a genuinely ambiguous identity into one number.

**Detector-derived features**, left-joined so a vessel-month with zero flagged events gets 0 in
every count column and NULL in every mean/max column, never a missing row entirely:

* ``gaps``: ``n_gaps``, ``n_gaps_high_probability`` (``probability >= HIGH_GAP_PROBABILITY`` --
  0.6, an arbitrary, documented threshold, not empirically calibrated), ``mean_gap_probability``,
  ``max_gap_duration_hours``.
* ``spoofing``: one count column per ``kind`` (``n_impossible_speed``, ``n_on_land``,
  ``n_synthetic_circle``, ``n_simultaneous_position``) plus ``n_spoofing_events_total``.
* ``sts``: ``n_sts_episodes`` (mmsi as EITHER ``mmsi_a`` or ``mmsi_b``), ``mean_sts_confidence``,
  ``max_sts_confidence``.
* ``identity_anomalies``: one count column per ``kind`` (``n_no_valid_imo``, ``n_name_change``,
  ``n_name_flapping``, ``n_callsign_change``, ``n_callsign_flapping``, ``n_reused_mmsi``,
  ``n_shared_imo``, ``n_shared_identity``) plus ``n_identity_anomalies_total``.
* ``behaviour``: ``n_destination_course_mismatch``, ``n_draught_change_unexplained``.

``sts_gfw_agreement.parquet`` (P2-7) is explicitly OUT of scope -- it is a meta-validation table
(detector-vs-GFW agreement), not a vessel risk signal, and is never read here.

**Temporal-cutoff filter on every detector join -- read this before trusting a multi-month build.**
Every event is filtered to "knowable as of this vessel-month's end" before being aggregated, but the
two detector families differ sharply in how trustworthy that cutoff actually is:

* ``identity_anomalies`` and ``behaviour`` carry a real ``knowable_at`` column, the product of a
  dedicated review pass in P2-5/P2-6 that worked out exactly when each kind of whole-window or
  gap-bounded judgement actually becomes knowable (see ``docs/DECISIONS.md``). Filtering on
  ``knowable_at <= month_end`` here is the correct, general rule. For every whole-window judgement
  (``identity_anomalies``'s 8 kinds, and ``behaviour``'s ``draught_change_unexplained``) this is a
  confirmed no-op on the real 2024-06 build: their ``knowable_at`` already equals ``window_end``
  exactly, matching P2-5/P2-6's own prior finding (``docs/DECISIONS.md``). **It is NOT a no-op for
  ``behaviour``'s gap-bounded ``destination_course_mismatch``, however** -- checked directly against
  the real build, not assumed: 6 of 152 real events have a voyage ending in the last few minutes of
  2024-06-30, so their ``knowable_at`` (``voyage.end_time + DEFAULT_GAP_HOURS``, see
  ``detect.behaviour``) lands in the small hours of 2024-07-01, strictly after ``window_end`` --
  and this filter correctly excludes exactly those 6 from the panel. A real, live demonstration
  that this filter earns its keep even within a single-month build, not just a defensive no-op.
* ``gaps``, ``spoofing`` and ``sts`` have **no ``knowable_at`` column at all.** This module filters
  them using each table's own event timestamp as the best available proxy (``gap_end`` for gaps,
  ``event_time`` for spoofing, ``end_time`` for sts). Using each event's own timestamp as a proxy is
  harmless for THIS panel -- a single month, where "the event's own timestamp" and "window_end" both
  fall inside the same one month regardless of which is used. Each detector has now actually been
  checked (not assumed) for whether this proxy is sound for a future multi-month rolling-cutoff
  panel (P4-3), with three different results:

  * ``gaps`` -- **checked and SOUND, not an open P4-3 risk.** ``detect.gaps.score_gap`` calls
    ``liveness_verdict`` with no ``as_of`` override, so it defaults to the gap's own start date
    (``gap.gap_start.date()``); ``detect.liveness``'s baseline window is
    ``[as_of - 30d, as_of)``, strictly before the gap begins, with an explicit guard raising if
    ``as_of`` is ever after ``window_start``; the corroboration read is bounded by
    ``window_end + 1h`` where ``window_end`` is the gap's own ``gap_end``. ``detect.liveness``'s own
    docstring states the caller contract explicitly: a caller attaching a verdict under a temporal
    cutoff ``T`` must stamp it with ``max(window_end, as_of)`` -- for a gap that is exactly
    ``gap_end``, precisely what this panel already filters on. Verified directly against the real
    build: 0 of 74,546 real gaps have ``gap_end > window_end``.
  * ``spoofing`` -- three of the four kinds (``impossible_speed``, ``on_land``,
    ``simultaneous_position``) are genuinely point-in-time checks; ``event_time`` is a sound
    ``knowable_at`` proxy for them, and this caveat should not be read as tainting all of
    ``spoofing``. ``synthetic_circle`` is different and **confirmed backdated, not merely
    plausible**: ``detect.spoofing.check_synthetic_circles`` stamps ``event_time = voyage_start``
    (see ``detect/spoofing.py``), so the judgement "this voyage traces a synthetic circle" -- which
    cannot actually be known until the circle has been traced -- is dated to before the voyage, and
    therefore the circle, even happened. The true earliest-knowable time is the voyage's end, not
    its start. Verified directly against the real build: 93 real ``synthetic_circle`` events use
    this convention, 2 of which fall within the final 24 hours of the window -- a live leakage risk
    for a rolling-cutoff panel, not a hypothetical one. Remains an open P4-3 blocker for this one
    kind.
  * ``sts`` -- **confirmed to be a bigger problem than a missing ``knowable_at`` column can fix by
    filtering alone.** Two independent issues in ``detect.sts``'s own scoring, both verified
    directly against ``detect/sts.py`` and the real ``sts.parquet``, not assumed: (1) the
    ``_repetition`` CTE (source of ``pair_same_place_count`` / ``score_repetition``) is a self-join
    on a spatial predicate with **no temporal ordering constraint**, so an episode's own repetition
    score already includes episodes that happen LATER in the window -- verified: 819 of 1,689 real
    episodes (48.5%) have at least one same-pair, same-place episode later in the window
    contributing to their own ``pair_same_place_count``. (2) ``_context`` (feeding
    ``departure_state_*`` into ``score_rendezvous``) reads slots up to
    ``end_time + CONTEXT_WINDOW_HOURS`` (6.0h), so an episode's true earliest-knowable time is
    ``end_time`` plus up to 6 hours, not ``end_time`` itself -- verified: 18 real episodes end
    within 6h of the window's own edge. **Filtering by a ``knowable_at`` proxy alone cannot fix
    this for ``sts``** -- the detector's own scoring would need to be re-run per cutoff (or
    ``pair_same_place_count`` made time-ordered), materially more work than adding a column. This
    remains the largest of the three open P4-3 blockers.

  Stated here AND as a standing open-question entry in ``docs/STATE.md`` so it cannot be lost as a
  code comment nobody reads before P4-3 starts.

**Label columns**, all five prefixed ``label_`` (see "Label columns are structurally separated from
features" below for why), joined by ``imo`` against this panel row's own representative ``imo``.
**Why by ``imo`` and not by ``mmsi`` directly**: not because ``sanctions_matches.parquet`` lacks an
``mmsi`` column -- it has one (``process.sanctions_match``'s own P3-2 join surfaces it, verified
directly against the real schema before writing this paragraph) -- but because a *reused* mmsi's
panel row already resolves to one *representative* imo (see "Representative IMO" above), and
joining on that same resolved ``imo`` is the key consistent with how the rest of this panel already
resolved identity, not a second, independent resolution. Verified against the real build: joining by
``mmsi`` instead of ``imo`` produces zero disagreements in this window's labels, so the choice is
about consistency of method, not a different real-world answer:

* ``label_is_sanctioned_ever``: this vessel's imo appears in ``sanctions_matches`` at all, any
  ``designation_date``.
* ``label_earliest_designation_date``: NULL if never matched.
* ``label_is_sanctioned_as_of_window_end``: ``earliest_designation_date <= window_end``. Read as
  "already under sanctions during/before this observation" -- a DIFFERENT analytical question from
  forward prediction. An already-sanctioned vessel still transiting is itself interesting, but
  conflating it with a forward-looking positive label would be wrong (a model that "predicts" a
  designation that already happened before the observation window isn't predicting anything).
* ``label_is_sanctioned_after_window_end``: ``earliest_designation_date > window_end``. This is the
  forward-looking training-target population Phase 4 will most likely want -- per P3-2's own
  finding (``docs/STATE.md``), 147 of the 164 real matched vessels were designated only after this
  window, a real forward-looking positive population, not a synthetic one.
* ``label_n_sanctions_sources``: count of distinct ``(source, source_id)`` for this imo in
  ``sanctions_matches`` (corroboration strength -- two authorities agreeing is a stronger signal
  than one).

**The final choice of which of these columns is "the" model label is deliberately deferred to
Phase 4** (most likely ``label_is_sanctioned_after_window_end`` as the positive class, with
``label_is_sanctioned_as_of_window_end`` vessels needing separate handling rather than folding into
the same negative/positive split) -- this module's job is to provide all three cleanly, not to
collapse them prematurely. **Phase 4 training code must select features as all columns except
``mmsi``, ``imo``, ``year_month``, and everything prefixed ``label_`` -- never include a
``label_*`` column in X.**

**Label columns are structurally separated from features, not just documented as such.** All five
sanctions-derived columns carry a ``label_`` prefix precisely so a feature-selection step that
forgets the prose above still fails loudly rather than silently training on the label.
Concretely, without the prefix: ``is_sanctioned_ever`` is an exact superset of the likely target
(164 = 17 + 147 -- verified directly against the real build) and would give a trivial AUC ~1.0 as a
feature; ``earliest_designation_date`` is literally the expression the labels are derived from;
``n_sanctions_sources`` is 0 for every real negative by construction (verified). A dedicated test
(``tests/test_panel.py``) enumerates the panel's actual output columns and asserts the set of
``label_``-prefixed columns is exactly these five, and that no other column name contains
"sanction" or "designat" -- so a future rename accidentally re-introducing an unprefixed label
column fails a test instead of silently landing in a future model's feature matrix.

**Explicitly out of scope, documented rather than solved:**

* **Vessel age.** No build-year field exists anywhere in this project's ingested data (confirmed
  against the real clean-partition schema before writing this module) -- this blocks P4-1's naive
  baseline ("tanker over 15 years old") exactly as specified. Not solved here; see
  ``docs/STATE.md``'s open questions.
* **Flag of convenience.** ``flag_country`` (raw MID-derived flag-state name) is provided;
  classifying which flags count as "of convenience" is P4-1's job, per ``process.mid``'s own
  docstring, not this module's.

One shared DuckDB connection is opened once and reused across the whole build, mirroring every
other module in this project.
"""

from __future__ import annotations

import argparse
import calendar
import logging
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.identity import IDENTITY_PATH
from process.mid import country_of
from process.partitions import existing_partitions
from process.sanctions_match import MATCHES_PATH
from process.tracks import VOYAGES_PATH

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
DETECT_ROOT = Path("data/detect")
PANEL_PATH = Path("data/processed/vessel_month_panel.parquet")

# Arbitrary, documented threshold -- see module docstring's gaps bullet. Not empirically
# calibrated; revisit once a labelled comparison exists.
HIGH_GAP_PROBABILITY = 0.6

IDENTITY_ANOMALY_KINDS = (
    "no_valid_imo",
    "name_change",
    "name_flapping",
    "callsign_change",
    "callsign_flapping",
    "reused_mmsi",
    "shared_imo",
    "shared_identity",
)
SPOOFING_KINDS = ("impossible_speed", "on_land", "synthetic_circle", "simultaneous_position")


def _git_sha() -> str:
    """Short git commit SHA of the working tree, or "unknown" if it can't be determined.

    Provenance metadata only, never correctness-critical -- same posture as every other module's
    own copy of this helper.
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


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition.

    Duplicated from detect.behaviour's own helper rather than imported -- every module in this
    project owns its own copy of these small helpers, see detect.behaviour's module docstring.
    """
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def _months_spanned(window_start: date, window_end: date) -> list[tuple[int, int]]:
    """(year, month) for every calendar month window_start..window_end touches, in order.

    Generic on purpose -- see module docstring. For the current single-window build this is
    always a list of length one.
    """
    months: list[tuple[int, int]] = []
    year, month = window_start.year, window_start.month
    while (year, month) <= (window_end.year, window_end.month):
        months.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def _month_end_timestamp(year: int, month: int, window_end: date) -> datetime:
    """End-of-day timestamp for `year`-`month`'s last day, capped at window_end.

    Capping matters for the last month a window touches, which may end mid-month (e.g. a window
    ending 2024-06-15 must not claim events from 2024-06-16..30 were knowable). Same end-of-day
    convention as detect.identity_anomalies/detect.behaviour's own window_end computation.
    """
    last_day = calendar.monthrange(year, month)[1]
    month_end_date = min(date(year, month, last_day), window_end)
    return datetime.combine(month_end_date, datetime.max.time())


def _build_year_months(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date
) -> None:
    """Materialize `_year_months`: (year_month DATE, month_end_ts TIMESTAMP), one row per
    calendar month the window touches.
    """
    rows = [
        (date(year, month, 1), _month_end_timestamp(year, month, window_end))
        for year, month in _months_spanned(window_start, window_end)
    ]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _year_months (year_month DATE, month_end_ts TIMESTAMP)"
    )
    con.executemany("INSERT INTO _year_months VALUES (?, ?)", rows)


def _build_roster(con: duckdb.DuckDBPyConnection, mmsi_imo_path: Path) -> None:
    """Materialize `_roster`: one row per distinct mmsi with its static identity features.

    Requires nothing. Reads only mmsi_imo.parquet, already a small table -- no raw-partition scan.
    See module docstring's "Representative IMO" section for the arg_max tie-break.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _roster AS "
        "SELECT mmsi, "
        "arg_max(imo, last_seen) FILTER (WHERE imo IS NOT NULL) AS imo, "
        "bool_or(is_orphaned) AS is_orphaned, "
        "bool_or(is_reused) AS is_reused, "
        "sum(message_count) AS total_message_count "
        f"FROM read_parquet('{mmsi_imo_path.as_posix()}') "
        "GROUP BY mmsi"
    )


def _build_flag_country(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize `_flag_country`: (mmsi, flag_country), applying process.mid.country_of over
    the small distinct-mmsi roster in Python -- never the raw message table.

    Requires `_roster`.
    """
    mmsi_rows = con.execute("SELECT mmsi FROM _roster").fetchall()
    rows = [(mmsi, country_of(mmsi)) for (mmsi,) in mmsi_rows]
    con.execute("CREATE OR REPLACE TEMP TABLE _flag_country (mmsi BIGINT, flag_country VARCHAR)")
    if rows:
        con.executemany("INSERT INTO _flag_country VALUES (?, ?)", rows)


def _build_ship_type(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Materialize `_ship_type`: (mmsi, ship_type), the modal non-null ship_type per mmsi.

    One aggregate GROUP BY + row_number query over mmsi/ship_type only -- never a per-row Python
    scan. Produces an empty table (not an error) if `partitions` is empty, matching
    detect.behaviour._build_matched_ports's own empty-input posture.
    """
    if not partitions:
        con.execute("CREATE OR REPLACE TEMP TABLE _ship_type (mmsi BIGINT, ship_type VARCHAR)")
        return
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _ship_type AS "
        "WITH counts AS ("
        "  SELECT mmsi, ship_type, count(*) AS n FROM ("
        + _partitions_union_sql(partitions, "mmsi, ship_type")
        + "  ) WHERE ship_type IS NOT NULL AND ship_type != '' "
        "  GROUP BY mmsi, ship_type"
        "), ranked AS ("
        "  SELECT mmsi, ship_type, "
        "         row_number() OVER (PARTITION BY mmsi ORDER BY n DESC, ship_type) AS rn "
        "  FROM counts"
        ") "
        "SELECT mmsi, ship_type FROM ranked WHERE rn = 1"
    )


def _build_voyage_counts(
    con: duckdb.DuckDBPyConnection, voyages_path: Path, window_start: date, window_end: date
) -> None:
    """Materialize `_voyage_counts`: (mmsi, voyage_count) within [window_start, window_end]."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _voyage_counts AS "
        "SELECT mmsi, count(*) AS voyage_count "
        f"FROM read_parquet('{voyages_path.as_posix()}') "
        "WHERE CAST(start_time AS DATE) >= ? AND CAST(start_time AS DATE) <= ? "
        "GROUP BY mmsi",
        [window_start, window_end],
    )


def _build_panel_base(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize `_panel_base`: (mmsi, year_month, month_end_ts), the panel's own grain.

    Requires `_roster` and `_year_months`. Every subsequent detector aggregate joins against this.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _panel_base AS "
        "SELECT r.mmsi, ym.year_month, ym.month_end_ts "
        "FROM _roster r CROSS JOIN _year_months ym"
    )


def _build_gaps_agg(con: duckdb.DuckDBPyConnection, gaps_path: Path) -> None:
    """Materialize `_gaps_agg`. Temporal cutoff proxy: gap_end -- see module docstring's caveat."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _gaps_agg AS "
        "SELECT pb.mmsi, pb.year_month, "
        "count(*) AS n_gaps, "
        "count(*) FILTER (WHERE g.probability >= ?) AS n_gaps_high_probability, "
        "avg(g.probability) AS mean_gap_probability, "
        "max(g.duration_hours) AS max_gap_duration_hours "
        "FROM _panel_base pb "
        f"JOIN read_parquet('{gaps_path.as_posix()}') g "
        "ON g.mmsi = pb.mmsi AND g.gap_end <= pb.month_end_ts "
        "GROUP BY pb.mmsi, pb.year_month",
        [HIGH_GAP_PROBABILITY],
    )


def _build_spoofing_agg(con: duckdb.DuckDBPyConnection, spoofing_path: Path) -> None:
    """Materialize `_spoofing_agg`. Temporal cutoff proxy: event_time -- see module docstring."""
    kind_cols = ", ".join(
        f"count(*) FILTER (WHERE s.kind = '{kind}') AS n_{kind}" for kind in SPOOFING_KINDS
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _spoofing_agg AS "
        f"SELECT pb.mmsi, pb.year_month, {kind_cols}, "
        "count(*) AS n_spoofing_events_total "
        "FROM _panel_base pb "
        f"JOIN read_parquet('{spoofing_path.as_posix()}') s "
        "ON s.mmsi = pb.mmsi AND s.event_time <= pb.month_end_ts "
        "GROUP BY pb.mmsi, pb.year_month"
    )


def _build_sts_agg(con: duckdb.DuckDBPyConnection, sts_path: Path) -> None:
    """Materialize `_sts_agg`. mmsi as EITHER side of the encounter. Temporal cutoff proxy:
    end_time -- see module docstring.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _sts_agg AS "
        "SELECT pb.mmsi, pb.year_month, "
        "count(*) AS n_sts_episodes, "
        "avg(t.confidence) AS mean_sts_confidence, "
        "max(t.confidence) AS max_sts_confidence "
        "FROM _panel_base pb "
        f"JOIN read_parquet('{sts_path.as_posix()}') t "
        "ON (t.mmsi_a = pb.mmsi OR t.mmsi_b = pb.mmsi) AND t.end_time <= pb.month_end_ts "
        "GROUP BY pb.mmsi, pb.year_month"
    )


def _build_identity_agg(con: duckdb.DuckDBPyConnection, identity_anomalies_path: Path) -> None:
    """Materialize `_identity_agg`. Real knowable_at cutoff -- see module docstring."""
    kind_cols = ", ".join(
        f"count(*) FILTER (WHERE ia.kind = '{kind}') AS n_{kind}"
        for kind in IDENTITY_ANOMALY_KINDS
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _identity_agg AS "
        f"SELECT pb.mmsi, pb.year_month, {kind_cols}, "
        "count(*) AS n_identity_anomalies_total "
        "FROM _panel_base pb "
        f"JOIN read_parquet('{identity_anomalies_path.as_posix()}') ia "
        "ON ia.mmsi = pb.mmsi AND ia.knowable_at <= pb.month_end_ts "
        "GROUP BY pb.mmsi, pb.year_month"
    )


def _build_behaviour_agg(con: duckdb.DuckDBPyConnection, behaviour_path: Path) -> None:
    """Materialize `_behaviour_agg`. Real knowable_at cutoff -- see module docstring."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _behaviour_agg AS "
        "SELECT pb.mmsi, pb.year_month, "
        "count(*) FILTER (WHERE b.kind = 'destination_course_mismatch') "
        "  AS n_destination_course_mismatch, "
        "count(*) FILTER (WHERE b.kind = 'draught_change_unexplained') "
        "  AS n_draught_change_unexplained "
        "FROM _panel_base pb "
        f"JOIN read_parquet('{behaviour_path.as_posix()}') b "
        "ON b.mmsi = pb.mmsi AND b.knowable_at <= pb.month_end_ts "
        "GROUP BY pb.mmsi, pb.year_month"
    )


def _build_sanctions_agg(con: duckdb.DuckDBPyConnection, sanctions_matches_path: Path) -> None:
    """Materialize `_sanctions_agg`: (imo, is_sanctioned_ever, earliest_designation_date,
    n_sanctions_sources), one row per distinct sanctioned imo actually matched to this window's
    AIS identities. Joined against the panel's own representative imo, not mmsi -- see module
    docstring.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _sanctions_agg AS "
        "SELECT imo, true AS is_sanctioned_ever, "
        "min(designation_date) AS earliest_designation_date, "
        "count(DISTINCT source || ':' || source_id) AS n_sanctions_sources "
        f"FROM read_parquet('{sanctions_matches_path.as_posix()}') "
        "WHERE imo IS NOT NULL "
        "GROUP BY imo"
    )


_PANEL_SELECT_SQL = (
    "SELECT pb.mmsi, pb.year_month, "
    "r.imo, r.is_orphaned, r.is_reused, r.total_message_count, "
    "fc.flag_country, st.ship_type, COALESCE(vc.voyage_count, 0) AS voyage_count, "
    "COALESCE(g.n_gaps, 0) AS n_gaps, "
    "COALESCE(g.n_gaps_high_probability, 0) AS n_gaps_high_probability, "
    "g.mean_gap_probability, g.max_gap_duration_hours, "
    "COALESCE(sp.n_impossible_speed, 0) AS n_impossible_speed, "
    "COALESCE(sp.n_on_land, 0) AS n_on_land, "
    "COALESCE(sp.n_synthetic_circle, 0) AS n_synthetic_circle, "
    "COALESCE(sp.n_simultaneous_position, 0) AS n_simultaneous_position, "
    "COALESCE(sp.n_spoofing_events_total, 0) AS n_spoofing_events_total, "
    "COALESCE(sts.n_sts_episodes, 0) AS n_sts_episodes, "
    "sts.mean_sts_confidence, sts.max_sts_confidence, "
    "COALESCE(ia.n_no_valid_imo, 0) AS n_no_valid_imo, "
    "COALESCE(ia.n_name_change, 0) AS n_name_change, "
    "COALESCE(ia.n_name_flapping, 0) AS n_name_flapping, "
    "COALESCE(ia.n_callsign_change, 0) AS n_callsign_change, "
    "COALESCE(ia.n_callsign_flapping, 0) AS n_callsign_flapping, "
    "COALESCE(ia.n_reused_mmsi, 0) AS n_reused_mmsi, "
    "COALESCE(ia.n_shared_imo, 0) AS n_shared_imo, "
    "COALESCE(ia.n_shared_identity, 0) AS n_shared_identity, "
    "COALESCE(ia.n_identity_anomalies_total, 0) AS n_identity_anomalies_total, "
    "COALESCE(bh.n_destination_course_mismatch, 0) AS n_destination_course_mismatch, "
    "COALESCE(bh.n_draught_change_unexplained, 0) AS n_draught_change_unexplained, "
    "COALESCE(sa.is_sanctioned_ever, false) AS is_sanctioned_ever, "
    "sa.earliest_designation_date, "
    "COALESCE(sa.earliest_designation_date <= ?, false) AS is_sanctioned_as_of_window_end, "
    "COALESCE(sa.earliest_designation_date > ?, false) AS is_sanctioned_after_window_end, "
    "COALESCE(sa.n_sanctions_sources, 0) AS n_sanctions_sources, "
    "? AS window_start, ? AS window_end, {built_at_sql} AS built_at, ? AS git_sha "
    "FROM _panel_base pb "
    "JOIN _roster r ON r.mmsi = pb.mmsi "
    "LEFT JOIN _flag_country fc ON fc.mmsi = pb.mmsi "
    "LEFT JOIN _ship_type st ON st.mmsi = pb.mmsi "
    "LEFT JOIN _voyage_counts vc ON vc.mmsi = pb.mmsi "
    "LEFT JOIN _gaps_agg g ON g.mmsi = pb.mmsi AND g.year_month = pb.year_month "
    "LEFT JOIN _spoofing_agg sp ON sp.mmsi = pb.mmsi AND sp.year_month = pb.year_month "
    "LEFT JOIN _sts_agg sts ON sts.mmsi = pb.mmsi AND sts.year_month = pb.year_month "
    "LEFT JOIN _identity_agg ia ON ia.mmsi = pb.mmsi AND ia.year_month = pb.year_month "
    "LEFT JOIN _behaviour_agg bh ON bh.mmsi = pb.mmsi AND bh.year_month = pb.year_month "
    "LEFT JOIN _sanctions_agg sa ON sa.imo = r.imo "
    "ORDER BY pb.mmsi, pb.year_month"
)


def build_panel(
    window_start: date,
    window_end: date,
    mmsi_imo_path: Path = IDENTITY_PATH,
    sanctions_matches_path: Path = MATCHES_PATH,
    voyages_path: Path = VOYAGES_PATH,
    detect_root: Path = DETECT_ROOT,
    clean_root: Path = CLEAN_ROOT,
    out_path: Path = PANEL_PATH,
    force: bool = False,
) -> Path:
    """Build the vessel-month panel over [window_start, window_end] and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError naming what builds each missing input: no clean
    partitions in range: process.clean.clean_range; missing mmsi_imo_path:
    process.identity.resolve_range; missing sanctions_matches_path:
    process.sanctions_match.match_sanctions; missing voyages_path: process.tracks.reconstruct_range;
    missing any of the five detector tables under detect_root: their respective build_* function
    (detect.gaps/spoofing/sts/identity_anomalies/behaviour).

    Opens exactly one DuckDB connection and reuses it across the whole build.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    if not mmsi_imo_path.exists():
        raise FileNotFoundError(
            f"No identity table at {mmsi_imo_path}; run process.identity.resolve_range first"
        )
    if not sanctions_matches_path.exists():
        raise FileNotFoundError(
            f"No sanctions matches table at {sanctions_matches_path}; run "
            "process.sanctions_match.match_sanctions first"
        )
    if not voyages_path.exists():
        raise FileNotFoundError(
            f"No voyages table at {voyages_path}; run process.tracks.reconstruct_range first"
        )
    gaps_path = detect_root / "gaps.parquet"
    spoofing_path = detect_root / "spoofing.parquet"
    sts_path = detect_root / "sts.parquet"
    identity_anomalies_path = detect_root / "identity_anomalies.parquet"
    behaviour_path = detect_root / "behaviour.parquet"
    for path, builder in (
        (gaps_path, "detect.gaps.build_gaps"),
        (spoofing_path, "detect.spoofing.build_spoofing_events"),
        (sts_path, "detect.sts.build_sts_events"),
        (identity_anomalies_path, "detect.identity_anomalies.build_identity_anomalies"),
        (behaviour_path, "detect.behaviour.build_behaviour_events"),
    ):
        if not path.exists():
            raise FileNotFoundError(f"No detector table at {path}; run {builder} first")

    partitions = existing_partitions(window_start, window_end, clean_root)
    if not partitions:
        raise FileNotFoundError(
            f"No clean partitions found for {window_start.isoformat()}..{window_end.isoformat()} "
            f"under {clean_root}; run process.clean.clean_range first"
        )

    con = duckdb.connect()
    try:
        _build_year_months(con, window_start, window_end)
        _build_roster(con, mmsi_imo_path)
        _build_flag_country(con)
        _build_ship_type(con, partitions)
        _build_voyage_counts(con, voyages_path, window_start, window_end)
        _build_panel_base(con)
        _build_gaps_agg(con, gaps_path)
        _build_spoofing_agg(con, spoofing_path)
        _build_sts_agg(con, sts_path)
        _build_identity_agg(con, identity_anomalies_path)
        _build_behaviour_agg(con, behaviour_path)
        _build_sanctions_agg(con, sanctions_matches_path)

        # Naive-timestamp-literal convention, same as detect.behaviour/detect.identity_anomalies's
        # own built_at handling: binding a timezone-aware datetime as a `?` parameter makes DuckDB
        # land a TIMESTAMPTZ column, which then requires the optional `pytz` package to read back
        # -- an avoidable runtime dependency this project does not otherwise need.
        built_at = datetime.now(timezone.utc)
        built_at_sql = f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
        git_sha = _git_sha()
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE _panel AS "
            f"{_PANEL_SELECT_SQL.format(built_at_sql=built_at_sql)}",
            [window_end, window_end, window_start, window_end, git_sha],
        )

        (n_rows,) = con.execute("SELECT count(*) FROM _panel").fetchone()
        (n_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM _panel").fetchone()
        (n_with_imo,) = con.execute("SELECT count(*) FROM _panel WHERE imo IS NOT NULL").fetchone()
        (n_orphaned,) = con.execute("SELECT count(*) FROM _panel WHERE is_orphaned").fetchone()
        (n_reused,) = con.execute("SELECT count(*) FROM _panel WHERE is_reused").fetchone()
        (n_ever, n_as_of, n_after) = con.execute(
            "SELECT count(*) FILTER (WHERE is_sanctioned_ever), "
            "count(*) FILTER (WHERE is_sanctioned_as_of_window_end), "
            "count(*) FILTER (WHERE is_sanctioned_after_window_end) FROM _panel"
        ).fetchone()
        logger.info(
            "Built panel: %d row(s), %d distinct mmsi, %d with a valid imo, %d orphaned, "
            "%d reused",
            n_rows,
            n_mmsi,
            n_with_imo,
            n_orphaned,
            n_reused,
        )
        logger.info(
            "Sanctions labels: %d ever-sanctioned, %d already sanctioned as of window_end, "
            "%d sanctioned only after window_end (the forward-looking population)",
            n_ever,
            n_as_of,
            n_after,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT * FROM _panel) TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the labelled vessel-month panel from the identity table, all five "
        "detector tables and the sanctions match table."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--mmsi-imo-path", default=str(IDENTITY_PATH), help="Path to the mmsi<->imo identity table"
    )
    parser.add_argument(
        "--sanctions-matches-path",
        default=str(MATCHES_PATH),
        help="Path to the sanctions matches table",
    )
    parser.add_argument(
        "--voyages-path", default=str(VOYAGES_PATH), help="Path to the voyages table"
    )
    parser.add_argument(
        "--detect-root", default=str(DETECT_ROOT), help="Root of the five detector tables"
    )
    parser.add_argument(
        "--clean-root", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument("--out-path", default=str(PANEL_PATH), help="Output path for the panel")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    build_panel(
        start,
        end,
        mmsi_imo_path=Path(args.mmsi_imo_path),
        sanctions_matches_path=Path(args.sanctions_matches_path),
        voyages_path=Path(args.voyages_path),
        detect_root=Path(args.detect_root),
        clean_root=Path(args.clean_root),
        out_path=Path(args.out_path),
        force=args.force,
    )


if __name__ == "__main__":
    main()
