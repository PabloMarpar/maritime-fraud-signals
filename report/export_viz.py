"""Export the project's tables to small static files for the web map (``viz/``).

**What this is for.** The web front end (``viz/``, an Astro site) is static: it cannot query
DuckDB or read Parquet at the scale this project works at. This module reduces every built window
to a handful of files the browser loads directly, under ``viz/public/data/``:

- ``windows.json`` -- one entry per exported window: date range, map extent, counts.
- ``w/<start>_<end>/fleet.json`` + ``tracks.bin`` -- animated tracks of the window's *focus
  fleet* (see :func:`focus_fleet_sql`), resampled to :data:`TRACK_STEP_MINUTES`, split into trips
  at silences and impossible jumps, positions quantized to uint16 over :data:`MAP_BBOX`.
- ``w/<start>_<end>/events.json`` -- detector events to draw on the map: AIS gaps of the focus
  fleet, every ship-to-ship episode, spoofing events other than ``on_land`` (10M dockside rows in
  June, see ``detect/spoofing.py``), behaviour events.
- ``w/<start>_<end>/density.webp`` -- all traffic, as distinct vessels per pixel, rendered in Web
  Mercator so it can be stretched over :data:`MAP_BBOX` without reprojection.
- ``vessels/index.json`` -- every mmsi in every window, one short row each, for search.
- ``vessels/d/<mmsi>.json`` -- a dossier for every vessel with a valid IMO, every tanker, and every
  vessel with a detector event: identity, names, destinations, a simplified track and all events,
  per window.
- ``sanctions.json`` -- every sanctioned IMO in the frozen label snapshot, so the live page can
  flag a sanctioned vessel it sees.
- ``shadow.json`` -- one record per sanctioned vessel (by IMO) seen in any exported window, for
  the shadow-fleet page: names and flags used, when it was seen, its designations and how long
  after the first sighting they came (:func:`shadow_fleet`). Computed from the dossiers alone, so
  ``--shadow-only`` rebuilds it without touching ``data/``.

**Read-only on ``data/``.** Everything under ``data/`` is only read, through DuckDB, so this can
run while ``scripts/build_archive_windows.sh`` writes new windows. Cap ``--threads`` then.

**Nothing here is a model output.** The site shows what the pipeline measured and what the
sanctions lists say; prediction is deliberately out of scope until a model beats the R2 baseline
(``CLAUDE.md``).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np

from process.foc import is_foc
from process.partitions import existing_partitions, git_sha
from report.countries import iso2_of

logger = logging.getLogger(__name__)

PANEL_ROOT = Path("data/processed/panel")
THIN_ROOT = Path("data/tracks/thin")
CLEAN_ROOT = Path("data/clean/ais_dk")
DETECT_ROOT = Path("data/detect")
SANCTIONS_PATH = Path("data/reference/sanctions.parquet")
SANCTIONS_MATCHES_PATH = Path("data/identity/sanctions_matches.parquet")
OUT_ROOT = Path("viz/public/data")

# (west, south, east, north). Covers the 0.1th..99.9th percentile of every thinned position in
# the 2024-06 and 2024-11 windows (lon 2.4..18.4, lat 53.5..59.0) with a margin. Positions outside
# are receiver artifacts or far-away vessels and are dropped from the map export.
MAP_BBOX = (2.0, 53.3, 19.0, 59.3)

TRACK_STEP_MINUTES = 15
# A silence longer than this starts a new trip, so the map never draws a straight line across
# a gap (gaps are drawn separately, from detect.gaps).
TRIP_BREAK_MINUTES = 60
# An implied speed above this between consecutive resampled points also starts a new trip: it is
# a bad fix, not a voyage. Checked only when the points are at least 5 minutes apart, since GPS
# jitter over seconds implies absurd speeds.
MAX_TRACK_SPEED_KNOTS = 40.0
FOCUS_STS_MIN_CONFIDENCE = 0.5

DENSITY_WIDTH_PX = 2000
DOSSIER_SIMPLIFY_DEG = 0.002
DOSSIER_MAX_GAPS = 60
DOSSIER_MAX_SPOOF_EVENTS = 60

SPOOF_KINDS = ("impossible_speed", "synthetic_circle", "simultaneous_position")
ALL_SPOOF_KINDS = ("on_land", *SPOOF_KINDS)
BEHAVIOUR_KINDS = ("destination_course_mismatch", "draught_change_unexplained")
GAP_VERDICTS = ("receiver_alive", "area_dark", "no_evidence")

# Status codes shared with the front end (viz/src/lib/data.ts).
STATUS_NONE = 0
STATUS_LISTED_LATER = 1  # designated after the window ended
STATUS_LISTED_BY_END = 2  # already designated by the window's end

# All traffic density colour ramp, dark map: an achromatic cool-grey sequential ramp so the
# background recedes behind the three categorical hues the front end uses (validated with the
# dataviz skill's validator, see viz/src/styles/tokens.css).
DENSITY_RAMP = (
    (0.00, (27, 38, 56)),
    (0.35, (58, 76, 104)),
    (0.70, (132, 150, 178)),
    (1.00, (232, 238, 248)),
)

_WINDOW_DIR_RE = re.compile(r"^window=(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    @property
    def id(self) -> str:
        return f"{self.start.isoformat()}_{self.end.isoformat()}"

    @property
    def partition(self) -> str:
        return f"window={self.id}"

    @property
    def t0(self) -> datetime:
        return datetime(self.start.year, self.start.month, self.start.day)  # noqa: DTZ001


def discover_windows(panel_root: Path = PANEL_ROOT) -> list[Window]:
    """Every window with a built panel, oldest first. The panel is the last step of a window's
    build (``scripts/build_archive_windows.sh``), so its presence means the window is complete."""
    found = []
    if not panel_root.exists():
        return found
    for child in panel_root.iterdir():
        match = _WINDOW_DIR_RE.match(child.name)
        if match and any(child.glob("*.parquet")):
            found.append(
                Window(date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2)))
            )
    return sorted(found, key=lambda w: w.start)


# --------------------------------------------------------------------------------------------
# Small pure helpers (unit-tested)
# --------------------------------------------------------------------------------------------


def encode_polyline(coords: list[tuple[float, float]], precision: int = 5) -> str:
    """Google encoded-polyline string for (lon, lat) pairs. The format stores (lat, lon); the
    front end's decoder (viz/src/lib/polyline.ts) returns (lon, lat) again."""
    factor = 10**precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lon, lat in coords:
        lat_i = round(lat * factor)
        lon_i = round(lon * factor)
        for delta in (lat_i - prev_lat, lon_i - prev_lon):
            value = ~(delta << 1) if delta < 0 else (delta << 1)
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = lat_i, lon_i
    return "".join(out)


