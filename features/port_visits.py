"""Per-mmsi GFW port-visit features (task P4-11), extracted once per window from
``data/reference/gfw/{vessel_ids,port_visits}.parquet`` (:mod:`ingest.gfw_port_visits`).

**Why this exists.** Danish AIS coverage cannot see a vessel once it leaves the Baltic/Skagen
approaches, so it has no direct evidence of a Russian oil-terminal port call -- the behaviour
closest to the sanctions designation reason itself. GFW's port-visits dataset has global reach
(any vessel broadcasting AIS anywhere) and lets this project ask "did this vessel call at a
Russian export terminal before the cutoff" directly, joined onto the panel by imo.

**Grain: one row per mmsi in the panel window**, mirroring ``features/static.py``'s conventions
(same ``atomic_write_parquet``/``window_partition_path`` plumbing, same window-partitioned output
layout). Columns, exactly the set frozen in ``docs/DECISIONS.md``'s 2026-09-29 P4-11
pre-registration -- **do not change these definitions**, add a new, separately named column
instead:

- ``gfw_resolved`` -- 1 if the mmsi's imo has at least one GFW vessel id (:mod:`ingest.
  gfw_port_visits` found it in ``/v3/vessels/search``), else 0. A null imo (orphaned mmsi)
  structurally can never resolve.
- ``pv_n_total`` -- count of qualifying port-visit events (see below).
- ``pv_n_rus`` -- of those, count with ``start_anchorage_flag = 'RUS'``.
- ``pv_any_rus`` -- ``pv_n_rus > 0``.
- ``pv_n_rus_oil`` -- of the RUS-flagged ones, count whose ``start_anchorage_name`` matches
  :data:`RUS_OIL_TERMINAL_REGEX` (case-insensitive).
- ``pv_days_since_rus`` -- days between the most recent qualifying RUS-flagged visit's ``end`` and
  ``window_end``, capped at 270 (the lookback bound itself); 270 when there is none.
- ``pv_share_south`` -- share of qualifying visits with ``start_anchorage_flag`` in
  (IND, TUR, CHN, ARE, EGY). **Assumption, not specified in the pre-registration:** when
  ``pv_n_total = 0`` this is defined as 0.0, not NULL/NaN -- "no visits observed" reads as "no
  south-route visits observed" rather than an undefined ratio, consistent with every other
  resolved-but-visit-less vessel getting real zeros, not missing values, in every other pv_*
  column.
- ``pv_n_sanctioned_states`` -- count of **distinct** states (out of IRN, VEN, SYR, PRK) with a
  qualifying visit. **Interpreted as distinct-country count, not visit count**, unlike
  ``pv_n_rus``/``pv_n_rus_oil`` -- the pre-registration names it "n_sanctioned_states" (states,
  plural) rather than "n_sanctioned_visits", and a same-vessel repeat visit to one already-seen
  sanctioned state adds no new information a model wants counted again. Ranges 0-4.

A "qualifying" visit (identical filter for every pv_* column above): ``confidence >= 3`` AND
``end < window_end`` (strict -- an event still ongoing, or ending exactly at the cutoff, must not
leak into a row that is knowable only as of ``window_end``) AND ``end >= window_end - 270 days``
(the longest lookback the earliest archive window allows, since GFW port-visit data starts
2023-06-01 -- see ``ingest.gfw_port_visits``).

**Unresolved imo (``gfw_resolved = 0``, including every orphaned/null-imo mmsi): every pv_*
column is NaN**, not 0 -- "never checked" must not look like "checked, found nothing" to a
downstream model doing training-median imputation (same posture as ``features/static.py``'s
declared-destination columns, though those default to FALSE/0 for a different reason: a vessel
that broadcast nothing declared nothing, which genuinely is "no", not "unknown").

**Temporal status.** Every column is knowable only once ``window_end`` has passed (the ``end <
window_end`` filter is exactly the cutoff), the same posture as ``features/static.py``.

**The RUS oil-terminal name list is frozen in the P4-11 pre-registration** (``docs/DECISIONS.md``,
2026-09-29): PRIMORSK, UST-LUGA/UST LUGA, VYSOTSK, NOVOROSSIYSK, SHESKHARIS, TAMAN, TUAPSE, KAVKAZ,
KOZMINO, NAKHODKA, DE-KASTRI/DE KASTRI, MURMANSK, SABETTA, SAINT PETERSBURG/ST PETERSBURG.

**Relabelling caveat, stated rather than hidden** (same posture as ``features/static.py``'s
``dest_russia``): a Russian-terminal port call is close to the sanctions designation reason
itself. Using it is not a leak -- GFW's port-visit history is public before designation -- but the
README must describe it that way.

All of this runs as DuckDB aggregates over ``read_parquet`` views; nothing is pulled into pandas.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.partitions import atomic_write_parquet, git_sha, window_partition_path

logger = logging.getLogger(__name__)

PANEL_ROOT = Path("data/processed/panel")
GFW_REFERENCE_ROOT = Path("data/reference/gfw")
VESSEL_IDS_PATH = GFW_REFERENCE_ROOT / "vessel_ids.parquet"
PORT_VISITS_PATH = GFW_REFERENCE_ROOT / "port_visits.parquet"
# data/processed/port_visits/window=<start>_<end>/part-0.parquet, one file per window.
PORT_VISITS_OUT_ROOT = Path("data/processed/port_visits")

MIN_CONFIDENCE = 3
LOOKBACK_DAYS = 270

SOUTH_ROUTE_FLAGS = ("IND", "TUR", "CHN", "ARE", "EGY")
SANCTIONED_STATE_FLAGS = ("IRN", "VEN", "SYR", "PRK")

# Frozen 2026-09-29 -- see module docstring before editing. RE2 syntax (DuckDB), matched against
# the upper-cased, whitespace-collapsed anchorage name, same convention as features.static's
# destination regexes.
RUS_OIL_TERMINAL_REGEX = (
    r"(PRIMORSK|UST.?LUGA|VYSOTSK|NOVOROSSIYSK|SHESKHARIS|TAMAN|TUAPSE|KAVKAZ|KOZMINO|NAKHODKA"
    r"|DE.?KASTRI|MURMANSK|SABETTA|(SAINT|ST)\.? ?PETERSBURG)"
)

# The 2-day P3-4/A4 validation window sits alongside every real window-partitioned tree -- see
# model.walk_forward._window_dirs, the same skip rule, applied here independently since this
# module has no dependency on model.walk_forward.
MIN_WINDOW_DAYS = 20


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _in_list_sql(values: tuple[str, ...]) -> str:
    return ", ".join(_sql_literal(v) for v in values)


def panel_window_dirs(panel_root: Path = PANEL_ROOT) -> list[tuple[date, date, Path]]:
    """Every ``window=<start>_<end>`` directory under ``panel_root``, sorted, excluding the 2-day
    P3-4/A4 validation window (``(end - start).days < MIN_WINDOW_DAYS``)."""
    out = []
    for d in sorted(panel_root.glob("window=*")):
        try:
            start_s, end_s = d.name.removeprefix("window=").split("_")
            start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
        except ValueError:
            logger.warning("Skipping %s: directory name is not window=<start>_<end>", d)
            continue
        if (end - start).days < MIN_WINDOW_DAYS:
            logger.info("Skipping %s: shorter than %d days (validation window)", d, MIN_WINDOW_DAYS)
            continue
        out.append((start, end, d))
    return out


def port_visits_features_sql(
    panel_sql: str, vessel_ids_sql: str, port_visits_sql: str, window_end: date
) -> str:
    """The per-mmsi aggregate for one window. Split out so tests can check the SQL against
    in-memory tables, mirroring features.static.static_features_sql's convention."""
    window_end_literal = f"DATE {_sql_literal(window_end.isoformat())}"
    return f"""
    WITH panel AS (
        SELECT mmsi, imo FROM ({panel_sql})
    ),
    resolved AS (
        SELECT DISTINCT imo FROM ({vessel_ids_sql})
    ),
    visits AS (
        SELECT imo, "end", confidence, start_anchorage_flag,
               regexp_replace(upper(trim(start_anchorage_name)), '\\s+', ' ', 'g') AS start_name
        FROM ({port_visits_sql})
        WHERE confidence >= {MIN_CONFIDENCE}
          AND "end" < {window_end_literal}
          AND "end" >= {window_end_literal} - INTERVAL '{LOOKBACK_DAYS}' DAY
    ),
    agg AS (
        SELECT
            imo,
            CAST(count(*) AS BIGINT) AS pv_n_total,
            CAST(count(*) FILTER (WHERE start_anchorage_flag = 'RUS') AS BIGINT) AS pv_n_rus,
            CAST(count(*) FILTER (
                WHERE start_anchorage_flag = 'RUS'
                  AND regexp_matches(start_name, {_sql_literal(RUS_OIL_TERMINAL_REGEX)})
            ) AS BIGINT) AS pv_n_rus_oil,
            CAST(count(*) FILTER (WHERE start_anchorage_flag IN ({_in_list_sql(SOUTH_ROUTE_FLAGS)}))
                AS BIGINT) AS pv_n_south,
            CAST(count(DISTINCT start_anchorage_flag) FILTER (
                WHERE start_anchorage_flag IN ({_in_list_sql(SANCTIONED_STATE_FLAGS)})
            ) AS BIGINT) AS pv_n_sanctioned_states,
            max("end") FILTER (WHERE start_anchorage_flag = 'RUS') AS last_rus_end
        FROM visits
        GROUP BY imo
    )
    SELECT
        p.mmsi,
        CASE WHEN r.imo IS NULL THEN 0 ELSE 1 END AS gfw_resolved,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.pv_n_total, 0) END AS pv_n_total,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.pv_n_rus, 0) END AS pv_n_rus,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.pv_n_rus, 0) > 0 END AS pv_any_rus,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.pv_n_rus_oil, 0) END AS pv_n_rus_oil,
        CASE
            WHEN r.imo IS NULL THEN NULL
            WHEN a.last_rus_end IS NULL THEN {LOOKBACK_DAYS}
            ELSE LEAST({LOOKBACK_DAYS}, date_diff('day', a.last_rus_end, {window_end_literal}))
        END AS pv_days_since_rus,
        CASE
            WHEN r.imo IS NULL THEN NULL
            WHEN coalesce(a.pv_n_total, 0) = 0 THEN 0.0
            ELSE CAST(a.pv_n_south AS DOUBLE) / a.pv_n_total
        END AS pv_share_south,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.pv_n_sanctioned_states, 0) END
            AS pv_n_sanctioned_states
    FROM panel p
    LEFT JOIN resolved r ON r.imo = p.imo
    LEFT JOIN agg a ON a.imo = p.imo
    """


