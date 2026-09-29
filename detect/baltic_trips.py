"""Implied eastern-Baltic loading from draught (P4-3h): a tanker crossing empty (or near-empty)
into the eastern Baltic and returning laden is circumstantial evidence of a Russian Baltic port
call this project's Danish-only AIS coverage can never see directly.

**Pre-registered spec -- do not change definitions here without updating
``docs/DECISIONS.md``'s 2026-09-29 "P4-3h" entry first.** This module implements exactly that
entry; every constant below cites it.

**Gate line.** Meridian :data:`GATE_LONGITUDE_DEG` (14.0 E) between :data:`GATE_LAT_MIN` (54.3 N)
and :data:`GATE_LAT_MAX` (56.0 N) -- the Arkona basin, east of the Sound and the Fehmarn Belt, west
of Bornholm. A crossing is two consecutive positions of one IMO (any of its MMSI -- see "Identity"
below), on opposite sides of the meridian, both inside the latitude band, at most
:data:`MAX_CROSSING_GAP_HOURS` apart. Candidates are Tankers only, with a valid IMO, per the
population definition below.

**Round trip.** An eastbound crossing paired with that IMO's very NEXT westbound crossing (not any
later one), :data:`MIN_ROUND_TRIP_DAYS`..\\ :data:`MAX_ROUND_TRIP_DAYS` days later. Crossing draught
is the median reported draught in the +-\\ :data:`DRAUGHT_WINDOW_HOURS` around each crossing (NaN
if none -- such trips are dropped, never written with a NaN draught). Relative change is
``(west_draught - east_draught) / max_draught_before_west``, where the denominator is the highest
draught this IMO ever reported (any MMSI) strictly before the westbound crossing -- unbounded
lookback, not scoped to this one trip.

**Identity population, per source month.** ``process.identity``'s window table (representative
valid IMO for a reused MMSI: the one with the latest ``last_seen``, mirroring
``features.panel``'s own tie-break) joined to ``process.ship_type``'s window reference (modal
non-null ship_type by message count, mirroring ``features.panel._build_ship_type`` exactly),
restricted to ``ship_type = 'Tanker'``. Built fresh per calendar-month window
(:func:`_month_windows`), because that is the grain both reference tables are built at -- a
physical vessel whose modal type resolution flips between two adjacent months (sparse Message 5
data) silently drops out of the roster for the flipped month; not fixed here, an accepted
limitation of reusing the project's existing per-window identity/ship_type convention rather than
inventing a new whole-archive resolution.

**March 2024 has no ``process.identity``/``process.ship_type`` window (the archive's monthly
windows start 2024-04-01).** Per the task that pre-registered this module: ship_type for March is
resolved directly from March's own clean partitions (same modal-by-message-count logic, in
memory, no new reference partition written), but the MMSI<->IMO mapping reuses **April's**
identity window verbatim (:func:`_build_march_roster`) -- a March-only MMSI never seen in April is
therefore simply not a candidate. Documented, not solved: a real, if small, coverage gap for the
one month with no monthly identity build of its own.

**Position source, deliberately different by month.** Crossing detection for 2024-04-01 onward
reads ``process.thin``'s 5-minute-bucketed tracks, not full clean data -- a specific, documented
exception to that module's own "never for re-detection" rule (its jumps-between-buckets caveat is
about detectors needing close-range geometry, e.g. ``detect.spoofing``'s position jumps or
``detect.sts``'s encounter geometry; a Baltic-gate-line crossing is the opposite kind of event --
coarse, basin-scale, and the crossing definition already tolerates a 2h gap between the two
positions that define it, two orders of magnitude looser than the 5-minute bucket). March, which
has no thin partition, uses clean data directly, unthinned (the filtered volume -- Tanker roster x
latitude band -- is tiny regardless of resolution, so no in-memory thinning step was needed).
**Draught, by contrast, is never in ``process.thin``'s schema and is always read from clean data,
every month including March** -- this is the one place every month pays the full clean-partition
scan, mitigated by projecting only ``mmsi, timestamp, draught`` and filtering to the tiny Tanker
roster before any join.

**Thresholds are fixed on March 2024 only** (:data:`MARCH_START`..\\ :data:`MARCH_END`, which
precede every archive window's rows): :func:`select_laden_return_threshold` bins the relative
change of March-departing round trips (``east_time`` in March) into a histogram, smooths it, and
takes the lowest point between its two tallest local maxima ("main modes"). If fewer than
:data:`MIN_TRIPS_FOR_THRESHOLD` trips exist, or the smoothed histogram has fewer than two distinct
local maxima, this falls back to :data:`FALLBACK_LADEN_RETURN_THRESHOLD` (0.20, i.e. 20% of the
vessel's own historical max draught). The chosen value is frozen as :data:`LADEN_RETURN_THRESHOLD`
-- see that constant's own comment for the real run's numbers, recorded in this session's report,
not re-derived here on every import.

**Validation gate (label-free).** :func:`validate_against_gfw` samples up to 100 laden-return trips
(uniform, fixed seed, all months) and reports the share with a GFW port visit
(``confidence >= 3``) at a Russian-flagged anchorage between the two crossings. Returns ``None``
(logged, not raised) if the GFW port-visits table does not exist yet -- P4-11 is a separate,
concurrently-built task. **The exact column names this function reads
(``imo``/``start``/``end``/``confidence``/``start_anchorage_flag``), taken from
``ingest.gfw_port_visits.PORT_VISITS_SCHEMA`` (confirmed against that module's own real schema
constant this session, though the real ``port_visits.parquet`` file itself was not yet built)** -- see the function's own docstring.

**Output.** One FLAT table, ``data/detect/baltic_trips/part-0.parquet`` -- not window-partitioned
like every other detector here. Reason: a trip is a cross-time fact by construction (an eastbound
crossing in one month paired with a westbound crossing in the next), so there is no single window
it belongs to; ``features.baltic_trips`` (this project's per-window feature aggregator, mirroring
``features.static``) is the module that turns this flat table into vessel-month features under a
strict ``knowable_at`` cutoff.

All of this runs as DuckDB queries over views/temp tables; raw AIS rows are never pulled into
Python. Only the small, already-aggregated relative-change values reach Python, for
:func:`select_laden_return_threshold`'s histogram.
"""

