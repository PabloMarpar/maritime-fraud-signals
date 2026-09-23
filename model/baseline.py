"""P4-1: the naive baseline every later Phase 4 model (P4-2 Isolation Forest, P4-3 LightGBM) must
beat to justify its own existence. See ``docs/DECISIONS.md``'s P4-1 entry for the full background
on why the baseline ended up shaped this way.

**Why three nested rules, not one.** ``CLAUDE.md`` originally defined the baseline literally as
"tanker over 15 years old under a flag of convenience". No build-year data exists in any source
this project can legally use in bulk (GFW's vessel registry has no build-year field; Equasis and
IMO GISIS both prohibit bulk/automated extraction in their terms). A Wikidata-derived source
(``ingest.wikidata_ships`` / ``data/reference/ship_build_year.parquet``) is CC0 and viable, but
crowd-sourced coverage could itself correlate with the sanctions label -- a vessel might have a
Wikidata entry *because* it became notable after being sanctioned, which would make build-year
*availability* predict the label rather than build-year *value*. ``model.build_year_gate`` checks
for exactly that contamination before this module's age term (R3) may be trusted. Reporting three
nested rules, rather than only the final one, keeps the comparison honest regardless of that gate's
outcome, and gives P4-2/P4-3 a clearly-named target at every level:

* **R1_tanker**: ``ship_type = 'Tanker'``.
* **R2_tanker_foc**: R1 AND the flag is an ITF-listed flag of convenience (:func:`process.foc.is_foc`
  on ``flag_country``).
* **R3_tanker_foc_age**: R2 AND the vessel is more than :data:`AGE_THRESHOLD_YEARS` years old as of
  ``window_end``. Evaluated only if :data:`BUILD_YEAR_PATH` exists AND
  ``model.build_year_gate``'s own verdict (:data:`BUILD_YEAR_GATE_PATH`) says GO; otherwise this
  rule's row is written with ``verdict="blocked_by_gate"`` and every metric NULL, never silently
  skipped or approximated.

**Population.** Exactly ``model.evaluation``'s P4-0 population (``imo IS NOT NULL AND NOT
label_is_sanctioned_as_of_window_end``) -- the same one ``model.discriminative_check`` uses, so a
later "beats the baseline" claim compares like with like. Real numbers (2024-06 window, measured
2026-09-23): 4,864 rows, 147 positive (3.02% prevalence). R1 alone: 893 flagged, 135 true positives,
15.1% precision, 5.0x lift. R2: 572 flagged, 125 true positives, 21.9% precision, 7.2x lift -- a
demanding baseline even before any age term, which is why it ships on its own rather than waiting
on the Wikidata gate.

**A vessel with unknown build year never drops out of the population.** Doing so would shrink R3's
population relative to R1/R2/P4-0's, breaking comparability. Instead an unknown build year simply
fails R3 (``r3 = false``, not NULL) for that row, once the rule is evaluated at all -- see
:func:`_load_rule_columns`. Only a fully gate-blocked R3 writes NULL, for every row at once. Note
this makes R3 decompose as ``R2 AND has_build_year AND old`` -- the ``has_build_year`` term is
exactly the Wikidata-notability channel ``model.build_year_gate``'s G2 exists to detect, so R3
inherits some of that risk even once GO is reached, not just before it; not fixed here since R3
never activates under the real NO-GO verdict, but a future GO should re-read this note.

**Ranking and precision@k -- read this before comparing precision@20 across modules.**
A single binary rule has no internal ranking; this module's sort key for "top 20" is (rule value
DESC, mmsi ASC). **The MMSI's first three digits are the MID, i.e. the flag state** (see
``process.mid``) -- sorting by MMSI ascending is NOT a neutral tie-break, it sorts by flag. On the
real population R2's top-20-by-mmsi is 100% Cyprus (the lowest-numbered flag-of-convenience MID),
which contributes 0 of R2's 125 true positives; R1's top 20 spans two low-MID flags with the same
result. **precision@20 = 0.000 for both R1 and R2 is therefore a deterministic property of the sort
key, not a random draw that "happened to" miss** -- it will read 0.000 on every future run of this
same window unless a low-MID-flag vessel is designated. ``n_tied_at_cutoff`` documents how many
rows tie the boundary value (typically ``n_flagged`` itself for a binary rule), which is evidence
the top-20 slice is arbitrary among the flagged rows, not proof of what drove the result -- see
:func:`model.evaluation.n_tied_at_cutoff`. **The number to compare P4-2/P4-3 against is precision
at n_flagged (i.e. plain ``precision``, already reported per rule), or precision at a matched alert
budget of the same size -- never precision@20 from this module.**
``baseline_scores.parquet`` additionally carries a combined ``tier`` column (R3 -> 3, R2 -> 2,
R1 -> 1, none -> 0, then mmsi ascending) for P4-2/P4-3 to join against as a ranking target, without
re-deriving the rule logic; the same MID-ordering caveat applies to any P4-2/P4-3 comparison that
breaks ties by mmsi.

**Output.** ``data/processed/baseline.parquet`` (one row per rule),
``data/processed/baseline_scores.parquet`` (one row per vessel-month: ``mmsi, year_month, r1, r2,
r3, tier``), and a plain-text summary at ``outputs/baseline_summary.txt``. Same
``built_at``/``git_sha`` provenance convention as every other module, written via
``process.partitions.atomic_write_parquet``.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from scipy.stats import fisher_exact
from sklearn.metrics import roc_auc_score

from features.panel import PANEL_PATH
from model.evaluation import (
    add_tanker_foc_columns,
    bootstrap_auc_ci,
    load_population,
    n_tied_at_cutoff,
    precision_at_k,
)
from process.partitions import atomic_write_parquet, git_sha

logger = logging.getLogger(__name__)

OUT_PATH = Path("data/processed/baseline.parquet")
SCORES_PATH = Path("data/processed/baseline_scores.parquet")
SUMMARY_PATH = Path("outputs/baseline_summary.txt")

BUILD_YEAR_PATH = Path("data/reference/ship_build_year.parquet")
BUILD_YEAR_GATE_PATH = Path("data/processed/build_year_gate.parquet")

AGE_THRESHOLD_YEARS = 15
N_BOOTSTRAP = 2000
BOOTSTRAP_SEED = 0
PRECISION_AT_K = 20

RULES: tuple[tuple[str, str], ...] = (
    ("R1_tanker", "ship_type = 'Tanker'"),
    ("R2_tanker_foc", "R1 AND flag is an ITF-listed flag of convenience"),
    ("R3_tanker_foc_age", f"R2 AND vessel age > {AGE_THRESHOLD_YEARS} years as of window_end"),
)


@dataclass(frozen=True)
class RuleResult:
    """One row of baseline.parquet: one rule's precision/recall/lift/AUC against the population."""

    rule: str
    description: str
    n_flagged: int | None
    tp: int | None
    fp: int | None
    precision: float | None
    recall: float | None
    lift: float | None
    auc: float | None
    auc_ci_low: float | None
    auc_ci_high: float | None
    fisher_p: float | None
    precision_at_20: float | None
    n_ties_at_cut: int | None
    verdict: str
    blocked_reason: str | None


