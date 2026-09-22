"""Unit tests for pipeline.window. Every downstream builder (backfill.backfill_range,
detect.liveness.build_liveness, process.thin.build_thin_tracks,
process.ship_type.build_ship_type_reference, process.tracks.reconstruct_range,
process.identity.resolve_range, detect.anchorages.build_anchorages,
detect.spoofing.build_spoofing_events, detect.sts.build_sts_events,
detect.behaviour.build_behaviour_events, detect.identity_anomalies.build_identity_events) is
monkeypatched with a fake that writes a small synthetic Parquet file at the path a real call
would use and returns it -- running the real builders here would need a real land mask, the
DuckDB spatial extension and realistic multi-column schemas, far more than a fast unit test needs
to check what this module actually owns: call order, path wiring, and that verified_at is only
ever recorded when every artifact checks out. Nothing here touches data/ or the network.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from detect import anchorages, behaviour, identity_anomalies, liveness, spoofing, sts
from pipeline import backfill, manifest, window
from process import identity, ship_type, thin, tracks
from process.partitions import daterange, partition_path, window_partition_path

START = date(2024, 6, 3)
END = date(2024, 6, 5)
LEAD_IN_DAYS = 2
LEAD_IN_START = START - timedelta(days=LEAD_IN_DAYS)


def _write_clean_partition(root: Path, day: date) -> None:
    path = partition_path(day, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, timestamp TIMESTAMP, latitude DOUBLE, longitude DOUBLE)"
        )
        con.execute(
            "INSERT INTO t VALUES (219000001, TIMESTAMP '2024-06-01 00:00:00', 55.0, 12.0)"
        )
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_parquet(path: Path, n_rows: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (x BIGINT)")
        if n_rows:
            con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n_rows)])
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return path


def _patch_all_builders(monkeypatch, calls: dict, *, event_rows: int = 1, skip_days: set = frozenset()):
    """Patch every downstream builder with a fake, recording (start, end, kwargs) per call name."""

    def fake_backfill_range(start, end, **kwargs):
        calls.setdefault("backfill_range", []).append((start, end, kwargs))
        for day in daterange(start, end):
            _write_clean_partition(kwargs.get("data_root") / "clean" / "ais_dk", day)

    def fake_build_liveness(start, end, in_root, out_root, **kwargs):
        calls.setdefault("build_liveness", []).append((start, end, kwargs))
        paths = []
        for day in daterange(start, end):
            if partition_path(day, in_root).exists():
                paths.append(_write_parquet(partition_path(day, out_root), 1))
        return paths

    def fake_build_thin_tracks(start, end, in_root, out_root, **kwargs):
        calls.setdefault("build_thin_tracks", []).append((start, end, kwargs))
        paths = []
        for day in daterange(start, end):
            if day in skip_days:
                continue
            if partition_path(day, in_root).exists():
                paths.append(_write_parquet(partition_path(day, out_root), 1))
        return paths

    def _window_fake(name, n_rows=1, write=True):
        def _fake(start, end, in_root=None, out_root=None, **kwargs):
            calls.setdefault(name, []).append((start, end, kwargs))
            out_path = window_partition_path(start, end, out_root)
            if write:
                _write_parquet(out_path, n_rows)
            return out_path

        return _fake

    monkeypatch.setattr(backfill, "backfill_range", fake_backfill_range)
    monkeypatch.setattr(liveness, "build_liveness", fake_build_liveness)
    monkeypatch.setattr(thin, "build_thin_tracks", fake_build_thin_tracks)
    monkeypatch.setattr(ship_type, "build_ship_type_reference", _window_fake("ship_type"))
    monkeypatch.setattr(tracks, "reconstruct_range", _window_fake("tracks"))
    monkeypatch.setattr(identity, "resolve_range", _window_fake("identity"))
    monkeypatch.setattr(anchorages, "build_anchorages", _window_fake("anchorages"))
    monkeypatch.setattr(
        spoofing, "build_spoofing_events", _window_fake("spoofing", n_rows=event_rows)
    )
    monkeypatch.setattr(sts, "build_sts_events", _window_fake("sts", n_rows=event_rows))
    monkeypatch.setattr(
        behaviour, "build_behaviour_events", _window_fake("behaviour", n_rows=event_rows)
    )
    monkeypatch.setattr(
        identity_anomalies,
        "build_identity_events",
        _window_fake("identity_anomalies", n_rows=event_rows),
    )
    return calls


def test_happy_path_records_verified_at_and_fingerprint_for_every_window_day(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)

    manifest_path = tmp_path / "manifest.json"
    data = manifest.load(manifest_path)
    for day in daterange(START, END):
        entry = data[day.isoformat()]
        assert "verified_at" in entry
        assert entry["fingerprint_message_count"] == 1
        assert entry["fingerprint_n_distinct_mmsi"] == 1
        assert entry["fingerprint_bbox"] == [55.0, 55.0, 12.0, 12.0]
    # Lead-in-only days have no manifest entry from this call at all -- process_window never
    # marks them verified_at, only the window's own [start, end] days.
    for day in daterange(LEAD_IN_START, START - timedelta(days=1)):
        assert day.isoformat() not in data


def test_backfill_and_liveness_cover_the_lead_in_range_not_just_the_window(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)

    (bf_start, bf_end, _kwargs) = calls["backfill_range"][0]
    assert (bf_start, bf_end) == (LEAD_IN_START, END)
    (lv_start, lv_end, _kwargs) = calls["build_liveness"][0]
    assert (lv_start, lv_end) == (LEAD_IN_START, END)


def test_producers_other_than_backfill_and_liveness_scope_to_the_window_only(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)

    for name in (
        "build_thin_tracks",
        "ship_type",
        "tracks",
        "identity",
        "anchorages",
        "spoofing",
        "sts",
        "behaviour",
        "identity_anomalies",
    ):
        (start, end, _kwargs) = calls[name][0]
        assert (start, end) == (START, END), f"{name} was not scoped to the window"


def test_dependent_detectors_receive_glob_paths_not_single_window_paths(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)

    _start, _end, spoofing_kwargs = calls["spoofing"][0]
    assert spoofing_kwargs["voyages_path"] == tmp_path / "tracks" / "voyages" / "window=*" / "part-0.parquet"
    _start, _end, sts_kwargs = calls["sts"][0]
    assert sts_kwargs["anchorages_path"] == tmp_path / "coverage" / "anchorages" / "window=*" / "part-0.parquet"
    _start, _end, behaviour_kwargs = calls["behaviour"][0]
    assert behaviour_kwargs["voyages_path"] == tmp_path / "tracks" / "voyages" / "window=*" / "part-0.parquet"
    assert behaviour_kwargs["anchorages_path"] == tmp_path / "coverage" / "anchorages" / "window=*" / "part-0.parquet"
    assert behaviour_kwargs["sts_path"] == tmp_path / "detect" / "sts" / "window=*" / "part-0.parquet"
    # identity_anomalies gets the ROOT (it resolves the exact window itself), not a glob -- P3-4/A2's
    # hard-blocker fix requires the caller's own exact window, not the accumulated history.
    _start, _end, ia_kwargs = calls["identity_anomalies"][0]
    assert ia_kwargs["ship_type_reference_root"] == tmp_path / "reference" / "ship_type"


def test_force_flag_propagates_to_every_builder(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS, force=True)

    assert calls["backfill_range"][0][2]["force"] is True
    for name in (
        "build_liveness",
        "build_thin_tracks",
        "ship_type",
        "tracks",
        "identity",
        "anchorages",
        "spoofing",
        "sts",
        "behaviour",
        "identity_anomalies",
    ):
        assert calls[name][0][2]["force"] is True


def test_zero_row_event_detectors_do_not_fail_verification(tmp_path, monkeypatch):
    """spoofing/sts/behaviour/identity_anomalies finding nothing is a legitimate real result
    (e.g. P2-4's own real run found zero STS episodes in some windows), not a build failure."""
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls, event_rows=0)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)

    manifest_path = tmp_path / "manifest.json"
    data = manifest.load(manifest_path)
    assert "verified_at" in data[START.isoformat()]


def test_missing_required_artifact_raises_and_writes_no_manifest_entries(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    def broken_tracks(start, end, in_root=None, out_root=None, **kwargs):
        calls.setdefault("tracks", []).append((start, end, kwargs))
        return window_partition_path(start, end, out_root)  # never actually written

    monkeypatch.setattr(tracks, "reconstruct_range", broken_tracks)

    with pytest.raises(RuntimeError, match="failed verification"):
        window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)

    manifest_path = tmp_path / "manifest.json"
    data = manifest.load(manifest_path)
    assert "verified_at" not in data.get(START.isoformat(), {})


def test_missing_thin_day_partition_raises(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls, skip_days={date(2024, 6, 4)})

    with pytest.raises(RuntimeError, match="thin"):
        window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS)


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    calls: dict = {}
    _patch_all_builders(monkeypatch, calls)

    window.process_window(START, END, data_root=tmp_path, lead_in_days=LEAD_IN_DAYS, dry_run=True)

    assert calls == {}
    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "clean").exists()