from __future__ import annotations

import argparse
import logging
import random
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np

from process.identity import IDENTITY_ROOT
from process.partitions import (
    atomic_write_parquet,
    existing_partitions,
    git_sha,
    partition_exists,
)
from process.ship_type import SHIP_TYPE_ROOT

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
THIN_ROOT = Path("data/tracks/thin")
DETECT_ROOT = Path("data/detect")
# data/detect/baltic_trips/part-0.parquet -- ONE flat file, not window-partitioned. See module
# docstring's "Output" section for why.
TRIPS_ROOT = DETECT_ROOT / "baltic_trips"
TRIPS_PATH = TRIPS_ROOT / "part-0.parquet"

# P4-11's output (built concurrently by another session as of this writing) -- see
# validate_against_gfw. Assumed, not yet confirmed, schema.
GFW_PORT_VISITS_PATH = Path("data/reference/gfw/port_visits.parquet")

# Gate line -- docs/DECISIONS.md 2026-09-29 "P4-3h".
GATE_LONGITUDE_DEG = 14.0
GATE_LAT_MIN = 54.3
GATE_LAT_MAX = 56.0
MAX_CROSSING_GAP_HOURS = 2.0

# Round trip -- docs/DECISIONS.md 2026-09-29 "P4-3h".
MIN_ROUND_TRIP_DAYS = 2
MAX_ROUND_TRIP_DAYS = 30
DRAUGHT_WINDOW_HOURS = 12.0

# March 2024 clean days used for threshold-fixing -- docs/DECISIONS.md 2026-09-29 "P4-3h":
# "clean days 2024-03-02..03-31, which precede every window's rows".
MARCH_START = date(2024, 3, 2)
MARCH_END = date(2024, 3, 31)
# The archive's first proper monthly window (process.identity/process.ship_type both start here).
FIRST_MONTHLY_WINDOW_START = date(2024, 4, 1)
LAST_ARCHIVE_DAY = date(2025, 2, 26)

# Excluded everywhere in this project -- a 2-day debugging build, not a real archive window
# (docs/STATE.md, docs/DECISIONS.md P3-4/A2). Never generated by _month_windows, listed here only
# so a caller reading data/identity or data/reference/ship_type globs directly remembers to skip
# it too.
SKIPPED_WINDOW = (date(2024, 6, 10), date(2024, 6, 11))

# Threshold selection (step 2 of the pre-registered spec) -- see select_laden_return_threshold.
FALLBACK_LADEN_RETURN_THRESHOLD = 0.20
MIN_TRIPS_FOR_THRESHOLD = 10
THRESHOLD_HISTOGRAM_BINS = 20
THRESHOLD_SMOOTHING_WINDOW = 3

