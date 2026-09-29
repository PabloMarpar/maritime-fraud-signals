"""Per-mmsi cross-month vessel history features (task P4-12), from GFW self-reported identity
segments (``data/reference/gfw/vessel_ids.parquet``, :mod:`ingest.gfw_port_visits`) and this
archive's own monthly panels (``data/processed/panel``, ``data/processed/static``).

**Why this exists.** A single month of Danish AIS tells nothing about a vessel's flag/name churn
or its longer trading pattern. GFW's self-reported identity history (flag, name, MMSI/``ssvid``
per date range, global reach) and this project's own multi-month archive both carry that
information -- pre-registered in ``docs/DECISIONS.md``'s 2026-09-29 P4-12 entry, which this module
implements exactly. **Do not change these definitions**; add a new, separately named column
instead.

**Grain: one row per mmsi in the panel window**, mirroring ``features/static.py`` and
``features/port_visits.py``'s conventions (same ``atomic_write_parquet``/``window_partition_path``
plumbing, same window-partitioned output layout). Columns:

- ``hist_n_flags_730d``, ``hist_n_names_730d``, ``hist_n_mmsi_730d`` -- distinct flag / shipname /
  ssvid among GFW identity segments active at any time in ``[window_end - 730d, window_end)``.
- ``hist_flag_age_days`` -- days from the start of the current flag run to ``window_end``, capped
  at 1,825. The current flag run starts at the latest segment (by ``transmissionDateFrom``) whose
  flag differs from the segment immediately before it in that ordering; if the flag never changed,
  it starts at the earliest segment.
- ``hist_ais_age_days`` -- ``window_end`` minus the earliest ``transmissionDateFrom``, capped at
  3,650.
- ``hist_to_foc_730d`` -- true if, inside the 730-day window, a segment under a non-FOC flag is
  immediately followed (in the same ordering) by one under an FOC flag, at that switch segment's
  own ``transmissionDateFrom``. FOC-vs-not is decided by :data:`FOC_ISO3` (built from
  ``process.foc``, see its guard test below); an unknown/null flag is never FOC, matching
  ``process.foc.is_foc``'s ``None`` handling.
- ``hist_prior_windows_seen`` -- share of the previous (up to) 6 monthly panel windows (the ones
  immediately preceding W in the built archive, NOT limited to the 730-day span above) in which
  the imo appears in that window's panel. NaN when W is the first built window.
- ``hist_prior_dest_russia`` -- true if any of those same previous windows has
  ``features.static``'s ``dest_russia`` true for any mmsi mapped to the imo in that window. NaN
  when W is the first built window (same condition as ``hist_prior_windows_seen``, not tied to
  GFW resolution at all -- these two columns come from this project's own archive).

**IMOs GFW does not resolve** (no row at all in ``vessel_ids.parquet``): every GFW-derived column
(``hist_n_flags_730d`` .. ``hist_to_foc_730d``) is NaN. The two ``hist_prior_*`` columns are
independent of GFW resolution and follow their own NaN rule above.

**A GFW-resolved imo with no qualifying segment before window_end** (e.g. its earliest
``transmissionDateFrom`` in GFW's data is after this window's ``window_end``): the three distinct
counts read 0 (nothing observed, not "unknown" -- consistent with ``features.static``/
``features.port_visits``'s "resolved but nothing found -> real zero" posture), ``hist_to_foc_730d``
is false, but ``hist_flag_age_days``/``hist_ais_age_days`` are NaN (no segment to measure an age
from at all). An assumption, not explicit in the pre-registration.

**Segment clipping and filtering, exactly as pre-registered.** Only segments with
``transmissionDateFrom < window_end`` are read at all; a kept segment's effective end is
``LEAST(transmissionDateTo, window_end)`` (or ``window_end`` itself when ``transmissionDateTo`` is
null, i.e. "still active") -- a ``transmissionDateTo`` after ``window_end`` is never read.
"Active at any time in [window_end - 730d, window_end)" is decided against this clipped end, so a
segment straddling ``window_end`` still counts (clipped, not excluded) and a segment that starts
only after ``window_end`` is excluded outright by the first filter.

**Tie-break for overlapping/simultaneous segments.** Every ordering here (the flag run, the
flag-change detection) sorts by ``transmissionDateFrom`` ascending, then ``gfw_vessel_id``
ascending as a deterministic tie-break for the rare case of two segments sharing the exact same
``transmissionDateFrom``. Segments whose date ranges overlap without sharing a start date are
already ordered deterministically by start date alone; the tie-break only matters for equal start
dates.

**Not features** (per the pre-registration): flag/name changes after ``window_end`` (they mostly
follow designation, per CREA reporting) and GFW ``registryInfo`` (ownership data compiled with
later knowledge) -- neither is read here at all.

All of this runs as DuckDB aggregates over ``read_parquet`` views; nothing is pulled into pandas.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from process.foc import FOC_NAME_TO_MID_COUNTRY
from process.partitions import atomic_write_parquet, git_sha, window_partition_path

logger = logging.getLogger(__name__)

PANEL_ROOT = Path("data/processed/panel")
STATIC_ROOT = Path("data/processed/static")
GFW_REFERENCE_ROOT = Path("data/reference/gfw")
VESSEL_IDS_PATH = GFW_REFERENCE_ROOT / "vessel_ids.parquet"
# data/processed/history/window=<start>_<end>/part-0.parquet, one file per window.
HISTORY_OUT_ROOT = Path("data/processed/history")

FLAG_AGE_CAP_DAYS = 1825
AIS_AGE_CAP_DAYS = 3650
LOOKBACK_DAYS = 730
MAX_PRIOR_WINDOWS = 6

# The 2-day P3-4/A4 validation window sits alongside every real window-partitioned tree -- same
# skip rule as features.port_visits.panel_window_dirs, reproduced here independently (this module
# has no dependency on that one, matching this project's per-module convention, see
# features/static.py's module docstring).
MIN_WINDOW_DAYS = 20

# ISO3166-1 alpha-3 flag codes for process.foc's FOC country names -- GFW's own `flag` field is
# ISO3, while process.foc classifies by process.mid.MID_COUNTRY's country-name strings, so this
# mapping has to be built by hand rather than guessed at runtime (P4-12 pre-registration).
# "Madeira" has no ISO3166 code of its own (it flies Portugal's "PRT", which also covers
# Portugal's ordinary, non-FOC flag) -- mapping it to PRT would misclassify every ordinary
# Portugal-flagged vessel as FOC, so it is deliberately left out and recorded in
# HIST_FOC_UNMAPPED, mirroring process.foc's own UNMAPPED_ITF_REGISTRIES pattern rather than
# silently dropping it.
FOC_COUNTRY_TO_ISO3: dict[str, str] = {
    "Antigua and Barbuda": "ATG",
    "Bahamas": "BHS",
    "Barbados": "BRB",
    "Belize": "BLZ",
    "Bermuda": "BMU",
    "Bolivia": "BOL",
    "Cameroon": "CMR",
    "Cayman Islands": "CYM",
    "Comoros": "COM",
    "Cook Islands": "COK",
    "Curacao": "CUW",
    "Cyprus": "CYP",
    "Dominica": "DMA",
    "Equatorial Guinea": "GNQ",
    "Eswatini": "SWZ",
    "Faroe Islands": "FRO",
    "Gabon": "GAB",
    "Gambia": "GMB",
    "Georgia": "GEO",
    "Gibraltar": "GIB",
    "Guinea-Bissau": "GNB",
    "Honduras": "HND",
    "Jamaica": "JAM",
    "Lebanon": "LBN",
    "Liberia": "LBR",
    "Malta": "MLT",
    "Marshall Islands": "MHL",
    "Mauritius": "MUS",
    "Moldova": "MDA",
    "Mongolia": "MNG",
    "Myanmar": "MMR",
    "Niue": "NIU",
    "North Korea": "PRK",
    "Palau": "PLW",
    "Panama": "PAN",
    "Saint Kitts and Nevis": "KNA",
    "Saint Vincent and the Grenadines": "VCT",
    "San Marino": "SMR",
    "Sao Tome and Principe": "STP",
    "Sierra Leone": "SLE",
    "Sri Lanka": "LKA",
    "Tanzania": "TZA",
    "Togo": "TGO",
    "Tuvalu": "TUV",
    "Vanuatu": "VUT",
}
HIST_FOC_UNMAPPED: dict[str, str] = {
    "Madeira": "shares Portugal's ISO3 'PRT' with its ordinary, non-FOC flag -- see module docstring",
}
# Guard, mirroring tests.test_foc's pattern: if process.foc ever adds a country this module hasn't
# decided how to handle, this fails loudly at import time rather than silently under-counting FOC
# flags.
assert set(FOC_COUNTRY_TO_ISO3) | set(HIST_FOC_UNMAPPED) == set(FOC_NAME_TO_MID_COUNTRY.values()), (
    "process.foc gained/lost a country name not reflected in FOC_COUNTRY_TO_ISO3/HIST_FOC_UNMAPPED"
)

FOC_ISO3: frozenset[str] = frozenset(FOC_COUNTRY_TO_ISO3.values())


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _in_list_sql(values: frozenset[str]) -> str:
    return ", ".join(_sql_literal(v) for v in sorted(values))


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


def prior_windows_for(
    start: date, panel_root: Path = PANEL_ROOT, max_windows: int = MAX_PRIOR_WINDOWS
) -> list[tuple[date, date, Path]]:
    """The up-to-``max_windows`` monthly panel windows immediately preceding ``start`` in the
    built archive (chronological order), used by ``hist_prior_windows_seen``/
    ``hist_prior_dest_russia``. Empty when ``start`` is (or precedes) the first built window."""
    earlier = [w for w in panel_window_dirs(panel_root) if w[0] < start]
    return earlier[-max_windows:] if max_windows else earlier


def gfw_derived_features_sql(panel_sql: str, vessel_ids_sql: str, window_end: date) -> str:
    """CTEs for the GFW-identity-derived columns (hist_n_flags_730d .. hist_to_foc_730d), plus a
    final SELECT keyed by mmsi. Split out (mirrors features.static/features.port_visits's
    convention of a testable *_sql function) so a test can check it directly against in-memory
    tables."""
    window_end_literal = f"DATE {_sql_literal(window_end.isoformat())}"
    foc_list = _in_list_sql(FOC_ISO3)
    return f"""
    WITH segs AS (
        SELECT imo, gfw_vessel_id, ssvid, shipname, flag, transmission_date_from,
               CASE WHEN transmission_date_to IS NULL THEN {window_end_literal}
                    ELSE LEAST(transmission_date_to, {window_end_literal}) END AS clipped_end
        FROM ({vessel_ids_sql})
        WHERE transmission_date_from < {window_end_literal}
    ),
    ordered AS (
        SELECT *,
            LAG(flag) OVER w AS prev_flag,
            LAG(transmission_date_from) OVER w AS prev_from
        FROM segs
        WINDOW w AS (PARTITION BY imo ORDER BY transmission_date_from, gfw_vessel_id)
    ),
    flagged AS (
        SELECT *,
            (prev_from IS NOT NULL AND flag IS DISTINCT FROM prev_flag) AS is_flag_change,
            coalesce(flag IN ({foc_list}), false) AS is_foc_now,
            coalesce(prev_flag IN ({foc_list}), false) AS is_foc_prev
        FROM ordered
    ),
    run_start AS (
        SELECT imo,
            coalesce(
                max(transmission_date_from) FILTER (WHERE is_flag_change),
                min(transmission_date_from)
            ) AS run_start_date
        FROM flagged
        GROUP BY imo
    ),
    ais_age AS (
        SELECT imo, min(transmission_date_from) AS earliest_from
        FROM segs
        GROUP BY imo
    ),
    active_730 AS (
        SELECT imo, flag, shipname, ssvid
        FROM segs
        WHERE clipped_end > {window_end_literal} - INTERVAL '{LOOKBACK_DAYS}' DAY
    ),
    agg730 AS (
        SELECT imo,
            CAST(count(DISTINCT flag) AS BIGINT) AS hist_n_flags_730d,
            CAST(count(DISTINCT shipname) AS BIGINT) AS hist_n_names_730d,
            CAST(count(DISTINCT ssvid) AS BIGINT) AS hist_n_mmsi_730d
        FROM active_730
        GROUP BY imo
    ),
    to_foc AS (
        SELECT imo,
            bool_or(
                prev_from IS NOT NULL
                AND NOT is_foc_prev AND is_foc_now
                AND transmission_date_from >= {window_end_literal} - INTERVAL '{LOOKBACK_DAYS}' DAY
                AND transmission_date_from < {window_end_literal}
            ) AS hist_to_foc_730d
        FROM flagged
        GROUP BY imo
    ),
    resolved AS (
        SELECT DISTINCT imo FROM ({vessel_ids_sql})
    )
    SELECT
        p.mmsi,
        p.imo,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.hist_n_flags_730d, 0) END
            AS hist_n_flags_730d,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.hist_n_names_730d, 0) END
            AS hist_n_names_730d,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(a.hist_n_mmsi_730d, 0) END
            AS hist_n_mmsi_730d,
        CASE
            WHEN r.imo IS NULL OR rs.run_start_date IS NULL THEN NULL
            ELSE LEAST({FLAG_AGE_CAP_DAYS}, date_diff('day', rs.run_start_date, {window_end_literal}))
        END AS hist_flag_age_days,
        CASE
            WHEN r.imo IS NULL OR aa.earliest_from IS NULL THEN NULL
            ELSE LEAST({AIS_AGE_CAP_DAYS}, date_diff('day', aa.earliest_from, {window_end_literal}))
        END AS hist_ais_age_days,
        CASE WHEN r.imo IS NULL THEN NULL ELSE coalesce(tf.hist_to_foc_730d, false) END
            AS hist_to_foc_730d
    FROM ({panel_sql}) p
    LEFT JOIN resolved r ON r.imo = p.imo
    LEFT JOIN agg730 a ON a.imo = p.imo
    LEFT JOIN run_start rs ON rs.imo = p.imo
    LEFT JOIN ais_age aa ON aa.imo = p.imo
    LEFT JOIN to_foc tf ON tf.imo = p.imo
    """


def _prior_window_sql(
    prior_windows: list[tuple[date, date, Path]],
    static_root: Path = STATIC_ROOT,
) -> tuple[str, str]:
    """(prior_seen_sql, prior_dest_russia_sql): two SELECT statements, each returning distinct
    imo-level facts unioned across ``prior_windows`` (already at most MAX_PRIOR_WINDOWS, already
    strictly before the target window). Missing panel/static partitions are tolerated (contribute
    nothing) rather than raising -- a historical window not yet built should not crash a later
    one's history features."""
    seen_parts = []
    dest_parts = []
    for i, (_s, _e, panel_dir) in enumerate(prior_windows):
        panel_path = panel_dir / "part-0.parquet"
        static_path = static_root / panel_dir.name / "part-0.parquet"
        if not panel_path.exists():
            continue
        seen_parts.append(
            f"SELECT DISTINCT imo, {i} AS window_idx "
            f"FROM read_parquet('{panel_path.as_posix()}') WHERE imo IS NOT NULL"
        )
        if static_path.exists():
            dest_parts.append(
                f"SELECT DISTINCT p.imo FROM read_parquet('{panel_path.as_posix()}') p "
                f"JOIN read_parquet('{static_path.as_posix()}') s ON s.mmsi = p.mmsi "
                "WHERE p.imo IS NOT NULL AND s.dest_russia"
            )
    seen_sql = " UNION ALL ".join(seen_parts) if seen_parts else (
        "SELECT NULL::VARCHAR AS imo, NULL::INTEGER AS window_idx WHERE false"
    )
    dest_sql = " UNION ALL ".join(dest_parts) if dest_parts else (
        "SELECT NULL::VARCHAR AS imo WHERE false"
    )
    return seen_sql, dest_sql


