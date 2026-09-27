"""Per-mmsi static and declared-voyage features, extracted once per window from clean AIS.

**Why this exists.** P4-0..P4-3 showed that the five behavioural detectors carry almost no signal
against the forward sanctions label in Danish waters: future-sanctioned vessels transit normally.
What does carry signal (exploratory out-of-time check, train 2024-06 -> test 2024-11, see
``docs/DECISIONS.md``'s 2026-09-27 entry) is what a vessel *is* and where it *says* it is going:
hull size (the Aframax class, ~245 m, is the shadow fleet's workhorse), laden/ballast draught
swing, and the free-text AIS destination field (Russian Baltic oil ports, the Suez/India/Turkey
export route, "for orders"). None of that survives into the panel today, and clean partitions are
eventually pruned (P3-4/A5), so this module reduces them once per window to one small row per
mmsi: ``data/processed/static/window=<start>_<end>/part-0.parquet``.

**Grain: one row per mmsi seen in the window.** Columns:

- ``n_messages`` -- every message in the window, for context only.
- ``length_m``, ``width_m`` -- median of positive declared values (``0`` means "not set" in AIS
  static data and is excluded; NULL if never set).
- ``min_draught_m``, ``max_draught_m``, ``draught_range_m`` -- extremes of positive declared
  draught and their difference. Draught is hand-keyed by the crew, so a single typo moves the
  extremes; kept as extremes (not quantiles) because that is what the frozen evaluation used.
- ``n_destinations`` -- distinct normalized destination strings, excluding blanks and the
  literal ``UNKNOWN`` placeholder.
- ``dest_russia``, ``dest_south_route``, ``dest_for_orders`` -- whether ANY normalized destination
  string in the window matches :data:`DEST_RUSSIA_REGEX`, :data:`DEST_SOUTH_ROUTE_REGEX` or
  :data:`DEST_FOR_ORDERS_REGEX`. FALSE (never NULL) when the vessel declared nothing.

**The three regexes are frozen, pre-registered on 2026-09-27 before any 2024-04..2025-02 window
beyond June/November was built.** They were written from public shadow-fleet reporting (KSE, CREA)
*after* looking at June 2024's most common tanker destinations, so June itself is not a clean test
of them; November is out-of-time but was also examined once; every other month is clean. Changing
them invalidates the walk-forward evaluation's pre-registration -- add a new, separately named
column instead.

**Temporal status.** Every column is a whole-window aggregate, knowable only once ``window_end``
has passed -- the same posture as ``detect.identity_anomalies``'s batch kinds. A model trained at
a cutoff may use a window's row only if that window ended before the cutoff.

**Relabelling caveat, stated rather than hidden.** A declared Russian-port destination is close
to the *reason* vessels get designated (carrying Russian oil). Using it is not a leak -- it is
public before designation -- but a model built on it finds "tankers in the Russian trade" and the
sanctions lists catch up with them; the README must describe it that way.

All of this runs as one DuckDB aggregate query over views; the data is never pulled into Python.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.partitions import (
    atomic_write_parquet,
    existing_partitions,
    git_sha,
    window_partition_path,
)

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
# data/processed/static/window=<start>_<end>/part-0.parquet, one file per window.
STATIC_ROOT = Path("data/processed/static")

# Frozen 2026-09-27 -- see the module docstring before editing. RE2 syntax (DuckDB), matched
# against the upper-cased, whitespace-collapsed destination.
DEST_RUSSIA_REGEX = (
    r"(\bRU ?(ULU|PRI|LED|VYS|KGD|BAL)\b|PRIMORSK|UST.?LUGA|ULUGA|PETERSBURG|VYSOTSK|RUSSIA"
    r"|GULF OF FINLAND|FINLAND GULF)"
)
DEST_SOUTH_ROUTE_REGEX = (
    r"(PORT SAID|\bEG ?(PSD|SUZ)\b|SUEZ|INDIA|\bIN ?(SIK|JGA|VAD|MUN)\b|SIKKA|VADINAR|JAMNAGAR"
    r"|ALIAGA|\bTR ?(ALI|TUT|IST)\b|TURKEY|KALAMATA|LACONIA|CEUTA|CHINA|\bCN ?[A-Z]{3}\b"
    r"|FUJAIRAH|SINGAPORE)"
)
DEST_FOR_ORDERS_REGEX = r"(FOR ORDER|\bOPL\b)"

# Normalized destinations that mean "nothing declared".
_BLANK_DESTINATIONS = ("", "UNKNOWN")


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition.

    Every module in this project owns its own copy of this small helper rather than importing
    one shared version -- see e.g. features.panel's module docstring.
    """
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def static_features_sql(source_sql: str) -> str:
    """The per-mmsi aggregate over `source_sql` (any relation with mmsi, length, width, draught,
    destination). Split out so tests can check the SQL against an in-memory table."""
    blanks = ", ".join(_sql_literal(v) for v in _BLANK_DESTINATIONS)
    return f"""
    WITH m AS (
        SELECT mmsi, length, width, draught,
               regexp_replace(upper(trim(destination)), '\\s+', ' ', 'g') AS dest
        FROM ({source_sql})
    ),
    d AS (
        SELECT mmsi, CASE WHEN dest IS NULL OR dest IN ({blanks}) THEN NULL ELSE dest END AS dest,
               length, width, draught
        FROM m
    )
    SELECT
        mmsi,
        CAST(count(*) AS BIGINT) AS n_messages,
        median(length) FILTER (WHERE length > 0) AS length_m,
        median(width) FILTER (WHERE width > 0) AS width_m,
        min(draught) FILTER (WHERE draught > 0) AS min_draught_m,
        max(draught) FILTER (WHERE draught > 0) AS max_draught_m,
        max(draught) FILTER (WHERE draught > 0) - min(draught) FILTER (WHERE draught > 0)
            AS draught_range_m,
        CAST(count(DISTINCT dest) AS BIGINT) AS n_destinations,
        coalesce(bool_or(regexp_matches(dest, {_sql_literal(DEST_RUSSIA_REGEX)})), false)
            AS dest_russia,
        coalesce(bool_or(regexp_matches(dest, {_sql_literal(DEST_SOUTH_ROUTE_REGEX)})), false)
            AS dest_south_route,
        coalesce(bool_or(regexp_matches(dest, {_sql_literal(DEST_FOR_ORDERS_REGEX)})), false)
            AS dest_for_orders
    FROM d
    GROUP BY mmsi
    """


