"""Unit tests for features.baltic_trips. All fixtures are synthetic, written to tmp_path; nothing
here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from features import baltic_trips as fbt


def _ts(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)  # noqa: DTZ001


def _write_trips(path: Path, rows: list[tuple]) -> None:
    """rows: (imo, east_time, west_time, is_laden_return)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE trips (imo VARCHAR, east_time TIMESTAMP, west_time TIMESTAMP, "
        "is_laden_return BOOLEAN)"
    )
    if rows:
        con.executemany("INSERT INTO trips VALUES (?, ?, ?, ?)", rows)
    con.execute(f"COPY trips TO '{path.as_posix()}' (FORMAT PARQUET)")
    con.close()


def _write_identity_window(root: Path, window_start: date, window_end: date, rows: list[tuple]) -> None:
    """rows: (mmsi, imo, message_count, first_seen, last_seen, is_orphaned, is_reused)."""
    out_dir = root / f"window={window_start.isoformat()}_{window_end.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE identity (mmsi BIGINT, imo VARCHAR, message_count BIGINT, "
        "first_seen DATE, last_seen DATE, is_orphaned BOOLEAN, is_reused BOOLEAN)"
    )
    con.executemany("INSERT INTO identity VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY identity TO '{(out_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()


def _read_features(out_path: Path) -> dict[int, dict]:
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT * FROM read_parquet('{out_path.as_posix()}')").fetchall()
        cols = [d[0] for d in con.description]
    finally:
        con.close()
    return {row[cols.index("mmsi")]: dict(zip(cols, row, strict=True)) for row in rows}


WINDOW_START = date(2024, 6, 1)
WINDOW_END = date(2024, 6, 30)


def test_mmsi_with_no_trips_gets_zero_and_nan(tmp_path: Path):
    trips_path = tmp_path / "trips.parquet"
    _write_trips(trips_path, [])
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, WINDOW_START, WINDOW_END,
        [(111, None, 10, WINDOW_START, WINDOW_END, True, False)],  # orphaned mmsi, no imo
    )
    out_path = fbt.build_baltic_trip_features(
        WINDOW_START, WINDOW_END, trips_path=trips_path, identity_root=identity_root,
        out_root=tmp_path / "out",
    )
    features = _read_features(out_path)
    row = features[111]
    assert row["lr_n_trips"] == 0
    assert row["lr_n_laden_returns"] == 0
    assert row["lr_laden_share"] is None
    assert row["lr_rate_per_30d"] == pytest.approx(0.0)


def test_mmsi_with_trips_aggregates_correctly(tmp_path: Path):
    trips_path = tmp_path / "trips.parquet"
    _write_trips(
        trips_path,
        [
            ("9074729", _ts(date(2024, 6, 5), 0), _ts(date(2024, 6, 10), 0), True),
            ("9074729", _ts(date(2024, 6, 12), 0), _ts(date(2024, 6, 20), 0), False),
        ],
    )
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, WINDOW_START, WINDOW_END,
        [(111, "9074729", 10, WINDOW_START, WINDOW_END, False, False)],
    )
    out_path = fbt.build_baltic_trip_features(
        WINDOW_START, WINDOW_END, trips_path=trips_path, identity_root=identity_root,
        out_root=tmp_path / "out",
    )
    row = _read_features(out_path)[111]
    assert row["lr_n_trips"] == 2
    assert row["lr_n_laden_returns"] == 1
    assert row["lr_laden_share"] == pytest.approx(0.5)
    assert row["lr_rate_per_30d"] > 0


def test_strict_less_than_window_end_boundary(tmp_path: Path):
    """A trip whose westbound crossing lands exactly at window_end (end of day) must be excluded
    -- not yet knowable when the window closes. One microsecond earlier must be included."""
    window_end_dt = datetime.combine(WINDOW_END, datetime.max.time())
    trips_path = tmp_path / "trips.parquet"
    _write_trips(
        trips_path,
        [
            ("9074729", _ts(date(2024, 6, 5), 0), window_end_dt, True),  # excluded
            ("1234567", _ts(date(2024, 6, 5), 0), window_end_dt - timedelta(microseconds=1), True),  # included
        ],
    )
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, WINDOW_START, WINDOW_END,
        [
            (111, "9074729", 10, WINDOW_START, WINDOW_END, False, False),
            (222, "1234567", 10, WINDOW_START, WINDOW_END, False, False),
        ],
    )
    out_path = fbt.build_baltic_trip_features(
        WINDOW_START, WINDOW_END, trips_path=trips_path, identity_root=identity_root,
        out_root=tmp_path / "out",
    )
    features = _read_features(out_path)
    assert features[111]["lr_n_trips"] == 0  # the exactly-at-window_end trip is excluded
    assert features[222]["lr_n_trips"] == 1  # the one-microsecond-earlier trip is included