def quantize(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Map values in [lo, hi] to uint16 0..65535 (clipped). Inverse: lo + q / 65535 * (hi - lo)."""
    scaled = np.rint((values - lo) / (hi - lo) * 65535.0)
    return np.clip(scaled, 0, 65535).astype("<u2")


def trip_runs(vessel_idx: np.ndarray, trip: np.ndarray, min_points: int = 2) -> np.ndarray:
    """(vessel_idx, offset, count) for each run of equal (vessel_idx, trip) in already-sorted
    arrays, dropping runs shorter than ``min_points`` (a single point cannot be animated).

    Offsets index the ORIGINAL arrays; the caller keeps only the points inside kept runs and
    re-bases offsets with :func:`compact_runs`."""
    n = len(vessel_idx)
    if n == 0:
        return np.zeros((0, 3), dtype=np.int64)
    change = np.ones(n, dtype=bool)
    change[1:] = (vessel_idx[1:] != vessel_idx[:-1]) | (trip[1:] != trip[:-1])
    starts = np.flatnonzero(change)
    counts = np.diff(np.append(starts, n))
    keep = counts >= min_points
    return np.column_stack([vessel_idx[starts[keep]], starts[keep], counts[keep]]).astype(np.int64)


def compact_runs(runs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Given runs from :func:`trip_runs`, return (point_index, rebased_runs): the indices of the
    points to keep, in order, and the runs with offsets into the kept-points array."""
    if len(runs) == 0:
        return np.zeros(0, dtype=np.int64), runs
    index = np.concatenate([np.arange(o, o + c) for _v, o, c in runs])
    new_offsets = np.concatenate([[0], np.cumsum(runs[:-1, 2])])
    rebased = runs.copy()
    rebased[:, 1] = new_offsets
    return index, rebased


def mercator_y(lat_deg: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))


def density_size(bbox: tuple[float, float, float, float], width_px: int) -> tuple[int, int]:
    """(width, height) in pixels of a Web Mercator raster covering ``bbox`` at ``width_px``."""
    west, south, east, north = bbox
    dx = math.radians(east - west)
    dy = mercator_y(north) - mercator_y(south)
    return width_px, max(1, round(width_px * dy / dx))


def colorize_density(counts: np.ndarray) -> np.ndarray:
    """RGBA uint8 image from a (H, W) grid of counts. Log scale normalized to the 99.5th
    percentile of non-empty cells; empty cells are fully transparent."""
    out = np.zeros((*counts.shape, 4), dtype=np.uint8)
    nonzero = counts > 0
    if not nonzero.any():
        return out
    ceiling = max(float(np.percentile(counts[nonzero], 99.5)), 1.0)
    level = np.clip(np.log1p(counts) / math.log1p(ceiling), 0.0, 1.0)
    stops = np.array([s for s, _c in DENSITY_RAMP])
    for channel in range(3):
        values = np.array([c[channel] for _s, c in DENSITY_RAMP], dtype=float)
        out[..., channel] = np.interp(level, stops, values).astype(np.uint8)
    alpha = np.where(nonzero, 40 + 215 * level**0.9, 0)
    out[..., 3] = alpha.astype(np.uint8)
    return out


def status_of(listed_by_end: bool | None, listed_later: bool | None) -> int:
    if listed_by_end:
        return STATUS_LISTED_BY_END
    if listed_later:
        return STATUS_LISTED_LATER
    return STATUS_NONE


def _r(value: float | None, digits: int = 4) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def _minutes(ts: datetime | None, t0: datetime) -> int | None:
    if ts is None:
        return None
    return round((ts - t0).total_seconds() / 60.0)


def _iso(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%MZ")
    return value.isoformat()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), "utf-8")


# --------------------------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------------------------


def _union_sql(partitions: list[tuple[date, Path]], columns: str) -> str:
    """UNION ALL over day partitions (every module owns its own copy, see features.static)."""
    return " UNION ALL ".join(
        f"SELECT {columns} FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
    )


