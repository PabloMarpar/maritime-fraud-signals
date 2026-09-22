"""Unit tests for model.discriminative_check. All fixtures are synthetic, written to tmp_path;
nothing here touches data/. Every call uses a small n_bootstrap -- correctness of the verdict
logic does not depend on bootstrap resolution, and keeping it small keeps this suite fast.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from features.panel import RATE_BASE_COLUMNS
from model import discriminative_check as dc

WINDOW_START = date(2024, 6, 1)
WINDOW_END = date(2024, 6, 30)

# Every feature column the module expects to find in the panel -- a synthetic row must supply all
# of them (defaulted to 0.0 below) even when a test only cares about one, or querying any OTHER
# feature in the loop fails with "column not found".
FEATURE_COLUMNS: list[str] = []
for _base in RATE_BASE_COLUMNS:
    _short = _base.removeprefix("n_")
    FEATURE_COLUMNS += [
        _base,
        f"rate_{_short}_per_voyage",
        f"rate_{_short}_per_observed_day",
        f"rate_{_short}_per_1000_messages",
    ]


def _row(
    mmsi: int,
    *,
    imo: str | None = "1234567",
    ship_type: str | None = "Tanker",
    n_observed_days: int = 10,
    is_after: bool = False,
    is_as_of: bool = False,
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
    **feature_overrides: float | None,
) -> dict:
    """One synthetic panel row. feature_overrides may set any name in FEATURE_COLUMNS; anything
    not overridden defaults to 0.0.
    """
    row = {
        "mmsi": mmsi,
        "imo": imo,
        "ship_type": ship_type,
        "n_observed_days": n_observed_days,
        "label_is_sanctioned_after_window_end": is_after,
        "label_is_sanctioned_as_of_window_end": is_as_of,
        "window_start": window_start,
        "window_end": window_end,
    }
    for col in FEATURE_COLUMNS:
        row[col] = feature_overrides.get(col, 0.0)
    unknown = set(feature_overrides) - set(FEATURE_COLUMNS)
    assert not unknown, f"not a real feature column: {unknown}"
    return row


def _write_panel(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("rows_df", pd.DataFrame(rows))
        con.execute("CREATE TABLE t AS SELECT * FROM rows_df")
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _run(panel_path: Path, out_path: Path, summary_path: Path, **kwargs) -> Path:
    kwargs.setdefault("n_bootstrap", 20)
    kwargs.setdefault("n_exposure_buckets", 1)  # collapse exposure to a no-op; test ship_type only
    return dc.build_discriminative_check(
        panel_path=panel_path, out_path=out_path, summary_path=summary_path, **kwargs
    )


def _read_results(out_path: Path) -> list[dict]:
    con = duckdb.connect()
    try:
        cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{out_path.as_posix()}'").fetchall()]
        rows = con.execute(f"SELECT * FROM '{out_path.as_posix()}'").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        con.close()


def _result_for(results: list[dict], feature: str, matched: bool) -> dict:
    for r in results:
        if r["feature"] == feature and r["matched"] == matched:
            return r
    raise AssertionError(f"no result for feature={feature} matched={matched}")


def test_perfect_separation_discriminates_as_expected(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    results = _read_results(out)
    r = _result_for(results, "n_gaps", matched=False)

    assert r["n_pos"] == 6
    assert r["n_neg"] == 6
    assert r["auc"] == pytest.approx(1.0)
    assert r["auc_ci_low"] > 0.5
    assert r["verdict"] == "discriminates_as_expected"


def test_perfect_inverse_separation_discriminates_opposite(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=0.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=5.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    r = _result_for(_read_results(out), "n_gaps", matched=False)

    assert r["auc"] == pytest.approx(0.0)
    assert r["auc_ci_high"] < 0.5
    assert r["verdict"] == "discriminates_opposite"


def test_identical_distributions_gives_no_discrimination(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=1.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=1.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    r = _result_for(_read_results(out), "n_gaps", matched=False)

    # Every value identical (both classes) -> every bootstrap resample also ties exactly ->
    # AUC is deterministically 0.5 with zero-width CI, not a fluke of a particular resample.
    assert r["auc"] == pytest.approx(0.5)
    assert r["auc_ci_low"] == pytest.approx(0.5)
    assert r["auc_ci_high"] == pytest.approx(0.5)
    assert r["verdict"] == "no_discrimination"


def test_too_few_positives_is_insufficient_data(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(3)]  # < MIN_GROUP_SIZE
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    r = _result_for(_read_results(out), "n_gaps", matched=False)

    assert r["n_pos"] == 3
    assert r["verdict"] == "insufficient_data"
    assert r["auc"] is None
    assert r["auc_ci_low"] is None
    assert r["mannwhitney_p"] is None
    # Means/medians are still reported even without a verdict -- insufficient for a CI, not for
    # descriptive stats.
    assert r["mean_pos"] == pytest.approx(5.0)


def test_orphaned_and_already_sanctioned_rows_excluded_from_population(tmp_path):
    rows = [
        _row(1, imo=None, is_after=True, n_gaps=9.0),  # orphaned: imo IS NULL, excluded
        _row(2, is_as_of=True, n_gaps=9.0),  # already sanctioned as of window_end, excluded
    ]
    rows += [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    r = _result_for(_read_results(out), "n_gaps", matched=False)

    # If the orphaned or already-sanctioned row leaked in, n_pos would be 7 or the mean would be
    # pulled toward 9.0.
    assert r["n_pos"] == 6
    assert r["mean_pos"] == pytest.approx(5.0)


def test_null_feature_value_excluded_not_imputed(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    # Two extra positive rows with this ONE feature undefined (e.g. rate_*_per_voyage when
    # voyage_count was 0 this month) -- must be dropped from n_gaps's own count, not treated as 0.
    rows += [_row(3000 + i, is_after=True, n_gaps=None) for i in range(2)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    r = _result_for(_read_results(out), "n_gaps", matched=False)

    assert r["n_pos"] == 6  # not 8
    assert r["mean_pos"] == pytest.approx(5.0)


def test_matched_scope_drops_ship_types_with_only_one_class(tmp_path):
    # Tanker: both classes present -> a matched group.
    rows = [_row(1000 + i, ship_type="Tanker", is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, ship_type="Tanker", is_after=False, n_gaps=0.0) for i in range(6)]
    # Cargo: negatives only, no positive counterpart -> dropped from the matched scope entirely.
    rows += [_row(3000 + i, ship_type="Cargo", is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    results = _read_results(out)
    unmatched = _result_for(results, "n_gaps", matched=False)
    matched = _result_for(results, "n_gaps", matched=True)

    assert unmatched["n_neg"] == 12  # 6 Tanker + 6 Cargo
    assert matched["n_neg"] == 6  # Cargo dropped, only Tanker's matched negatives remain
    assert matched["n_pos"] == 6


def test_raises_when_panel_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        _run(tmp_path / "nope.parquet", tmp_path / "out.parquet", tmp_path / "summary.txt")


def test_raises_when_panel_spans_more_than_one_window(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [
        _row(2000 + i, is_after=False, n_gaps=0.0, window_start=date(2024, 7, 1), window_end=date(2024, 7, 31))
        for i in range(6)
    ]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    with pytest.raises(ValueError, match="one distinct"):
        _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")


def test_idempotent_by_default_then_force_rebuilds(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    out_path = tmp_path / "out.parquet"
    summary_path = tmp_path / "summary.txt"
    _write_panel(panel_path, rows)

    _run(panel_path, out_path, summary_path)
    first_mtime = out_path.stat().st_mtime_ns

    _run(panel_path, out_path, summary_path)  # no force -- should be a no-op
    assert out_path.stat().st_mtime_ns == first_mtime

    _run(panel_path, out_path, summary_path, force=True)
    assert out_path.exists()  # rebuilt without error


def test_output_carries_window_and_provenance_columns(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)

    out = _run(panel_path, tmp_path / "out.parquet", tmp_path / "summary.txt")
    row = _read_results(out)[0]
    assert row["window_start"] == WINDOW_START
    assert row["window_end"] == WINDOW_END
    assert row["git_sha"] is not None
    assert row["built_at"] is not None
    assert row["n_bootstrap"] == 20
    assert row["n_exposure_buckets"] == 1


def test_summary_file_reports_decision(tmp_path):
    rows = [_row(1000 + i, is_after=True, n_gaps=5.0) for i in range(6)]
    rows += [_row(2000 + i, is_after=False, n_gaps=0.0) for i in range(6)]
    panel_path = tmp_path / "panel.parquet"
    summary_path = tmp_path / "summary.txt"
    _write_panel(panel_path, rows)

    _run(panel_path, tmp_path / "out.parquet", summary_path)
    text = summary_path.read_text(encoding="utf-8")

    assert "DECISION:" in text
    # n_gaps is not one of the rate_* columns, so it alone cannot produce a GO verdict even
    # though it perfectly separates here -- the decision only counts rate_* features.
    assert "NO-GO" in text


def test_rate_feature_discriminating_as_expected_yields_go_decision(tmp_path):
    rows = [
        _row(1000 + i, is_after=True, rate_draught_change_unexplained_per_voyage=5.0)
        for i in range(6)
    ]
    rows += [
        _row(2000 + i, is_after=False, rate_draught_change_unexplained_per_voyage=0.0)
        for i in range(6)
    ]
    panel_path = tmp_path / "panel.parquet"
    summary_path = tmp_path / "summary.txt"
    _write_panel(panel_path, rows)

    _run(panel_path, tmp_path / "out.parquet", summary_path)
    text = summary_path.read_text(encoding="utf-8")

    assert "DECISION: GO" in text
    assert "rate_draught_change_unexplained_per_voyage" in text
