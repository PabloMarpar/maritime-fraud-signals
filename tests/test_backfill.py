"""Unit tests for pipeline.backfill. download_day/clean_day are monkeypatched with fakes
that write small synthetic Parquet via DuckDB into tmp_path; nothing here touches the
network or real DMA data.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from pipeline import backfill, manifest

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def _write_partition(root: Path, day: date, row_count: int) -> Path:
    path = root / f"date={day.isoformat()}" / "part-0.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT)")
        if row_count:
            con.executemany("INSERT INTO t VALUES (?)", [(219000000 + i,) for i in range(row_count)])
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return path


def _fakes(monkeypatch, raw_rows: dict, clean_rows: dict):
    """Patch download_day/clean_day with fakes; returns lists tracking calls made."""
    download_calls: list[date] = []
    clean_calls: list[date] = []

    def fake_download_day(day, out_root=None, force=False, client=None, tmp_dir=None):
        download_calls.append(day)
        return _write_partition(out_root, day, raw_rows[day])

    def fake_clean_day(day, in_root=None, out_root=None, force=False):
        clean_calls.append(day)
        return _write_partition(out_root, day, clean_rows[day])

    monkeypatch.setattr(backfill, "download_day", fake_download_day)
    monkeypatch.setattr(backfill, "clean_day", fake_clean_day)
    return download_calls, clean_calls


def test_backfill_day_full_cycle_deletes_raw_and_records_manifest(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    download_calls, clean_calls = _fakes(monkeypatch, {DAY: 100}, {DAY: 90})

    backfill.backfill_day(DAY, data_root=data_root)

    assert download_calls == [DAY]
    assert clean_calls == [DAY]
    raw_path = data_root / "raw" / "ais_dk" / f"date={DAY.isoformat()}" / "part-0.parquet"
    assert not raw_path.exists()
    assert not raw_path.parent.exists()  # empty date= dir also cleaned up

    manifest_path = data_root / "manifest.json"
    entry = manifest.load(manifest_path)[DAY.isoformat()]
    assert entry["raw_rows"] == 100
    assert entry["clean_rows"] == 90
    assert "downloaded_at" in entry
    assert "cleaned_at" in entry
    assert "raw_discarded_at" in entry
    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_backfill_day_already_clean_is_skipped_without_force(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    manifest_path = data_root / "manifest.json"
    manifest.record(
        manifest_path, DAY, raw_rows=100, downloaded_at="t1", clean_rows=90, cleaned_at="t2"
    )
    download_calls, clean_calls = _fakes(monkeypatch, {DAY: 100}, {DAY: 90})

    backfill.backfill_day(DAY, data_root=data_root)

    assert download_calls == []
    assert clean_calls == []


def test_backfill_day_already_clean_but_force_reruns(tmp_path, monkeypatch):
    """Negative case for the skip logic: force=True redoes an already-clean day."""
    data_root = tmp_path / "data"
    manifest_path = data_root / "manifest.json"
    manifest.record(
        manifest_path, DAY, raw_rows=100, downloaded_at="t1", clean_rows=90, cleaned_at="t2"
    )
    download_calls, clean_calls = _fakes(monkeypatch, {DAY: 100}, {DAY: 90})

    backfill.backfill_day(DAY, data_root=data_root, force=True)

    assert download_calls == [DAY]
    assert clean_calls == [DAY]


def test_backfill_day_low_retention_keeps_raw(tmp_path, monkeypatch):
    """clean_rows far below raw_rows (< 10%) must not delete the raw file."""
    data_root = tmp_path / "data"
    _fakes(monkeypatch, {DAY: 100}, {DAY: 5})  # 5% retention

    backfill.backfill_day(DAY, data_root=data_root)

    raw_path = data_root / "raw" / "ais_dk" / f"date={DAY.isoformat()}" / "part-0.parquet"
    assert raw_path.exists()
    manifest_path = data_root / "manifest.json"
    entry = manifest.load(manifest_path)[DAY.isoformat()]
    assert "raw_discarded_at" not in entry


def test_backfill_day_zero_clean_rows_keeps_raw(tmp_path, monkeypatch):
    """Negative case: an empty clean result must not delete the raw file either."""
    data_root = tmp_path / "data"
    _fakes(monkeypatch, {DAY: 100}, {DAY: 0})

    backfill.backfill_day(DAY, data_root=data_root)

    raw_path = data_root / "raw" / "ais_dk" / f"date={DAY.isoformat()}" / "part-0.parquet"
    assert raw_path.exists()
    manifest_path = data_root / "manifest.json"
    entry = manifest.load(manifest_path)[DAY.isoformat()]
    assert "raw_discarded_at" not in entry


def test_backfill_day_good_retention_negative_case_deletes_raw(tmp_path, monkeypatch):
    """Negative case for the retention guard: healthy retention (well above 10%) does delete."""
    data_root = tmp_path / "data"
    _fakes(monkeypatch, {DAY: 100}, {DAY: 95})

    backfill.backfill_day(DAY, data_root=data_root)

    raw_path = data_root / "raw" / "ais_dk" / f"date={DAY.isoformat()}" / "part-0.parquet"
    assert not raw_path.exists()


def test_backfill_day_keep_raw_flag_preserves_raw_even_on_success(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    _fakes(monkeypatch, {DAY: 100}, {DAY: 90})

    backfill.backfill_day(DAY, data_root=data_root, keep_raw=True)

    raw_path = data_root / "raw" / "ais_dk" / f"date={DAY.isoformat()}" / "part-0.parquet"
    assert raw_path.exists()
    manifest_path = data_root / "manifest.json"
    entry = manifest.load(manifest_path)[DAY.isoformat()]
    assert "raw_discarded_at" not in entry


def test_backfill_day_disk_guard_raises_before_download(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    download_calls, clean_calls = _fakes(monkeypatch, {DAY: 100}, {DAY: 90})
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(total=1, used=1, free=1)
    )

    with pytest.raises(RuntimeError):
        backfill.backfill_day(DAY, data_root=data_root, min_free_gb=20.0)

    assert download_calls == []
    assert clean_calls == []


def test_backfill_day_disk_guard_negative_case_plenty_of_space(tmp_path, monkeypatch):
    """Negative case: ample free space must not trip the guard."""
    data_root = tmp_path / "data"
    huge = 1_000 * 1e9
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(total=huge, used=0, free=huge)
    )
    download_calls, _clean_calls = _fakes(monkeypatch, {DAY: 100}, {DAY: 90})

    backfill.backfill_day(DAY, data_root=data_root, min_free_gb=20.0)

    assert download_calls == [DAY]


def test_backfill_day_dry_run_touches_nothing(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)  # pre-exists, so the disk-guard mkdir is a no-op
    download_calls, clean_calls = _fakes(monkeypatch, {DAY: 100}, {DAY: 90})

    backfill.backfill_day(DAY, data_root=data_root, dry_run=True)

    assert download_calls == []
    assert clean_calls == []
    assert list(data_root.iterdir()) == []  # nothing written: no raw/, clean/, manifest.json
    assert manifest.load(data_root / "manifest.json") == {}


def test_backfill_range_skips_already_clean_day_proving_resumability(tmp_path, monkeypatch):
    """A day already marked clean before backfill_range runs is skipped -- the mechanism
    that makes rerunning backfill_range after a crash resume rather than redo everything.
    """
    data_root = tmp_path / "data"
    manifest_path = data_root / "manifest.json"
    manifest.record(
        manifest_path, DAY, raw_rows=100, downloaded_at="t1", clean_rows=90, cleaned_at="t2"
    )
    download_calls, clean_calls = _fakes(monkeypatch, {DAY: 100, DAY2: 200}, {DAY: 90, DAY2: 180})

    backfill.backfill_range(DAY, DAY2, data_root=data_root)

    assert download_calls == [DAY2]
    assert clean_calls == [DAY2]


def test_backfill_range_reraises_on_real_failure(tmp_path, monkeypatch):
    """A genuine error (not the low-retention warning case) must propagate, not be swallowed."""
    data_root = tmp_path / "data"

    def broken_download(day, out_root=None, force=False, client=None, tmp_dir=None):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(backfill, "download_day", broken_download)

    with pytest.raises(ConnectionError):
        backfill.backfill_range(DAY, DAY2, data_root=data_root)