def _haversine_km_sql(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    """Great-circle distance in km, written out rather than ST_Distance_Sphere (whose argument
    order bit this project once, see docs/DECISIONS.md's P2-4 entry)."""
    return (
        f"2 * 6371.0 * asin(sqrt(pow(sin(radians({lat2} - {lat1}) / 2), 2) + "
        f"cos(radians({lat1})) * cos(radians({lat2})) * pow(sin(radians({lon2} - {lon1}) / 2), 2)))"
    )


def focus_fleet_sql() -> str:
    """The vessels animated on the map: every tanker, every vessel on a sanctions list, and both
    parties to every ship-to-ship episode with confidence >= FOCUS_STS_MIN_CONFIDENCE. Expects
    views ``panel`` and ``sts``."""
    return f"""
    SELECT mmsi FROM panel WHERE ship_type = 'Tanker' OR label_is_sanctioned_ever
    UNION SELECT mmsi_a FROM sts WHERE confidence >= {FOCUS_STS_MIN_CONFIDENCE}
    UNION SELECT mmsi_b FROM sts WHERE confidence >= {FOCUS_STS_MIN_CONFIDENCE}
    """


def track_points_sql(t0: datetime, step_minutes: int, mmsi_table: str) -> str:
    """Resampled, trip-split track points for the mmsi in ``mmsi_table``, from view ``thin``.

    One point per (mmsi, step bucket) -- the bucket's first fix, with its real timestamp. A new
    trip starts after a silence of more than TRIP_BREAK_MINUTES or an implied speed above
    MAX_TRACK_SPEED_KNOTS. Returns (mmsi, trip, tmin, lon, lat) ordered for :func:`trip_runs`.
    """
    west, south, east, north = MAP_BBOX
    t0_sql = f"TIMESTAMP '{t0.strftime('%Y-%m-%d %H:%M:%S')}'"
    dist = _haversine_km_sql("plat", "plon", "lat", "lon")
    return f"""
    WITH b AS (
        SELECT mmsi, timestamp AS ts, longitude AS lon, latitude AS lat,
               row_number() OVER (
                   PARTITION BY mmsi, time_bucket(INTERVAL '{int(step_minutes)} minutes', timestamp)
                   ORDER BY timestamp, longitude, latitude
               ) AS rn
        FROM thin
        WHERE mmsi IN (SELECT mmsi FROM {mmsi_table})
          AND longitude BETWEEN {west} AND {east} AND latitude BETWEEN {south} AND {north}
    ),
    p AS (SELECT mmsi, ts, lon, lat FROM b WHERE rn = 1),
    l AS (
        SELECT *, lag(ts) OVER w AS pts, lag(lon) OVER w AS plon, lag(lat) OVER w AS plat
        FROM p WINDOW w AS (PARTITION BY mmsi ORDER BY ts)
    ),
    f AS (
        SELECT *,
               CASE
                   WHEN pts IS NULL THEN 1
                   WHEN ts - pts > INTERVAL '{TRIP_BREAK_MINUTES} minutes' THEN 1
                   WHEN epoch(ts - pts) >= 300
                        AND {dist} / (epoch(ts - pts) / 3600.0) > {MAX_TRACK_SPEED_KNOTS} * 1.852
                        THEN 1
                   ELSE 0
               END AS brk
        FROM l
    )
    SELECT mmsi,
           CAST(sum(brk) OVER (PARTITION BY mmsi ORDER BY ts ROWS UNBOUNDED PRECEDING) AS INTEGER)
               AS trip,
           CAST(round(epoch(ts - {t0_sql}) / 60.0) AS INTEGER) AS tmin,
           lon, lat
    FROM f
    ORDER BY mmsi, trip, tmin
    """


# --------------------------------------------------------------------------------------------
# Per-window export
# --------------------------------------------------------------------------------------------


@dataclass
class WindowResult:
    window: Window
    summary: dict
    vessels: dict[int, dict]  # per-mmsi facts for the roster and the dossiers


def _create_views(con: duckdb.DuckDBPyConnection, w: Window) -> None:
    def detector(name: str) -> str:
        path = DETECT_ROOT / name / w.partition
        return f"read_parquet('{path.as_posix()}/*.parquet')"

    con.execute(
        f"CREATE OR REPLACE VIEW panel AS SELECT * FROM "
        f"read_parquet('{(PANEL_ROOT / w.partition).as_posix()}/*.parquet')"
    )
    for name in ("gaps", "sts", "spoofing", "behaviour", "identity_anomalies"):
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {detector(name)}")

    thin_parts = existing_partitions(w.start, w.end, THIN_ROOT)
    if not thin_parts:
        raise FileNotFoundError(f"No thin track partitions for window {w.id}")
    con.execute(
        "CREATE OR REPLACE VIEW thin AS "
        + _union_sql(thin_parts, "mmsi, timestamp, longitude, latitude")
    )
    clean_parts = existing_partitions(w.start, w.end, CLEAN_ROOT)
    if not clean_parts:
        raise FileNotFoundError(f"No clean partitions for window {w.id}")
    con.execute(
        "CREATE OR REPLACE VIEW clean AS "
        + _union_sql(
            clean_parts,
            "mmsi, timestamp, name, callsign, destination, length, width, draught",
        )
    )


def _export_tracks(
    con: duckdb.DuckDBPyConnection, w: Window, out_dir: Path, roster: dict[int, dict]
) -> dict:
    con.execute("CREATE OR REPLACE TEMP TABLE focus AS " + focus_fleet_sql())
    focus = [r[0] for r in con.execute("SELECT mmsi FROM focus ORDER BY mmsi").fetchall()]
    vessel_index = {m: i for i, m in enumerate(focus)}

    arrays = con.execute(track_points_sql(w.t0, TRACK_STEP_MINUTES, "focus")).fetchnumpy()
    mmsi = np.asarray(arrays["mmsi"], dtype=np.int64)
    vidx = np.array([vessel_index[m] for m in mmsi], dtype=np.int64)
    runs = trip_runs(vidx, np.asarray(arrays["trip"], dtype=np.int64))
    keep, runs = compact_runs(runs)

    west, south, east, north = MAP_BBOX
    lon_q = quantize(np.asarray(arrays["lon"], dtype=float)[keep], west, east)
    lat_q = quantize(np.asarray(arrays["lat"], dtype=float)[keep], south, north)
    tmin = np.asarray(arrays["tmin"], dtype=np.int64)[keep]
    if len(tmin) and (tmin.min() < 0 or tmin.max() > 65535):
        raise ValueError(f"Track minutes out of uint16 range in window {w.id}")
    t_q = tmin.astype("<u2")

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "tracks.bin").open("wb") as fh:
        fh.write(lon_q.tobytes())
        fh.write(lat_q.tobytes())
        fh.write(t_q.tobytes())

    vessels = []
    for m in focus:
        info = roster.get(m, {})
        vessels.append(
            [m, info.get("name"), info.get("ship_type"), info.get("iso2"), info.get("status", 0),
             info.get("length")]
        )
    _write_json(
        out_dir / "fleet.json",
        {
            "window": w.id,
            "t0": _iso(w.t0),
            "step": TRACK_STEP_MINUTES,
            "bbox": list(MAP_BBOX),
            "n": len(t_q),
            "vessels": vessels,
            "trips": runs.tolist(),
        },
    )
    logger.info(
        "%s: %d focus vessels, %d trips, %d points (%.1f MB)",
        w.id, len(focus), len(runs), len(t_q), len(t_q) * 6 / 1e6,
    )
    return {"n_focus": len(focus), "n_trips": len(runs), "n_points": len(t_q)}


