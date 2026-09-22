"""P4-0: the discriminative check -- a go/no-go gate that must run, and pass, before any Phase 4
model is trained. See ``docs/PLAN_P4-0_P3-4.md`` (Part B) and ``docs/DECISIONS.md``'s 2026-09-22
entry for the full background.

**Why this module exists.** An ad-hoc check run 2026-09-22 directly against the real panel found
that raw detector counts encode how much a vessel operates inside Danish AIS coverage, not evasion
-- among tankers with a valid imo, vessels sanctioned AFTER this window's end showed LESS raw
signal than never-sanctioned ones on every detector except ``n_draught_change_unexplained``, with
near-identical median message counts, ruling out raw-message-volume as the explanation. This
module formalizes that check: it tests whether ``features.panel``'s P4-0 exposure/rate columns
(``n_observed_hours``/``n_observed_days`` and the ``rate_*`` columns, see that module's docstring)
rescue a real signal once presence is normalized out, with matched controls and reported
uncertainty rather than bare means.

**Population.** Every panel row with ``imo IS NOT NULL`` (the panel's own hard modelling-
population restriction, see ``features.panel``'s docstring) AND
``NOT label_is_sanctioned_as_of_window_end`` -- vessels already under sanctions before/during this
window are a different analytical question (see ``features.panel``'s "Label columns" section) and
are excluded from BOTH classes here, not folded into either one. Within what remains, the two
classes are exhaustive and disjoint by construction (``label_is_sanctioned_ever`` = ``as_of`` OR
``after``, mutually exclusive, and ``as_of`` rows are already excluded):

* **Positive**: ``label_is_sanctioned_after_window_end`` -- the forward-looking population.
* **Negative**: everything else, i.e. never sanctioned at all.

**Matched controls.** A vessel-month's own detector counts are confounded by how much of its
voyage this project's Danish/Baltic AIS coverage actually saw -- comparing a resident Danish ferry
against a transiting tanker is comparing exposure, not risk. Every row is assigned to a
``(ship_type, exposure_bucket)`` group, where ``exposure_bucket`` is an ``ntile`` quantile bucket
of ``n_observed_days`` (:data:`N_EXPOSURE_BUCKETS` buckets, an unvalidated default, same posture as
every other threshold in this project). A group counts as "matched" only if it contains at least
one positive AND at least one negative row; groups with only one class (most non-Tanker ship types
here have zero positives -- see ``docs/STATE.md``) contribute no matched comparison and are
dropped from the matched scope, not padded or imputed. Both scopes are reported side by side in the
output (``matched`` column) precisely so a reader can see whether matching changes the story, not
just the matched result alone.

**Effect size and uncertainty, per feature, per scope.** With only ~147 real positives, reporting
a bare mean difference invites overinterpreting noise. For every feature this module reports:

* raw n/mean/median/%-nonzero for each class (rows with a NULL feature value -- e.g. a
  ``rate_*_per_voyage`` column when ``voyage_count`` was 0 this month -- are excluded from that
  feature's own comparison, never imputed to 0; NULL means "rate undefined", per
  ``features.panel``'s own docstring).
* **AUC** (feature value as a univariate score for the positive class, via
  ``sklearn.metrics.roc_auc_score`` -- equivalent to the Mann-Whitney U statistic normalized by
  ``n_pos * n_neg``) as the effect size: 0.5 is no separation, >0.5 means higher feature values
  associate with the positive class (the direction every detector is nominally built to expect),
  <0.5 means the opposite.
* a **95% bootstrap confidence interval** on that AUC (:data:`N_BOOTSTRAP` resamples, stratified
  by class so every resample keeps the same class sizes, :data:`BOOTSTRAP_SEED` fixed for
  reproducibility) -- the actual decision-relevant number, not the point estimate alone.
* a **Mann-Whitney U p-value** (two-sided) as a complementary, non-bootstrap significance check.
* an explicit **verdict**: ``discriminates_as_expected`` (CI entirely above 0.5),
  ``discriminates_opposite`` (CI entirely below 0.5), ``no_discrimination`` (CI straddles 0.5), or
  ``insufficient_data`` (fewer than :data:`MIN_GROUP_SIZE_FOR_VERDICT` rows in either class after
  NULL exclusion -- a real, expected outcome for many matched-scope non-Tanker features here, not
  an error).

**The decision this module exists to make.** Per the approved plan: if no normalized (``rate_*``)
feature discriminates in the expected direction under the matched scope, with a CI excluding
no-effect, the verdict is NO-GO -- stop and rethink detectors or geographic coverage before P3-4 or
any Phase 4 model. This module computes that decision explicitly (logged, and in the summary file)
rather than leaving it for a human to infer from a table of numbers.

**Output.** One row per (feature, scope) at ``data/processed/discriminative_check.parquet`` (28
features x 2 scopes = 56 rows for the current single-window panel), plus a plain-text summary at
``outputs/discriminative_check_summary.txt``. Every row carries the panel's own
``window_start``/``window_end`` (read back from the panel itself, not re-specified -- this module
raises if the panel does not have exactly one distinct window, since it has only been validated
against a single-window panel), plus ``built_at``/``git_sha`` provenance, same convention as every
other module in this project.

One shared DuckDB connection is opened once and reused across the whole build, mirroring every
other module in this project.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from scipy.stats import mannwhitneyu
from sklearn.metrics import roc_auc_score

from features.panel import PANEL_PATH, RATE_BASE_COLUMNS

logger = logging.getLogger(__name__)

OUT_PATH = Path("data/processed/discriminative_check.parquet")
SUMMARY_PATH = Path("outputs/discriminative_check_summary.txt")

# Unvalidated defaults -- see module docstring.
N_EXPOSURE_BUCKETS = 5
N_BOOTSTRAP = 2000
BOOTSTRAP_SEED = 0
CI_LOW_PCT = 2.5
CI_HIGH_PCT = 97.5
MIN_GROUP_SIZE_FOR_VERDICT = 5


def _git_sha() -> str:
    """Short git commit SHA of the working tree, or "unknown" if it can't be determined.

    Provenance metadata only, never correctness-critical -- every module in this project owns its
    own copy of this helper rather than importing a shared one.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _feature_columns() -> list[str]:
    """The 28 features this check evaluates: each of features.panel.RATE_BASE_COLUMNS's 7 raw
    count columns, plus its 3 rate variants -- reusing that list rather than a separate copy, so
    this module can never silently drift from the columns features.panel actually produces.
    """
    columns: list[str] = []
    for base in RATE_BASE_COLUMNS:
        short = base.removeprefix("n_")
        columns.append(base)
        columns.append(f"rate_{short}_per_voyage")
        columns.append(f"rate_{short}_per_observed_day")
        columns.append(f"rate_{short}_per_1000_messages")
    return columns


