"""Unit tests for pipeline.prune. All fixtures are synthetic, written to tmp_path; nothing here
touches data/ or performs a real deletion outside tmp_path. See tests/test_window.py for the
sibling orchestrator module's own fixture conventions, which this mirrors.

Most tests monkeypatch ``prune.REFERENCE_EVENT_RATES_PER_DAY`` to ``{}`` -- this disables the
statistical sanity gate entirely (see ``_statistical_sanity_failures``: no reference means no
check), so fixtures for every OTHER gate don't also need to reproduce real-scale event counts.
The sanity gate itself gets its own dedicated tests with a small, explicit reference dict.
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import duckdb

from pipeline import manifest, prune
from process.partitions import daterange, partition_path, window_partition_path

DAY = date(2024, 6, 10)
LEAD_IN_DAYS = 2  # small on purpose, keeps fixtures tiny
LEAD_IN_START = DAY - timedelta(days=LEAD_IN_DAYS)
MMSI = [111111111, 222222222, 333333333, 444444444, 555555555]


def _write_mmsi_table(path: Path, mmsi_list: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT)")
        con.executemany("INSERT INTO t VALUES (?)", [(m,) for m in mmsi_list])
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_window_artifact(
    root: Path,
    start: date,
    end: date,
    n_rows: int = 1,
    with_window_columns: bool = True,
    kind: str | None = None,
) -> Path:
    path = window_partition_path(start, end, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        cols = "x BIGINT" + (", kind VARCHAR" if kind is not None else "")
        con.execute(f"CREATE TABLE t ({cols})")
        for i in range(n_rows):
            if kind is not None:
                con.execute("INSERT INTO t VALUES (?, ?)", [i, kind])
            else:
                con.execute("INSERT INTO t VALUES (?)", [i])
        select_cols = "*"
        if with_window_columns:
            select_cols += (
                f", DATE '{start.isoformat()}' AS window_start, "
                f"DATE '{end.isoformat()}' AS window_end"
            )
        con.execute(f"COPY (SELECT {select_cols} FROM t) TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    return path


def _build_good_window(
    tmp_path: Path,
    monkeypatch,
    group_start: date = DAY,
    group_end: date = DAY,
    lead_in_days: int = LEAD_IN_DAYS,
    mmsi_list: list[int] | None = None,
    thin_mmsi_list: list[int] | None = None,
) -> tuple[dict[str, Path], Path]:
    """Build every artifact prune._evaluate_group checks, all passing, for [group_start,
    group_end]. Returns (roots, manifest_path). Disables the statistical sanity gate (see module
    docstring) -- tests that care about it override REFERENCE_EVENT_RATES_PER_DAY themselves.
    """
    monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {})
    mmsi_list = mmsi_list if mmsi_list is not None else MMSI
    thin_mmsi_list = thin_mmsi_list if thin_mmsi_list is not None else mmsi_list

    roots = prune._window_roots(tmp_path)
    for day in daterange(group_start, group_end):
        _write_mmsi_table(partition_path(day, roots["clean"]), mmsi_list)
        _write_mmsi_table(partition_path(day, roots["thin"]), thin_mmsi_list)
    for day in daterange(group_start - timedelta(days=lead_in_days), group_end):
        _write_mmsi_table(partition_path(day, roots["liveness"]), [999999999])

    _write_window_artifact(roots["ship_type"], group_start, group_end)
    _write_window_artifact(roots["voyages"], group_start, group_end, with_window_columns=False)
    _write_window_artifact(roots["identity"], group_start, group_end, with_window_columns=False)
    _write_window_artifact(roots["anchorages"], group_start, group_end)
    _write_window_artifact(roots["spoofing"], group_start, group_end, kind="impossible_speed")
    _write_window_artifact(roots["sts"], group_start, group_end)
    _write_window_artifact(roots["behaviour"], group_start, group_end)
    _write_window_artifact(roots["identity_anomalies"], group_start, group_end)

    manifest_path = tmp_path / "manifest.json"
    for day in daterange(group_start, group_end):
        manifest.record(
            manifest_path,
            day,
            cleaned_at="2026-01-01T00:00:00+00:00",  # a real candidate is always past "clean"
            verified_at="2026-01-01T00:00:00+00:00",
            fingerprint_message_count=len(mmsi_list),
            fingerprint_n_distinct_mmsi=len(mmsi_list),
            fingerprint_min_timestamp="2024-06-10T00:00:00",
            fingerprint_max_timestamp="2024-06-10T23:59:59",
            fingerprint_bbox=[54.0, 55.0, 10.0, 11.0],
        )
    return roots, manifest_path


# ---------------------------------------------------------------------------
# Happy path / dry-run / --yes-delete
# ---------------------------------------------------------------------------


def test_happy_path_prunes_with_yes_delete(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert not partition_path(DAY, roots["clean"]).parent.exists()
    assert (tmp_path / ".trash" / "date=2024-06-10" / "part-0.parquet").exists()
    assert manifest.day_state(manifest_path, DAY) == "reduced"


def test_dry_run_without_yes_delete_touches_nothing(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)

    prune.prune(data_root=tmp_path, yes_delete=False, lead_in_days=LEAD_IN_DAYS)

    assert partition_path(DAY, roots["clean"]).exists()
    assert not (tmp_path / ".trash").exists()
    assert manifest.day_state(manifest_path, DAY) == "clean"  # not "reduced"


def test_dry_run_still_backs_up_manifest(tmp_path, monkeypatch):
    _build_good_window(tmp_path, monkeypatch)

    prune.prune(data_root=tmp_path, yes_delete=False, lead_in_days=LEAD_IN_DAYS)

    assert (tmp_path / "manifest.json.bak").exists()


# ---------------------------------------------------------------------------
# .no-prune kill switch
# ---------------------------------------------------------------------------


def test_no_prune_sentinel_aborts_with_nothing_touched(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    (tmp_path / ".no-prune").touch()

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert partition_path(DAY, roots["clean"]).exists()
    assert not (tmp_path / "manifest.json.bak").exists()
    assert manifest.day_state(manifest_path, DAY) == "clean"


# ---------------------------------------------------------------------------
# max_days
# ---------------------------------------------------------------------------


def test_max_days_caps_candidates_processed_per_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {})
    # Five separate, non-contiguous single-day "windows" (gap of 3 days between each so they never
    # merge into one contiguous group -- see _group_contiguous).
    days = [DAY + timedelta(days=3 * i) for i in range(5)]
    for day in days:
        _build_good_window(tmp_path, monkeypatch, group_start=day, group_end=day)
    manifest_path = tmp_path / "manifest.json"

    prune.prune(data_root=tmp_path, max_days=2, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    reduced = [d for d in days if manifest.day_state(manifest_path, d) == "reduced"]
    assert len(reduced) == 2
    assert reduced == sorted(days)[:2]  # the two earliest candidates, per _select_candidates


# ---------------------------------------------------------------------------
# Individual gates, each failing alone blocks only that day/group
# ---------------------------------------------------------------------------


def test_missing_window_artifact_blocks_pruning(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    shutil.rmtree(window_partition_path(DAY, DAY, roots["anchorages"]).parent)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert partition_path(DAY, roots["clean"]).exists()
    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_unreadable_window_artifact_blocks_pruning(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    path = window_partition_path(DAY, DAY, roots["sts"])
    path.write_text("not a parquet file")

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_empty_required_artifact_blocks_pruning(tmp_path, monkeypatch):
    """voyages must be nonempty (allow_zero=False), unlike the event detectors."""
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    _write_window_artifact(
        roots["voyages"], DAY, DAY, n_rows=0, with_window_columns=False
    )  # overwrite with 0 rows

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_zero_row_event_detectors_do_not_block_pruning(tmp_path, monkeypatch):
    """spoofing/sts/behaviour/identity_anomalies finding nothing is a legitimate result, not a
    build failure -- mirrors pipeline.window's own posture for the same four tables."""
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    for name in ("spoofing", "sts", "behaviour", "identity_anomalies"):
        kind = "impossible_speed" if name == "spoofing" else None
        _write_window_artifact(roots[name], DAY, DAY, n_rows=0, kind=kind)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "reduced"