def prior_window_features_sql(
    panel_sql: str,
    prior_windows: list[tuple[date, date, Path]],
    static_root: Path = STATIC_ROOT,
) -> str:
    """hist_prior_windows_seen / hist_prior_dest_russia for every mmsi in panel_sql, keyed by imo,
    given the (already-resolved) list of up-to-6 prior windows. Split out for testability."""
    n_prior = len(prior_windows)
    if n_prior == 0:
        return (
            "SELECT mmsi, imo, NULL::DOUBLE AS hist_prior_windows_seen, "
            "NULL::BOOLEAN AS hist_prior_dest_russia "
            f"FROM ({panel_sql})"
        )
    seen_sql, dest_sql = _prior_window_sql(prior_windows, static_root=static_root)
    return f"""
    WITH prior_seen AS (
        SELECT imo, CAST(count(DISTINCT window_idx) AS DOUBLE) AS n_seen
        FROM ({seen_sql})
        GROUP BY imo
    ),
    prior_dest_russia AS (
        SELECT DISTINCT imo FROM ({dest_sql})
    )
    SELECT
        p.mmsi,
        p.imo,
        coalesce(ps.n_seen, 0.0) / {float(n_prior)} AS hist_prior_windows_seen,
        (pdr.imo IS NOT NULL) AS hist_prior_dest_russia
    FROM ({panel_sql}) p
    LEFT JOIN prior_seen ps ON ps.imo = p.imo
    LEFT JOIN prior_dest_russia pdr ON pdr.imo = p.imo
    """