@dataclass(frozen=True)
class FeatureResult:
    """One row of the output: a single feature's positive-vs-negative comparison, in one scope
    (matched or unmatched). See module docstring for what each field means.
    """

    feature: str
    matched: bool
    n_pos: int
    n_neg: int
    mean_pos: float | None
    mean_neg: float | None
    median_pos: float | None
    median_neg: float | None
    pct_nonzero_pos: float | None
    pct_nonzero_neg: float | None
    auc: float | None
    auc_ci_low: float | None
    auc_ci_high: float | None
    mannwhitney_p: float | None
    verdict: str


def _bootstrap_auc_ci(
    pos: np.ndarray, neg: np.ndarray, n_bootstrap: int, rng: np.random.Generator
) -> tuple[float, float]:
    """Stratified bootstrap 95% CI on the univariate AUC: each resample redraws n_pos positives
    and n_neg negatives independently (with replacement), so every resample keeps the real class
    balance -- an unstratified resample of the pooled data could occasionally draw all-one-class
    and silently bias the interval.
    """
    n_pos, n_neg = len(pos), len(neg)
    labels = np.concatenate([np.ones(n_pos), np.zeros(n_neg)])
    aucs = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        pos_sample = rng.choice(pos, size=n_pos, replace=True)
        neg_sample = rng.choice(neg, size=n_neg, replace=True)
        values = np.concatenate([pos_sample, neg_sample])
        try:
            aucs[i] = roc_auc_score(labels, values)
        except ValueError:
            # Every value identical across the whole resample -- roc_auc_score has nothing to
            # rank. Rare at real sample sizes; dropped rather than treated as a fabricated 0.5.
            aucs[i] = np.nan
    aucs = aucs[~np.isnan(aucs)]
    if len(aucs) == 0:
        return (float("nan"), float("nan"))
    return (
        float(np.percentile(aucs, CI_LOW_PCT)),
        float(np.percentile(aucs, CI_HIGH_PCT)),
    )


