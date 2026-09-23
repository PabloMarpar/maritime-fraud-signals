"""Unit tests for model.build_year_gate. All fixtures are synthetic, written to tmp_path; nothing
here touches data/ or the network. Small n_bootstrap keeps this suite fast without affecting
correctness of the pass/fail logic being tested.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from model import build_year_gate as gate

WINDOW_START = date(2024, 6, 1)
WINDOW_END = date(2024, 6, 30)


def _row(mmsi: int, *, imo: str, is_after: bool = False, ship_type: str = "Tanker", flag_country: str = "Panama") -> dict:
    # Panama is an ITF flag-of-convenience registry (process.foc), so the default row satisfies
    # both r1 (tanker) and r2 (tanker+FOC) -- the whole-population and tanker+FOC-scoped G2 tests
    # coincide unless a test overrides ship_type/flag_country to build a divergent subpopulation.
    return {
        "mmsi": mmsi,
        "imo": imo,
        "ship_type": ship_type,
        "flag_country": flag_country,
        "label_is_sanctioned_after_window_end": is_after,
        "label_is_sanctioned_as_of_window_end": False,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }


def _write_panel(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("rows_df", pd.DataFrame(rows))
        con.execute("CREATE TABLE t AS SELECT * FROM rows_df")
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_build_year(path: Path, rows: list[tuple[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("rows_df", pd.DataFrame(rows, columns=["imo", "build_year"]))
        con.execute("CREATE TABLE t AS SELECT * FROM rows_df")
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _run(tmp_path: Path, rows: list[dict], build_year_rows: list[tuple[str, int]], **kwargs):
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)
    build_year_path = tmp_path / "build_year.parquet"
    _write_build_year(build_year_path, build_year_rows)
    out_path = tmp_path / "gate.parquet"
    summary_path = tmp_path / "summary.txt"
    kwargs.setdefault("n_bootstrap", 100)
    result_path = gate.build_gate_check(
        panel_path=panel_path,
        build_year_path=build_year_path,
        out_path=out_path,
        summary_path=summary_path,
        **kwargs,
    )
    return result_path, summary_path


def _read_one(path: Path) -> dict:
    con = duckdb.connect()
    try:
        cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{path.as_posix()}'").fetchall()]
        (row,) = con.execute(f"SELECT * FROM '{path.as_posix()}'").fetchall()
        return dict(zip(cols, row, strict=True))
    finally:
        con.close()


def test_go_when_all_gates_pass(tmp_path):
    # 100% coverage, identical between classes -> G1/G2 trivially pass; all years well in the past.
    rows = [_row(1000 + i, imo=f"{i:07d}", is_after=(i < 10)) for i in range(100)]
    build_year_rows = [(f"{i:07d}", 2000) for i in range(100)]
    # Fix up imo to pass checksum by reusing a known-valid pattern isn't needed here since
    # build_year_gate never re-validates checksums (that's ingest.wikidata_ships's job) -- it
    # trusts build_year_path as already-reconciled input.
    out_path, summary_path = _run(tmp_path, rows, build_year_rows)

    result = _read_one(out_path)
    assert result["g1_pass"] is True
    assert result["g2_pass"] is True
    assert result["g3_pass"] is True
    assert result["overall_verdict"] == "GO"
    assert "OVERALL VERDICT: GO" in summary_path.read_text(encoding="utf-8")


def test_g1_fails_below_coverage_threshold(tmp_path):
    # 40 rows total (20 pos, 20 neg), only 5+5=10 covered -> 25% coverage, balanced across classes
    # so G2 passes cleanly and the failure is isolated to G1.
    rows = [_row(1000 + i, imo=f"{i:07d}", is_after=(i < 20)) for i in range(40)]
    build_year_rows = [(f"{i:07d}", 2000) for i in range(5)] + [
        (f"{i:07d}", 2000) for i in range(20, 25)
    ]
    out_path, _ = _run(tmp_path, rows, build_year_rows)

    result = _read_one(out_path)
    assert result["g1_pass"] is False
    assert result["g1_coverage"] == pytest.approx(0.25)
    assert result["g2_pass"] is True  # coverage_pos == coverage_neg == 25%
    assert result["overall_verdict"] == "NO-GO"


def test_g2_fails_on_differential_coverage(tmp_path):
    # 30 positive (all 30 covered, 100%), 30 negative (only 15 covered, 50%) -> overall coverage
    # 45/60=75% (G1 passes), but a large, significant coverage gap between classes (G2 fails).
    rows = [_row(1000 + i, imo=f"{i:07d}", is_after=(i < 30)) for i in range(60)]
    build_year_rows = [(f"{i:07d}", 2000) for i in range(30)] + [
        (f"{i:07d}", 2000) for i in range(30, 45)
    ]
    out_path, _ = _run(tmp_path, rows, build_year_rows)

    result = _read_one(out_path)
    assert result["g1_pass"] is True
    assert result["g2_pass"] is False
    assert result["g2_global_coverage_pos"] == pytest.approx(1.0)
    assert result["g2_global_coverage_neg"] == pytest.approx(0.5)
    assert result["g2_global_fisher_p"] < 0.01
    assert result["overall_verdict"] == "NO-GO"


def test_g2_scoped_check_catches_a_gap_the_global_test_would_miss(tmp_path):
    # The real risk model.build_year_gate's scoped G2 check exists for: a large, well-covered,
    # balanced non-tanker/non-FOC population dilutes the global coverage gap below the 5pp floor,
    # while the tanker+FOC subpopulation -- where R3 actually operates -- has a real 80pp gap.
    tanker_foc_pos = [
        _row(2000 + i, imo=f"tf-pos-{i:04d}", is_after=True, ship_type="Tanker", flag_country="Panama")
        for i in range(30)
    ]
    tanker_foc_neg = [
        _row(3000 + i, imo=f"tf-neg-{i:04d}", is_after=False, ship_type="Tanker", flag_country="Panama")
        for i in range(30)
    ]
    diluting_neg = [
        _row(4000 + i, imo=f"dil-{i:04d}", is_after=False, ship_type="Cargo", flag_country="Denmark")
        for i in range(600)
    ]
    rows = tanker_foc_pos + tanker_foc_neg + diluting_neg
    build_year_rows = (
        [(f"tf-pos-{i:04d}", 2000) for i in range(30)]  # all 30 tanker+FOC positives covered
        + [(f"tf-neg-{i:04d}", 2000) for i in range(6)]  # only 6/30 tanker+FOC negatives covered
        + [(f"dil-{i:04d}", 2000) for i in range(600)]  # all 600 diluting negatives covered
    )
    out_path, _ = _run(tmp_path, rows, build_year_rows)

    result = _read_one(out_path)
    # Global gap is diluted below the 5pp floor by the 600 well-covered non-tanker rows.
    assert result["g2_global_coverage_gap_pp"] < 5.0
    assert result["g2_global_pass"] is True
    # But the scoped check, restricted to the 60 tanker+FOC rows, sees the real 80pp gap.
    assert result["g2_scoped_coverage_pos"] == pytest.approx(1.0)
    assert result["g2_scoped_coverage_neg"] == pytest.approx(0.2)
    assert result["g2_scoped_pass"] is False
    # So the overall gate correctly fails even though the global-only test alone would have passed.
    assert result["g2_pass"] is False
    assert result["overall_verdict"] == "NO-GO"


def test_g3_isolated_failure_fails_gate_but_does_not_raise(tmp_path):
    # 150 covered rows, all balanced/high-coverage so G1/G2 pass; exactly 1 has a build_year after
    # window_end's year (2024) -- 1/150 = 0.67%, below the 1% systemic-raise threshold.
    rows = [_row(1000 + i, imo=f"{i:07d}", is_after=(i < 15)) for i in range(150)]
    build_year_rows = [(f"{i:07d}", 2000) for i in range(149)] + [("0000149", 2025)]
    out_path, summary_path = _run(tmp_path, rows, build_year_rows)

    result = _read_one(out_path)
    assert result["g3_pass"] is False
    assert result["g3_n_implausible"] == 1
    assert result["overall_verdict"] == "NO-GO"
    assert "mmsi=1149 imo=0000149 build_year=2025" in summary_path.read_text(encoding="utf-8")


def test_g3_systemic_failure_raises(tmp_path):
    # 50 covered rows, 5 (10%) with a build_year after window_end's year -- well above the 1%
    # systemic-raise threshold, so this must raise rather than silently report NO-GO.
    rows = [_row(1000 + i, imo=f"{i:07d}", is_after=(i < 5)) for i in range(50)]
    build_year_rows = [(f"{i:07d}", 2000) for i in range(45)] + [
        (f"{i:07d}", 2025) for i in range(45, 50)
    ]
    with pytest.raises(ValueError, match="systemically"):
        _run(tmp_path, rows, build_year_rows)


def test_missing_build_year_source_raises_file_not_found(tmp_path):
    rows = [_row(1000, imo="1234567")]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)
    with pytest.raises(FileNotFoundError):
        gate.build_gate_check(
            panel_path=panel_path,
            build_year_path=tmp_path / "nope.parquet",
            out_path=tmp_path / "gate.parquet",
            summary_path=tmp_path / "summary.txt",
            n_bootstrap=20,
        )


def test_idempotent_unless_forced(tmp_path):
    rows = [_row(1000 + i, imo=f"{i:07d}", is_after=(i < 10)) for i in range(100)]
    build_year_rows = [(f"{i:07d}", 2000) for i in range(100)]
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)
    build_year_path = tmp_path / "build_year.parquet"
    _write_build_year(build_year_path, build_year_rows)
    out_path = tmp_path / "gate.parquet"
    summary_path = tmp_path / "summary.txt"

    gate.build_gate_check(
        panel_path=panel_path, build_year_path=build_year_path,
        out_path=out_path, summary_path=summary_path, n_bootstrap=20,
    )
    first_mtime = out_path.stat().st_mtime_ns

    gate.build_gate_check(
        panel_path=panel_path, build_year_path=build_year_path,
        out_path=out_path, summary_path=summary_path, n_bootstrap=20,
    )
    assert out_path.stat().st_mtime_ns == first_mtime

    gate.build_gate_check(
        panel_path=panel_path, build_year_path=build_year_path,
        out_path=out_path, summary_path=summary_path, n_bootstrap=20, force=True,
    )
    assert out_path.stat().st_mtime_ns != first_mtime