def _resolve_r3_availability(
    build_year_path: Path,
    build_year_gate_path: Path,
    window_start: date,
    window_end: date,
) -> tuple[bool, str | None]:
    """Whether R3 may be evaluated at all, and why not if it can't.

    Fail-closed at every step: no build-year source, no recorded gate verdict, a gate verdict for
    a DIFFERENT window than the one this run is scoring, a gate computed BEFORE the build-year
    source it's supposed to have checked, or a verdict other than GO, all block R3 -- a missing or
    stale check is never treated as a passing one. The window/freshness checks exist because a
    gate verdict is only evidence about the population and snapshot it was actually run against;
    without them a GO computed for one window (or one Wikidata snapshot) would silently authorise
    R3 for a different one the moment `ingest.wikidata_ships`/`features.panel` are re-run.
    """
    if not build_year_path.exists():
        return False, "no build-year data source ingested (run ingest.wikidata_ships first)"
    if not build_year_gate_path.exists():
        return False, (
            "build-year source exists but model.build_year_gate has not been run against it"
        )
    if build_year_gate_path.stat().st_mtime < build_year_path.stat().st_mtime:
        return False, (
            f"{build_year_gate_path} predates {build_year_path} -- the build-year source was "
            "re-fetched after the gate last ran; re-run model.build_year_gate before trusting R3"
        )
    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT overall_verdict, window_start, window_end "
            f"FROM '{build_year_gate_path.as_posix()}' LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    verdict, gate_window_start, gate_window_end = row
    if (gate_window_start, gate_window_end) != (window_start, window_end):
        return False, (
            f"{build_year_gate_path} was computed for window "
            f"{gate_window_start.isoformat()}_{gate_window_end.isoformat()}, not this run's "
            f"{window_start.isoformat()}_{window_end.isoformat()} -- re-run "
            "model.build_year_gate against the current panel before trusting R3"
        )
    if verdict != "GO":
        return False, f"model.build_year_gate verdict is {verdict!r}, not GO"
    return True, None


