"""Synthetic tests for features.vessel_tokens (P4-3j inputs)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from features import vessel_tokens as vt
from process.partitions import partition_path, window_partition_path

START = date(2024, 6, 1)
END = date(2024, 6, 2)
T0 = datetime(2024, 6, 1, 0, 0)  # noqa: DTZ001  (AIS timestamps are naive UTC, like the clean data)


def _write(path: Path, create_sql: str, rows: list[tuple], placeholders: int, table: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(create_sql)
        if rows:
            con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * placeholders)})", rows)
        con.execute(f"COPY {table} TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _clean(root: Path, day: date, rows: list[tuple]) -> None:
    """rows: (timestamp, mmsi, lat, lon, sog, cog, heading, draught, nav_status)."""
    _write(
        partition_path(day, root),
        "CREATE TABLE c (timestamp TIMESTAMP, mmsi BIGINT, latitude DOUBLE, longitude DOUBLE, "
        "sog DOUBLE, cog DOUBLE, heading BIGINT, draught DOUBLE, navigational_status VARCHAR)",
        rows,
        9,
        "c",
    )


def _fixture(tmp_path: Path, rows: list[tuple], events: dict | None = None):
    clean = tmp_path / "clean"
    by_day: dict[date, list] = {}
    for r in rows:
        by_day.setdefault(r[0].date(), []).append(r)
    for day, rs in by_day.items():
        _clean(clean, day, rs)
    panel = tmp_path / "panel"
    _write(
        window_partition_path(START, END, panel),
        "CREATE TABLE p (mmsi BIGINT, imo VARCHAR)",
        [(1, "9000001"), (2, None)],
        2,
        "p",
    )
    grid = tmp_path / "grid.parquet"
    _write(
        grid,
        "CREATE TABLE g (lat_i BIGINT, lon_i BIGINT, on_land BOOLEAN, dist_land_km DOUBLE)",
        [(i, j, False, 12.0) for i in range(60, 70) for j in range(150, 170)],
        4,
        "g",
    )
    detect = tmp_path / "detect"
    for key, (create, n, table, evrows) in (events or {}).items():
        _write(window_partition_path(START, END, detect / key), create, evrows, n, table)
    track, event = vt.build_tokens(
        START,
        END,
        clean_root=clean,
        panel_root=panel,
        detect_root=detect,
        anchorages_root=tmp_path / "no_anchorages",
        land_grid_path=grid,
        track_root=tmp_path / "track",
        event_root=tmp_path / "event",
    )
    con = duckdb.connect()
    try:
        tracks = con.execute(f"SELECT * FROM read_parquet('{track.as_posix()}') ORDER BY mmsi, hour_idx").fetchdf()
        ev = con.execute(f"SELECT * FROM read_parquet('{event.as_posix()}') ORDER BY mmsi, hour_idx").fetchdf()
    finally:
        con.close()
    return tracks, ev


LAT, LON = 56.6, 10.6  # grid cell (62, 162): 12 km from land in the fixture


def _pt(minute: int, mmsi: int = 1, sog=10.0, cog=90.0, heading=90, draught=8.0, ns="Under way using engine"):
    return (T0 + timedelta(minutes=minute), mmsi, LAT, LON, sog, cog, heading, draught, ns)


def test_only_vessels_with_a_valid_imo_get_tokens(tmp_path):
    tracks, _ = _fixture(tmp_path, [_pt(0), _pt(5, mmsi=2)])
    assert set(tracks["mmsi"]) == {1}


def test_no_position_or_identity_column_is_stored(tmp_path):
    tracks, _ = _fixture(tmp_path, [_pt(0)])
    assert not {"latitude", "longitude", "lat", "lon", "imo", "cog", "name"} & set(tracks.columns)
    assert set(vt.TRACK_CHANNELS) <= set(tracks.columns)


def test_missing_draught_is_zero_with_a_flag_not_the_clip_value(tmp_path):
    # Regression: least(NULL, 30) is 30 in DuckDB; a missing draught must not become 30 m.
    tracks, _ = _fixture(tmp_path, [_pt(0, draught=None), _pt(5, sog=None, draught=None)])
    row = tracks.iloc[0]
    assert row["draught"] == 0 and row["draught_missing"] == 1
    assert row["sog_mean"] == pytest.approx(10.0)


def test_hourly_channels(tmp_path):
    rows = [_pt(0, cog=90), _pt(10, cog=90), _pt(20, cog=270, sog=0.2)]  # hour 0; slow point ignored
    rows += [_pt(180, cog=180, draught=11.0)]  # hour 3: +90 deg, draught +3, 3 h later
    tracks, _ = _fixture(tmp_path, rows)
    h0, h3 = tracks.iloc[0], tracks.iloc[1]
    assert h0["hour_idx"] == 0 and h3["hour_idx"] == 3
    assert h0["cog_dispersion"] == pytest.approx(0.0, abs=1e-6)
    assert h3["cog_change"] == pytest.approx(0.5, abs=1e-4)
    assert h3["draught_delta"] == pytest.approx(3.0)
    assert h3["log_dt_h"] == pytest.approx(math.log(1 + 3))
    assert h0["log_dist_land_km"] == pytest.approx(math.log(1 + 12.0), abs=1e-5)
    assert h0["region_id"] == 1 * 4 + 1  # middle latitude band, 9-11 E band


def test_event_tokens_types_and_cap(tmp_path):
    gap_rows = [(1, T0 + timedelta(hours=h), 2.0, 0.9) for h in range(vt.MAX_EVENTS + 5)]
    events = {
        "gaps": (
            "CREATE TABLE g (mmsi BIGINT, gap_start TIMESTAMP, duration_hours DOUBLE, probability DOUBLE)",
            4, "g", gap_rows,
        ),
        "spoofing": (
            "CREATE TABLE s (mmsi BIGINT, kind VARCHAR, event_time TIMESTAMP, confidence DOUBLE)",
            4, "s", [(1, "on_land", T0, 1.0), (1, "weird_new_kind", T0, 0.5), (2, "impossible_speed", T0, 1.0)],
        ),
    }
    _, ev = _fixture(tmp_path, [_pt(0)], events)
    assert len(ev) == vt.MAX_EVENTS  # capped, earliest first
    assert set(ev["mmsi"]) == {1}
    assert "spoof_on_land" not in set(ev["type"])  # on_land is a track channel, not an event
    assert "other" in set(ev["type"])
