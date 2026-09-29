"""P4-3j inputs: per vessel-month token tables for our own vessel encoder (``model.vessel_encoder``).

Design pre-registered in ``docs/DECISIONS.md`` (2026-09-29, "P4-3j ... detailed design"). This
module only turns one window's clean AIS and detector outputs into tokens; it knows nothing about
labels.

**Track tokens** -- one row per (mmsi, observed hour) for every panel vessel with a valid IMO:

- ``hour_idx``: hours since window_start (the only positional input).
- Channels (:data:`TRACK_CHANNELS`): log1p messages, SOG mean/max/std, COG dispersion within the
  hour (1 - mean resultant length over points with SOG > 0.5 kn), |COG change| vs the previous
  observed hour (/180), mean |heading - COG| (/180, SOG > 1 kn, valid heading), max draught and a
  missing flag, draught change vs the last known draught, nav-status fractions, log1p distance to
  land in km (0.05-degree grid, capped at :data:`LAND_DISTANCE_CAP_KM`), a flag for being inside a
  coastal anchorage cell of the window's OWN mask, the share of the hour's messages that
  ``detect.spoofing`` flagged ``on_land``, and log1p hours since the previous observed hour.
- ``region_id``: one of 12 coarse regions (3 latitude x 4 longitude bands) of the hour's mean
  position. **A training target only** (next-month region), never an input; latitude and
  longitude themselves are not stored.

**Event tokens** -- one row per detector event of that vessel in that window (gaps, sts,
behaviour, identity anomalies, spoofing except on_land): ``type`` (:data:`EVENT_TYPES`),
``hour_idx``, ``log_duration_h``, ``score``; at most :data:`MAX_EVENTS` per vessel-month, earliest
first.

Everything is computed from the window's own partitions, so a token never depends on data after
its window's end.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import duckdb

from ingest.landmask import LAND_PATH
from process.partitions import (
    atomic_write_parquet,
    existing_partitions,
    window_partition_path,
)

logger = logging.getLogger(__name__)

CLEAN_ROOT = Path("data/clean/ais_dk")
PANEL_ROOT = Path("data/processed/panel")
DETECT_ROOT = Path("data/detect")
ANCHORAGES_ROOT = Path("data/coverage/anchorages")
LAND_GRID_PATH = Path("data/reference/land_distance_grid.parquet")
TRACK_ROOT = Path("data/processed/tokens/track")
EVENT_ROOT = Path("data/processed/tokens/event")

LAND_GRID_DEG = 0.05
LAND_GRID_BBOX = (53.5, 59.5, 2.5, 17.0)  # lat_min, lat_max, lon_min, lon_max
LAND_DISTANCE_CAP_KM = 50.0
LAND_PREFILTER_DEG = 0.8
ANCHORAGE_CELL_DEG = 0.01

# 3 latitude x 4 longitude bands over the Danish/Baltic area -> region_id 0..11.
REGION_LAT_EDGES = (55.5, 57.3)
REGION_LON_EDGES = (9.0, 11.0, 13.0)
N_REGIONS = (len(REGION_LAT_EDGES) + 1) * (len(REGION_LON_EDGES) + 1)

TRACK_CHANNELS = (
    "log_msgs",
    "sog_mean",
    "sog_max",
    "sog_std",
    "cog_dispersion",
    "cog_change",
    "heading_cog_diff",
    "draught",
    "draught_missing",
    "draught_delta",
    "frac_underway",
    "frac_anchor",
    "frac_moored",
    "frac_restricted",
    "log_dist_land_km",
    "in_anchorage",
    "on_land_frac",
    "log_dt_h",
)

EVENT_TYPES = (
    "gap",
    "sts",
    "beh_destination_course_mismatch",
    "beh_draught_change_unexplained",
    "id_no_valid_imo",
    "id_name_change",
    "id_name_flapping",
    "id_callsign_change",
    "id_callsign_flapping",
    "id_reused_mmsi",
    "id_shared_imo",
    "id_shared_identity",
    "spoof_impossible_speed",
    "spoof_synthetic_circle",
    "spoof_simultaneous_position",
    "other",
)
MAX_EVENTS = 64

_UNDERWAY = "('Under way using engine', 'Under way sailing')"
_RESTRICTED = "('Restricted maneuverability', 'Constrained by her draught')"


def build_land_grid(land_path: Path = LAND_PATH, out_path: Path = LAND_GRID_PATH, force: bool = False) -> Path:
    """Distance to land (km, capped) and an on-land flag for every 0.05-degree cell centre in
    :data:`LAND_GRID_BBOX`. Static geography, built once."""
    if out_path.exists() and not force:
        return out_path
    lat0, lat1, lon0, lon1 = LAND_GRID_BBOX
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute(
            "CREATE TEMP TABLE _land AS SELECT (UNNEST(ST_Dump(geom))).geom AS geom "
            "FROM read_parquet(?)",
            [str(land_path)],
        )
        con.execute(
            f"""
            CREATE TEMP TABLE _cells AS
            SELECT i AS lat_i, j AS lon_i,
                   {lat0} + (i + 0.5) * {LAND_GRID_DEG} AS lat,
                   {lon0} + (j + 0.5) * {LAND_GRID_DEG} AS lon
            FROM range(0, {round((lat1 - lat0) / LAND_GRID_DEG)}) r1(i),
                 range(0, {round((lon1 - lon0) / LAND_GRID_DEG)}) r2(j)
            """
        )
        # ST_Distance_Sphere takes ST_Point(lat, lon) in this DuckDB build (see detect.anchorages).
        con.execute(
            f"""
            CREATE TEMP TABLE _grid AS
            SELECT c.lat_i, c.lon_i,
                EXISTS (SELECT 1 FROM _land l WHERE ST_Contains(l.geom, ST_Point(c.lon, c.lat)))
                    AS on_land,
                (SELECT min(ST_Distance_Sphere(ST_Point(ST_Y(p), ST_X(p)), ST_Point(c.lat, c.lon)))
                 FROM (SELECT ST_ClosestPoint(l.geom, ST_Point(c.lon, c.lat)) AS p FROM _land l
                       WHERE ST_DWithin(l.geom, ST_Point(c.lon, c.lat), {LAND_PREFILTER_DEG})))
                    AS dist_m
            FROM _cells c
            """
        )
        atomic_write_parquet(
            con,
            f"SELECT lat_i, lon_i, on_land, CASE WHEN on_land THEN 0.0 ELSE "
            f"least(coalesce(dist_m / 1000.0, {LAND_DISTANCE_CAP_KM}), {LAND_DISTANCE_CAP_KM}) "
            f"END AS dist_land_km FROM _grid ORDER BY lat_i, lon_i",
            out_path,
        )
    finally:
        con.close()
    logger.info("Wrote %s", out_path)
    return out_path


def _clip(expr: str, lo: float, hi: float, null_value: float = 0.0) -> str:
    """Clamp `expr` to [lo, hi], mapping NULL to `null_value`. Never use least()/greatest() for
    this: DuckDB's skip NULLs, so least(NULL, 30) is 30 (a missing draught became 30 m before this
    helper existed)."""
    return (
        f"CASE WHEN ({expr}) IS NULL THEN {null_value} "
        f"WHEN ({expr}) < {lo} THEN {lo} WHEN ({expr}) > {hi} THEN {hi} ELSE ({expr}) END"
    )


def _roster_sql(panel_path: Path) -> str:
    """Panel vessels with a valid IMO -- NOT filtered by sanctions status (that is the label)."""
    return f"SELECT DISTINCT mmsi FROM read_parquet('{panel_path.as_posix()}') WHERE imo IS NOT NULL"


def _region_sql(lat: str, lon: str) -> str:
    lat_band = " + ".join(f"({lat} >= {e})::INT" for e in REGION_LAT_EDGES)
    lon_band = " + ".join(f"({lon} >= {e})::INT" for e in REGION_LON_EDGES)
    return f"(({lat_band}) * {len(REGION_LON_EDGES) + 1} + ({lon_band}))"


def track_tokens_sql(
    clean_paths: list[Path],
    roster_sql: str,
    window_start: date,
    land_grid_path: Path,
    anchorages_path: Path | None,
    on_land_path: Path | None,
) -> str:
    """SELECT producing the track-token table (see module docstring)."""
    files = ", ".join(f"'{p.as_posix()}'" for p in clean_paths)
    lat0, _lat1, lon0, _lon1 = LAND_GRID_BBOX
    anchor_cte = (
        f"""anchor AS (
            SELECT DISTINCT round(cell_lat, 2) AS cell_lat, round(cell_lon, 2) AS cell_lon
            FROM read_parquet('{anchorages_path.as_posix()}') WHERE is_coastal),"""
        if anchorages_path is not None
        else "anchor AS (SELECT NULL::DOUBLE AS cell_lat, NULL::DOUBLE AS cell_lon WHERE false),"
    )
    on_land_cte = (
        f"""on_land AS (
            SELECT mmsi, date_trunc('hour', event_time) AS hour_ts, count(*) AS n_on_land
            FROM read_parquet('{on_land_path.as_posix()}') WHERE kind = 'on_land'
            GROUP BY ALL),"""
        if on_land_path is not None
        else "on_land AS (SELECT NULL::BIGINT AS mmsi, NULL::TIMESTAMP AS hour_ts, "
        "0 AS n_on_land WHERE false),"
    )
    moving = "sog > 0.5 AND cog IS NOT NULL AND cog >= 0 AND cog < 360"
    return f"""
    WITH roster AS ({roster_sql}),
    pts AS (
        SELECT c.mmsi, date_trunc('hour', c.timestamp) AS hour_ts, c.latitude, c.longitude,
               c.sog, c.cog, c.heading, c.draught, c.navigational_status AS ns
        FROM read_parquet([{files}]) c SEMI JOIN roster r ON c.mmsi = r.mmsi
    ),
    {anchor_cte}
    {on_land_cte}
    hours AS (
        SELECT mmsi, hour_ts,
            count(*) AS n_msgs,
            avg(sog) AS sog_mean, max(sog) AS sog_max, stddev_pop(sog) AS sog_std,
            avg(cos(radians(cog))) FILTER (WHERE {moving}) AS cog_c,
            avg(sin(radians(cog))) FILTER (WHERE {moving}) AS cog_s,
            avg(abs(((heading - cog + 540) % 360) - 180))
                FILTER (WHERE sog > 1 AND heading BETWEEN 0 AND 359 AND cog >= 0 AND cog < 360)
                AS hdg_cog,
            max(draught) FILTER (WHERE draught > 0) AS draught,
            avg((ns IN {_UNDERWAY})::INT) AS frac_underway,
            avg((ns = 'At anchor')::INT) AS frac_anchor,
            avg((ns = 'Moored')::INT) AS frac_moored,
            avg((ns IN {_RESTRICTED})::INT) AS frac_restricted,
            avg(latitude) AS lat, avg(longitude) AS lon
        FROM pts GROUP BY mmsi, hour_ts
    ),
    placed AS (
        SELECT h.*,
            coalesce(g.dist_land_km, {LAND_DISTANCE_CAP_KM}) AS dist_land_km,
            (a.cell_lat IS NOT NULL)::INT AS in_anchorage,
            coalesce(o.n_on_land, 0) AS n_on_land,
            {_region_sql('h.lat', 'h.lon')} AS region_id
        FROM hours h
        LEFT JOIN read_parquet('{land_grid_path.as_posix()}') g
            ON g.lat_i = floor((h.lat - {lat0}) / {LAND_GRID_DEG})
           AND g.lon_i = floor((h.lon - {lon0}) / {LAND_GRID_DEG})
        LEFT JOIN anchor a
            ON a.cell_lat = round(floor(h.lat / {ANCHORAGE_CELL_DEG}) * {ANCHORAGE_CELL_DEG}, 2)
           AND a.cell_lon = round(floor(h.lon / {ANCHORAGE_CELL_DEG}) * {ANCHORAGE_CELL_DEG}, 2)
        LEFT JOIN on_land o ON o.mmsi = h.mmsi AND o.hour_ts = h.hour_ts
    ),
    seq AS (
        SELECT *,
            CASE WHEN cog_c IS NULL THEN NULL ELSE degrees(atan2(cog_s, cog_c)) END AS cog_mean,
            lag(hour_ts) OVER w AS prev_ts,
            last_value(draught IGNORE NULLS) OVER (
                PARTITION BY mmsi ORDER BY hour_ts
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_draught
        FROM placed
        WINDOW w AS (PARTITION BY mmsi ORDER BY hour_ts)
    ),
    seq2 AS (SELECT *, lag(cog_mean) OVER (PARTITION BY mmsi ORDER BY hour_ts) AS prev_cog FROM seq)
    SELECT
        mmsi,
        CAST(epoch(hour_ts - TIMESTAMP '{window_start.isoformat()}') / 3600 AS INTEGER) AS hour_idx,
        CAST(region_id AS TINYINT) AS region_id,
        CAST(ln(1 + n_msgs) AS FLOAT) AS log_msgs,
        CAST({_clip('sog_mean', 0, 40)} AS FLOAT) AS sog_mean,
        CAST({_clip('sog_max', 0, 40)} AS FLOAT) AS sog_max,
        CAST({_clip('sog_std', 0, 20)} AS FLOAT) AS sog_std,
        CAST(CASE WHEN cog_c IS NULL THEN 0 ELSE 1 - sqrt(cog_c * cog_c + cog_s * cog_s) END
             AS FLOAT) AS cog_dispersion,
        CAST(CASE WHEN cog_mean IS NULL OR prev_cog IS NULL THEN 0
             ELSE abs(((cog_mean - prev_cog + 540) % 360) - 180) / 180 END AS FLOAT) AS cog_change,
        CAST(coalesce(hdg_cog, 0) / 180 AS FLOAT) AS heading_cog_diff,
        CAST({_clip('draught', 0, 30)} AS FLOAT) AS draught,
        CAST((draught IS NULL)::INT AS FLOAT) AS draught_missing,
        CAST({_clip('draught - prev_draught', -15, 15)} AS FLOAT) AS draught_delta,
        CAST(frac_underway AS FLOAT) AS frac_underway,
        CAST(frac_anchor AS FLOAT) AS frac_anchor,
        CAST(frac_moored AS FLOAT) AS frac_moored,
        CAST(frac_restricted AS FLOAT) AS frac_restricted,
        CAST(ln(1 + dist_land_km) AS FLOAT) AS log_dist_land_km,
        CAST(in_anchorage AS FLOAT) AS in_anchorage,
        CAST({_clip('n_on_land / n_msgs', 0, 1)} AS FLOAT) AS on_land_frac,
        CAST(ln(1 + CASE WHEN prev_ts IS NULL
                     THEN epoch(hour_ts - TIMESTAMP '{window_start.isoformat()}') / 3600
                     ELSE epoch(hour_ts - prev_ts) / 3600 END) AS FLOAT) AS log_dt_h
    FROM seq2
    ORDER BY mmsi, hour_idx
    """


def event_tokens_sql(
    roster_sql: str, window_start: date, window_end: date, paths: dict[str, Path | None]
) -> str:
    """SELECT producing the event-token table (see module docstring). `paths` maps gaps/sts/
    behaviour/identity_anomalies/spoofing to that window's partition (None = not built)."""
    ws = f"TIMESTAMP '{window_start.isoformat()}'"
    n_hours = ((window_end - window_start).days + 1) * 24
    parts = []

    def add(key: str, sql: str) -> None:
        p = paths.get(key)
        if p is not None:
            parts.append(sql.format(p=p.as_posix()))

    add("gaps", "SELECT mmsi, 'gap' AS type, gap_start AS t, duration_hours AS dur, "
        "probability AS score FROM read_parquet('{p}')")
    add("sts", "SELECT mmsi_a AS mmsi, 'sts' AS type, start_time AS t, duration_hours AS dur, "
        "confidence AS score FROM read_parquet('{p}') UNION ALL "
        "SELECT mmsi_b, 'sts', start_time, duration_hours, confidence FROM read_parquet('{p}')")
    add("behaviour", "SELECT mmsi, 'beh_' || kind AS type, event_time AS t, 0.0 AS dur, "
        "confidence AS score FROM read_parquet('{p}')")
    add("identity_anomalies", "SELECT mmsi, 'id_' || kind AS type, event_time AS t, 0.0 AS dur, "
        "confidence AS score FROM read_parquet('{p}')")
    add("spoofing", "SELECT mmsi, 'spoof_' || kind AS type, event_time AS t, 0.0 AS dur, "
        "confidence AS score FROM read_parquet('{p}') WHERE kind <> 'on_land'")
    if not parts:
        parts.append("SELECT NULL::BIGINT AS mmsi, NULL AS type, NULL::TIMESTAMP AS t, "
                     "0.0 AS dur, 0.0 AS score WHERE false")
    known = ", ".join(f"'{t}'" for t in EVENT_TYPES)
    return f"""
    WITH roster AS ({roster_sql}),
    ev AS ({' UNION ALL '.join(parts)}),
    placed AS (
        SELECT e.mmsi,
            CASE WHEN e.type IN ({known}) THEN e.type ELSE 'other' END AS type,
            CAST(greatest(least(epoch(e.t - {ws}) / 3600, {n_hours - 1}), 0) AS INTEGER) AS hour_idx,
            CAST(ln(1 + greatest(coalesce(e.dur, 0), 0)) AS FLOAT) AS log_duration_h,
            CAST(coalesce(e.score, 0) AS FLOAT) AS score
        FROM ev e SEMI JOIN roster r ON e.mmsi = r.mmsi
        WHERE e.t IS NOT NULL
    )
    SELECT mmsi, type, hour_idx, log_duration_h, score FROM (
        SELECT *, row_number() OVER (PARTITION BY mmsi ORDER BY hour_idx, type, score DESC) AS rn
        FROM placed
    ) WHERE rn <= {MAX_EVENTS}
    ORDER BY mmsi, hour_idx, type
    """


def _partition_or_none(root: Path, start: date, end: date) -> Path | None:
    p = window_partition_path(start, end, root)
    return p if p.exists() else None


def build_tokens(
    start: date,
    end: date,
    clean_root: Path = CLEAN_ROOT,
    panel_root: Path = PANEL_ROOT,
    detect_root: Path = DETECT_ROOT,
    anchorages_root: Path = ANCHORAGES_ROOT,
    land_grid_path: Path = LAND_GRID_PATH,
    track_root: Path = TRACK_ROOT,
    event_root: Path = EVENT_ROOT,
    threads: int | None = None,
    force: bool = False,
) -> tuple[Path, Path]:
    """Write one window's track and event tokens. Idempotent unless force=True."""
    track_out = window_partition_path(start, end, track_root)
    event_out = window_partition_path(start, end, event_root)
    if track_out.exists() and event_out.exists() and not force:
        logger.info("Tokens for %s..%s exist, skipping", start, end)
        return track_out, event_out
    panel_path = window_partition_path(start, end, panel_root)
    if not panel_path.exists():
        raise FileNotFoundError(f"No panel at {panel_path}; run features.panel first")
    clean = [p for _d, p in existing_partitions(start, end, clean_root)]
    if not clean:
        raise FileNotFoundError(f"No clean partitions for {start}..{end} under {clean_root}")
    if not land_grid_path.exists():
        build_land_grid(out_path=land_grid_path)
    roster = _roster_sql(panel_path)
    det = {
        k: _partition_or_none(detect_root / k, start, end)
        for k in ("gaps", "sts", "behaviour", "identity_anomalies", "spoofing")
    }
    con = duckdb.connect()
    try:
        if threads is not None:
            con.execute(f"SET threads = {int(threads)}")
        atomic_write_parquet(
            con,
            track_tokens_sql(
                clean,
                roster,
                start,
                land_grid_path,
                _partition_or_none(anchorages_root, start, end),
                det["spoofing"],
            ),
            track_out,
        )
        atomic_write_parquet(con, event_tokens_sql(roster, start, end, det), event_out)
        n_tok, n_v = con.execute(
            f"SELECT count(*), count(DISTINCT mmsi) FROM read_parquet('{track_out.as_posix()}')"
        ).fetchone()
        (n_ev,) = con.execute(f"SELECT count(*) FROM read_parquet('{event_out.as_posix()}')").fetchone()
    finally:
        con.close()
    logger.info("%s..%s: %d track tokens for %d vessels, %d event tokens", start, end, n_tok, n_v, n_ev)
    return track_out, event_out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build P4-3j vessel-encoder tokens for one window")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = _parse_args(argv)
    build_tokens(
        date.fromisoformat(a.start), date.fromisoformat(a.end), threads=a.threads, force=a.force
    )


if __name__ == "__main__":
    main()