def _evaluate_feature(
    feature: str,
    matched: bool,
    pos: np.ndarray,
    neg: np.ndarray,
    rng: np.random.Generator,
    n_bootstrap: int,
) -> FeatureResult:
    """Compare one feature's positive vs negative distribution in one scope. See module docstring
    for the verdict rule.
    """
    n_pos, n_neg = len(pos), len(neg)
    mean_pos = float(np.mean(pos)) if n_pos else None
    mean_neg = float(np.mean(neg)) if n_neg else None
    median_pos = float(np.median(pos)) if n_pos else None
    median_neg = float(np.median(neg)) if n_neg else None
    pct_nonzero_pos = float(np.mean(pos != 0)) if n_pos else None
    pct_nonzero_neg = float(np.mean(neg != 0)) if n_neg else None

    if n_pos < MIN_GROUP_SIZE_FOR_VERDICT or n_neg < MIN_GROUP_SIZE_FOR_VERDICT:
        return FeatureResult(
            feature=feature,
            matched=matched,
            n_pos=n_pos,
            n_neg=n_neg,
            mean_pos=mean_pos,
            mean_neg=mean_neg,
            median_pos=median_pos,
            median_neg=median_neg,
            pct_nonzero_pos=pct_nonzero_pos,
            pct_nonzero_neg=pct_nonzero_neg,
            auc=None,
            auc_ci_low=None,
            auc_ci_high=None,
            mannwhitney_p=None,
            verdict="insufficient_data",
        )

    labels = np.concatenate([np.ones(n_pos), np.zeros(n_neg)])
    values = np.concatenate([pos, neg])
    auc = float(roc_auc_score(labels, values))
    ci_low, ci_high = _bootstrap_auc_ci(pos, neg, n_bootstrap, rng)
    _, p_value = mannwhitneyu(pos, neg, alternative="two-sided")

    if ci_low > 0.5:
        verdict = "discriminates_as_expected"
    elif ci_high < 0.5:
        verdict = "discriminates_opposite"
    else:
        verdict = "no_discrimination"

    return FeatureResult(
        feature=feature,
        matched=matched,
        n_pos=n_pos,
        n_neg=n_neg,
        mean_pos=mean_pos,
        mean_neg=mean_neg,
        median_pos=median_pos,
        median_neg=median_neg,
        pct_nonzero_pos=pct_nonzero_pos,
        pct_nonzero_neg=pct_nonzero_neg,
        auc=auc,
        auc_ci_low=ci_low,
        auc_ci_high=ci_high,
        mannwhitney_p=float(p_value),
        verdict=verdict,
    )


def _fetch_class_arrays(
    con: duckdb.DuckDBPyConnection, table_sql: str, feature: str
) -> tuple[np.ndarray, np.ndarray]:
    """(positive values, negative values) for `feature` from `table_sql`, NULLs excluded."""
    rows = con.execute(
        f"SELECT label_is_sanctioned_after_window_end, {feature} "
        f"FROM {table_sql} WHERE {feature} IS NOT NULL"
    ).fetchall()
    pos = np.array([value for is_pos, value in rows if is_pos], dtype=float)
    neg = np.array([value for is_pos, value in rows if not is_pos], dtype=float)
    return pos, neg


_MATCHED_TABLE_SQL = (
    "(SELECT p.* FROM _population p JOIN _matched_groups mg "
    "ON mg.match_ship_type = p.match_ship_type AND mg.exposure_bucket = p.exposure_bucket)"
)


