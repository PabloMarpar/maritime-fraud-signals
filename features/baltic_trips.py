"""Per-mmsi implied eastern-Baltic loading features (P4-3h), one row per mmsi per window,
mirroring ``features.static``'s partition conventions exactly:
``data/processed/baltic_trips/window=<start>_<end>/part-0.parquet``.

**Why this is a separate module from ``detect.baltic_trips``.** That module builds ONE flat,
cross-time trips table (a trip is not scoped to any single window by construction -- see its own
docstring). This module is the strict, per-window, ``knowable_at``-gated reduction of that flat
table down to the vessel-month grain ``features.panel`` eventually joins -- the same detector/
features split every other detector in this project already has (``detect.gaps`` + ``features.panel``'s
own gap aggregation, ``detect.behaviour`` + the same).

**Population: every mmsi in this window's own identity table** (``process.identity``'s window
file), exactly like ``features.panel``'s population -- including orphaned mmsi (no valid IMO), who
get ``lr_n_trips=0`` by construction since a trip can only be found under a resolved IMO.
**Representative IMO for a reused mmsi**: the one with the latest ``last_seen`` -- the identical
tie-break ``features.panel`` and ``detect.baltic_trips`` already use, so this module's join key
agrees with both rather than re-deriving a third resolution.

**Strict temporal rule (pre-registered, ``docs/DECISIONS.md``'s 2026-09-29 "P4-3h" entry):** a
trip counts toward window W only if its westbound crossing time (``knowable_at`` for the whole
trip -- the moment the return leg, and therefore the relative-change verdict, is actually
observed) is in ``[max(MARCH_START, window_end - LOOKBACK_DAYS), window_end)`` -- a strict ``<``
upper bound (never ``<=``): a trip whose westbound crossing lands exactly at ``window_end``
(midnight, see below) is NOT yet knowable when the window closes. ``window_end`` here is the same
end-of-day convention ``detect.behaviour``/``detect.identity_anomalies`` already use
(``datetime.combine(window_end, datetime.max.time())``), not bare midnight -- using midnight would
silently exclude every trip whose westbound crossing happens later on the window's own last day.

**Columns**, per the pre-registered spec exactly:

* ``lr_n_trips`` -- count of eligible trips for this mmsi's imo, 0 (not NULL) with none.
* ``lr_n_laden_returns`` -- count of those with ``is_laden_return``, 0 with none.
* ``lr_laden_share`` -- ``lr_n_laden_returns / lr_n_trips``, NULL (NaN on read into pandas/NumPy)
  when ``lr_n_trips = 0`` -- "share of nothing" is undefined, not zero.
* ``lr_rate_per_30d`` -- laden returns per 30 *observable* days, where the observable-day count is
  the lookback window's own length in days (``window_end - max(MARCH_START, window_end -
  LOOKBACK_DAYS)``), which is shorter than :data:`LOOKBACK_DAYS` for every window whose lookback
  hits the ``MARCH_START`` floor rather than the 270-day cap. This is well-defined (0.0, not NULL)
  even when ``lr_n_trips = 0`` -- the denominator never depends on whether any trips were found.

One shared DuckDB connection per window build, mirroring every other module in this project.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from detect.baltic_trips import MARCH_START, TRIPS_PATH
from process.identity import IDENTITY_ROOT
from process.partitions import (
    atomic_write_parquet,
    git_sha,
    partition_exists,
    window_partition_path,
)

logger = logging.getLogger(__name__)

# data/processed/baltic_trips/window=<start>_<end>/part-0.parquet, one file per window -- see
# module docstring.
FEATURES_ROOT = Path("data/processed/baltic_trips")

# Pre-registered lookback -- docs/DECISIONS.md 2026-09-29 "P4-3h": "trips whose westbound crossing
# is in [max(2024-03-02, window_end - 270 d), window_end)".
LOOKBACK_DAYS = 270


def _identity_window_path(window_start: date, window_end: date, identity_root: Path) -> Path:
    return window_partition_path(window_start, window_end, identity_root)


def build_baltic_trip_features(
    window_start: date,
    window_end: date,
    trips_path: Path = TRIPS_PATH,
    identity_root: Path = IDENTITY_ROOT,
    out_root: Path = FEATURES_ROOT,
    force: bool = False,
) -> Path:
    """Build one window's implied-loading features, writing
    ``out_root/window=<window_start>_<window_end>/part-0.parquet``.

    Idempotent per window: a no-op unless force=True or the output is missing. Raises
    FileNotFoundError naming what to build first if `trips_path` (detect.baltic_trips.build_baltic_trips)
    or this window's identity table (process.identity.resolve_range) is missing.
    """
    out_path = window_partition_path(window_start, window_end, out_root)
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    if not partition_exists(trips_path):
        raise FileNotFoundError(
            f"No trips table at {trips_path}; run detect.baltic_trips.build_baltic_trips first"
        )
    identity_path = _identity_window_path(window_start, window_end, identity_root)
    if not identity_path.exists():
        raise FileNotFoundError(
            f"No identity window at {identity_path}; run process.identity.resolve_range first"
        )

    lower_bound = max(MARCH_START, window_end - timedelta(days=LOOKBACK_DAYS))
    observable_days = (window_end - lower_bound).days
    window_end_dt = datetime.combine(window_end, datetime.max.time())
    lower_bound_dt = datetime.combine(lower_bound, datetime.min.time())

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _mmsi_imo AS "
            "WITH imo_ranked AS ("
            "  SELECT mmsi, imo, row_number() OVER (PARTITION BY mmsi ORDER BY last_seen DESC) AS rn "
            "  FROM read_parquet(?) WHERE imo IS NOT NULL"
            "), rep_imo AS (SELECT mmsi, imo FROM imo_ranked WHERE rn = 1), "
            "roster AS (SELECT DISTINCT mmsi FROM read_parquet(?)) "
            "SELECT ros.mmsi, rep.imo FROM roster ros LEFT JOIN rep_imo rep ON rep.mmsi = ros.mmsi",
            [str(identity_path), str(identity_path)],
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _agg AS "
            "SELECT imo, count(*) AS n_trips, "
            "       count(*) FILTER (WHERE is_laden_return) AS n_laden_returns "
            "FROM read_parquet(?) "
            "WHERE west_time >= ? AND west_time < ? "
            "GROUP BY imo",
            [str(trips_path), lower_bound_dt, window_end_dt],
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _features AS "
            "SELECT mi.mmsi, "
            "  coalesce(a.n_trips, 0) AS lr_n_trips, "
            "  coalesce(a.n_laden_returns, 0) AS lr_n_laden_returns, "
            "  CASE WHEN coalesce(a.n_trips, 0) = 0 THEN NULL "
            "       ELSE a.n_laden_returns::DOUBLE / a.n_trips END AS lr_laden_share, "
            f"  coalesce(a.n_laden_returns, 0)::DOUBLE / {observable_days} * 30 AS lr_rate_per_30d "
            "FROM _mmsi_imo mi LEFT JOIN _agg a ON a.imo = mi.imo"
        )

        (n_mmsi,) = con.execute("SELECT count(*) FROM _features").fetchone()
        (n_with_trips,) = con.execute(
            "SELECT count(*) FROM _features WHERE lr_n_trips > 0"
        ).fetchone()
        (n_laden,) = con.execute(
            "SELECT sum(lr_n_laden_returns) FROM _features"
        ).fetchone()
        logger.info(
            "%s..%s: %d mmsi, %d with >=1 eligible trip, %d laden return(s) total "
            "(lookback %s..%s, %d observable day(s))",
            window_start.isoformat(), window_end.isoformat(), n_mmsi, n_with_trips,
            n_laden or 0, lower_bound.isoformat(), window_end.isoformat(), observable_days,
        )

        built_at = datetime.now(timezone.utc)
        sha = git_sha()
        atomic_write_parquet(
            con,
            "SELECT *, "
            f"DATE '{window_start.isoformat()}' AS window_start, "
            f"DATE '{window_end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{sha}' AS git_sha "
            "FROM _features ORDER BY mmsi",
            out_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build one window's per-mmsi implied eastern-Baltic loading features (P4-3h) "
        "from detect.baltic_trips' flat trips table."
    )
    parser.add_argument("--start", required=True, help="Window start day, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Window end day, YYYY-MM-DD")
    parser.add_argument("--trips-path", default=str(TRIPS_PATH))
    parser.add_argument("--identity-root", default=str(IDENTITY_ROOT))
    parser.add_argument("--out-root", default=str(FEATURES_ROOT))
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_baltic_trip_features(
        window_start=date.fromisoformat(args.start),
        window_end=date.fromisoformat(args.end),
        trips_path=Path(args.trips_path),
        identity_root=Path(args.identity_root),
        out_root=Path(args.out_root),
        force=args.force,
    )


if __name__ == "__main__":
    main()
