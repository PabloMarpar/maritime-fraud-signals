"""Join sanctions-listed vessels to this project's own AIS identity table, and quantify how
often -- and how ambiguously -- they actually matched.

**This is an IMO-to-IMO join, not an "IMO and MMSI" join in the sense the task title suggests.**
A sanctions list (OFAC/EU/UK, see ``ingest/sanctions.py``) has no MMSI field at all: MMSI is a
radio-transponder identifier assigned in the field, not something a vessel registry or a
sanctions authority tracks. The IMO number is the one identifier both sides share -- it is what
``ingest.sanctions`` captures from the designating authorities, and what
``process.identity.resolve_range`` recovers from AIS Message 5 broadcasts. The join key is
therefore ``sanctions.imo = mmsi_imo.imo``; MMSI enters the *output* only as the AIS-side
identifier that comes along for the ride once an IMO matches, one row per (sanctioned-vessel-
record, matching mmsi) pair. A caller expecting a direct MMSI column on the sanctions side will
not find one, by construction, not by omission.

**Checksum validation on both sides, same logic, not reimplemented.** ``mmsi_imo.parquet`` is
already checksum-valid by construction (:mod:`process.identity` only ever writes a ``(mmsi, imo)``
pair for an ``imo`` that passed :data:`process.identity.VALID_IMO_SQL`). ``sanctions.parquet``'s
``imo`` column has never been checked: it is bare digits typed by a human at OFAC/EU/UK, and nothing
guarantees it is even structurally a real IMO. This module imports and reuses
``process.identity.VALID_IMO_SQL`` verbatim against the sanctions side too, rather than
reimplementing the checksum arithmetic a second time -- a sanctions-list data-entry typo that
happens to not even be a structurally valid IMO must not silently join against anything (or, worse,
against the wrong vessel by coincidence). Sanctions rows that fail this check are excluded from
the join and counted, not silently dropped -- see ``n_sanctions_checksum_invalid`` in the logged
summary.

**Output** (``data/identity/sanctions_matches.parquet``): one row per (sanctioned-vessel-record,
matching mmsi) pair -- ``mmsi, imo, source, source_id, vessel_name, sanctions_flag, program,
designation_date, designation_date_precision, ais_first_seen, ais_last_seen, ais_message_count,
mmsi_is_reused``. Two rows are NOT collapsed into one in two situations, both real facts worth
keeping separate:

* A sanctioned vessel observed under two different MMSI in this window (only possible if that
  IMO's own AIS history is itself ambiguous -- see ``mmsi_is_reused`` below) produces two rows.
* A vessel sanctioned by more than one source record (e.g. both OFAC and UK list the same ship,
  or a single source lists it twice under two programmes) produces one row per source record.

**Ambiguity, quantified, not silently absorbed into a single match-rate number:**

1. ``mmsi_is_reused`` (carried straight from ``mmsi_imo.parquet``) -- the matched MMSI paired with
   more than one *distinct* valid IMO somewhere in this AIS window, independent of the sanctions
   match. The AIS identity itself is already ambiguous in the data; such matches are flagged as
   lower-confidence, not dropped -- a real detector or analyst still gets to see the evidence, with
   the caveat attached rather than hidden.
2. **Corroboration**: the same ``imo`` matched by more than one sanctions source record. Not an
   error -- two authorities agreeing a vessel is sanctioned is closer to a "second opinion" that
   backs a designation. Counted per ``imo`` as the number of distinct ``(source, source_id)``
   records that produced a match, reported as corroborated-across-N-sources vs single-source.
3. **Same MMSI, two different sanctioned IMOs** -- only reachable through case 1 (a reused MMSI)
   where more than one of that MMSI's distinct IMOs independently happens to be sanctioned. This
   is the genuinely confusing case: an analyst looking up that MMSI cannot tell, from this table
   alone, which sanctioned vessel it actually is. Logged loudly (at WARNING, not INFO) if it occurs
   for real, even once; see the module's real-run note below for whether it did.
4. **Checksum-invalid sanctions IMO** -- a sanctions-list data-entry typo, excluded from the join
   (see above). Counted, not silently dropped.

**Real 2026-09-21 run over the 30-day window (2024-06-01..2024-06-30) -- NOT near-zero, unlike
P2-7's GFW comparison.** Before running, the working assumption (by analogy with P2-7's near-zero
GFW-encounters overlap) was that a narrow 30-day Danish-only window would barely intersect a global
sanctions list. That assumption turned out to be wrong, for a project-specific reason P2-7's
analogy did not carry over: the Danish straits are not just *a* shipping lane, they are *the*
chokepoint essentially all Baltic-origin (heavily Russian) crude and product oil transits (see
``docs/DECISIONS.md``'s founding scope decision) -- exactly the corridor a sanctioned-tanker
"shadow fleet" would be expected to use, unlike GFW's fishing-economy encounter dataset, which has
no particular reason to concentrate here. Real numbers: of 2,191 checksum-valid sanctioned
records, **205 (9.4%) matched at least one mmsi observed in this window, covering 164 distinct
sanctioned imo / 164 distinct mmsi** -- by source, UK 130/662 (19.6%), OFAC 75/1,527 (4.9%), EU
0/2 (0%, too small a source to read anything into). Of ambiguity kinds (1)-(4): (4) checksum-
invalid is exercised (1 of 2,192 real sanctions rows with an imo fails the checksum, excluded);
(2) corroboration is exercised substantially (41 of the 164 matched imo are listed by both OFAC
and UK, the rest single-source); (1) ``mmsi_is_reused`` and (3) the double-distinct-imo-same-mmsi
case did NOT occur in the real run (0 matches either way) -- both are implemented and unit-tested
against synthetic fixtures, not just defensive code for a case that can never happen, but this
window's 164 matched mmsi happened not to exercise them. See ``docs/DECISIONS.md`` for the full
real-run entry and the sanity checks run against it before trusting the number.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb

from ingest.sanctions import SANCTIONS_PATH
from process.identity import IDENTITY_GLOB, VALID_IMO_SQL
from process.partitions import partition_exists

logger = logging.getLogger(__name__)

MATCHES_PATH = Path("data/identity/sanctions_matches.parquet")


def _build(con: duckdb.DuckDBPyConnection, sanctions_path: Path, identity_path: Path) -> None:
    """Build the `matches` view (plus the intermediate views the summary queries read from).

    Runs as a handful of DuckDB queries over views; row-level data is never pulled into Python.
    """
    con.execute(
        "CREATE OR REPLACE VIEW sanctions_all AS "
        f"SELECT * FROM read_parquet('{sanctions_path.as_posix()}')"
    )

    con.execute(
        "CREATE OR REPLACE VIEW sanctions_valid AS "
        "SELECT * FROM sanctions_all "
        f"WHERE imo IS NOT NULL AND imo != '' AND {VALID_IMO_SQL}"
    )

    # Structurally has an imo but fails the checksum -- a likely data-entry typo on the
    # sanctions source's side, excluded from the join and counted, not silently dropped.
    #
    # Deliberately an anti-join against sanctions_valid, NOT `WHERE ... AND NOT (VALID_IMO_SQL)`:
    # the latter crashed on the real OFAC/UK data with a CAST error. VALID_IMO_SQL's checksum
    # arithmetic (CAST(substr(imo, 7, 1) AS INTEGER), etc.) only ever runs safely on a string
    # already confirmed to be 7 digits, relying on `regexp_full_match(...) AND ...` being
    # evaluated as a top-level conjunction so DuckDB short-circuits later conjuncts on rows that
    # already failed the regex -- exactly the pattern process.identity's own plain
    # `WHERE ... AND VALID_IMO_SQL` uses. Wrapping the same expression in `NOT(...)` turns it
    # into one nested boolean expression rather than a top-level WHERE conjunction, which loses
    # that short-circuit and evaluates the CAST on non-numeric substrings of malformed real
    # sanctions imo values (confirmed against the real 2026-09-21 data, not a synthetic case).
    con.execute(
        "CREATE OR REPLACE VIEW sanctions_checksum_invalid AS "
        "SELECT s.* FROM sanctions_all s "
        "WHERE s.imo IS NOT NULL AND s.imo != '' "
        "AND NOT EXISTS (SELECT 1 FROM sanctions_valid v "
        "WHERE v.source = s.source AND v.source_id = s.source_id)"
    )

    # mmsi_imo.parquet is already checksum-valid by construction (process.identity only ever
    # writes a pair for a valid imo); orphan sentinel rows (imo IS NULL) never join.
    #
    # GROUP BY (mmsi, imo), not a plain SELECT: identity_path defaults to a glob over every
    # window process.identity has built (P3-4/A2), and the same (mmsi, imo) pair legitimately
    # recurs across windows for a vessel active in more than one. Without this aggregation, the
    # later JOIN would emit one row per (sanctions record x window occurrence) instead of per
    # (sanctions record x mmsi), silently multiplying match counts once a second window exists --
    # not yet observable against today's single-window real data, but a real bug the glob default
    # would otherwise introduce silently.
    con.execute(
        "CREATE OR REPLACE VIEW identity_valid AS "
        "SELECT mmsi, imo, sum(message_count) AS message_count, "
        "min(first_seen) AS first_seen, max(last_seen) AS last_seen, bool_or(is_reused) AS is_reused "
        f"FROM read_parquet('{identity_path.as_posix()}') WHERE imo IS NOT NULL "
        "GROUP BY mmsi, imo"
    )

    con.execute(
        "CREATE OR REPLACE VIEW matches AS "
        "SELECT i.mmsi AS mmsi, s.imo AS imo, s.source AS source, s.source_id AS source_id, "
        "s.vessel_name AS vessel_name, s.flag AS sanctions_flag, s.program AS program, "
        "s.designation_date AS designation_date, "
        "s.designation_date_precision AS designation_date_precision, "
        "i.first_seen AS ais_first_seen, i.last_seen AS ais_last_seen, "
        "i.message_count AS ais_message_count, i.is_reused AS mmsi_is_reused "
        "FROM sanctions_valid s JOIN identity_valid i ON s.imo = i.imo"
    )


def match_sanctions(
    sanctions_path: Path = SANCTIONS_PATH,
    identity_path: Path = IDENTITY_GLOB,
    out_path: Path = MATCHES_PATH,
    force: bool = False,
) -> Path:
    """Join sanctions.parquet to mmsi_imo.parquet by (checksum-valid) IMO and land the result.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError naming what builds each missing input -- no sanctions
    table: ingest.sanctions.build_sanctions; no identity table: process.identity.resolve_range.
    Logs the match-rate and ambiguity summary described in the module docstring.
    ``identity_path`` defaults to a glob over every window process.identity has built
    (``process.identity.IDENTITY_GLOB``, P3-4/A2).
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    if not sanctions_path.exists():
        raise FileNotFoundError(
            f"No sanctions table at {sanctions_path}; run ingest.sanctions.build_sanctions first"
        )
    if not partition_exists(identity_path):
        raise FileNotFoundError(
            f"No identity table at {identity_path}; run process.identity.resolve_range first"
        )

    con = duckdb.connect()
    try:
        _build(con, sanctions_path, identity_path)

        (n_valid,) = con.execute("SELECT count(*) FROM sanctions_valid").fetchone()
        (n_checksum_invalid,) = con.execute(
            "SELECT count(*) FROM sanctions_checksum_invalid"
        ).fetchone()
        (n_no_imo,) = con.execute(
            "SELECT count(*) FROM sanctions_all WHERE imo IS NULL OR imo = ''"
        ).fetchone()

        (n_matched_records,) = con.execute(
            "SELECT count(DISTINCT source || ':' || source_id) FROM matches"
        ).fetchone()
        (n_matched_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM matches").fetchone()
        match_rate = n_matched_records / n_valid if n_valid else 0.0

        by_source = con.execute(
            "SELECT s.source, count(*) AS n_valid, "
            "count(DISTINCT m.source || ':' || m.source_id) AS n_matched "
            "FROM sanctions_valid s LEFT JOIN matches m "
            "ON s.source = m.source AND s.source_id = m.source_id "
            "GROUP BY s.source ORDER BY s.source"
        ).fetchall()

        (n_reused_matches,) = con.execute(
            "SELECT count(*) FROM matches WHERE mmsi_is_reused"
        ).fetchone()

        corroboration = con.execute(
            "SELECT count(*) FILTER (WHERE n_sources > 1) AS n_corroborated, "
            "count(*) FILTER (WHERE n_sources = 1) AS n_single_source "
            "FROM (SELECT imo, count(DISTINCT source || ':' || source_id) AS n_sources "
            "FROM matches GROUP BY imo)"
        ).fetchone()
        n_corroborated, n_single_source = corroboration

        ambiguous_mmsi = con.execute(
            "SELECT mmsi, count(DISTINCT imo) AS n_imo FROM matches "
            "GROUP BY mmsi HAVING count(DISTINCT imo) > 1"
        ).fetchall()

        logger.info(
            "Sanctions IMO validity: %d checksum-valid, %d checksum-invalid (excluded), "
            "%d with no imo at all",
            n_valid,
            n_checksum_invalid,
            n_no_imo,
        )
        logger.info(
            "Match rate: %d/%d sanctioned records (%.2f%%) found >=1 matching mmsi in this AIS "
            "window; %d distinct mmsi matched",
            n_matched_records,
            n_valid,
            match_rate * 100,
            n_matched_mmsi,
        )
        for source, source_n_valid, source_n_matched in by_source:
            source_rate = source_n_matched / source_n_valid if source_n_valid else 0.0
            logger.info(
                "  %s: %d/%d (%.2f%%)", source, source_n_matched, source_n_valid, source_rate * 100
            )
        logger.info(
            "Ambiguity: %d matched rows with mmsi_is_reused (lower-confidence, not dropped); "
            "%d imo corroborated across >1 sanctions source, %d single-source",
            n_reused_matches,
            n_corroborated,
            n_single_source,
        )
        if ambiguous_mmsi:
            logger.warning(
                "%d mmsi resolve to more than one distinct sanctioned imo -- genuinely "
                "ambiguous, cannot tell which sanctioned vessel this mmsi is from this table "
                "alone: %s",
                len(ambiguous_mmsi),
                [mmsi for mmsi, _n_imo in ambiguous_mmsi],
            )
        else:
            logger.info("Ambiguity: no mmsi matched more than one distinct sanctioned imo")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT * FROM matches) TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join sanctions.parquet to mmsi_imo.parquet by checksum-valid IMO; log the "
        "match-rate and ambiguity summary."
    )
    parser.add_argument(
        "--sanctions-path", default=str(SANCTIONS_PATH), help="Path to the combined sanctions table"
    )
    parser.add_argument(
        "--identity-path",
        default=str(IDENTITY_GLOB),
        help="Path or glob for the mmsi<->imo identity table(s)",
    )
    parser.add_argument("--out-path", default=str(MATCHES_PATH), help="Output path for the matches table")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    match_sanctions(
        sanctions_path=Path(args.sanctions_path),
        identity_path=Path(args.identity_path),
        out_path=Path(args.out_path),
        force=args.force,
    )


if __name__ == "__main__":
    main()