def test_missing_thin_partition_blocks_pruning(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    partition_path(DAY, roots["thin"]).unlink()

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_thin_day_count_mismatch_blocks_pruning(tmp_path, monkeypatch):
    start, end = DAY, DAY + timedelta(days=1)
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch, group_start=start, group_end=end)
    partition_path(end, roots["thin"]).unlink()  # only 1 of 2 clean days has a thin partition

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, start) == "clean"
    assert manifest.day_state(manifest_path, end) == "clean"


def test_thin_mmsi_ratio_below_threshold_blocks_pruning(tmp_path, monkeypatch):
    _roots, manifest_path = _build_good_window(
        tmp_path, monkeypatch, mmsi_list=MMSI, thin_mmsi_list=MMSI[:1]  # 1/5 = 20% << 95%
    )

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_thin_mmsi_ratio_at_or_above_threshold_does_not_block(tmp_path, monkeypatch):
    """Negative control for the ratio gate: exact match (100%) must not be flagged."""
    _roots, manifest_path = _build_good_window(
        tmp_path, monkeypatch, mmsi_list=MMSI, thin_mmsi_list=MMSI
    )

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "reduced"


def test_incomplete_liveness_lead_in_blocks_pruning(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    partition_path(LEAD_IN_START, roots["liveness"]).unlink()  # first lead-in day missing

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_complete_liveness_lead_in_does_not_block(tmp_path, monkeypatch):
    """Negative control: a fully-covered lead-in range must not be flagged."""
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "reduced"


def test_missing_fingerprint_field_blocks_pruning(tmp_path, monkeypatch):
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    data = manifest.load(manifest_path)
    del data[DAY.isoformat()]["fingerprint_bbox"]
    manifest._write_atomic(manifest_path, data)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_provenance_mismatch_blocks_pruning(tmp_path, monkeypatch):
    """A window-partitioned artifact whose own window_start/window_end columns don't match the
    window being pruned must block -- simulates a stale or mislabelled artifact on disk."""
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    # Overwrite behaviour's window=2024-06-10_2024-06-10 file with one whose OWN columns claim a
    # different window than the path implies.
    wrong_end = DAY + timedelta(days=5)
    path = window_partition_path(DAY, DAY, roots["behaviour"])
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (x BIGINT)")
        con.execute("INSERT INTO t VALUES (1)")
        con.execute(
            "COPY (SELECT *, DATE '2024-06-10' AS window_start, "
            f"DATE '{wrong_end.isoformat()}' AS window_end FROM t) "
            f"TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_low_free_disk_blocks_pruning(tmp_path, monkeypatch):
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    fake_usage = shutil._ntuple_diskusage(total=100e9, used=99e9, free=1e9)
    monkeypatch.setattr(prune.shutil, "disk_usage", lambda _path: fake_usage)

    prune.prune(data_root=tmp_path, yes_delete=True, min_free_gb=20.0, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_sufficient_free_disk_does_not_block(tmp_path, monkeypatch):
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    fake_usage = shutil._ntuple_diskusage(total=100e9, used=1e9, free=99e9)
    monkeypatch.setattr(prune.shutil, "disk_usage", lambda _path: fake_usage)

    prune.prune(data_root=tmp_path, yes_delete=True, min_free_gb=20.0, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "reduced"


# ---------------------------------------------------------------------------
# Statistical sanity gate
# ---------------------------------------------------------------------------


def test_statistical_sanity_gate_catches_order_of_magnitude_deviation(tmp_path, monkeypatch):
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    # sts reference is 50/day; the fixture's sts artifact has 1 row (n_rows default) -- 50x under.
    monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {"sts": 50.0})

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_statistical_sanity_gate_passes_within_order_of_magnitude(tmp_path, monkeypatch):
    """Negative control: an observed count within 10x of the reference must not be flagged --
    without this, a detector that keeps working correctly could never pass the gate either."""
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    _write_window_artifact(roots["sts"], DAY, DAY, n_rows=3)  # reference 10/day, 3 is within 10x
    monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {"sts": 10.0})

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "reduced"


def test_statistical_sanity_gate_catches_deviation_in_either_direction(tmp_path, monkeypatch):
    """An observed count far ABOVE the reference (not just below) must also be caught -- a
    detector emitting spurious duplicates would look like this."""
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    _write_window_artifact(roots["sts"], DAY, DAY, n_rows=1000)  # reference 10/day, 100x over
    monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {"sts": 10.0})

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "clean"


# ---------------------------------------------------------------------------
# A gate failing for one group does not abort the whole run
# ---------------------------------------------------------------------------


def test_one_group_failing_does_not_block_an_independent_group(tmp_path, monkeypatch):
    good_day = DAY
    bad_day = DAY + timedelta(days=10)  # far enough apart to never be contiguous
    _good_roots, manifest_path = _build_good_window(
        tmp_path, monkeypatch, group_start=good_day, group_end=good_day
    )
    bad_roots, _ = _build_good_window(
        tmp_path, monkeypatch, group_start=bad_day, group_end=bad_day
    )
    shutil.rmtree(window_partition_path(bad_day, bad_day, bad_roots["anchorages"]).parent)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, good_day) == "reduced"
    assert manifest.day_state(manifest_path, bad_day) == "clean"