# Frozen 2026-09-29 on the real March 2024 run (docs/DECISIONS.md's "P4-3h" entry) -- 316 March-
# departing round trips (built with the FALLBACK threshold, which only gates is_laden_return, not
# rel_change or trip inclusion, so re-running the threshold step against that same build is valid),
# well above MIN_TRIPS_FOR_THRESHOLD. The smoothed 20-bin histogram of rel_change IS genuinely
# bimodal: two main modes at -0.207 (bin center; a cluster of round trips with LOWER draught on
# return -- not a laden-return signal at all) and +0.327 (a clear laden-return cluster), with the
# lowest point of the smoothed histogram between them at 0.1133 -- this session's own computed
# value, NOT the 0.20 fallback. See this session's report for the full binned histogram. Recompute
# only by re-running select_laden_return_threshold against a fresh March build; never hand-edit.
LADEN_RETURN_THRESHOLD = 0.1133


def _partitions_union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL of `SELECT {columns} FROM read_parquet(...)` over every partition.

    Every module in this project owns its own copy of this small helper rather than importing
    one shared version -- see e.g. features.static's module docstring.
    """
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def _window_partition_path(window_start: date, window_end: date, root: Path) -> Path:
    return root / f"window={window_start.isoformat()}_{window_end.isoformat()}" / "part-0.parquet"


def _month_windows(
    first_start: date = FIRST_MONTHLY_WINDOW_START, last_day: date = LAST_ARCHIVE_DAY
) -> list[tuple[date, date]]:
    """Calendar-month windows from ``first_start``'s month through ``last_day``, each capped at
    ``last_day`` for a final partial month -- matches the archive's real windows on disk
    (``data/identity/mmsi_imo/window=*``) exactly, without reading the directory. Never generates
    :data:`SKIPPED_WINDOW`, by construction.
    """
    windows: list[tuple[date, date]] = []
    cur = date(first_start.year, first_start.month, 1)
    while cur <= last_day:
        next_month = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        month_end = min(next_month - timedelta(days=1), last_day)
        windows.append((cur, month_end))
        cur = next_month
    return windows


def _build_roster(
    con: duckdb.DuckDBPyConnection,
    window_start: date,
    window_end: date,
    identity_root: Path,
    ship_type_root: Path,
) -> int:
    """Materialize `_roster`: (mmsi, imo) for Tankers with a representative valid IMO in this
    month's identity/ship_type windows -- see module docstring's "Identity population" section.
    Returns the roster's row count (0, not an error, if either window's file is missing -- a real
    possibility this module tolerates by simply contributing no candidates from that month).
    """
    identity_path = _window_partition_path(window_start, window_end, identity_root)
    ship_type_path = _window_partition_path(window_start, window_end, ship_type_root)
    if not identity_path.exists() or not ship_type_path.exists():
        logger.warning(
            "No identity/ship_type window for %s..%s (%s / %s missing) -- 0 roster candidates",
            window_start.isoformat(), window_end.isoformat(), identity_path, ship_type_path,
        )
        con.execute("CREATE OR REPLACE TEMP TABLE _roster (mmsi BIGINT, imo VARCHAR)")
        return 0
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _roster AS "
        "WITH imo_ranked AS ("
        "  SELECT mmsi, imo, row_number() OVER (PARTITION BY mmsi ORDER BY last_seen DESC) AS rn "
        "  FROM read_parquet(?) WHERE imo IS NOT NULL"
        "), rep_imo AS (SELECT mmsi, imo FROM imo_ranked WHERE rn = 1), "
        "type_counts AS ("
        "  SELECT mmsi, ship_type, sum(n_messages) AS n FROM read_parquet(?) "
        "  WHERE ship_type IS NOT NULL AND ship_type != '' GROUP BY mmsi, ship_type"
        "), type_ranked AS ("
        "  SELECT mmsi, ship_type, row_number() OVER (PARTITION BY mmsi ORDER BY n DESC, ship_type) AS rn "
        "  FROM type_counts"
        "), modal_type AS (SELECT mmsi, ship_type FROM type_ranked WHERE rn = 1) "
        "SELECT r.mmsi, r.imo FROM rep_imo r JOIN modal_type t ON t.mmsi = r.mmsi "
        "WHERE t.ship_type = 'Tanker'",
        [str(identity_path), str(ship_type_path)],
    )
    (n,) = con.execute("SELECT count(*) FROM _roster").fetchone()
    return n


def _build_march_roster(
    con: duckdb.DuckDBPyConnection, in_root: Path, april_identity_path: Path
) -> int:
    """Materialize `_roster`: (mmsi, imo) for March 2024, ship_type resolved from March's own
    clean partitions in memory, IMO mapping reused verbatim from April's identity window -- see
    module docstring's "March 2024 has no window" section. Returns the roster's row count.
    """
    partitions = existing_partitions(MARCH_START, MARCH_END, in_root)
    if not partitions:
        raise FileNotFoundError(f"No clean partitions for March 2024 under {in_root}")
    if not april_identity_path.exists():
        raise FileNotFoundError(f"No April identity window at {april_identity_path}")
    source = _partitions_union_sql(partitions, "mmsi, ship_type")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _roster AS "
        "WITH type_counts AS ("
        f"  SELECT mmsi, ship_type, count(*) AS n FROM ({source}) "
        "  WHERE ship_type IS NOT NULL AND ship_type != '' GROUP BY mmsi, ship_type"
        "), type_ranked AS ("
        "  SELECT mmsi, ship_type, row_number() OVER (PARTITION BY mmsi ORDER BY n DESC, ship_type) AS rn "
        "  FROM type_counts"
        "), modal_type AS (SELECT mmsi, ship_type FROM type_ranked WHERE rn = 1), "
        "imo_ranked AS ("
        "  SELECT mmsi, imo, row_number() OVER (PARTITION BY mmsi ORDER BY last_seen DESC) AS rn "
        "  FROM read_parquet(?) WHERE imo IS NOT NULL"
        "), rep_imo AS (SELECT mmsi, imo FROM imo_ranked WHERE rn = 1) "
        "SELECT m.mmsi, r.imo FROM modal_type m JOIN rep_imo r ON r.mmsi = m.mmsi "
        "WHERE m.ship_type = 'Tanker'",
        [str(april_identity_path)],
    )
    (n,) = con.execute("SELECT count(*) FROM _roster").fetchone()
    return n


def _append_gate_positions_from_thin(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date, thin_root: Path
) -> None:
    """Append to `_gate_positions` (imo, mmsi, timestamp, latitude, longitude) from thinned
    tracks, restricted to `_roster` and the gate's latitude band. Requires `_roster` built first.
    """
    partitions = existing_partitions(window_start, window_end, thin_root)
    if not partitions:
        logger.warning("No thin partitions for %s..%s", window_start.isoformat(), window_end.isoformat())
        return
    source = _partitions_union_sql(partitions, "mmsi, timestamp, latitude, longitude")
    con.execute(
        "INSERT INTO _gate_positions "
        "SELECT r.imo, t.mmsi, t.timestamp, t.latitude, t.longitude "
        f"FROM ({source}) t JOIN _roster r ON r.mmsi = t.mmsi "
        f"WHERE t.latitude BETWEEN {GATE_LAT_MIN} AND {GATE_LAT_MAX}"
    )


def _append_gate_positions_and_draughts_from_clean(
    con: duckdb.DuckDBPyConnection, start: date, end: date, in_root: Path
) -> None:
    """Append to both `_gate_positions` and `_draught_readings` from ONE pass over clean
    partitions in [start, end], restricted to `_roster` -- used for March (no thin partition) and
    reused for every other month's draught extraction (never in process.thin's schema). Requires
    `_roster` built first.
    """
    partitions = existing_partitions(start, end, in_root)
    if not partitions:
        logger.warning("No clean partitions for %s..%s", start.isoformat(), end.isoformat())
        return
    source = _partitions_union_sql(partitions, "mmsi, timestamp, latitude, longitude, draught")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _clean_chunk AS "
        f"SELECT r.imo, c.mmsi, c.timestamp, c.latitude, c.longitude, c.draught "
        f"FROM ({source}) c JOIN _roster r ON r.mmsi = c.mmsi"
    )
    con.execute(
        "INSERT INTO _gate_positions "
        "SELECT imo, mmsi, timestamp, latitude, longitude FROM _clean_chunk "
        f"WHERE latitude BETWEEN {GATE_LAT_MIN} AND {GATE_LAT_MAX}"
    )
    con.execute(
        "INSERT INTO _draught_readings "
        "SELECT imo, mmsi, timestamp, draught FROM _clean_chunk WHERE draught > 0"
    )


def _append_draught_readings_from_clean(
    con: duckdb.DuckDBPyConnection, window_start: date, window_end: date, in_root: Path
) -> None:
    """Append to `_draught_readings` (imo, mmsi, timestamp, draught) from clean partitions,
    restricted to `_roster` and draught > 0 -- draught is never in process.thin's schema, so every
    month (not just March) reads clean data for this. Requires `_roster` built first.
    """
    partitions = existing_partitions(window_start, window_end, in_root)
    if not partitions:
        logger.warning(
            "No clean partitions for draught extraction %s..%s",
            window_start.isoformat(), window_end.isoformat(),
        )
        return
    source = _partitions_union_sql(partitions, "mmsi, timestamp, draught")
    con.execute(
        "INSERT INTO _draught_readings "
        "SELECT r.imo, c.mmsi, c.timestamp, c.draught "
        f"FROM ({source}) c JOIN _roster r ON r.mmsi = c.mmsi "
        "WHERE c.draught > 0"
    )


def detect_crossings(con: duckdb.DuckDBPyConnection) -> int:
    """Materialize `_crossings` (imo, crossing_mmsi, crossing_time, latitude, longitude,
    direction) from `_gate_positions` -- see module docstring's "Gate line" section. Requires
    `_gate_positions` (imo, mmsi, timestamp, latitude, longitude), already restricted to the
    latitude band by every caller that populates it, so "both inside the latitude band" is
    satisfied by construction, not re-checked here. Returns the crossing count.

    A crossing's `crossing_time`/`latitude`/`longitude` are the LATER of the two positions that
    define it (the position already observed on the new side); `crossing_mmsi` is that same
    position's own MMSI, which may differ from the earlier position's MMSI (the IMO's radio
    identity can change between the two -- see "of one IMO (any of its mmsi)" in the pre-
    registered spec).
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _crossings AS "
        "WITH ordered AS ("
        "  SELECT imo, mmsi, timestamp, latitude, longitude, "
        "         lag(timestamp) OVER w AS prev_timestamp, "
        "         lag(longitude) OVER w AS prev_longitude "
        "  FROM _gate_positions "
        "  WINDOW w AS (PARTITION BY imo ORDER BY timestamp)"
        ") "
        "SELECT imo, mmsi AS crossing_mmsi, timestamp AS crossing_time, latitude, longitude, "
        "       CASE WHEN prev_longitude < %(gate)s AND longitude >= %(gate)s THEN 'E' "
        "            WHEN prev_longitude >= %(gate)s AND longitude < %(gate)s THEN 'W' "
        "       END AS direction "
        "FROM ordered "
        "WHERE prev_timestamp IS NOT NULL "
        f"  AND date_diff('second', prev_timestamp, timestamp) <= {MAX_CROSSING_GAP_HOURS * 3600} "
        "  AND (prev_longitude < %(gate)s) != (longitude < %(gate)s)".replace(
            "%(gate)s", str(GATE_LONGITUDE_DEG)
        )
    )
    (n,) = con.execute("SELECT count(*) FROM _crossings").fetchone()
    return n