def test_lookback_excludes_trips_before_270_days(tmp_path: Path):
    """A trip whose westbound crossing is older than the 270-day lookback (and not covered by the
    MARCH_START floor for this window) must not count."""
    late_window_end = date(2025, 2, 26)
    late_window_start = date(2025, 2, 1)
    too_old_west_time = _ts(late_window_end - timedelta(days=271), 0)
    just_inside_west_time = _ts(late_window_end - timedelta(days=269), 0)
    trips_path = tmp_path / "trips.parquet"
    _write_trips(
        trips_path,
        [
            ("1111111", too_old_west_time - timedelta(days=5), too_old_west_time, True),
            ("2222222", just_inside_west_time - timedelta(days=5), just_inside_west_time, True),
        ],
    )
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, late_window_start, late_window_end,
        [
            (111, "1111111", 10, late_window_start, late_window_end, False, False),
            (222, "2222222", 10, late_window_start, late_window_end, False, False),
        ],
    )
    out_path = fbt.build_baltic_trip_features(
        late_window_start, late_window_end, trips_path=trips_path, identity_root=identity_root,
        out_root=tmp_path / "out",
    )
    features = _read_features(out_path)
    assert features[111]["lr_n_trips"] == 0
    assert features[222]["lr_n_trips"] == 1


def test_lookback_floors_at_march_start_for_an_early_window(tmp_path: Path):
    """For a window less than 270 days after MARCH_START, the lookback floor is MARCH_START, not
    window_end - 270d -- a trip just after MARCH_START in a window's own early history must count."""
    window_start, window_end = date(2024, 4, 1), date(2024, 4, 30)
    from detect.baltic_trips import MARCH_START

    west_time = _ts(MARCH_START, 12)  # right at the floor
    trips_path = tmp_path / "trips.parquet"
    _write_trips(
        trips_path, [("9074729", west_time, west_time, True)],
    )
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, window_start, window_end,
        [(111, "9074729", 10, window_start, window_end, False, False)],
    )
    out_path = fbt.build_baltic_trip_features(
        window_start, window_end, trips_path=trips_path, identity_root=identity_root,
        out_root=tmp_path / "out",
    )
    row = _read_features(out_path)[111]
    assert row["lr_n_trips"] == 1


def test_reused_mmsi_uses_latest_last_seen_imo(tmp_path: Path):
    """Mirrors features.panel/detect.baltic_trips' own representative-IMO tie-break."""
    trips_path = tmp_path / "trips.parquet"
    _write_trips(
        trips_path,
        [("2222222", _ts(date(2024, 6, 5), 0), _ts(date(2024, 6, 10), 0), True)],
    )
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, WINDOW_START, WINDOW_END,
        [
            (111, "1111111", 5, WINDOW_START, date(2024, 6, 10), False, True),
            (111, "2222222", 5, WINDOW_START, date(2024, 6, 20), False, True),  # latest last_seen
        ],
    )
    out_path = fbt.build_baltic_trip_features(
        WINDOW_START, WINDOW_END, trips_path=trips_path, identity_root=identity_root,
        out_root=tmp_path / "out",
    )
    row = _read_features(out_path)[111]
    assert row["lr_n_trips"] == 1


def test_idempotent_skip(tmp_path: Path):
    trips_path = tmp_path / "trips.parquet"
    _write_trips(trips_path, [])
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, WINDOW_START, WINDOW_END,
        [(111, None, 10, WINDOW_START, WINDOW_END, True, False)],
    )
    out_root = tmp_path / "out"
    out_path = fbt.build_baltic_trip_features(
        WINDOW_START, WINDOW_END, trips_path=trips_path, identity_root=identity_root, out_root=out_root
    )
    mtime_1 = out_path.stat().st_mtime_ns
    fbt.build_baltic_trip_features(
        WINDOW_START, WINDOW_END, trips_path=trips_path, identity_root=identity_root, out_root=out_root
    )
    assert out_path.stat().st_mtime_ns == mtime_1


def test_missing_trips_path_raises(tmp_path: Path):
    identity_root = tmp_path / "identity"
    _write_identity_window(
        identity_root, WINDOW_START, WINDOW_END,
        [(111, None, 10, WINDOW_START, WINDOW_END, True, False)],
    )
    with pytest.raises(FileNotFoundError):
        fbt.build_baltic_trip_features(
            WINDOW_START, WINDOW_END,
            trips_path=tmp_path / "does_not_exist.parquet",
            identity_root=identity_root,
            out_root=tmp_path / "out",
        )


def test_missing_identity_window_raises(tmp_path: Path):
    trips_path = tmp_path / "trips.parquet"
    _write_trips(trips_path, [])
    with pytest.raises(FileNotFoundError):
        fbt.build_baltic_trip_features(
            WINDOW_START, WINDOW_END,
            trips_path=trips_path,
            identity_root=tmp_path / "no_such_identity_root",
            out_root=tmp_path / "out",
        )