def _write_summary(
    summary_path: Path,
    results: list[FeatureResult],
    n_pop: int,
    n_pos_total: int,
    n_neg_total: int,
    n_matched_groups: int,
    n_matched_rows: int,
    overall_decision: str,
    go_features: list[str],
) -> None:
    lines = [
        "P4-0 discriminative check -- go/no-go gate for Phase 4 modelling",
        "=" * 66,
        (
            f"Population: {n_pop} vessel-month rows (imo IS NOT NULL, excluding "
            "already-sanctioned-as-of-window_end)"
        ),
        f"  positive (sanctioned after window_end): {n_pos_total}",
        f"  negative (never sanctioned): {n_neg_total}",
        (
            f"Matched controls: {n_matched_groups} (ship_type, exposure_bucket) group(s) contain "
            f"both classes; {n_matched_rows} of {n_pop} population rows fall in one"
        ),
        "",
        f"DECISION: {overall_decision}",
        (
            f"  Matched, normalized (rate_*) feature(s) discriminating as expected: "
            f"{', '.join(go_features)}"
            if go_features
            else "  No matched, normalized (rate_*) feature discriminates as expected."
        ),
        "",
    ]
    header = (
        f"{'feature':<42}{'n_pos':>7}{'n_neg':>7}{'auc':>8}{'ci_low':>8}{'ci_high':>8}"
        f"  {'verdict'}"
    )
    for matched in (False, True):
        lines.append(f"-- {'Matched' if matched else 'Unmatched'} scope --")
        lines.append(header)
        for r in results:
            if r.matched != matched:
                continue
            auc_s = f"{r.auc:.3f}" if r.auc is not None else "n/a"
            lo_s = f"{r.auc_ci_low:.3f}" if r.auc_ci_low is not None else "n/a"
            hi_s = f"{r.auc_ci_high:.3f}" if r.auc_ci_high is not None else "n/a"
            lines.append(
                f"{r.feature:<42}{r.n_pos:>7}{r.n_neg:>7}{auc_s:>8}{lo_s:>8}{hi_s:>8}"
                f"  {r.verdict}"
            )
        lines.append("")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def build_discriminative_check(
    panel_path: Path = PANEL_PATH,
    out_path: Path = OUT_PATH,
    summary_path: Path = SUMMARY_PATH,
    n_exposure_buckets: int = N_EXPOSURE_BUCKETS,
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
    force: bool = False,
) -> Path:
    """Run the P4-0 discriminative check against `panel_path` and write out_path + summary_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError if panel_path doesn't exist (run
    features.panel.build_panel first) and ValueError if the panel spans more than one distinct
    (window_start, window_end) pair -- this module has only been validated against a single-window
    panel, see module docstring.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    if not panel_path.exists():
        raise FileNotFoundError(f"No panel at {panel_path}; run features.panel.build_panel first")

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _population AS "
            "SELECT *, COALESCE(ship_type, '__unknown__') AS match_ship_type, "
            "ntile(?) OVER (ORDER BY n_observed_days) AS exposure_bucket "
            f"FROM read_parquet('{panel_path.as_posix()}') "
            "WHERE imo IS NOT NULL AND NOT label_is_sanctioned_as_of_window_end",
            [n_exposure_buckets],
        )

        window_rows = con.execute(
            "SELECT DISTINCT window_start, window_end FROM _population"
        ).fetchall()
        if len(window_rows) != 1:
            raise ValueError(
                f"Expected exactly one distinct (window_start, window_end) in {panel_path}, "
                f"found {len(window_rows)} -- this module has only been validated against a "
                "single-window panel; a multi-window panel needs this check re-scoped, not "
                "blindly run over pooled windows."
            )
        window_start, window_end = window_rows[0]

        (n_pop, n_pos_total, n_neg_total) = con.execute(
            "SELECT count(*), "
            "count(*) FILTER (WHERE label_is_sanctioned_after_window_end), "
            "count(*) FILTER (WHERE NOT label_is_sanctioned_after_window_end) "
            "FROM _population"
        ).fetchone()
        logger.info(
            "Population: %d vessel-month row(s) (imo IS NOT NULL, excluding already-sanctioned-"
            "as-of-window_end), %d positive (sanctioned after window_end), %d negative "
            "(never sanctioned)",
            n_pop,
            n_pos_total,
            n_neg_total,
        )

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _matched_groups AS "
            "SELECT match_ship_type, exposure_bucket FROM _population "
            "GROUP BY match_ship_type, exposure_bucket "
            "HAVING count(*) FILTER (WHERE label_is_sanctioned_after_window_end) > 0 "
            "AND count(*) FILTER (WHERE NOT label_is_sanctioned_after_window_end) > 0"
        )
        (n_matched_groups,) = con.execute("SELECT count(*) FROM _matched_groups").fetchone()
        (n_matched_rows,) = con.execute(f"SELECT count(*) FROM {_MATCHED_TABLE_SQL}").fetchone()
        logger.info(
            "Matched controls: %d (ship_type, exposure_bucket) group(s) contain both classes; "
            "%d of %d population rows fall in one",
            n_matched_groups,
            n_matched_rows,
            n_pop,
        )

        rng = np.random.default_rng(seed)
        results: list[FeatureResult] = []
        for feature in _feature_columns():
            for matched, table_sql in ((False, "_population"), (True, _MATCHED_TABLE_SQL)):
                pos, neg = _fetch_class_arrays(con, table_sql, feature)
                results.append(_evaluate_feature(feature, matched, pos, neg, rng, n_bootstrap))

        go_features = [
            r.feature
            for r in results
            if r.matched and r.feature.startswith("rate_") and r.verdict == "discriminates_as_expected"
        ]
        overall_decision = "GO" if go_features else "NO-GO"
        logger.info(
            "P4-0 DECISION: %s -- matched, normalized (rate_*) feature(s) discriminating as "
            "expected with a bootstrap CI excluding no-effect: %s",
            overall_decision,
            ", ".join(go_features) if go_features else "none",
        )

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _results (feature VARCHAR, matched BOOLEAN, "
            "n_pos BIGINT, n_neg BIGINT, mean_pos DOUBLE, mean_neg DOUBLE, "
            "median_pos DOUBLE, median_neg DOUBLE, pct_nonzero_pos DOUBLE, "
            "pct_nonzero_neg DOUBLE, auc DOUBLE, auc_ci_low DOUBLE, auc_ci_high DOUBLE, "
            "mannwhitney_p DOUBLE, verdict VARCHAR)"
        )
        con.executemany(
            "INSERT INTO _results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.feature,
                    r.matched,
                    r.n_pos,
                    r.n_neg,
                    r.mean_pos,
                    r.mean_neg,
                    r.median_pos,
                    r.median_neg,
                    r.pct_nonzero_pos,
                    r.pct_nonzero_neg,
                    r.auc,
                    r.auc_ci_low,
                    r.auc_ci_high,
                    r.mannwhitney_p,
                    r.verdict,
                )
                for r in results
            ],
        )

        _write_summary(
            summary_path,
            results,
            n_pop,
            n_pos_total,
            n_neg_total,
            n_matched_groups,
            n_matched_rows,
            overall_decision,
            go_features,
        )

        built_at = datetime.now(timezone.utc)
        built_at_sql = f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
        git_sha = _git_sha()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{window_start.isoformat()}' AS window_start, "
            f"DATE '{window_end.isoformat()}' AS window_end, "
            f"{n_exposure_buckets} AS n_exposure_buckets, {n_bootstrap} AS n_bootstrap, "
            f"{built_at_sql} AS built_at, '{git_sha}' AS git_sha "
            "FROM _results ORDER BY matched, feature) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P4-0: discriminative check with exposure-normalized features -- the go/no-go "
        "gate before any Phase 4 modelling."
    )
    parser.add_argument("--panel-path", default=str(PANEL_PATH), help="Path to the vessel-month panel")
    parser.add_argument("--out-path", default=str(OUT_PATH), help="Output path for the result table")
    parser.add_argument(
        "--summary-path", default=str(SUMMARY_PATH), help="Output path for the readable summary"
    )
    parser.add_argument(
        "--n-exposure-buckets",
        type=int,
        default=N_EXPOSURE_BUCKETS,
        help="Number of n_observed_days quantile buckets for matching",
    )
    parser.add_argument(
        "--n-bootstrap", type=int, default=N_BOOTSTRAP, help="Number of bootstrap resamples per feature"
    )
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED, help="Bootstrap RNG seed")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_discriminative_check(
        panel_path=Path(args.panel_path),
        out_path=Path(args.out_path),
        summary_path=Path(args.summary_path),
        n_exposure_buckets=args.n_exposure_buckets,
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
        force=args.force,
    )


if __name__ == "__main__":
    main()