def pair_round_trips(con: duckdb.DuckDBPyConnection) -> int:
    """Materialize `_round_trips` (imo, east_time, east_mmsi, east_latitude, east_longitude,
    west_time, west_mmsi, west_latitude, west_longitude) -- an eastbound crossing paired with that
    IMO's very next westbound crossing (LATERAL join, one row per eastbound crossing that HAS a
    later westbound crossing at all), THEN filtered to the [MIN_ROUND_TRIP_DAYS,
    MAX_ROUND_TRIP_DAYS] gap. Requires `_crossings`. Returns the round-trip count.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _round_trips AS "
        "SELECT e.imo, e.crossing_time AS east_time, e.crossing_mmsi AS east_mmsi, "
        "       e.latitude AS east_latitude, e.longitude AS east_longitude, "
        "       w.west_time, w.west_mmsi, w.west_latitude, w.west_longitude "
        "FROM (SELECT * FROM _crossings WHERE direction = 'E') e "
        "JOIN LATERAL ("
        "  SELECT crossing_time AS west_time, crossing_mmsi AS west_mmsi, "
        "         latitude AS west_latitude, longitude AS west_longitude "
        "  FROM _crossings w2 "
        "  WHERE w2.imo = e.imo AND w2.direction = 'W' AND w2.crossing_time > e.crossing_time "
        "  ORDER BY w2.crossing_time ASC LIMIT 1"
        ") w ON true "
        f"WHERE date_diff('day', e.crossing_time, w.west_time) BETWEEN {MIN_ROUND_TRIP_DAYS} "
        f"  AND {MAX_ROUND_TRIP_DAYS}"
    )
    (n,) = con.execute("SELECT count(*) FROM _round_trips").fetchone()
    return n


def compute_trip_draughts(con: duckdb.DuckDBPyConnection) -> None:
    """Materialize `_trips` = `_round_trips` plus east_draught, west_draught,
    max_draught_before_west, rel_change (NULL wherever a required reading is missing -- callers
    drop those rows, see module docstring's "Round trip" section). Requires `_round_trips` and
    `_draught_readings`.
    """
    half_window = f"INTERVAL '{DRAUGHT_WINDOW_HOURS} HOURS'"
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _trips AS "
        "SELECT rt.*, "
        "  (SELECT median(draught) FROM _draught_readings d WHERE d.imo = rt.imo "
        f"   AND d.timestamp BETWEEN rt.east_time - {half_window} AND rt.east_time + {half_window}"
        "  ) AS east_draught, "
        "  (SELECT median(draught) FROM _draught_readings d WHERE d.imo = rt.imo "
        f"   AND d.timestamp BETWEEN rt.west_time - {half_window} AND rt.west_time + {half_window}"
        "  ) AS west_draught, "
        "  (SELECT max(draught) FROM _draught_readings d WHERE d.imo = rt.imo "
        "   AND d.timestamp < rt.west_time"
        "  ) AS max_draught_before_west "
        "FROM _round_trips rt"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _trips AS "
        "SELECT *, CASE WHEN east_draught IS NOT NULL AND west_draught IS NOT NULL "
        "                AND max_draught_before_west IS NOT NULL AND max_draught_before_west > 0 "
        "               THEN (west_draught - east_draught) / max_draught_before_west END "
        "          AS rel_change "
        "FROM _trips"
    )


def select_laden_return_threshold(
    rel_changes: Sequence[float | None],
    n_bins: int = THRESHOLD_HISTOGRAM_BINS,
    smoothing_window: int = THRESHOLD_SMOOTHING_WINDOW,
    min_trips: int = MIN_TRIPS_FOR_THRESHOLD,
) -> tuple[float, dict]:
    """The lowest point of a smoothed histogram of `rel_changes` between its two tallest local
    maxima ("main modes") -- docs/DECISIONS.md's 2026-09-29 "P4-3h" entry. Falls back to
    :data:`FALLBACK_LADEN_RETURN_THRESHOLD` if there are fewer than `min_trips` finite values, or
    the smoothed histogram has fewer than two distinct local maxima ("no bimodality visible").

    Returns (threshold, info) where info always has `n`, `fallback`, and -- only when a histogram
    was actually computed -- `counts`, `edges`, `modes` (the two chosen mode bin-centers, low-to-
    high), `valley` (the chosen bin's center, == the returned threshold).

    Pure Python/NumPy over an already-small list of numbers (one per round trip, never millions) --
    the only place this module pulls per-event values out of DuckDB.
    """
    values = np.array(
        [v for v in rel_changes if v is not None and np.isfinite(v)], dtype=float
    )
    info: dict = {"n": int(values.size), "fallback": True}
    if values.size < min_trips:
        return FALLBACK_LADEN_RETURN_THRESHOLD, info

    counts, edges = np.histogram(values, bins=n_bins)
    centers = (edges[:-1] + edges[1:]) / 2.0
    info["counts"] = counts.tolist()
    info["edges"] = edges.tolist()

    kernel = np.ones(smoothing_window) / smoothing_window
    smoothed = np.convolve(counts.astype(float), kernel, mode="same")

    # Strict local maxima only (a flat run of equal bins is not itself "two modes"); endpoints
    # count if they beat their one neighbour. A bin with zero smoothed density is never a peak.
    peak_idx = [
        i
        for i in range(len(smoothed))
        if smoothed[i] > 0
        and (i == 0 or smoothed[i] > smoothed[i - 1])
        and (i == len(smoothed) - 1 or smoothed[i] > smoothed[i + 1])
    ]
    if len(peak_idx) < 2:
        return FALLBACK_LADEN_RETURN_THRESHOLD, info

    # The two TALLEST peaks ("main modes"), then reordered low-to-high along the axis.
    top2 = sorted(peak_idx, key=lambda i: smoothed[i], reverse=True)[:2]
    left_idx, right_idx = sorted(top2)
    valley_idx = left_idx + int(np.argmin(smoothed[left_idx : right_idx + 1]))

    threshold = float(centers[valley_idx])
    info["fallback"] = False
    info["modes"] = [float(centers[left_idx]), float(centers[right_idx])]
    info["valley"] = threshold
    return threshold, info


def validate_against_gfw(
    con: duckdb.DuckDBPyConnection,
    trips_path: Path = TRIPS_PATH,
    port_visits_path: Path = GFW_PORT_VISITS_PATH,
    n_sample: int = 100,
    seed: int = 20260929,
    min_confidence: int = 3,
    rus_flag_value: str = "RUS",
    imo_col: str = "imo",
    start_col: str = "start",
    end_col: str = "end",
    confidence_col: str = "confidence",
    anchorage_flag_col: str = "start_anchorage_flag",
) -> dict | None:
    """Label-free validation gate (step 4 of the pre-registered spec). Samples up to `n_sample`
    laden-return trips (uniform, fixed `seed`, pooled across every month) and reports the share
    with a GFW port visit at a Russian-flagged anchorage overlapping [east_time, west_time].

    Returns None (logged, not raised) if `port_visits_path` does not exist yet -- P4-11 is a
    separate, concurrently-built task as of this session; see module docstring. Returns
    {"n_sampled", "n_with_rus_visit", "share"} otherwise ("share" is None, not NaN or ZeroDivision,
    when there are zero laden-return trips to sample from).

    **Column-name assumption, not yet verified against the real file**: `imo`, `start`, `end`
    (visit start/end timestamps), `confidence` (int, GFW's own scale), `start_anchorage_flag` (the
    START anchorage's ISO3 flag/country code) -- taken directly from
    `ingest.gfw_port_visits.PORT_VISITS_SCHEMA` (confirmed this session), but named here as keyword
    arguments anyway so a caller can override them if that schema changes before `port_visits.parquet`
    is actually built, without editing this function's body.
    A visit "between the two crossings" is read as temporal OVERLAP with [east_time, west_time],
    not "starts inside" -- a visit that straddles either crossing still counts as evidence the
    vessel was there during the round trip.
    """
    if not partition_exists(port_visits_path):
        logger.warning(
            "GFW port visits not built yet at %s -- validation gate is pending, not run",
            port_visits_path,
        )
        return None
    if not partition_exists(trips_path):
        raise FileNotFoundError(f"No trips table at {trips_path}; run build_baltic_trips first")

    laden = con.execute(
        "SELECT imo, east_time, west_time FROM read_parquet(?) "
        "WHERE is_laden_return ORDER BY imo, east_time",
        [str(trips_path)],
    ).fetchall()
    if not laden:
        return {"n_sampled": 0, "n_with_rus_visit": 0, "share": None}

    rng = random.Random(seed)
    sample = laden if len(laden) <= n_sample else rng.sample(laden, n_sample)

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _gfw_sample (imo VARCHAR, east_time TIMESTAMP, west_time TIMESTAMP)"
    )
    con.executemany("INSERT INTO _gfw_sample VALUES (?, ?, ?)", sample)

    (n_with,) = con.execute(
        "SELECT count(*) FROM _gfw_sample s WHERE EXISTS ("
        f"  SELECT 1 FROM read_parquet(?) v "
        f'  WHERE v."{imo_col}" = s.imo AND v."{confidence_col}" >= ? '
        f'    AND v."{anchorage_flag_col}" = ? '
        f'    AND v."{start_col}" < s.west_time AND v."{end_col}" > s.east_time'
        ")",
        [str(port_visits_path), min_confidence, rus_flag_value],
    ).fetchone()
    return {"n_sampled": len(sample), "n_with_rus_visit": n_with, "share": n_with / len(sample)}


def build_baltic_trips(
    in_root: Path = CLEAN_ROOT,
    thin_root: Path = THIN_ROOT,
    identity_root: Path = IDENTITY_ROOT,
    ship_type_root: Path = SHIP_TYPE_ROOT,
    out_path: Path = TRIPS_PATH,
    laden_return_threshold: float = LADEN_RETURN_THRESHOLD,
    first_monthly_window_start: date = FIRST_MONTHLY_WINDOW_START,
    last_archive_day: date = LAST_ARCHIVE_DAY,
    march_start: date = MARCH_START,
    march_end: date = MARCH_END,
    force: bool = False,
    threads: int | None = None,
) -> Path:
    """Run the whole P4-3h pipeline over the real archive (March 2024 clean data, then every
    calendar-month window April..the last archive day) and write ONE flat table to `out_path`.

    Idempotent: a no-op unless force=True or out_path is missing. Opens exactly one DuckDB
    connection, reused across every month -- mirroring every other module in this project.
    """
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    con = duckdb.connect()
    try:
        if threads is not None:
            con.execute(f"SET threads = {int(threads)}")

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _gate_positions "
            "(imo VARCHAR, mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, longitude DOUBLE)"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _draught_readings "
            "(imo VARCHAR, mmsi BIGINT, timestamp TIMESTAMP, draught DOUBLE)"
        )

        # March: ship_type resolved fresh from clean data, IMO mapping reused from April.
        april_identity_path = _window_partition_path(
            date(2024, 4, 1), date(2024, 4, 30), identity_root
        )
        n_roster = _build_march_roster(con, in_root, april_identity_path)
        logger.info("March 2024 roster: %d Tanker/valid-IMO mmsi", n_roster)
        _append_gate_positions_and_draughts_from_clean(con, march_start, march_end, in_root)

        for window_start, window_end in _month_windows(first_monthly_window_start, last_archive_day):
            n_roster = _build_roster(con, window_start, window_end, identity_root, ship_type_root)
            logger.info(
                "%s..%s roster: %d Tanker/valid-IMO mmsi",
                window_start.isoformat(), window_end.isoformat(), n_roster,
            )
            _append_gate_positions_from_thin(con, window_start, window_end, thin_root)
            _append_draught_readings_from_clean(con, window_start, window_end, in_root)

        (n_positions,) = con.execute("SELECT count(*) FROM _gate_positions").fetchone()
        (n_draughts,) = con.execute("SELECT count(*) FROM _draught_readings").fetchone()
        logger.info(
            "Gate-band positions: %d; draught readings: %d", n_positions, n_draughts
        )

        n_crossings = detect_crossings(con)
        n_trips_raw = pair_round_trips(con)
        logger.info("Crossings: %d; round-trip candidates: %d", n_crossings, n_trips_raw)
        compute_trip_draughts(con)

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _trips_final AS "
            "SELECT imo, east_mmsi, west_mmsi, east_time, west_time, "
            "east_latitude, east_longitude, west_latitude, west_longitude, "
            "east_draught, west_draught, max_draught_before_west, rel_change, "
            f"(rel_change >= {laden_return_threshold}) AS is_laden_return "
            "FROM _trips "
            "WHERE east_draught IS NOT NULL AND west_draught IS NOT NULL "
            "AND max_draught_before_west IS NOT NULL"
        )
        (n_final,) = con.execute("SELECT count(*) FROM _trips_final").fetchone()
        (n_laden,) = con.execute(
            "SELECT count(*) FROM _trips_final WHERE is_laden_return"
        ).fetchone()
        logger.info(
            "Round trips with usable draught: %d of %d candidate(s) (%d dropped for missing "
            "draught); %d laden return(s) at threshold %.3f",
            n_final, n_trips_raw, n_trips_raw - n_final, n_laden, laden_return_threshold,
        )

        built_at = datetime.now(timezone.utc)
        sha = git_sha()
        atomic_write_parquet(
            con,
            "SELECT *, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{sha}' AS git_sha "
            "FROM _trips_final ORDER BY imo, east_time",
            out_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the implied eastern-Baltic loading (draught-based) trips table over "
        "the whole archive (P4-3h)."
    )
    parser.add_argument("--in-dir", default=str(CLEAN_ROOT), help="Root of the clean Parquet partitions")
    parser.add_argument("--thin-dir", default=str(THIN_ROOT), help="Root of the thinned-track partitions")
    parser.add_argument("--identity-root", default=str(IDENTITY_ROOT))
    parser.add_argument("--ship-type-root", default=str(SHIP_TYPE_ROOT))
    parser.add_argument("--out-path", default=str(TRIPS_PATH))
    parser.add_argument(
        "--threads", type=int, default=None, help="Cap DuckDB threads (default: all cores)"
    )
    parser.add_argument("--force", action="store_true", help="Rebuild even if the output already exists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_baltic_trips(
        in_root=Path(args.in_dir),
        thin_root=Path(args.thin_dir),
        identity_root=Path(args.identity_root),
        ship_type_root=Path(args.ship_type_root),
        out_path=Path(args.out_path),
        threads=args.threads,
        force=args.force,
    )


if __name__ == "__main__":
    main()