def _export_density(con: duckdb.DuckDBPyConnection, w: Window, out_dir: Path) -> dict:
    from PIL import Image

    west, south, east, north = MAP_BBOX
    width, height = density_size(MAP_BBOX, DENSITY_WIDTH_PX)
    y0, y1 = mercator_y(south), mercator_y(north)
    rows = con.execute(
        f"""
        SELECT cx, cy, count(DISTINCT mmsi) AS n FROM (
            SELECT mmsi,
                   CAST(floor((longitude - {west}) / ({east} - {west}) * {width}) AS INTEGER) AS cx,
                   CAST(floor((ln(tan(pi() / 4 + radians(latitude) / 2)) - {y0}) / ({y1 - y0})
                              * {height}) AS INTEGER) AS cy
            FROM thin
            WHERE longitude BETWEEN {west} AND {east} AND latitude BETWEEN {south} AND {north}
        )
        WHERE cx BETWEEN 0 AND {width - 1} AND cy BETWEEN 0 AND {height - 1}
        GROUP BY cx, cy
        """
    ).fetchnumpy()
    grid = np.zeros((height, width), dtype=np.float64)
    grid[height - 1 - rows["cy"], rows["cx"]] = rows["n"]
    image = Image.fromarray(colorize_density(grid))
    out_dir.mkdir(parents=True, exist_ok=True)
    image.save(out_dir / "density.webp", format="WEBP", quality=88, method=6)
    return {"density_px": [width, height], "density_cells": int((grid > 0).sum())}


def _roster(con: duckdb.DuckDBPyConnection, w: Window) -> dict[int, dict]:
    """Per-mmsi identity for every vessel in the window: the panel's columns plus names,
    callsigns, destinations and dimensions from clean AIS."""
    roster: dict[int, dict] = {}
    for row in con.execute(
        """
        SELECT mmsi, imo, flag_country, ship_type, total_message_count, voyage_count,
               n_observed_days, label_is_sanctioned_as_of_window_end,
               label_is_sanctioned_after_window_end, label_earliest_designation_date
        FROM panel
        """
    ).fetchall():
        (m, imo, flag, ship_type, n_msg, n_voy, n_days, by_end, later, first_designation) = row
        roster[m] = {
            "imo": imo,
            "flag": flag,
            "iso2": iso2_of(flag),
            "foc": is_foc(flag),
            "ship_type": ship_type,
            "messages": n_msg,
            "voyages": n_voy,
            "observed_days": n_days,
            "status": status_of(by_end, later),
            "designated": _iso(first_designation),
            "names": [],
            "callsigns": [],
            "destinations": [],
        }

    # One scan of clean AIS for names, callsigns and destinations, with first/last sighting.
    ident = con.execute(
        """
        WITH c AS (
            SELECT mmsi, timestamp AS ts,
                   nullif(regexp_replace(upper(trim(name)), '\\s+', ' ', 'g'), '') AS name,
                   nullif(regexp_replace(upper(trim(callsign)), '\\s+', ' ', 'g'), '') AS callsign,
                   nullif(regexp_replace(upper(trim(destination)), '\\s+', ' ', 'g'), '') AS dest
            FROM clean
        )
        SELECT mmsi, GROUPING(name, callsign, dest) AS g, name, callsign, dest,
               count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts
        FROM c
        GROUP BY GROUPING SETS ((mmsi, name), (mmsi, callsign), (mmsi, dest))
        """
    ).fetchall()
    # GROUPING bit set = column aggregated away: (mmsi, name) -> 0b011, callsign -> 0b101,
    # dest -> 0b110.
    field_of = {3: ("names", 2), 5: ("callsigns", 3), 6: ("destinations", 4)}
    for row in ident:
        m, g = row[0], row[1]
        if m not in roster or g not in field_of:
            continue
        field, col = field_of[g]
        value = row[col]
        if value is None or value == "UNKNOWN":
            continue
        roster[m][field].append([value, row[5], _iso(row[6]), _iso(row[7])])
    for info in roster.values():
        for field in ("names", "callsigns", "destinations"):
            info[field].sort(key=lambda r: -r[1])
        info["name"] = info["names"][0][0] if info["names"] else None
        info["callsign"] = info["callsigns"][0][0] if info["callsigns"] else None

    for m, length, width, dmin, dmax in con.execute(
        """
        SELECT mmsi, median(length) FILTER (WHERE length > 0),
               median(width) FILTER (WHERE width > 0),
               min(draught) FILTER (WHERE draught > 0), max(draught) FILTER (WHERE draught > 0)
        FROM clean GROUP BY mmsi
        """
    ).fetchall():
        if m in roster:
            roster[m]["length"] = _r(length, 0)
            roster[m]["width"] = _r(width, 0)
            roster[m]["draught"] = [_r(dmin, 1), _r(dmax, 1)] if dmin is not None else None
    return roster