def build_static_features(
    start: date,
    end: date,
    in_root: Path = CLEAN_ROOT,
    out_root: Path = STATIC_ROOT,
    force: bool = False,
    threads: int | None = None,
) -> Path:
    """Aggregate static/declared-voyage features per mmsi over every clean partition in
    [start, end], writing ``out_root/window=<start>_<end>/part-0.parquet``.

    Idempotent per window: if that exact window's output already exists, this is a no-op unless
    force=True. Returns the output path either way. Raises FileNotFoundError if no clean partition
    exists in the requested range; a partial range only warns (process.partitions). `threads`
    caps DuckDB's parallelism, e.g. to leave CPU for a concurrent pipeline run.
    """
    out_path = window_partition_path(start, end, out_root)
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
        if threads is not None:
            con.execute(f"SET threads = {int(threads)}")
        source = _partitions_union_sql(partitions, "mmsi, length, width, draught, destination")
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _static AS " + static_features_sql(source)
        )
        n_mmsi, n_ru, n_south, n_orders = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE dest_russia), "
            "count(*) FILTER (WHERE dest_south_route), count(*) FILTER (WHERE dest_for_orders) "
            "FROM _static"
        ).fetchone()
        logger.info(
            "Built static features: %d mmsi (%d dest_russia, %d dest_south_route, "
            "%d dest_for_orders)",
            n_mmsi,
            n_ru,
            n_south,
            n_orders,
        )

        built_at = datetime.now(timezone.utc)
        sha = git_sha()
        atomic_write_parquet(
            con,
            "SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{sha}' AS git_sha "
            "FROM _static ORDER BY mmsi",
            out_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate per-mmsi static (size, draught) and declared-destination "
        "features from clean AIS Parquet partitions for one window."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions"
    )
    parser.add_argument(
        "--out-root",
        default=str(STATIC_ROOT),
        help="Root directory for window-partitioned static feature output",
    )
    parser.add_argument(
        "--threads", type=int, default=None, help="Cap DuckDB threads (default: all cores)"
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
    build_static_features(
        start,
        end,
        in_root=Path(args.in_dir),
        out_root=Path(args.out_root),
        force=args.force,
        threads=args.threads,
    )


if __name__ == "__main__":
    main()
