"""The acceptance gate for P4-1's build-year age term (R3), before ``model.baseline`` may trust
it. See ``docs/DECISIONS.md``'s P4-1 entry for the full background.

**Why this exists.** ``ingest.wikidata_ships`` is CC0 and legally usable, but it is
crowd-sourced: a vessel might have a Wikidata entry, or a more complete one, *because* it became
notable after being sanctioned -- which would make build-year *availability* itself predict the
sanctions label, not build-year *value*. That would make R3 ("older than 15 years") a proxy for
"has a Wikidata page", a form of the exact temporal-leakage/circularity failure ``CLAUDE.md``
warns every modelling change must be checked for. This module checks for it, against exactly
``model.evaluation``'s P4-0 population, before ``model.baseline`` is allowed to compute R3 at all.

**Three gates, all evaluated together; failing ANY one blocks R3:**

* **G1 coverage** -- the fraction of the population with a non-NULL ``build_year`` (after joining
  ``ingest.wikidata_ships``'s output on ``imo``) must be >= :data:`MIN_COVERAGE`. Below that, the
  age term would be computed on a small, unrepresentative slice, no matter how clean.
* **G2 differential coverage, the contamination check** -- a Fisher exact test on the 2x2
  (covered/uncovered x positive/negative) table, run TWICE: once over the whole population, and
  once restricted to the tanker+FOC (R1 AND R2, via ``model.evaluation.add_tanker_foc_columns``)
  subpopulation R3 actually operates inside. Both must pass, independently -- a real risk a
  whole-population-only test would miss: the global coverage gap can look small while the gap
  inside the specific vessels R3 scores is worse (confirmed on the real 2026-09-23 run: global gap
  9.59pp vs 11.31pp inside R2 -- the global test happened to be conservative here, not by
  guarantee). Each test fails if p < :data:`FISHER_P_THRESHOLD` AND the coverage gap exceeds
  :data:`MAX_COVERAGE_GAP_PP` percentage points. The bootstrap AUC of the bare ``has_build_year``
  indicator against the label is also computed and reported (whole population only), but does NOT
  independently drive the verdict: for a binary predictor, AUC = 0.5 + (coverage_pos -
  coverage_neg)/2 exactly (confirmed on the real run: 0.5 + (0.9456-0.8497)/2 = 0.5480, the file's
  own recorded value) -- it is the same statistic as the coverage gap, restated, with no
  independent effect-size floor of its own. An earlier version of this module let its own CI
  independently fail G2, which would have made :data:`MAX_COVERAGE_GAP_PP` dead weight at any
  population size large enough for a small, practically-irrelevant gap to still produce a
  CI excluding 0.5 -- removed for that reason, not just simplified.
* **G3 plausibility** -- every ``build_year`` actually joined to the population should fall at or
  before ``window_end``'s year; a vessel this project observed transmitting AIS in the window
  cannot have been built after that window. A real run found exactly this on 2026-09-23: one
  Wikidata item (mmsi 211401960, imo 9832767) claims a 2025 service-entry date. Verified by hand
  that this is not a join/reconciliation bug (exactly one Wikidata item claims that IMO, correctly
  reconciled) -- isolated source noise, most plausibly a wrong date on the Wikidata side. An
  isolated case like this still fails G3 (forcing NO-GO) but lets the gate complete and report, so
  the offending row is visible in the summary rather than hidden behind a crash; a rate above
  :data:`MAX_IMPLAUSIBLE_FRACTION_BEFORE_RAISE` of covered rows instead raises a hard
  :exc:`ValueError`, since that pattern would look like a systemic join bug, not noise.

**Output.** ``data/processed/build_year_gate.parquet`` (one row, machine-readable --
``model.baseline`` reads its ``overall_verdict`` column, ``"GO"``/``"NO-GO"``, to decide whether to
compute R3 at all) and a plain-text ``outputs/build_year_gate_summary.txt`` for a human, including
the age distribution by ship_type and named IMO spot checks for manual verification.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from scipy.stats import fisher_exact
from sklearn.metrics import roc_auc_score

from features.panel import PANEL_PATH
from ingest.wikidata_ships import BUILD_YEAR_PATH
from model.evaluation import add_tanker_foc_columns, bootstrap_auc_ci, load_population
from process.partitions import atomic_write_parquet, git_sha

logger = logging.getLogger(__name__)

OUT_PATH = Path("data/processed/build_year_gate.parquet")
SUMMARY_PATH = Path("outputs/build_year_gate_summary.txt")

MIN_COVERAGE = 0.5
FISHER_P_THRESHOLD = 0.01
MAX_COVERAGE_GAP_PP = 5.0
N_BOOTSTRAP = 2000
BOOTSTRAP_SEED = 0
N_SPOT_CHECK_ROWS = 10

# G3: an isolated implausible build_year (source noise -- e.g. one bad Wikidata date) fails G3 but
# still lets the gate complete and report; a rate above this fraction of covered rows instead
# raises, since that pattern looks like a join bug (wrong IMO matched), not source noise.
# Unvalidated default, same posture as every other threshold in this project.
MAX_IMPLAUSIBLE_FRACTION_BEFORE_RAISE = 0.01


@dataclass(frozen=True)
class DifferentialCoverageResult:
    """One Fisher-exact differential-coverage test, over some population scope."""

    passed: bool
    fisher_p: float
    coverage_pos: float
    coverage_neg: float
    coverage_gap_pp: float
    n_pos: int
    n_neg: int


@dataclass(frozen=True)
class GateResult:
    overall_verdict: str
    n_pop: int
    n_covered: int
    g1_pass: bool
    g1_coverage: float
    g2_pass: bool
    g2_global: DifferentialCoverageResult
    g2_scoped: DifferentialCoverageResult
    g2_auc: float | None
    g2_auc_ci_low: float | None
    g2_auc_ci_high: float | None
    g3_pass: bool
    g3_n_implausible: int


def _differential_coverage_test(
    con: duckdb.DuckDBPyConnection, table_name: str, where: str = "TRUE"
) -> DifferentialCoverageResult:
    """One Fisher-exact differential-coverage test: does Wikidata-coverage rate differ between
    the positive and negative class, within the rows of `table_name` matching `where`.
    """
    (covered_pos, uncovered_pos, covered_neg, uncovered_neg) = con.execute(
        f"SELECT "
        f"count(*) FILTER (WHERE has_build_year AND label_is_sanctioned_after_window_end), "
        f"count(*) FILTER (WHERE NOT has_build_year AND label_is_sanctioned_after_window_end), "
        f"count(*) FILTER (WHERE has_build_year AND NOT label_is_sanctioned_after_window_end), "
        f"count(*) FILTER (WHERE NOT has_build_year AND NOT label_is_sanctioned_after_window_end) "
        f"FROM {table_name} WHERE {where}"
    ).fetchone()
    n_pos = covered_pos + uncovered_pos
    n_neg = covered_neg + uncovered_neg
    coverage_pos = covered_pos / n_pos if n_pos else 0.0
    coverage_neg = covered_neg / n_neg if n_neg else 0.0
    coverage_gap_pp = abs(coverage_pos - coverage_neg) * 100

    _, fisher_p = fisher_exact([[covered_pos, uncovered_pos], [covered_neg, uncovered_neg]])
    fisher_p = float(fisher_p)
    fails = fisher_p < FISHER_P_THRESHOLD and coverage_gap_pp > MAX_COVERAGE_GAP_PP

    return DifferentialCoverageResult(
        passed=not fails,
        fisher_p=fisher_p,
        coverage_pos=coverage_pos,
        coverage_neg=coverage_neg,
        coverage_gap_pp=coverage_gap_pp,
        n_pos=n_pos,
        n_neg=n_neg,
    )


def _run_gates(
    con: duckdb.DuckDBPyConnection,
    build_year_path: Path,
    window_end_year: int,
    n_bootstrap: int,
    seed: int,
) -> GateResult:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _gate_pop_base AS "
        "SELECT p.*, byr.build_year, (byr.build_year IS NOT NULL) AS has_build_year "
        "FROM _population p LEFT JOIN "
        f"(SELECT imo, build_year FROM read_parquet('{build_year_path.as_posix()}')) byr "
        "ON byr.imo = p.imo"
    )
    # r1/r2 added so G2 can also be tested inside the tanker+FOC subpopulation R3 actually scores
    # -- see module docstring for why the whole-population test alone is not sufficient.
    add_tanker_foc_columns(con, source_table="_gate_pop_base", dest_table="_gate_pop")

    (n_pop, n_covered) = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE has_build_year) FROM _gate_pop"
    ).fetchone()
    g1_coverage = n_covered / n_pop if n_pop else 0.0
    g1_pass = g1_coverage >= MIN_COVERAGE
    logger.info("G1 coverage: %d/%d = %.4f (pass=%s)", n_covered, n_pop, g1_coverage, g1_pass)

    g2_global = _differential_coverage_test(con, "_gate_pop")
    g2_scoped = _differential_coverage_test(con, "_gate_pop", where="r1 AND r2")
    g2_pass = g2_global.passed and g2_scoped.passed
    logger.info(
        "G2 differential coverage (whole population): pos=%.4f neg=%.4f gap=%.2fpp "
        "fisher_p=%.4g (pass=%s)",
        g2_global.coverage_pos,
        g2_global.coverage_neg,
        g2_global.coverage_gap_pp,
        g2_global.fisher_p,
        g2_global.passed,
    )
    logger.info(
        "G2 differential coverage (tanker+FOC subpopulation, n_pos=%d n_neg=%d): pos=%.4f "
        "neg=%.4f gap=%.2fpp fisher_p=%.4g (pass=%s)",
        g2_scoped.n_pos,
        g2_scoped.n_neg,
        g2_scoped.coverage_pos,
        g2_scoped.coverage_neg,
        g2_scoped.coverage_gap_pp,
        g2_scoped.fisher_p,
        g2_scoped.passed,
    )

    # Reported for interpretability only -- for a binary predictor this is algebraically
    # 0.5 + (coverage_pos - coverage_neg)/2, the same statistic as g2_global's gap restated, so it
    # does not get an independent vote on g2_pass. See module docstring.
    rows = con.execute(
        "SELECT label_is_sanctioned_after_window_end, has_build_year FROM _gate_pop"
    ).fetchall()
    labels = np.array([bool(r[0]) for r in rows])
    values = np.array([bool(r[1]) for r in rows], dtype=float)
    auc: float | None
    auc_ci_low: float | None
    auc_ci_high: float | None
    if 0 < labels.sum() < len(labels):
        auc = float(roc_auc_score(labels, values))
        rng = np.random.default_rng(seed)
        auc_ci_low, auc_ci_high = bootstrap_auc_ci(
            values[labels], values[~labels], n_bootstrap, rng
        )
    else:
        auc = auc_ci_low = auc_ci_high = None

    (n_implausible,) = con.execute(
        f"SELECT count(*) FROM _gate_pop WHERE has_build_year AND build_year > {window_end_year}"
    ).fetchone()
    implausible_fraction = n_implausible / n_covered if n_covered else 0.0
    if implausible_fraction > MAX_IMPLAUSIBLE_FRACTION_BEFORE_RAISE:
        offending = con.execute(
            "SELECT mmsi, imo, build_year FROM _gate_pop "
            f"WHERE build_year > {window_end_year} ORDER BY build_year DESC LIMIT 10"
        ).fetchall()
        raise ValueError(
            f"G3 plausibility violated systemically: {n_implausible}/{n_covered} "
            f"({implausible_fraction:.2%}) population row(s) have a build_year after the "
            f"window's own end year ({window_end_year}), above the "
            f"{MAX_IMPLAUSIBLE_FRACTION_BEFORE_RAISE:.0%} rate an isolated source-data error "
            "would explain -- this looks like a join bug (wrong IMO matched) or a reconciliation "
            f"bug in ingest.wikidata_ships, not noise to report around. Examples (mmsi, imo, "
            f"build_year): {offending}"
        )
    g3_pass = n_implausible == 0
    if n_implausible:
        logger.warning(
            "G3 plausibility: %d/%d covered row(s) have a build_year after window_end's year "
            "(%d) -- below the systemic-bug threshold, treated as isolated source-data noise, "
            "but G3 fails and forces NO-GO regardless of G1/G2. See summary for the offending "
            "row(s).",
            n_implausible,
            n_covered,
            window_end_year,
        )

    overall_verdict = "GO" if g1_pass and g2_pass and g3_pass else "NO-GO"
    return GateResult(
        overall_verdict=overall_verdict,
        n_pop=n_pop,
        n_covered=n_covered,
        g1_pass=g1_pass,
        g1_coverage=g1_coverage,
        g2_pass=g2_pass,
        g2_global=g2_global,
        g2_scoped=g2_scoped,
        g2_auc=auc,
        g2_auc_ci_low=auc_ci_low,
        g2_auc_ci_high=auc_ci_high,
        g3_pass=g3_pass,
        g3_n_implausible=n_implausible,
    )


def _write_summary(
    summary_path: Path,
    result: GateResult,
    age_by_ship_type: list[tuple],
    spot_checks: list[tuple],
    implausible_rows: list[tuple],
) -> None:
    lines = [
        "model.build_year_gate -- acceptance gate for P4-1's R3 age term",
        "=" * 66,
        (
            f"Population: {result.n_pop} vessel-month rows, {result.n_covered} with a build_year "
            f"({result.g1_coverage:.4f} coverage)"
        ),
        "",
        (
            f"G1 coverage >= {MIN_COVERAGE:.0%}: {'PASS' if result.g1_pass else 'FAIL'} "
            f"({result.g1_coverage:.4f})"
        ),
        (
            f"G2 differential coverage: {'PASS' if result.g2_pass else 'FAIL'} "
            "(both scopes below must pass)"
        ),
        (
            f"  whole population: {'PASS' if result.g2_global.passed else 'FAIL'} -- "
            f"coverage_pos={result.g2_global.coverage_pos:.4f} "
            f"coverage_neg={result.g2_global.coverage_neg:.4f} "
            f"gap={result.g2_global.coverage_gap_pp:.2f}pp fisher_p={result.g2_global.fisher_p:.4g}"
        ),
        (
            f"  tanker+FOC subpopulation (n_pos={result.g2_scoped.n_pos} "
            f"n_neg={result.g2_scoped.n_neg}): {'PASS' if result.g2_scoped.passed else 'FAIL'} -- "
            f"coverage_pos={result.g2_scoped.coverage_pos:.4f} "
            f"coverage_neg={result.g2_scoped.coverage_neg:.4f} "
            f"gap={result.g2_scoped.coverage_gap_pp:.2f}pp fisher_p={result.g2_scoped.fisher_p:.4g}"
        ),
        (
            f"  has_build_year AUC vs label (whole population, reported for context only -- "
            "does not independently drive the verdict, see module docstring): "
            f"{result.g2_auc:.3f} [{result.g2_auc_ci_low:.3f}, {result.g2_auc_ci_high:.3f}]"
            if result.g2_auc is not None
            else "  has_build_year AUC vs label: n/a (one class empty)"
        ),
        (
            f"G3 plausibility: {'PASS' if result.g3_pass else 'FAIL'} -- "
            f"{result.g3_n_implausible} covered row(s) with a build_year after window_end's year"
        ),
        "",
        f"OVERALL VERDICT: {result.overall_verdict}",
        "",
        "-- Age distribution by ship_type (vessels with a known build_year) --",
    ]
    for ship_type, n, mean_age, median_age in age_by_ship_type:
        lines.append(f"  {ship_type or '(null)':<20}{n:>6} rows  mean_age={mean_age:.1f}  median_age={median_age:.1f}")
    lines.append("")
    if result.g3_n_implausible:
        lines.append("-- G3 offending rows (mmsi, imo, build_year), needs manual investigation --")
        for mmsi, imo, build_year in implausible_rows:
            lines.append(f"  mmsi={mmsi} imo={imo} build_year={build_year}")
        lines.append("")
    lines.append("-- Spot-check sample (mmsi, imo, build_year) for manual verification --")
    for mmsi, imo, build_year in spot_checks:
        lines.append(f"  mmsi={mmsi} imo={imo} build_year={build_year}")

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def build_gate_check(
    panel_path: Path = PANEL_PATH,
    build_year_path: Path = BUILD_YEAR_PATH,
    out_path: Path = OUT_PATH,
    summary_path: Path = SUMMARY_PATH,
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
    force: bool = False,
) -> Path:
    """Run the R3 acceptance gate and write out_path + summary_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Raises
    FileNotFoundError if panel_path or build_year_path doesn't exist (run features.panel.build_panel
    / ingest.wikidata_ships.build_ship_build_year first), ValueError if the panel spans more than
    one window (see model.evaluation), or ValueError if G3's implausible-build-year rate looks
    systemic rather than isolated source noise (see module docstring and
    MAX_IMPLAUSIBLE_FRACTION_BEFORE_RAISE) -- that one is a real bug alarm, not a normal gate
    failure.
    """
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    if not build_year_path.exists():
        raise FileNotFoundError(
            f"No build-year source at {build_year_path}; run "
            "ingest.wikidata_ships.build_ship_build_year first"
        )

    con = duckdb.connect()
    try:
        window_start, window_end = load_population(con, panel_path)
        result = _run_gates(con, build_year_path, window_end.year, n_bootstrap, seed)

        age_by_ship_type = con.execute(
            "SELECT ship_type, count(*), "
            f"avg({window_end.year} - build_year), median({window_end.year} - build_year) "
            "FROM _gate_pop WHERE has_build_year GROUP BY ship_type ORDER BY count(*) DESC"
        ).fetchall()
        spot_checks = con.execute(
            "SELECT mmsi, imo, build_year FROM _gate_pop WHERE has_build_year "
            f"ORDER BY mmsi LIMIT {N_SPOT_CHECK_ROWS}"
        ).fetchall()
        implausible_rows = (
            con.execute(
                "SELECT mmsi, imo, build_year FROM _gate_pop "
                f"WHERE build_year > {window_end.year} ORDER BY build_year DESC"
            ).fetchall()
            if result.g3_n_implausible
            else []
        )
        _write_summary(summary_path, result, age_by_ship_type, spot_checks, implausible_rows)

        built_at = datetime.now(timezone.utc)
        built_at_sql = f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
        sha = git_sha()

        # A typed CREATE TABLE + executemany, not literals embedded in a SELECT: DuckDB's numeric
        # literal inference picks DECIMAL for a Python float's repr (confirmed real:
        # DECIMAL(17,16) instead of DOUBLE), not the IEEE-754 DOUBLE every other float column in
        # this project uses (model.baseline, model.discriminative_check both already use this
        # executemany pattern for exactly this reason).
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _result ("
            "overall_verdict VARCHAR, n_pop BIGINT, n_covered BIGINT, "
            "g1_pass BOOLEAN, g1_coverage DOUBLE, g2_pass BOOLEAN, "
            "g2_global_pass BOOLEAN, g2_global_fisher_p DOUBLE, g2_global_coverage_pos DOUBLE, "
            "g2_global_coverage_neg DOUBLE, g2_global_coverage_gap_pp DOUBLE, "
            "g2_scoped_pass BOOLEAN, g2_scoped_fisher_p DOUBLE, g2_scoped_coverage_pos DOUBLE, "
            "g2_scoped_coverage_neg DOUBLE, g2_scoped_coverage_gap_pp DOUBLE, "
            "g2_scoped_n_pos BIGINT, g2_scoped_n_neg BIGINT, "
            "g2_auc DOUBLE, g2_auc_ci_low DOUBLE, g2_auc_ci_high DOUBLE, "
            "g3_pass BOOLEAN, g3_n_implausible BIGINT"
            ")"
        )
        con.executemany(
            "INSERT INTO _result VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    result.overall_verdict,
                    result.n_pop,
                    result.n_covered,
                    result.g1_pass,
                    result.g1_coverage,
                    result.g2_pass,
                    result.g2_global.passed,
                    result.g2_global.fisher_p,
                    result.g2_global.coverage_pos,
                    result.g2_global.coverage_neg,
                    result.g2_global.coverage_gap_pp,
                    result.g2_scoped.passed,
                    result.g2_scoped.fisher_p,
                    result.g2_scoped.coverage_pos,
                    result.g2_scoped.coverage_neg,
                    result.g2_scoped.coverage_gap_pp,
                    result.g2_scoped.n_pos,
                    result.g2_scoped.n_neg,
                    result.g2_auc,
                    result.g2_auc_ci_low,
                    result.g2_auc_ci_high,
                    result.g3_pass,
                    result.g3_n_implausible,
                )
            ],
        )
        atomic_write_parquet(
            con,
            "SELECT *, "
            f"DATE '{window_start.isoformat()}' AS window_start, "
            f"DATE '{window_end.isoformat()}' AS window_end, "
            f"{built_at_sql} AS built_at, '{sha}' AS git_sha "
            "FROM _result",
            out_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="R3 acceptance gate: checks ingest.wikidata_ships's build-year source for "
        "coverage and label contamination before model.baseline may trust it."
    )
    parser.add_argument("--panel-path", default=str(PANEL_PATH), help="Path to the vessel-month panel")
    parser.add_argument(
        "--build-year-path", default=str(BUILD_YEAR_PATH), help="Path to the reconciled build-year table"
    )
    parser.add_argument("--out-path", default=str(OUT_PATH), help="Output path for the gate verdict")
    parser.add_argument(
        "--summary-path", default=str(SUMMARY_PATH), help="Output path for the readable summary"
    )
    parser.add_argument(
        "--n-bootstrap", type=int, default=N_BOOTSTRAP, help="Number of bootstrap resamples"
    )
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED, help="Bootstrap RNG seed")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_gate_check(
        panel_path=Path(args.panel_path),
        build_year_path=Path(args.build_year_path),
        out_path=Path(args.out_path),
        summary_path=Path(args.summary_path),
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
        force=args.force,
    )


if __name__ == "__main__":
    main()
