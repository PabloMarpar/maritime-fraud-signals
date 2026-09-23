"""Unit tests for model.baseline. All fixtures are synthetic, written to tmp_path; nothing here
touches data/. Every call uses a small n_bootstrap -- correctness does not depend on bootstrap
resolution, and keeping it small keeps this suite fast.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from model import baseline

WINDOW_START = date(2024, 6, 1)
WINDOW_END = date(2024, 6, 30)


def _row(
    mmsi: int,
    *,
    imo: str | None = "1234567",
    ship_type: str | None = "Tanker",
    flag_country: str | None = "Panama",
    is_after: bool = False,
    is_as_of: bool = False,
    year_month: date = WINDOW_START,
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
) -> dict:
    return {
        "mmsi": mmsi,
        "imo": imo,
        "ship_type": ship_type,
        "flag_country": flag_country,
        "year_month": year_month,
        "label_is_sanctioned_after_window_end": is_after,
        "label_is_sanctioned_as_of_window_end": is_as_of,
        "window_start": window_start,
        "window_end": window_end,
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


def _write_build_year(path: Path, rows: list[tuple[str, int | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register(
            "rows_df", pd.DataFrame(rows, columns=["imo", "build_year"])
        )
        con.execute("CREATE TABLE t AS SELECT * FROM rows_df")
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_gate(
    path: Path, verdict: str, window_start: date = WINDOW_START, window_end: date = WINDOW_END
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT '{verdict}' AS overall_verdict, "
            f"DATE '{window_start.isoformat()}' AS window_start, "
            f"DATE '{window_end.isoformat()}' AS window_end) "
            f"TO '{path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


def _run(tmp_path: Path, rows: list[dict], **kwargs) -> tuple[Path, Path, Path]:
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)
    out_path = tmp_path / "baseline.parquet"
    scores_path = tmp_path / "scores.parquet"
    summary_path = tmp_path / "summary.txt"
    kwargs.setdefault("n_bootstrap", 20)
    kwargs.setdefault("build_year_path", tmp_path / "no_such_build_year.parquet")
    kwargs.setdefault("build_year_gate_path", tmp_path / "no_such_gate.parquet")
    baseline.build_baseline(
        panel_path=panel_path, out_path=out_path, scores_path=scores_path,
        summary_path=summary_path, **kwargs,
    )
    return out_path, scores_path, summary_path


def _read(path: Path) -> list[dict]:
    con = duckdb.connect()
    try:
        cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{path.as_posix()}'").fetchall()]
        rows = con.execute(f"SELECT * FROM '{path.as_posix()}'").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        con.close()


def _rule(results: list[dict], rule: str) -> dict:
    for r in results:
        if r["rule"] == rule:
            return r
    raise AssertionError(f"no result for rule={rule}")


def _base_population(n_tanker_foc_pos=5, n_tanker_foc_neg=5, n_other_neg=10) -> list[dict]:
    rows = []
    for i in range(n_tanker_foc_pos):
        rows.append(_row(300000000 + i, ship_type="Tanker", flag_country="Panama", is_after=True))
    for i in range(n_tanker_foc_neg):
        rows.append(_row(400000000 + i, ship_type="Tanker", flag_country="Panama", is_after=False))
    for i in range(n_other_neg):
        rows.append(_row(500000000 + i, ship_type="Cargo", flag_country="Denmark", is_after=False))
    return rows


def test_r1_r2_precision_and_lift(tmp_path):
    rows = _base_population(n_tanker_foc_pos=5, n_tanker_foc_neg=5, n_other_neg=10)
    out_path, _, _ = _run(tmp_path, rows)
    results = _read(out_path)

    r1 = _rule(results, "R1_tanker")
    assert r1["n_flagged"] == 10  # all tankers (5 pos + 5 neg)
    assert r1["tp"] == 5
    assert r1["precision"] == pytest.approx(0.5)
    assert r1["recall"] == pytest.approx(1.0)  # all 5 positives are tankers
    prevalence = 5 / 20
    assert r1["lift"] == pytest.approx(0.5 / prevalence)
    assert r1["verdict"] == "evaluated"


def test_r2_is_subset_of_r1_and_adds_flag_filter(tmp_path):
    rows = _base_population(n_tanker_foc_pos=5, n_tanker_foc_neg=5, n_other_neg=10)
    # Add non-FOC tankers that should be excluded by R2 but included by R1.
    rows += [_row(600000000 + i, ship_type="Tanker", flag_country="Denmark", is_after=False) for i in range(3)]
    out_path, _, _ = _run(tmp_path, rows)
    results = _read(out_path)

    r1 = _rule(results, "R1_tanker")
    r2 = _rule(results, "R2_tanker_foc")
    assert r1["n_flagged"] == 13
    assert r2["n_flagged"] == 10
    assert r2["tp"] == 5
    assert r2["precision"] > r1["precision"]  # dropping non-FOC tankers should raise precision


def test_r3_blocked_when_no_build_year_source(tmp_path):
    rows = _base_population()
    out_path, scores_path, summary_path = _run(tmp_path, rows)
    r3 = _rule(_read(out_path), "R3_tanker_foc_age")

    assert r3["verdict"] == "blocked_by_gate"
    assert r3["n_flagged"] is None
    assert r3["precision"] is None
    assert "no build-year data source" in r3["blocked_reason"]
    assert "blocked_by_gate" in summary_path.read_text(encoding="utf-8")

    scores = _read(scores_path)
    assert all(s["r3"] is None for s in scores)
    assert all(s["tier"] <= 2 for s in scores)  # r3 NULL can never win the CASE WHEN


def test_r3_blocked_when_build_year_exists_but_no_gate_verdict(tmp_path):
    rows = _base_population()
    build_year_path = tmp_path / "ship_build_year.parquet"
    _write_build_year(build_year_path, [("1234567", 2000)])
    out_path, _, _ = _run(tmp_path, rows, build_year_path=build_year_path)
    r3 = _rule(_read(out_path), "R3_tanker_foc_age")

    assert r3["verdict"] == "blocked_by_gate"
    assert "has not been run" in r3["blocked_reason"]


def test_r3_blocked_when_gate_says_no_go(tmp_path):
    rows = _base_population()
    build_year_path = tmp_path / "ship_build_year.parquet"
    gate_path = tmp_path / "gate.parquet"
    _write_build_year(build_year_path, [("1234567", 2000)])
    _write_gate(gate_path, "NO-GO")
    out_path, _, _ = _run(tmp_path, rows, build_year_path=build_year_path, build_year_gate_path=gate_path)
    r3 = _rule(_read(out_path), "R3_tanker_foc_age")

    assert r3["verdict"] == "blocked_by_gate"
    assert "NO-GO" in r3["blocked_reason"]


def test_r3_blocked_when_gate_window_mismatches_panel_window(tmp_path):
    # A gate computed for a different window must never authorise R3 for this one, even if it
    # says GO -- a stale verdict from an unrelated run is not evidence about this population.
    rows = _base_population()
    build_year_path = tmp_path / "ship_build_year.parquet"
    gate_path = tmp_path / "gate.parquet"
    _write_build_year(build_year_path, [("1234567", 2000)])
    _write_gate(gate_path, "GO", window_start=date(2024, 7, 1), window_end=date(2024, 7, 31))
    out_path, _, _ = _run(tmp_path, rows, build_year_path=build_year_path, build_year_gate_path=gate_path)
    r3 = _rule(_read(out_path), "R3_tanker_foc_age")

    assert r3["verdict"] == "blocked_by_gate"
    assert "2024-07-01_2024-07-31" in r3["blocked_reason"]


def test_r3_blocked_when_gate_predates_build_year_source(tmp_path):
    # If the build-year source was re-fetched after the gate last ran, the gate's verdict is
    # evidence about a snapshot that no longer exists -- must not be trusted until re-run.
    import time

    rows = _base_population()
    build_year_path = tmp_path / "ship_build_year.parquet"
    gate_path = tmp_path / "gate.parquet"
    _write_gate(gate_path, "GO")
    time.sleep(0.01)
    _write_build_year(build_year_path, [("1234567", 2000)])  # written AFTER the gate
    out_path, _, _ = _run(tmp_path, rows, build_year_path=build_year_path, build_year_gate_path=gate_path)
    r3 = _rule(_read(out_path), "R3_tanker_foc_age")

    assert r3["verdict"] == "blocked_by_gate"
    assert "predates" in r3["blocked_reason"]


def test_r3_evaluated_when_gate_says_go_and_unknown_age_fails_r3(tmp_path):
    # Two tanker+FOC positives: one old (imo 1111111, built 1990 -> age 34 > 15, should pass R3),
    # one with no build-year row at all (imo 2222222 -> unknown age must FAIL r3, not be NULL/dropped).
    rows = [
        _row(700000001, imo="1111111", ship_type="Tanker", flag_country="Panama", is_after=True),
        _row(700000002, imo="2222222", ship_type="Tanker", flag_country="Panama", is_after=True),
        _row(700000003, imo="3333333", ship_type="Tanker", flag_country="Panama", is_after=False),
    ]
    build_year_path = tmp_path / "ship_build_year.parquet"
    gate_path = tmp_path / "gate.parquet"
    # imo 3333333 built 2020 -> age (2024-2020=4) <= 15, should fail R3 on age, not on missing data.
    _write_build_year(build_year_path, [("1111111", 1990), ("3333333", 2020)])
    _write_gate(gate_path, "GO")
    out_path, scores_path, _ = _run(tmp_path, rows, build_year_path=build_year_path, build_year_gate_path=gate_path)

    r3 = _rule(_read(out_path), "R3_tanker_foc_age")
    assert r3["verdict"] == "evaluated"
    assert r3["n_flagged"] == 1  # only 1111111 (old) qualifies
    assert r3["tp"] == 1

    scores = {s["mmsi"]: s for s in _read(scores_path)}
    assert scores[700000001]["r3"] is True
    assert scores[700000002]["r3"] is False  # unknown build year -> fails, never NULL
    assert scores[700000003]["r3"] is False  # known but too young -> fails
    assert scores[700000001]["tier"] == 3
    assert scores[700000002]["tier"] == 2  # still r2 (tanker+FOC), just not r3


def test_tier_ranking_matches_highest_satisfied_rule(tmp_path):
    rows = _base_population(n_tanker_foc_pos=2, n_tanker_foc_neg=0, n_other_neg=0)
    rows += [_row(800000001, ship_type="Tanker", flag_country="Denmark", is_after=False)]  # r1 only
    rows += [_row(800000002, ship_type="Cargo", flag_country="Panama", is_after=False)]  # neither
    _, scores_path, _ = _run(tmp_path, rows)
    scores = {s["mmsi"]: s for s in _read(scores_path)}

    assert scores[800000001]["tier"] == 1
    assert scores[800000002]["tier"] == 0


def test_idempotent_unless_forced(tmp_path):
    rows = _base_population()
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)
    out_path = tmp_path / "baseline.parquet"
    scores_path = tmp_path / "scores.parquet"
    summary_path = tmp_path / "summary.txt"

    baseline.build_baseline(
        panel_path=panel_path, out_path=out_path, scores_path=scores_path,
        summary_path=summary_path, n_bootstrap=20,
        build_year_path=tmp_path / "no.parquet", build_year_gate_path=tmp_path / "no2.parquet",
    )
    first_mtime = out_path.stat().st_mtime_ns

    baseline.build_baseline(
        panel_path=panel_path, out_path=out_path, scores_path=scores_path,
        summary_path=summary_path, n_bootstrap=20,
        build_year_path=tmp_path / "no.parquet", build_year_gate_path=tmp_path / "no2.parquet",
    )
    assert out_path.stat().st_mtime_ns == first_mtime  # no-op without force

    baseline.build_baseline(
        panel_path=panel_path, out_path=out_path, scores_path=scores_path,
        summary_path=summary_path, n_bootstrap=20, force=True,
        build_year_path=tmp_path / "no.parquet", build_year_gate_path=tmp_path / "no2.parquet",
    )
    assert out_path.stat().st_mtime_ns != first_mtime  # rebuilt with force=True


def test_missing_panel_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        baseline.build_baseline(
            panel_path=tmp_path / "nope.parquet",
            out_path=tmp_path / "out.parquet",
            scores_path=tmp_path / "scores.parquet",
            summary_path=tmp_path / "summary.txt",
            n_bootstrap=20,
            build_year_path=tmp_path / "no.parquet",
            build_year_gate_path=tmp_path / "no2.parquet",
        )


def test_multi_window_panel_raises_value_error(tmp_path):
    rows = _base_population()
    # A second window's row: same schema, different window_start/window_end.
    rows.append(
        _row(
            999000001, ship_type="Tanker", flag_country="Panama", is_after=False,
            window_start=date(2024, 7, 1), window_end=date(2024, 7, 31),
        )
    )
    panel_path = tmp_path / "panel.parquet"
    _write_panel(panel_path, rows)
    with pytest.raises(ValueError):
        baseline.build_baseline(
            panel_path=panel_path,
            out_path=tmp_path / "out.parquet",
            scores_path=tmp_path / "scores.parquet",
            summary_path=tmp_path / "summary.txt",
            n_bootstrap=20,
            build_year_path=tmp_path / "no.parquet",
            build_year_gate_path=tmp_path / "no2.parquet",
        )


def test_scores_table_has_one_row_per_population_row(tmp_path):
    rows = _base_population(n_tanker_foc_pos=5, n_tanker_foc_neg=5, n_other_neg=10)
    _, scores_path, _ = _run(tmp_path, rows)
    assert len(_read(scores_path)) == 20