def distinct_rus_anchorage_names(port_visits_path: Path = PORT_VISITS_PATH) -> list[tuple[str, bool]]:
    """Every distinct RUS-flag start-anchorage name seen anywhere in the fetched port visits
    (no confidence/date filtering -- a full inventory, not scoped to one window), each paired
    with whether :data:`RUS_OIL_TERMINAL_REGEX` matches it. Anchorage names only, no vessel data --
    see task P4-11's report requirement."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT DISTINCT
                regexp_replace(upper(trim(start_anchorage_name)), '\\s+', ' ', 'g') AS name,
                regexp_matches(
                    regexp_replace(upper(trim(start_anchorage_name)), '\\s+', ' ', 'g'),
                    {_sql_literal(RUS_OIL_TERMINAL_REGEX)}
                ) AS matched
            FROM read_parquet('{port_visits_path.as_posix()}')
            WHERE start_anchorage_flag = 'RUS' AND start_anchorage_name IS NOT NULL
            ORDER BY 1
            """
        ).fetchall()
    finally:
        con.close()
    return rows


def build_port_visits_features(
    start: date,
    end: date,
    panel_dir: Path,
    vessel_ids_path: Path = VESSEL_IDS_PATH,
    port_visits_path: Path = PORT_VISITS_PATH,
    out_root: Path = PORT_VISITS_OUT_ROOT,
    force: bool = False,
    threads: int | None = None,
) -> Path:
    """Build the pv_* features for one window, writing
    ``out_root/window=<start>_<end>/part-0.parquet``.

    Idempotent per window: a no-op if that exact window's output already exists, unless
    ``force=True``. ``threads`` caps DuckDB's parallelism, e.g. to leave CPU for a concurrent run.
    """
    out_path = window_partition_path(start, end, out_root)
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    panel_path = panel_dir / "part-0.parquet"
    if not panel_path.exists():
        raise FileNotFoundError(f"No panel partition at {panel_path}")

    con = duckdb.connect()
    try:
        if threads is not None:
            con.execute(f"SET threads = {int(threads)}")
        panel_sql = f"SELECT mmsi, imo FROM read_parquet('{panel_path.as_posix()}')"
        vessel_ids_sql = (
            f"SELECT imo, gfw_vessel_id FROM read_parquet('{vessel_ids_path.as_posix()}') "
            "WHERE use_for_features"
            if vessel_ids_path.exists()
            else "SELECT NULL::VARCHAR AS imo, NULL::VARCHAR AS gfw_vessel_id WHERE false"
        )
        port_visits_sql = (
            f"SELECT * FROM read_parquet('{port_visits_path.as_posix()}')"
            if port_visits_path.exists()
            else "SELECT NULL::VARCHAR AS imo, NULL::TIMESTAMP AS \"end\", "
            "NULL::INTEGER AS confidence, NULL::VARCHAR AS start_anchorage_flag, "
            "NULL::VARCHAR AS start_anchorage_name WHERE false"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _port_visits AS "
            + port_visits_features_sql(panel_sql, vessel_ids_sql, port_visits_sql, end)
        )
        n_mmsi, n_resolved, n_any_rus = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE gfw_resolved = 1), "
            "count(*) FILTER (WHERE pv_any_rus) FROM _port_visits"
        ).fetchone()
        logger.info(
            "Built port-visit features for %s..%s: %d mmsi (%d gfw_resolved, %d pv_any_rus)",
            start.isoformat(), end.isoformat(), n_mmsi, n_resolved, n_any_rus,
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
            "FROM _port_visits ORDER BY mmsi",
            out_path,
        )
    finally:
        con.close()
    return out_path