def _load_rule_columns(
    con: duckdb.DuckDBPyConnection, build_year_path: Path, r3_available: bool
) -> None:
    """Build the final _population table (from the _population_base table
    model.evaluation.load_population created) with r1, r2, r3 BOOLEAN columns added.

    r1/r2 come from model.evaluation.add_tanker_foc_columns, the same definition
    model.build_year_gate uses to scope its own G2 check -- not re-derived here. r3 is NULL for
    every row if not r3_available; otherwise a row with an unknown build year fails r3 (false), it
    is never dropped or left NULL -- see module docstring. Assumes build_year_path (when read)
    carries at most one row per imo -- ingest.wikidata_ships's reconciliation is responsible for
    that invariant, not this function.
    """
    add_tanker_foc_columns(con, source_table="_population_base", dest_table="_population_r1r2")

    if not r3_available:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _population AS "
            "SELECT *, NULL::BOOLEAN AS r3 FROM _population_r1r2"
        )
        return

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _build_year AS "
        f"SELECT imo, build_year FROM read_parquet('{build_year_path.as_posix()}') "
        "WHERE build_year IS NOT NULL"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _population AS "
        "SELECT p.*, "
        "(p.r2 AND COALESCE(EXTRACT(year FROM p.window_end) - byr.build_year > "
        f"{AGE_THRESHOLD_YEARS}, false)) AS r3 "
        "FROM _population_r1r2 p LEFT JOIN _build_year byr ON byr.imo = p.imo"
    )


def _evaluate_rule(
    con: duckdb.DuckDBPyConnection,
    rule: str,
    description: str,
    prevalence: float,
    rng: np.random.Generator,
    n_bootstrap: int,
    blocked_reason: str | None,
) -> RuleResult:
    """Compute one rule's full metric row, or an all-NULL blocked row if the rule column is
    entirely NULL (R3 with an unavailable gate; blocked_reason explains why).
    """
    column = rule.split("_")[0].lower()  # "r1" / "r2" / "r3"
    (n_non_null,) = con.execute(
        f"SELECT count({column}) FROM _population"
    ).fetchone()
    if n_non_null == 0:
        return RuleResult(
            rule=rule,
            description=description,
            n_flagged=None,
            tp=None,
            fp=None,
            precision=None,
            recall=None,
            lift=None,
            auc=None,
            auc_ci_low=None,
            auc_ci_high=None,
            fisher_p=None,
            precision_at_20=None,
            n_ties_at_cut=None,
            verdict="blocked_by_gate",
            blocked_reason=blocked_reason,
        )

    rows = con.execute(
        f"SELECT label_is_sanctioned_after_window_end, {column}, mmsi "
        f"FROM _population ORDER BY {column} DESC, mmsi ASC"
    ).fetchall()
    labels = np.array([bool(r[0]) for r in rows])
    values = np.array([bool(r[1]) for r in rows])

    n_flagged = int(values.sum())
    tp = int(np.sum(values & labels))
    fp = int(np.sum(values & ~labels))
    n_pos_total = int(labels.sum())
    fn = n_pos_total - tp
    tn = (len(rows) - n_flagged) - fn

    precision = tp / n_flagged if n_flagged else None
    recall = tp / n_pos_total if n_pos_total else None
    lift = precision / prevalence if precision is not None and prevalence else None

    auc = float(roc_auc_score(labels, values)) if 0 < n_pos_total < len(labels) else None
    if auc is not None:
        pos_values = values[labels].astype(float)
        neg_values = values[~labels].astype(float)
        ci_low, ci_high = bootstrap_auc_ci(pos_values, neg_values, n_bootstrap, rng)
    else:
        ci_low = ci_high = None

    _, fisher_p = fisher_exact([[tp, fp], [fn, tn]])

    # precision@20 is against the true label within the top-20 ranked rows (rule DESC, mmsi ASC);
    # n_tied_at_cutoff is against the ranking key itself (the rule's own boolean value), showing
    # how many rows share the rank-20 boundary's score -- see module docstring.
    prec_20 = precision_at_k(labels, PRECISION_AT_K)
    ties_20 = n_tied_at_cutoff(values, PRECISION_AT_K)

    return RuleResult(
        rule=rule,
        description=description,
        n_flagged=n_flagged,
        tp=tp,
        fp=fp,
        precision=precision,
        recall=recall,
        lift=lift,
        auc=auc,
        auc_ci_low=ci_low,
        auc_ci_high=ci_high,
        fisher_p=float(fisher_p),
        precision_at_20=prec_20,
        n_ties_at_cut=ties_20,
        verdict="evaluated",
        blocked_reason=None,
    )