def _export_events(
    con: duckdb.DuckDBPyConnection, w: Window, out_dir: Path, roster: dict[int, dict]
) -> dict:
    t0 = w.t0
    west, south, east, north = MAP_BBOX
    in_box = f"longitude BETWEEN {west} AND {east} AND latitude BETWEEN {south} AND {north}"

    gaps = [
        [m, _minutes(a, t0), _minutes(b, t0), _r(x1), _r(y1), _r(x2), _r(y2), _r(p, 2),
         GAP_VERDICTS.index(v) if v in GAP_VERDICTS else -1, _r(h, 1)]
        for m, a, b, x1, y1, x2, y2, p, v, h in con.execute(
            """
            SELECT mmsi, gap_start, gap_end, start_longitude, start_latitude, end_longitude,
                   end_latitude, probability, verdict, duration_hours
            FROM gaps WHERE mmsi IN (SELECT mmsi FROM focus) ORDER BY gap_start
            """
        ).fetchall()
    ]
    sts = [
        [a, b, _minutes(s, t0), _minutes(e, t0), _r(x), _r(y), _r(c, 2), _r(h, 1)]
        for a, b, s, e, x, y, c, h in con.execute(
            """
            SELECT mmsi_a, mmsi_b, start_time, end_time, longitude, latitude, confidence,
                   duration_hours
            FROM sts ORDER BY start_time
            """
        ).fetchall()
    ]
    kinds = ", ".join(f"'{k}'" for k in SPOOF_KINDS)
    spoof = [
        [m, SPOOF_KINDS.index(k), _minutes(t, t0), _r(x), _r(y), _r(c, 2)]
        for m, k, t, x, y, c in con.execute(
            f"""
            SELECT mmsi, kind, event_time, longitude, latitude, confidence
            FROM spoofing WHERE kind IN ({kinds}) AND {in_box} ORDER BY event_time
            """
        ).fetchall()
    ]
    behav = [
        [m, BEHAVIOUR_KINDS.index(k), _minutes(t, t0), _r(x), _r(y), _r(c, 2)]
        for m, k, t, x, y, c in con.execute(
            f"""
            SELECT mmsi, kind, event_time, longitude, latitude, confidence
            FROM behaviour WHERE {in_box} ORDER BY event_time
            """
        ).fetchall()
        if k in BEHAVIOUR_KINDS
    ]

    referenced = {r[0] for r in gaps + spoof + behav} | {r[0] for r in sts} | {r[1] for r in sts}
    names = {
        str(m): [roster[m].get("name"), roster[m].get("iso2"), roster[m].get("ship_type"),
                 roster[m].get("status", 0)]
        for m in sorted(referenced)
        if m in roster
    }
    _write_json(
        out_dir / "events.json",
        {
            "codes": {"gap": GAP_VERDICTS, "spoof": SPOOF_KINDS, "behav": BEHAVIOUR_KINDS},
            "gaps": gaps,
            "sts": sts,
            "spoof": spoof,
            "behav": behav,
            "names": names,
        },
    )
    return {"gaps": len(gaps), "sts": len(sts), "spoof": len(spoof), "behav": len(behav)}