def build_all_windows(
    panel_root: Path = PANEL_ROOT,
    vessel_ids_path: Path = VESSEL_IDS_PATH,
    port_visits_path: Path = PORT_VISITS_PATH,
    out_root: Path = PORT_VISITS_OUT_ROOT,
    force: bool = False,
    threads: int | None = None,
) -> list[Path]:
    """Build port-visit features for every monthly panel window (skipping the 2-day one)."""
    out_paths = []
    for start, end, panel_dir in panel_window_dirs(panel_root):
        out_paths.append(
            build_port_visits_features(
                start, end, panel_dir,
                vessel_ids_path=vessel_ids_path, port_visits_path=port_visits_path,
                out_root=out_root, force=force, threads=threads,
            )
        )
    return out_paths


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build GFW port-visit features (task P4-11) for every panel window."
    )
    parser.add_argument("--panel-root", default=str(PANEL_ROOT))
    parser.add_argument("--vessel-ids-path", default=str(VESSEL_IDS_PATH))
    parser.add_argument("--port-visits-path", default=str(PORT_VISITS_PATH))
    parser.add_argument("--out-root", default=str(PORT_VISITS_OUT_ROOT))
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_all_windows(
        panel_root=Path(args.panel_root),
        vessel_ids_path=Path(args.vessel_ids_path),
        port_visits_path=Path(args.port_visits_path),
        out_root=Path(args.out_root),
        force=args.force,
        threads=args.threads,
    )


if __name__ == "__main__":
    main()