def build_history_features(
    start: date,
    end: date,
    panel_dir: Path,
    panel_root: Path = PANEL_ROOT,
    static_root: Path = STATIC_ROOT,
    vessel_ids_path: Path = VESSEL_IDS_PATH,
    out_root: Path = HISTORY_OUT_ROOT,
    force: bool = False,
    threads: int | None = None,
) -> Path:
    """Build the hist_* features for one window, writing
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

    prior_windows = prior_windows_for(start, panel_root=panel_root)

    con = duckdb.connect()
    try:
        if threads is not None:
            con.execute(f"SET threads = {int(threads)}")
        panel_sql = f"SELECT mmsi, imo FROM read_parquet('{panel_path.as_posix()}')"
        vessel_ids_sql = (
            f"SELECT * FROM read_parquet('{vessel_ids_path.as_posix()}')"
            if vessel_ids_path.exists()
            else "SELECT NULL::VARCHAR AS imo, NULL::VARCHAR AS gfw_vessel_id, "
            "NULL::VARCHAR AS ssvid, NULL::VARCHAR AS shipname, NULL::VARCHAR AS flag, "
            "NULL::TIMESTAMP AS transmission_date_from, NULL::TIMESTAMP AS transmission_date_to "
            "WHERE false"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _gfw AS "
            + gfw_derived_features_sql(panel_sql, vessel_ids_sql, end)
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _prior AS "
            + prior_window_features_sql(panel_sql, prior_windows, static_root=static_root)
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _history AS "
            "SELECT g.mmsi, g.hist_n_flags_730d, g.hist_n_names_730d, g.hist_n_mmsi_730d, "
            "g.hist_flag_age_days, g.hist_ais_age_days, g.hist_to_foc_730d, "
            "pr.hist_prior_windows_seen, pr.hist_prior_dest_russia "
            "FROM _gfw g JOIN _prior pr ON pr.mmsi = g.mmsi"
        )
        n_mmsi, n_gfw, n_prior_seen = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE hist_n_flags_730d IS NOT NULL), "
            "count(*) FILTER (WHERE hist_prior_windows_seen IS NOT NULL) FROM _history"
        ).fetchone()
        logger.info(
            "Built history features for %s..%s: %d mmsi (%d GFW-resolved, %d with prior-window "
            "history, %d prior window(s) used)",
            start.isoformat(), end.isoformat(), n_mmsi, n_gfw, n_prior_seen, len(prior_windows),
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
            "FROM _history ORDER BY mmsi",
            out_path,
        )
    finally:
        con.close()
    return out_path


def build_all_windows(
    panel_root: Path = PANEL_ROOT,
    static_root: Path = STATIC_ROOT,
    vessel_ids_path: Path = VESSEL_IDS_PATH,
    out_root: Path = HISTORY_OUT_ROOT,
    force: bool = False,
    threads: int | None = None,
) -> list[Path]:
    """Build history features for every monthly panel window (skipping the 2-day one), in
    chronological order (required: each window's prior-window features read earlier windows'
    already-built panel/static partitions, not this module's own output)."""
    out_paths = []
    for start, end, panel_dir in panel_window_dirs(panel_root):
        out_paths.append(
            build_history_features(
                start, end, panel_dir,
                panel_root=panel_root, static_root=static_root, vessel_ids_path=vessel_ids_path,
                out_root=out_root, force=force, threads=threads,
            )
        )
    return out_paths


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build cross-month vessel history features (task P4-12) for every panel "
        "window."
    )
    parser.add_argument("--panel-root", default=str(PANEL_ROOT))
    parser.add_argument("--static-root", default=str(STATIC_ROOT))
    parser.add_argument("--vessel-ids-path", default=str(VESSEL_IDS_PATH))
    parser.add_argument("--out-root", default=str(HISTORY_OUT_ROOT))
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_all_windows(
        panel_root=Path(args.panel_root),
        static_root=Path(args.static_root),
        vessel_ids_path=Path(args.vessel_ids_path),
        out_root=Path(args.out_root),
        force=args.force,
        threads=args.threads,
    )


if __name__ == "__main__":
    main()