def _dossier_facts(
    con: duckdb.DuckDBPyConnection, w: Window, roster: dict[int, dict]
) -> set[int]:
    """Add per-window dossier facts (track, daily presence, events) to ``roster`` for every
    dossier vessel, and return that set of mmsi."""
    t0 = w.t0
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE dossier AS
        SELECT mmsi FROM panel
        WHERE imo IS NOT NULL OR ship_type = 'Tanker' OR label_is_sanctioned_ever
           OR n_sts_episodes > 0 OR n_identity_anomalies_total > 0
           OR n_destination_course_mismatch > 0 OR n_draught_change_unexplained > 0
           OR n_impossible_speed + n_synthetic_circle + n_simultaneous_position > 0
        """
    )
    members = {r[0] for r in con.execute("SELECT mmsi FROM dossier").fetchall()}
    for m in members:
        roster[m]["tracks"] = []
        roster[m]["hours_by_day"] = [0] * ((w.end - w.start).days + 1)
        roster[m]["events"] = {
            "gaps": [], "n_gaps": 0, "sts": [], "spoof": [], "spoof_counts": {}, "behav": [],
            "identity": [],
        }

    # Simplified track, one encoded polyline per trip.
    con.execute("LOAD spatial")
    for m, trip, geojson in con.execute(
        f"""
        WITH pts AS ({track_points_sql(t0, 5, "dossier")})
        SELECT mmsi, trip,
               ST_AsGeoJSON(ST_Simplify(ST_MakeLine(list(ST_Point(lon, lat) ORDER BY tmin)),
                                        {DOSSIER_SIMPLIFY_DEG}))
        FROM pts GROUP BY mmsi, trip HAVING count(*) >= 2 ORDER BY mmsi, trip
        """
    ).fetchall():
        coords = json.loads(geojson)["coordinates"] if geojson else []
        if len(coords) >= 2 and isinstance(coords[0], list):
            roster[m]["tracks"].append(encode_polyline([(c[0], c[1]) for c in coords]))

    # Last fix in the window: a vessel moored all month has no simplified track to draw.
    for m, lon, lat, ts in con.execute(
        """
        SELECT mmsi, arg_max(longitude, timestamp), arg_max(latitude, timestamp), max(timestamp)
        FROM thin WHERE mmsi IN (SELECT mmsi FROM dossier) GROUP BY mmsi
        """
    ).fetchall():
        roster[m]["last_pos"] = [_r(lon), _r(lat), _iso(ts)]

    # Hours observed per day (thin tracks are one fix per 5 minutes when the vessel is heard).
    for m, day, n in con.execute(
        """
        SELECT mmsi, CAST(timestamp AS DATE) AS day, count(*) FROM thin
        WHERE mmsi IN (SELECT mmsi FROM dossier) GROUP BY 1, 2
        """
    ).fetchall():
        i = (day - w.start).days
        if 0 <= i < len(roster[m]["hours_by_day"]):
            roster[m]["hours_by_day"][i] = round(n / 12.0, 1)

    for m, a, b, h, p, v, x1, y1, x2, y2 in con.execute(
        """
        SELECT mmsi, gap_start, gap_end, duration_hours, probability, verdict,
               start_longitude, start_latitude, end_longitude, end_latitude
        FROM gaps WHERE mmsi IN (SELECT mmsi FROM dossier)
        ORDER BY mmsi, probability DESC, duration_hours DESC
        """
    ).fetchall():
        ev = roster[m]["events"]
        ev["n_gaps"] += 1
        if len(ev["gaps"]) < DOSSIER_MAX_GAPS:
            ev["gaps"].append(
                [_iso(a), _iso(b), _r(h, 1), _r(p, 2), v, _r(x1), _r(y1), _r(x2), _r(y2)]
            )
    for ev_m in members:
        roster[ev_m]["events"]["gaps"].sort(key=lambda g: g[0] or "")

    for a, b, s, e, h, c, x, y in con.execute(
        """
        SELECT mmsi_a, mmsi_b, start_time, end_time, duration_hours, confidence, longitude,
               latitude
        FROM sts ORDER BY start_time
        """
    ).fetchall():
        for me, other in ((a, b), (b, a)):
            if me in members:
                o = roster.get(other, {})
                roster[me]["events"]["sts"].append(
                    [other, o.get("name"), o.get("iso2"), o.get("ship_type"), _iso(s), _iso(e),
                     _r(h, 1), _r(c, 2), _r(x), _r(y)]
                )

    for m, k, n in con.execute(
        "SELECT mmsi, kind, count(*) FROM spoofing WHERE mmsi IN (SELECT mmsi FROM dossier) "
        "GROUP BY 1, 2"
    ).fetchall():
        roster[m]["events"]["spoof_counts"][k] = n
    kinds = ", ".join(f"'{k}'" for k in SPOOF_KINDS)
    for m, k, t, x, y, c in con.execute(
        f"""
        SELECT mmsi, kind, event_time, longitude, latitude, confidence FROM (
            SELECT *, row_number() OVER (PARTITION BY mmsi ORDER BY event_time) AS rn
            FROM spoofing WHERE kind IN ({kinds}) AND mmsi IN (SELECT mmsi FROM dossier)
        ) WHERE rn <= {DOSSIER_MAX_SPOOF_EVENTS}
        ORDER BY mmsi, event_time
        """
    ).fetchall():
        roster[m]["events"]["spoof"].append([k, _iso(t), _r(x), _r(y), _r(c, 2)])

    for m, k, t, x, y, c, detail in con.execute(
        """
        SELECT mmsi, kind, event_time, longitude, latitude, confidence, detail
        FROM behaviour WHERE mmsi IN (SELECT mmsi FROM dossier) ORDER BY event_time
        """
    ).fetchall():
        roster[m]["events"]["behav"].append([k, _iso(t), _r(x), _r(y), _r(c, 2), detail])

    for m, k, t, field, old, new, shared, related, detail in con.execute(
        """
        SELECT mmsi, kind, event_time, identity_field, old_value, new_value, shared_value,
               related_mmsi, detail
        FROM identity_anomalies WHERE mmsi IN (SELECT mmsi FROM dossier) ORDER BY event_time
        """
    ).fetchall():
        roster[m]["events"]["identity"].append(
            [k, _iso(t), field, old, new, shared, related, detail]
        )
    return members


def export_window(con: duckdb.DuckDBPyConnection, w: Window, out_root: Path) -> WindowResult:
    out_dir = out_root / "w" / w.id
    _create_views(con, w)
    roster = _roster(con, w)
    summary = {"id": w.id, "start": w.start.isoformat(), "end": w.end.isoformat(),
               "t0": _iso(w.t0), "n_vessels": len(roster)}
    summary.update(_export_tracks(con, w, out_dir, roster))
    summary["counts"] = _export_events(con, w, out_dir, roster)
    summary.update(_export_density(con, w, out_dir))
    members = _dossier_facts(con, w, roster)
    for m, info in roster.items():
        info["dossier"] = m in members
    summary["n_dossiers"] = len(members)
    logger.info("%s: %s", w.id, summary)
    return WindowResult(w, summary, roster)


# --------------------------------------------------------------------------------------------
# Cross-window outputs
# --------------------------------------------------------------------------------------------


# Sanctions regime from a designation's programme text: the site says how many listed vessels
# are there over Russia and how many over Iran, rather than calling them all "Russian".
REGIME_NEEDLES = (("russia", ("RUSSIA", "UKRAINE", "PEESA")), ("iran", ("IRAN",)))


def sanction_regime(program: str | None) -> str:
    """'russia', 'iran' or 'other' for a sanctions programme name (OFAC/UK/EU wording)."""
    text = (program or "").upper()
    for regime, needles in REGIME_NEEDLES:
        if any(n in text for n in needles):
            return regime
    return "other"


def _window_start(window_id: str) -> date:
    return date.fromisoformat(window_id.split("_")[0])


def shadow_fleet(dossiers: list[dict]) -> list[dict]:
    """One record per sanctioned vessel seen, grouped by IMO across its mmsi (a new flag means a
    new mmsi), from the per-mmsi dossiers ``_write_vessels`` writes.

    A vessel counts when any of its mmsi carries a sanctions record (``dossier["sanctions"]``,
    matched by IMO). ``first``/``last`` are the first and last day with any observed hour.
    ``lead_days`` is the earliest designation minus the first sighting: positive when the vessel
    was in Danish waters before it was sanctioned.
    """
    groups: dict[str, list[dict]] = {}
    for d in dossiers:
        if d.get("sanctions"):
            groups.setdefault(d.get("imo") or f"mmsi:{d['mmsi']}", []).append(d)

    out = []
    for imo, members in groups.items():
        days: set[date] = set()
        hours_by_mmsi: dict[int, float] = {}
        names: dict[str, int] = {}
        dests: dict[str, int] = {}
        flags: list[tuple[date, str]] = []
        types: dict[str, int] = {}
        windows: set[str] = set()
        events = {"gaps": 0, "sts": 0, "draught": 0, "dest": 0, "spoof": 0}
        designations: dict[tuple[str, str], str] = {}
        for d in members:
            m = d["mmsi"]
            for source, _name, _flag, program, designated in d["sanctions"]:
                if designated:
                    key = (source, designated)
                    # Several programmes per source and day: prefer a named regime over "other".
                    if designations.get(key, "other") == "other":
                        designations[key] = sanction_regime(program)
            if d.get("ship_type"):
                types[d["ship_type"]] = types.get(d["ship_type"], 0) + 1
            first_seen_here: date | None = None
            for wid, info in d.get("windows", {}).items():
                start = _window_start(wid)
                hours = info.get("hours_by_day") or []
                seen = [date.fromordinal(start.toordinal() + i) for i, h in enumerate(hours) if h]
                if not seen:
                    continue
                windows.add(wid)
                days.update(seen)
                hours_by_mmsi[m] = hours_by_mmsi.get(m, 0.0) + sum(hours)
                earliest_here = min(seen)
                if first_seen_here is None or earliest_here < first_seen_here:
                    first_seen_here = earliest_here
                for name, n, *_ in info.get("names") or []:
                    names[name] = names.get(name, 0) + n
                for dest, n, *_ in info.get("destinations") or []:
                    dests[dest] = dests.get(dest, 0) + n
                ev = info.get("events") or {}
                events["gaps"] += ev.get("n_gaps", 0)
                events["sts"] += len(ev.get("sts") or [])
                for kind, *_ in ev.get("behav") or []:
                    if kind == "draught_change_unexplained":
                        events["draught"] += 1
                    elif kind == "destination_course_mismatch":
                        events["dest"] += 1
                events["spoof"] += sum(
                    n for k, n in (ev.get("spoof_counts") or {}).items() if k in SPOOF_KINDS
                )
            if first_seen_here is not None and d.get("iso2"):
                flags.append((first_seen_here, d["iso2"]))
        if not days:
            continue
        first, last = min(days), max(days)
        dated = sorted((day, source, regime) for (source, day), regime in designations.items())
        earliest = date.fromisoformat(dated[0][0]) if dated else None
        by_hours = sorted(hours_by_mmsi, key=lambda m: -hours_by_mmsi[m])
        ordered_flags: list[str] = []
        for _, iso2 in sorted(flags):
            if iso2 not in ordered_flags:
                ordered_flags.append(iso2)
        out.append({
            "imo": None if imo.startswith("mmsi:") else imo,
            "name": max(names, key=names.__getitem__) if names else None,
            "names": sorted(names, key=lambda n: -names[n]),
            "flags": ordered_flags,
            "mmsi": by_hours,
            "dossier": by_hours[0],
            "type": max(types, key=types.__getitem__) if types else None,
            "length": max((d.get("length") or 0 for d in members), default=0) or None,
            "first": first.isoformat(),
            "last": last.isoformat(),
            "days": len(days),
            "hours": round(sum(hours_by_mmsi.values()), 1),
            "windows": sorted(windows),
            "designations": [[source, day, regime] for day, source, regime in dated],
            "designated": earliest.isoformat() if earliest else None,
            "lead_days": (earliest - first).days if earliest else None,
            "events": events,
            "destinations": sorted(dests, key=lambda x: -dests[x])[:3],
        })
    out.sort(key=lambda r: (r["designated"] or "9999", r["name"] or ""))
    return out


def _write_shadow(dossiers: list[dict], out_root: Path, snapshot: str | None) -> int:
    fleet = shadow_fleet(dossiers)
    _write_json(out_root / "shadow.json", {"sanctions_snapshot": snapshot, "vessels": fleet})
    return len(fleet)


def write_shadow_from_export(out_root: Path = OUT_ROOT) -> int:
    """Rebuild ``shadow.json`` from an existing export's dossiers, without reading ``data/``."""
    dossiers = [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((out_root / "vessels" / "d").glob("*.json"))]
    meta = json.loads((out_root / "windows.json").read_text(encoding="utf-8"))
    n = _write_shadow(dossiers, out_root, meta.get("sanctions_snapshot"))
    logger.info("shadow.json: %d sanctioned vessels from %d dossiers", n, len(dossiers))
    return n