# ---------------------------------------------------------------------------
# .trash emptying (A0.2)
# ---------------------------------------------------------------------------


def test_empty_trash_removes_entries_confirmed_reduced(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    old_day = date(2024, 5, 1)
    manifest.record(manifest_path, old_day, clean_discarded_at="2026-01-01T00:00:00+00:00")
    trash_dir = tmp_path / ".trash" / "date=2024-05-01"
    trash_dir.mkdir(parents=True)
    (trash_dir / "part-0.parquet").write_bytes(b"x")

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert not trash_dir.exists()


def test_empty_trash_dry_run_leaves_entries_in_place(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    old_day = date(2024, 5, 1)
    manifest.record(manifest_path, old_day, clean_discarded_at="2026-01-01T00:00:00+00:00")
    trash_dir = tmp_path / ".trash" / "date=2024-05-01"
    trash_dir.mkdir(parents=True)
    (trash_dir / "part-0.parquet").write_bytes(b"x")

    prune.prune(data_root=tmp_path, yes_delete=False, lead_in_days=LEAD_IN_DAYS)

    assert trash_dir.exists()


def test_empty_trash_leaves_unconfirmed_entries_alone(tmp_path, monkeypatch):
    """A .trash entry whose manifest state is NOT 'reduced' (e.g. the quarantine step never got
    to record clean_discarded_at) must be left in place, even with --yes-delete -- see A0.2."""
    manifest_path = tmp_path / "manifest.json"
    unfinished_day = date(2024, 5, 2)
    manifest.record(manifest_path, unfinished_day, cleaned_at="2026-01-01T00:00:00+00:00")
    trash_dir = tmp_path / ".trash" / "date=2024-05-02"
    trash_dir.mkdir(parents=True)
    (trash_dir / "part-0.parquet").write_bytes(b"x")

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert trash_dir.exists()


def test_this_invocations_own_quarantine_is_not_emptied_by_it(tmp_path, monkeypatch):
    """A0.2's core guarantee: real emptying happens only on the INVOCATION AFTER the one that
    quarantined a day, never the same call."""
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    trash_dir = tmp_path / ".trash" / "date=2024-06-10"
    assert trash_dir.exists()  # quarantined this run
    assert manifest.day_state(manifest_path, DAY) == "reduced"

    # A second invocation with nothing new to prune should now empty it.
    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)
    assert not trash_dir.exists()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_already_reduced_day_is_not_a_candidate(tmp_path, monkeypatch):
    _roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    manifest.record(manifest_path, DAY, clean_discarded_at="2026-01-01T00:00:00+00:00")

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    # Nothing to do -- clean partition (if any) is untouched, no error.
    assert manifest.day_state(manifest_path, DAY) == "reduced"


def test_day_without_verified_at_is_not_a_candidate(tmp_path, monkeypatch):
    roots, manifest_path = _build_good_window(tmp_path, monkeypatch)
    data = manifest.load(manifest_path)
    del data[DAY.isoformat()]["verified_at"]
    manifest._write_atomic(manifest_path, data)

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert partition_path(DAY, roots["clean"]).exists()
    assert manifest.day_state(manifest_path, DAY) == "clean"


def test_no_candidates_is_a_clean_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {})
    manifest_path = tmp_path / "manifest.json"
    manifest.record(manifest_path, DAY, downloaded_at="2026-01-01T00:00:00+00:00")  # raw_only

    prune.prune(data_root=tmp_path, yes_delete=True, lead_in_days=LEAD_IN_DAYS)

    assert manifest.day_state(manifest_path, DAY) == "raw_only"