def _write_summary(
    summary_path: Path,
    results: list[RuleResult],
    n_pop: int,
    n_pos_total: int,
    prevalence: float,
) -> None:
    lines = [
        "P4-1 naive baseline -- the floor every Phase 4 model must beat",
        "=" * 66,
        f"Population: {n_pop} vessel-month rows, {n_pos_total} positive ({prevalence:.4f} prevalence)",
        "",
    ]
    header = (
        f"{'rule':<20}{'n_flagged':>10}{'tp':>6}{'precision':>10}{'recall':>8}{'lift':>7}"
        f"{'auc':>7}{'p20':>7}  {'verdict'}"
    )
    lines.append(header)
    for r in results:
        if r.verdict == "blocked_by_gate":
            lines.append(f"{r.rule:<20}{'--':>10}  blocked_by_gate: {r.blocked_reason}")
            continue
        prec_s = f"{r.precision:.3f}" if r.precision is not None else "n/a"
        rec_s = f"{r.recall:.3f}" if r.recall is not None else "n/a"
        lift_s = f"{r.lift:.2f}" if r.lift is not None else "n/a"
        auc_s = f"{r.auc:.3f}" if r.auc is not None else "n/a"
        p20_s = f"{r.precision_at_20:.3f}" if r.precision_at_20 is not None else "n/a"
        lines.append(
            f"{r.rule:<20}{r.n_flagged:>10}{r.tp:>6}{prec_s:>10}{rec_s:>8}{lift_s:>7}"
            f"{auc_s:>7}{p20_s:>7}  {r.verdict}"
        )
        lines.append(f"    {r.description}")
        if r.n_ties_at_cut is not None:
            lines.append(
                f"    n_tied_at_cutoff(k=20): {r.n_ties_at_cut} of {r.n_flagged} flagged rows tie "
                "the rank-20 boundary -- p20 here reflects this rule's mmsi tie-break (which "
                "sorts by flag state, see module docstring), not a real top-20; compare "
                "precision (this row's own column) against other models, not p20"
            )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")