def _sanctions(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, list], dict[int, list]]:
    """(by_imo, by_mmsi): every record of the frozen sanctions snapshot keyed by IMO, and the
    records matched to each AIS mmsi by process.sanctions_match."""
    by_imo: dict[str, list] = {}
    for source, name, imo, flag, program, designated in con.execute(
        f"""
        SELECT source, vessel_name, imo, flag, program, designation_date
        FROM read_parquet('{SANCTIONS_PATH.as_posix()}')
        WHERE imo IS NOT NULL ORDER BY designation_date
        """
    ).fetchall():
        by_imo.setdefault(imo, []).append([source, name, flag, program, _iso(designated)])
    by_mmsi: dict[int, list] = {}
    for m, source, name, flag, program, designated in con.execute(
        f"""
        SELECT mmsi, source, vessel_name, sanctions_flag, program, designation_date
        FROM read_parquet('{SANCTIONS_MATCHES_PATH.as_posix()}') ORDER BY designation_date
        """
    ).fetchall():
        by_mmsi.setdefault(m, []).append([source, name, flag, program, _iso(designated)])
    return by_imo, by_mmsi


def _write_vessels(
    results: list[WindowResult], out_root: Path, by_mmsi: dict[int, list]
) -> tuple[dict, list[dict]]:
    index: dict[int, list] = {}
    dossiers: dict[int, dict] = {}
    for bit, result in enumerate(results):
        wid = result.window.id
        for m, info in result.vessels.items():
            row = index.get(m)
            if row is None:
                # [mmsi, name, imo, iso2, ship_type, length, status, has_dossier, windows_mask]
                row = [m, None, None, None, None, None, 0, 0, 0]
                index[m] = row
            row[1] = info.get("name") or row[1]
            row[2] = info.get("imo") or row[2]
            row[3] = info.get("iso2") or row[3]
            row[4] = info.get("ship_type") or row[4]
            row[5] = info.get("length") or row[5]
            row[6] = 1 if (info.get("status") or m in by_mmsi) else row[6]
            row[8] |= 1 << bit
            if not info.get("dossier"):
                continue
            row[7] = 1
            d = dossiers.setdefault(m, {"mmsi": m, "windows": {}})
            for key in ("imo", "name", "callsign", "flag", "iso2", "foc", "ship_type", "length",
                        "width"):
                if info.get(key) is not None:
                    d[key] = info[key]
            d["windows"][wid] = {
                key: info.get(key)
                for key in ("status", "designated", "messages", "voyages", "observed_days",
                            "draught", "names", "callsigns", "destinations", "tracks",
                            "last_pos", "hours_by_day", "events")
            }
    dossier_dir = out_root / "vessels" / "d"
    if dossier_dir.exists():
        shutil.rmtree(dossier_dir)
    for m, d in dossiers.items():
        d["sanctions"] = by_mmsi.get(m, [])
        _write_json(dossier_dir / f"{m}.json", d)
    _write_json(
        out_root / "vessels" / "index.json",
        {
            "windows": [r.window.id for r in results],
            "fields": ["mmsi", "name", "imo", "iso2", "ship_type", "length", "listed",
                       "dossier", "windows"],
            "rows": sorted(index.values(), key=lambda r: (r[1] is None, r[1] or "", r[0])),
        },
    )
    return {"n_index": len(index), "n_dossiers": len(dossiers)}, list(dossiers.values())


