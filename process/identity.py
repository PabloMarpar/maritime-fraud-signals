"""Resolve MMSI (radio call sign) to IMO (permanent hull number) across days.

An MMSI is assigned to a *radio station*, not a ship: it can be reprogrammed
in the field, and unlike a hull number it carries no built-in check. The IMO
number is the closer thing to a permanent vessel identity, but it is only
broadcast in periodic AIS "static and voyage related data" messages, so it is
null on most rows for a given vessel and populated on a minority of them.
This module links the two by scanning cleaned partitions over a date range
and building a table of every (mmsi, imo) pairing actually observed. Two
identity-evasion signals fall out of that table directly:

* **Orphaned MMSI**: never paired with a valid IMO in the data scanned. Not
  necessarily fraud -- many small vessels legitimately have no IMO -- but it
  means the identity can't be independently verified.
* **Reused MMSI**: paired with two or more *distinct* valid IMOs, i.e. two
  physically different vessels appear to have shared the same radio
  identifier. This is raw material for Phase 2 detector 4, not the detector
  itself: this module only records the fact of the pairing.

**IMO validation.** A real IMO ship number is 7 digits with a check digit:
weight the first six digits 7,6,5,4,3,2, sum the products, and the sum's last
digit must equal the 7th digit. This is applied in SQL (see VALID_IMO_SQL)
against the distinct set of non-null ``imo`` strings only, not the raw
10M-row column, so the validation itself is cheap regardless of how many
messages carry that imo. The all-zero string ``0000000`` is excluded
explicitly: it passes the checksum trivially (0 == 0) but is a well-known
placeholder, not a real vessel. Structurally wrong values (wrong digit
count, non-numeric like the DMA source's ``Unknown`` sentinel, or a correct
digit count with a wrong check digit such as the widely-used test number
``1193046``) are rejected by the regex/checksum without needing a denylist.

**Output shape.** Unlike ``ingest.dma`` / ``process.clean``, this is not
written as a Hive-partitioned "one file per day" dataset: a (mmsi, imo)
pairing is a cross-date fact (an MMSI orphaned on day 1 might pair with an
IMO on day 40), so partitioning by date would scatter a single entity's
history across files and make "is this MMSI reused" a multi-file query
instead of a single-table one. Instead the whole date range asked for is
resolved into one flat table at ``data/identity/mmsi_imo.parquet``, rebuilt
atomically each time (like a materialized view, not an append log). There is
one row per (mmsi, imo) pair with a valid imo actually observed, plus one
extra sentinel row per orphaned mmsi (imo IS NULL) so that "every mmsi seen"
can be recovered from this table alone, without re-scanning the clean
partitions. Because of this cross-date design, there is deliberately no
``resolve_day`` counterpart to ``clean_day``/``download_day``: a single day
is simply a range of length one, passed to ``resolve_range``.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import duckdb

from process.partitions import existing_partitions

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
IDENTITY_PATH = Path("data/identity/mmsi_imo.parquet")

# A valid IMO ship number is exactly 7 digits, is not the all-zero
# placeholder, and satisfies the check-digit formula: weight digits 1-6 by
# 7,6,5,4,3,2, sum the products, and the sum modulo 10 must equal digit 7.
# Applied against the (small) set of distinct imo strings, never the raw
# message-level column -- see module docstring.
VALID_IMO_SQL = """
regexp_full_match(imo, '^[0-9]{7}$')
AND imo != '0000000'
AND (
    (CAST(substr(imo, 1, 1) AS INTEGER) * 7
   + CAST(substr(imo, 2, 1) AS INTEGER) * 6
   + CAST(substr(imo, 3, 1) AS INTEGER) * 5
   + CAST(substr(imo, 4, 1) AS INTEGER) * 4
   + CAST(substr(imo, 5, 1) AS INTEGER) * 3
   + CAST(substr(imo, 6, 1) AS INTEGER) * 2) % 10
    = CAST(substr(imo, 7, 1) AS INTEGER)
)
"""


def _resolve(con: duckdb.DuckDBPyConnection, partitions: list[tuple[date, Path]]) -> None:
    """Build the `final` view: one row per (mmsi, imo) pair plus orphan sentinels.

    Runs as a handful of DuckDB queries over views; row-level data is never
    pulled into Python, and the only computation DuckDB does per-message is
    the cheap regex/arithmetic checksum, not a Python UDF.
    """
    per_day = [
        f"SELECT mmsi, imo, DATE '{day.isoformat()}' AS obs_date "
        f"FROM read_parquet('{path.as_posix()}')"
        for day, path in partitions
    ]
    con.execute(f"CREATE OR REPLACE VIEW all_days AS {' UNION ALL '.join(per_day)}")

    con.execute(
        "CREATE OR REPLACE VIEW candidates AS "
        "SELECT mmsi, imo, obs_date FROM all_days WHERE mmsi IS NOT NULL"
    )

    con.execute(
        "CREATE OR REPLACE VIEW valid_pairs AS "
        "SELECT mmsi, imo, obs_date FROM candidates "
        f"WHERE imo IS NOT NULL AND imo != '' AND {VALID_IMO_SQL}"
    )

    con.execute(
        "CREATE OR REPLACE VIEW pair_agg AS "
        "SELECT mmsi, imo, count(*) AS message_count, "
        "min(obs_date) AS first_seen, max(obs_date) AS last_seen "
        "FROM valid_pairs GROUP BY mmsi, imo"
    )

    # MMSIs present in the data that never once paired with a valid imo.
    con.execute(
        "CREATE OR REPLACE VIEW orphan_mmsi AS "
        "SELECT c.mmsi, count(*) AS message_count, "
        "min(c.obs_date) AS first_seen, max(c.obs_date) AS last_seen "
        "FROM candidates c "
        "LEFT JOIN (SELECT DISTINCT mmsi FROM pair_agg) p ON c.mmsi = p.mmsi "
        "WHERE p.mmsi IS NULL "
        "GROUP BY c.mmsi"
    )

    con.execute(
        "CREATE OR REPLACE VIEW resolved AS "
        "SELECT mmsi, imo, message_count, first_seen, last_seen FROM pair_agg "
        "UNION ALL "
        "SELECT mmsi, CAST(NULL AS VARCHAR) AS imo, message_count, first_seen, last_seen "
        "FROM orphan_mmsi"
    )

    # count(imo) ignores NULLs, so per mmsi it counts exactly the number of
    # distinct valid-imo rows for that mmsi (0 for an orphan-only mmsi,
    # since pair_agg is already deduplicated to one row per (mmsi, imo)).
    con.execute(
        "CREATE OR REPLACE VIEW final AS "
        "SELECT mmsi, imo, message_count, first_seen, last_seen, "
        "imo IS NULL AS is_orphaned, "
        "count(imo) OVER (PARTITION BY mmsi) > 1 AS is_reused "
        "FROM resolved"
    )


def resolve_range(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_path: Path = IDENTITY_PATH,
    force: bool = False,
) -> Path:
    """Build the MMSI<->IMO identity table for every clean partition in [start, end].

    Idempotent: if out_path already exists, this is a no-op unless
    force=True. Returns out_path either way. Raises FileNotFoundError if no
    clean partition exists anywhere in the requested range (an empty result
    would silently look like "nothing to see here" rather than "nothing was
    read"); a partial range with some days missing only warns, see
    process.partitions.existing_partitions.
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
        _resolve(con, partitions)

        (n_mmsi,) = con.execute("SELECT count(DISTINCT mmsi) FROM final").fetchone()
        (n_with_imo,) = con.execute(
            "SELECT count(DISTINCT mmsi) FROM final WHERE NOT is_orphaned"
        ).fetchone()
        (n_orphaned,) = con.execute(
            "SELECT count(*) FROM final WHERE is_orphaned"
        ).fetchone()
        (n_reused,) = con.execute(
            "SELECT count(DISTINCT mmsi) FROM final WHERE is_reused"
        ).fetchone()
        logger.info(
            "Resolved %d distinct MMSI over %d day(s): %d with a valid IMO, "
            "%d orphaned, %d reused",
            n_mmsi,
            len(partitions),
            n_with_imo,
            n_orphaned,
            n_reused,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY (SELECT * FROM final) TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve MMSI<->IMO identity links from clean AIS Parquet partitions."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-path", default=str(IDENTITY_PATH), help="Output path for the identity table"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    resolve_range(
        start, end, in_root=Path(args.in_dir), out_path=Path(args.out_path), force=args.force
    )


if __name__ == "__main__":
    main()