def build_baseline(
    panel_path: Path = PANEL_PATH,
    out_path: Path = OUT_PATH,
    scores_path: Path = SCORES_PATH,
    summary_path: Path = SUMMARY_PATH,
    build_year_path: Path = BUILD_YEAR_PATH,
    build_year_gate_path: Path = BUILD_YEAR_GATE_PATH,
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
    force: bool = False,
) -> Path:
    """Run the P4-1 naive baseline against `panel_path` and write out_path + scores_path +
    summary_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError if panel_path doesn't exist and ValueError if the panel
    spans more than one distinct (window_start, window_end) pair -- see model.evaluation.
    """
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    con = duckdb.connect()
    try:
        window_start, window_end = load_population(con, panel_path, table_name="_population_base")

        r3_available, r3_blocked_reason = _resolve_r3_availability(
            build_year_path, build_year_gate_path, window_start, window_end
        )
        if not r3_available:
            logger.info("R3_tanker_foc_age blocked: %s", r3_blocked_reason)
        _load_rule_columns(con, build_year_path, r3_available)

        (n_pop, n_pos_total) = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE label_is_sanctioned_after_window_end) "
            "FROM _population"
        ).fetchone()
        prevalence = n_pos_total / n_pop
        logger.info(
            "Population: %d vessel-month row(s), %d positive (%.4f prevalence)",
            n_pop,
            n_pos_total,
            prevalence,
        )

        rng = np.random.default_rng(seed)
        results = [
            _evaluate_rule(
                con,
                rule,
                description,
                prevalence,
                rng,
                n_bootstrap,
                blocked_reason=r3_blocked_reason if rule == "R3_tanker_foc_age" else None,
            )
            for rule, description in RULES
        ]

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _results (rule VARCHAR, description VARCHAR, "
            "n_flagged BIGINT, tp BIGINT, fp BIGINT, precision DOUBLE, recall DOUBLE, "
            "lift DOUBLE, auc DOUBLE, auc_ci_low DOUBLE, auc_ci_high DOUBLE, fisher_p DOUBLE, "
            "precision_at_20 DOUBLE, n_ties_at_cut BIGINT, verdict VARCHAR, blocked_reason VARCHAR)"
        )
        con.executemany(
            "INSERT INTO _results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.rule,
                    r.description,
                    r.n_flagged,
                    r.tp,
                    r.fp,
                    r.precision,
                    r.recall,
                    r.lift,
                    r.auc,
                    r.auc_ci_low,
                    r.auc_ci_high,
                    r.fisher_p,
                    r.precision_at_20,
                    r.n_ties_at_cut,
                    r.verdict,
                    r.blocked_reason,
                )
                for r in results
            ],
        )

        _write_summary(summary_path, results, n_pop, n_pos_total, prevalence)

        built_at = datetime.now(timezone.utc)
        built_at_sql = f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
        sha = git_sha()

        atomic_write_parquet(
            con,
            "SELECT *, "
            f"DATE '{window_start.isoformat()}' AS window_start, "
            f"DATE '{window_end.isoformat()}' AS window_end, "
            f"{built_at_sql} AS built_at, '{sha}' AS git_sha "
            "FROM _results ORDER BY rule",
            out_path,
        )

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _tiers AS "
            "SELECT mmsi, year_month, r1, r2, r3, "
            "CASE WHEN r3 THEN 3 WHEN r2 THEN 2 WHEN r1 THEN 1 ELSE 0 END AS tier "
            "FROM _population"
        )
        atomic_write_parquet(
            con,
            "SELECT *, "
            f"DATE '{window_start.isoformat()}' AS window_start, "
            f"DATE '{window_end.isoformat()}' AS window_end, "
            f"{built_at_sql} AS built_at, '{sha}' AS git_sha "
            "FROM _tiers ORDER BY tier DESC, mmsi ASC",
            scores_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P4-1: the naive baseline every Phase 4 model must beat."
    )
    parser.add_argument("--panel-path", default=str(PANEL_PATH), help="Path to the vessel-month panel")
    parser.add_argument("--out-path", default=str(OUT_PATH), help="Output path for the rule results table")
    parser.add_argument("--scores-path", default=str(SCORES_PATH), help="Output path for per-vessel scores")
    parser.add_argument(
        "--summary-path", default=str(SUMMARY_PATH), help="Output path for the readable summary"
    )
    parser.add_argument(
        "--build-year-path", default=str(BUILD_YEAR_PATH), help="Path to the reconciled build-year table (R3)"
    )
    parser.add_argument(
        "--build-year-gate-path", default=str(BUILD_YEAR_GATE_PATH), help="Path to model.build_year_gate's verdict (R3)"
    )
    parser.add_argument(
        "--n-bootstrap", type=int, default=N_BOOTSTRAP, help="Number of bootstrap resamples per rule"
    )
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED, help="Bootstrap RNG seed")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_baseline(
        panel_path=Path(args.panel_path),
        out_path=Path(args.out_path),
        scores_path=Path(args.scores_path),
        summary_path=Path(args.summary_path),
        build_year_path=Path(args.build_year_path),
        build_year_gate_path=Path(args.build_year_gate_path),
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
        force=args.force,
    )


if __name__ == "__main__":
    main()