def export_all(
    windows: list[Window], out_root: Path = OUT_ROOT, threads: int | None = 4
) -> dict:
    con = duckdb.connect()
    try:
        if threads is not None:
            con.execute(f"SET threads = {int(threads)}")
        results = [export_window(con, w, out_root) for w in windows]
        by_imo, by_mmsi = _sanctions(con)
        snapshot = con.execute(
            f"SELECT max(built_at) FROM read_parquet('{SANCTIONS_PATH.as_posix()}')"
        ).fetchone()[0]
    finally:
        con.close()

    vessels, dossiers = _write_vessels(results, out_root, by_mmsi)
    _write_json(out_root / "sanctions.json", by_imo)
    vessels["n_shadow"] = _write_shadow(dossiers, out_root, _iso(snapshot))
    meta = {
        "built_at": _iso(datetime.now(timezone.utc)),
        "git_sha": git_sha(),
        "sanctions_snapshot": _iso(snapshot),
        "bbox": list(MAP_BBOX),
        "windows": [r.summary for r in results],
        **vessels,
    }
    _write_json(out_root / "windows.json", meta)
    logger.info("Export done: %d windows, %s", len(results), vessels)
    return meta


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export built windows to small static files for the web map (viz/)."
    )
    parser.add_argument(
        "--windows",
        nargs="*",
        help="Window ids <start>_<end> to export (default: every window with a built panel)",
    )
    parser.add_argument("--out", default=str(OUT_ROOT), help="Output directory")
    parser.add_argument(
        "--shadow-only", action="store_true",
        help="Only rebuild shadow.json from an existing export (reads no data/)",
    )
    parser.add_argument(
        "--threads", type=int, default=4, help="Cap DuckDB threads (default 4, leaves CPU for "
        "a concurrent archive build)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    if args.shadow_only:
        write_shadow_from_export(Path(args.out))
        return
    windows = discover_windows()
    if args.windows:
        wanted = set(args.windows)
        windows = [w for w in windows if w.id in wanted]
        missing = wanted - {w.id for w in windows}
        if missing:
            raise SystemExit(f"No built panel for window(s): {sorted(missing)}")
    if not windows:
        raise SystemExit("No built windows found under " + str(PANEL_ROOT))
    export_all(windows, Path(args.out), args.threads)


if __name__ == "__main__":
    main()
