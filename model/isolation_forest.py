"""P4-2: Isolation Forest (unsupervised anomaly scoring) on the vessel-month panel, evaluated
out-of-time against the P4-1 naive baseline.

**Temporal design.** The forest is fit on one window's panel (default 2024-06) and scores a LATER
window's panel (default 2024-11). No label is ever used in fitting; every preprocessing statistic
(median imputation) is computed on the training window only and applied unchanged to the test
window. The training window's population filter uses ``label_is_sanctioned_as_of_window_end``
-- the same P4-0 population ``model.evaluation`` defines, applied per window. That filter is only
*approximately* knowable at the window's end: ``designation_date`` comes from the 2026-09-21
sanctions-list snapshot, so a vessel delisted before the snapshot is missing and a re-designation
can hide an earlier listing. Out-of-time (test) metrics are the result; in-sample (train) metrics
are reported only as context.

**The two windows' label periods overlap.** June's forward positives are "designated after
2024-06-30", and 121 of June's 147 were designated after 2024-11-30, i.e. they are November-period
outcomes too. Harmless here (the forest never sees a label), but any choice made from June's
labels -- flipping a score's sign, picking a variant, or training P4-3's supervised model -- would
be using November's outcomes. Supervised work must cap the training label at designations in
(train window_end, test window_start).

**Comparison against the baseline -- matched alert budget.** Per ``CLAUDE.md``, the primary metric
is precision at a matched alert budget: on each split, ``k`` = the number of rows R2
(tanker + ITF flag of convenience) flags on that same split, and the forest's precision is measured
over its own top-``k`` rows. The verdict comes from a paired bootstrap over rows of
``precision_if@k - precision_r2`` (``k`` re-derived inside each resample as that resample's R2 count):
``beats_baseline`` only if the 95% interval is entirely above 0, ``loses_to_baseline`` if entirely
below, otherwise ``no_significant_difference``. The forest's own ``contamination='auto'`` flag count
is reported as secondary context, never as the comparison.

**Feature variants.**

* ``detectors``: every numeric/boolean panel column except identifiers, provenance and labels --
  the detector counts, the gap/sts mean and max columns, their P4-0 rate triples, exposure
  (``n_observed_*``, ``voyage_count``, ``total_message_count``) and ``is_reused``.
  ``is_orphaned`` is dropped because it is constant (false) inside the ``imo IS NOT NULL``
  population.
* ``detectors_context``: the above plus ``is_tanker`` and ``is_foc`` -- the two facts R2 is built
  from, so the forest has everything the baseline has.

The design (variants, preprocessing, metric, verdict rule) was fixed before the November scores
were first seen, and no score is sign-flipped: an AUC below 0.5 is reported as the model losing,
never "corrected" after looking at the test window.

**Preprocessing.** NULL -> the training window's column median (a NULL rate means "undefined, zero
exposure", not zero risk -- see ``features.panel``; the exposure columns themselves carry the
zero-exposure fact). Then ``log1p`` on every column whose training minimum is >= 0: Isolation Forest
draws split points uniformly between a feature's min and max, so without it a single heavy-tailed
column (``n_on_land`` runs into the tens of thousands) makes almost every split on that column
isolate one extreme vessel.

**Why no GPU.** The population is ~5k rows x ~60 features per window; the whole fit + 10-seed
stability check runs in seconds on CPU (``n_jobs=-1``). GPU Isolation Forest (cuML) has no native
Windows build and would add transfer overhead, not speed, at this size.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import roc_auc_score

from model.evaluation import add_tanker_foc_columns, bootstrap_auc_ci, load_population
from process.foc import FOC_NAME_TO_MID_COUNTRY
from process.partitions import atomic_write_parquet, git_sha

logger = logging.getLogger(__name__)

PANEL_ROOT = Path("data/processed/panel")
TRAIN_PANEL_PATH = PANEL_ROOT / "window=2024-06-01_2024-06-30" / "part-0.parquet"
TEST_PANEL_PATH = PANEL_ROOT / "window=2024-11-01_2024-11-30" / "part-0.parquet"
OUT_PATH = Path("data/processed/isolation_forest.parquet")
SCORES_PATH = Path("data/processed/isolation_forest_scores.parquet")
SUMMARY_PATH = Path("outputs/isolation_forest_summary.txt")

N_ESTIMATORS = 1000
SEED = 0
N_STABILITY_SEEDS = 10
N_BOOTSTRAP = 2000

NON_FEATURE_COLUMNS = frozenset(
    {
        "mmsi",
        "imo",
        "year_month",
        "window_start",
        "window_end",
        "built_at",
        "git_sha",
        "window",
        "is_orphaned",
        "flag_country",
        "ship_type",
    }
)
_NUMERIC_TYPES = ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "TINYINT", "DOUBLE", "FLOAT", "BOOLEAN")
_FORBIDDEN_SUBSTRINGS = ("label", "sanction", "designat")

CONTEXT_FEATURES_SQL = {
    "is_tanker": "COALESCE(ship_type, '') = 'Tanker'",
    "is_foc": "COALESCE(flag_country, '') IN ({foc})",
}
VARIANTS = ("detectors", "detectors_context")


def select_feature_columns(described: list[tuple[str, str]]) -> list[str]:
    """Numeric/boolean columns from a DESCRIBE result, minus identifiers, provenance and labels.

    Raises ValueError if any surviving column name looks label-derived -- a guard against a future
    panel column that forgets the ``label_`` prefix, not just a filter.
    """
    features = [
        name
        for name, dtype in described
        if name not in NON_FEATURE_COLUMNS
        and not name.startswith("label_")
        and dtype.upper().startswith(_NUMERIC_TYPES)
    ]
    leaked = [c for c in features if any(s in c.lower() for s in _FORBIDDEN_SUBSTRINGS)]
    if leaked:
        raise ValueError(f"Label-like columns would enter the feature matrix: {leaked}")
    return features


@dataclass
class Split:
    """One window's modelling population, ready to score."""

    name: str
    mmsi: np.ndarray
    year_month: np.ndarray
    labels: np.ndarray
    r2: np.ndarray
    features: dict[str, np.ndarray]
    window_start: object
    window_end: object


def load_split(name: str, panel_path: Path) -> Split:
    """Read `panel_path`'s P4-0 population with the R2 flag and every candidate feature column."""
    con = duckdb.connect()
    try:
        window_start, window_end = load_population(con, panel_path, table_name="_base")
        add_tanker_foc_columns(con, source_table="_base", dest_table="_pop")
        described = [(r[0], r[1]) for r in con.execute("DESCRIBE _pop").fetchall()]
        columns = select_feature_columns([d for d in described if d[0] not in ("r1", "r2")])
        foc_sql = ", ".join(f"'{v}'" for v in sorted(set(FOC_NAME_TO_MID_COUNTRY.values())))
        context_sql = [
            f"CAST(({expr.format(foc=foc_sql)}) AS DOUBLE) AS {name_}"
            for name_, expr in CONTEXT_FEATURES_SQL.items()
        ]
        feature_sql = [f"COALESCE(CAST({c} AS DOUBLE), 'NaN'::DOUBLE) AS {c}" for c in columns]
        data = con.execute(
            "SELECT mmsi, year_month, label_is_sanctioned_after_window_end AS label, r2, "
            f"{', '.join(feature_sql + context_sql)} FROM _pop ORDER BY mmsi, year_month"
        ).fetchnumpy()
    finally:
        con.close()
    return Split(
        name=name,
        mmsi=np.asarray(data["mmsi"]),
        year_month=np.datetime_as_string(np.asarray(data["year_month"]), unit="D"),
        labels=np.asarray(data["label"], dtype=bool),
        r2=np.asarray(data["r2"], dtype=bool),
        features={c: np.asarray(data[c], dtype=float) for c in columns + list(CONTEXT_FEATURES_SQL)},
        window_start=window_start,
        window_end=window_end,
    )


def variant_columns(variant: str, split: Split) -> list[str]:
    detector_cols = [c for c in split.features if c not in CONTEXT_FEATURES_SQL]
    if variant == "detectors":
        return detector_cols
    if variant == "detectors_context":
        return detector_cols + list(CONTEXT_FEATURES_SQL)
    raise ValueError(f"Unknown variant {variant!r}")


@dataclass(frozen=True)
class Preprocessor:
    """Median imputation + selective log1p, fitted on the training window only."""

    columns: tuple[str, ...]
    medians: np.ndarray
    log_mask: np.ndarray

    @classmethod
    def fit(cls, split: Split, columns: list[str]) -> Preprocessor:
        x = np.column_stack([split.features[c] for c in columns])
        with np.errstate(all="ignore"):
            medians = np.nanmedian(x, axis=0)
        medians = np.where(np.isnan(medians), 0.0, medians)
        filled = np.where(np.isnan(x), medians, x)
        log_mask = filled.min(axis=0) >= 0
        return cls(tuple(columns), medians, log_mask)

    def transform(self, split: Split) -> np.ndarray:
        x = np.column_stack([split.features[c] for c in self.columns])
        x = np.where(np.isnan(x), self.medians, x)
        x[:, self.log_mask] = np.log1p(np.clip(x[:, self.log_mask], 0, None))
        return x


def precision_at_k(labels: np.ndarray, scores: np.ndarray, k: int) -> float | None:
    """Precision among the `k` highest-scoring rows. Ties broken by row order (stable sort) --
    Isolation Forest scores are continuous, so ties at the cutoff are negligible in practice."""
    if k <= 0 or k > len(labels):
        return None
    order = np.argsort(-scores, kind="stable")
    return float(labels[order[:k]].mean())


def paired_bootstrap_precision_diff(
    labels: np.ndarray,
    scores: np.ndarray,
    r2: np.ndarray,
    n_bootstrap: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    """95% CI of precision_model@k - precision_r2, resampling rows with replacement; k is each
    resample's own R2 flag count, so the alert budget stays matched inside every resample."""
    n = len(labels)
    diffs = []
    for _ in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        lab, sc, rr = labels[idx], scores[idx], r2[idx]
        k = int(rr.sum())
        if k == 0:
            continue
        diffs.append(precision_at_k(lab, sc, k) - float(lab[rr].mean()))
    if not diffs:
        return (float("nan"), float("nan"))
    return (float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5)))


def fit_forest(x: np.ndarray, seed: int, n_estimators: int = N_ESTIMATORS) -> IsolationForest:
    return IsolationForest(
        n_estimators=n_estimators, contamination="auto", random_state=seed, n_jobs=-1
    ).fit(x)


def risk_scores(forest: IsolationForest, x: np.ndarray) -> np.ndarray:
    """Higher = more anomalous (sklearn's score_samples is the opposite sign)."""
    return -forest.score_samples(x)


RESULT_COLUMNS = (
    ("variant", "VARCHAR"),
    ("split", "VARCHAR"),
    ("n_features", "BIGINT"),
    ("n_pop", "BIGINT"),
    ("n_pos", "BIGINT"),
    ("prevalence", "DOUBLE"),
    ("auc", "DOUBLE"),
    ("auc_ci_low", "DOUBLE"),
    ("auc_ci_high", "DOUBLE"),
    ("k_matched", "BIGINT"),
    ("precision_r2", "DOUBLE"),
    ("lift_r2", "DOUBLE"),
    ("precision_at_k", "DOUBLE"),
    ("lift_at_k", "DOUBLE"),
    ("diff_ci_low", "DOUBLE"),
    ("diff_ci_high", "DOUBLE"),
    ("precision_at_k_seed_min", "DOUBLE"),
    ("precision_at_k_seed_max", "DOUBLE"),
    ("n_flagged_own", "BIGINT"),
    ("precision_own", "DOUBLE"),
    ("auc_seen_in_train", "DOUBLE"),
    ("auc_unseen_in_train", "DOUBLE"),
    ("verdict", "VARCHAR"),
)


def _subset_auc(labels: np.ndarray, scores: np.ndarray, mask: np.ndarray | None) -> float | None:
    if mask is None:
        return None
    lab = labels[mask]
    if lab.all() or not lab.any():
        return None
    return float(roc_auc_score(lab, scores[mask]))


def evaluate_split(
    variant: str,
    split: Split,
    x: np.ndarray,
    forest: IsolationForest,
    stability_forests: list[IsolationForest],
    n_bootstrap: int,
    rng: np.random.Generator,
    seen_in_train: np.ndarray | None = None,
) -> tuple[dict, np.ndarray]:
    """``seen_in_train`` (test split only) marks rows whose mmsi also appears in the training
    window, so the result can be shown not to be driven by vessel novelty."""
    labels, r2 = split.labels, split.r2
    scores = risk_scores(forest, x)
    n_pop, n_pos = len(labels), int(labels.sum())
    prevalence = n_pos / n_pop
    auc = float(roc_auc_score(labels, scores))
    auc_lo, auc_hi = bootstrap_auc_ci(scores[labels], scores[~labels], n_bootstrap, rng)
    k = int(r2.sum())
    precision_r2 = float(labels[r2].mean())
    prec_k = precision_at_k(labels, scores, k)
    diff_lo, diff_hi = paired_bootstrap_precision_diff(labels, scores, r2, n_bootstrap, rng)
    seed_precisions = [precision_at_k(labels, risk_scores(f, x), k) for f in stability_forests]
    own = forest.predict(x) == -1
    n_own = int(own.sum())
    if diff_lo > 0:
        verdict = "beats_baseline"
    elif diff_hi < 0:
        verdict = "loses_to_baseline"
    else:
        verdict = "no_significant_difference"
    row = {
        "variant": variant,
        "split": split.name,
        "n_features": x.shape[1],
        "n_pop": n_pop,
        "n_pos": n_pos,
        "prevalence": prevalence,
        "auc": auc,
        "auc_ci_low": auc_lo,
        "auc_ci_high": auc_hi,
        "k_matched": k,
        "precision_r2": precision_r2,
        "lift_r2": precision_r2 / prevalence,
        "precision_at_k": prec_k,
        "lift_at_k": prec_k / prevalence,
        "diff_ci_low": diff_lo,
        "diff_ci_high": diff_hi,
        "precision_at_k_seed_min": min(seed_precisions),
        "precision_at_k_seed_max": max(seed_precisions),
        "n_flagged_own": n_own,
        "precision_own": float(labels[own].mean()) if n_own else None,
        "auc_seen_in_train": _subset_auc(labels, scores, seen_in_train),
        "auc_unseen_in_train": _subset_auc(
            labels, scores, None if seen_in_train is None else ~seen_in_train
        ),
        "verdict": verdict,
    }
    return row, scores


def _write_summary(path: Path, rows: list[dict], n_overlap: int, train: Split, test: Split) -> None:
    lines = [
        "P4-2 Isolation Forest vs the R2 baseline (tanker + flag of convenience)",
        "=" * 72,
        (
            f"Fit on {train.window_start}..{train.window_end}, "
            f"tested out-of-time on {test.window_start}..{test.window_end}."
        ),
        (
            f"{n_overlap} of {len(test.mmsi)} test-window vessels also appear in the training "
            "window (no label is used in fitting, so this is not label leakage)."
        ),
        "Primary metric: precision at k = R2's own flag count on the same split (matched budget).",
        "",
        (
            f"{'variant':<19}{'split':<7}{'n_pos':>6}{'auc':>7}{'k':>5}{'prec_R2':>9}"
            f"{'prec_IF@k':>11}{'diff 95% CI':>18}  verdict"
        ),
    ]
    for r in rows:
        ci = f"[{r['diff_ci_low']:+.3f},{r['diff_ci_high']:+.3f}]"
        lines.append(
            f"{r['variant']:<19}{r['split']:<7}{r['n_pos']:>6}{r['auc']:>7.3f}{r['k_matched']:>5}"
            f"{r['precision_r2']:>9.3f}{r['precision_at_k']:>11.3f}{ci:>18}  {r['verdict']}"
        )
        own_prec = "n/a" if r["precision_own"] is None else f"{r['precision_own']:.3f}"
        lines.append(
            f"    lift@k {r['lift_at_k']:.2f}x vs R2 {r['lift_r2']:.2f}x; auc CI "
            f"[{r['auc_ci_low']:.3f}, {r['auc_ci_high']:.3f}]; prec@k across seeds "
            f"{r['precision_at_k_seed_min']:.3f}-{r['precision_at_k_seed_max']:.3f}; "
            f"own flags {r['n_flagged_own']} (precision {own_prec})"
        )
        if r["split"] == "test":
            seen, unseen = (
                "n/a" if v is None else f"{v:.3f}"
                for v in (r["auc_seen_in_train"], r["auc_unseen_in_train"])
            )
            lines.append(
                f"    auc on vessels seen in the training window {seen}, unseen {unseen}"
            )
    lines += [
        "",
        "The 'test' rows are the result; 'train' rows are in-sample context only.",
        "Lift below 1.0 means the model's alerts are worse than random picks: vessels later",
        "sanctioned look LESS anomalous than the rest, not more (see features.panel / P4-0).",
        "Caveats: labels come from a 2026 sanctions-list snapshot and measure agreement with who",
        "was caught, not who evaded (label bias, see README). The two windows differ seasonally",
        "(June has ~2x November's vessels and ~3x its gaps), a plausible source of drift. The",
        "windows' label periods overlap -- see the module docstring before any supervised use.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_isolation_forest(
    train_panel_path: Path = TRAIN_PANEL_PATH,
    test_panel_path: Path = TEST_PANEL_PATH,
    out_path: Path = OUT_PATH,
    scores_path: Path = SCORES_PATH,
    summary_path: Path = SUMMARY_PATH,
    n_estimators: int = N_ESTIMATORS,
    n_bootstrap: int = N_BOOTSTRAP,
    n_stability_seeds: int = N_STABILITY_SEEDS,
    seed: int = SEED,
    force: bool = False,
) -> Path:
    """Fit on the training window, score both windows, write metrics + scores + summary.

    Idempotent unless force=True. Raises ValueError if the test window does not start strictly
    after the training window ends -- an out-of-time evaluation must be out of time.
    """
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    train = load_split("train", train_panel_path)
    test = load_split("test", test_panel_path)
    if not test.window_start > train.window_end:
        raise ValueError(
            f"Test window {test.window_start}..{test.window_end} does not start after training "
            f"window {train.window_start}..{train.window_end}"
        )
    seen_in_train = np.isin(test.mmsi, train.mmsi)
    n_overlap = int(seen_in_train.sum())
    logger.info(
        "Train %d rows / %d positive; test %d rows / %d positive; %d test mmsi also in train",
        len(train.labels), int(train.labels.sum()), len(test.labels), int(test.labels.sum()), n_overlap,
    )

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    score_rows: list[tuple] = []
    for variant in VARIANTS:
        prep = Preprocessor.fit(train, variant_columns(variant, train))
        x_train, x_test = prep.transform(train), prep.transform(test)
        forest = fit_forest(x_train, seed, n_estimators)
        stability = [
            fit_forest(x_train, seed + 1 + i, n_estimators) for i in range(n_stability_seeds)
        ]
        for split, x, seen in ((train, x_train, None), (test, x_test, seen_in_train)):
            row, scores = evaluate_split(
                variant, split, x, forest, stability, n_bootstrap, rng, seen
            )
            rows.append(row)
            logger.info(
                "%s/%s: auc %.3f, precision@%d %.3f vs R2 %.3f -> %s",
                variant, split.name, row["auc"], row["k_matched"], row["precision_at_k"],
                row["precision_r2"], row["verdict"],
            )
            score_rows.extend(
                (variant, split.name, int(m), str(ym), float(s), bool(lab), bool(r))
                for m, ym, s, lab, r in zip(
                    split.mmsi, split.year_month, scores, split.labels, split.r2, strict=True
                )
            )

    _write_summary(summary_path, rows, n_overlap, train, test)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    provenance = (
        f"DATE '{train.window_start}' AS train_window_start, "
        f"DATE '{train.window_end}' AS train_window_end, "
        f"DATE '{test.window_start}' AS test_window_start, "
        f"DATE '{test.window_end}' AS test_window_end, "
        f"TIMESTAMP '{built_at}' AS built_at, '{git_sha()}' AS git_sha"
    )
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TEMP TABLE _results ("
            + ", ".join(f"{name} {dtype}" for name, dtype in RESULT_COLUMNS)
            + ")"
        )
        con.executemany(
            f"INSERT INTO _results VALUES ({', '.join('?' for _ in RESULT_COLUMNS)})",
            [tuple(r[name] for name, _ in RESULT_COLUMNS) for r in rows],
        )
        atomic_write_parquet(
            con, f"SELECT *, {provenance} FROM _results ORDER BY variant, split", out_path
        )
        con.execute(
            "CREATE TEMP TABLE _scores (variant VARCHAR, split VARCHAR, mmsi BIGINT, "
            "year_month DATE, anomaly_score DOUBLE, label BOOLEAN, r2 BOOLEAN)"
        )
        con.executemany("INSERT INTO _scores VALUES (?, ?, ?, ?, ?, ?, ?)", score_rows)
        atomic_write_parquet(
            con,
            f"SELECT *, {provenance} FROM _scores "
            "ORDER BY variant, split, anomaly_score DESC, mmsi",
            scores_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P4-2: Isolation Forest fit on one window, evaluated out-of-time on a later one."
    )
    parser.add_argument("--train-panel-path", default=str(TRAIN_PANEL_PATH))
    parser.add_argument("--test-panel-path", default=str(TEST_PANEL_PATH))
    parser.add_argument("--out-path", default=str(OUT_PATH))
    parser.add_argument("--scores-path", default=str(SCORES_PATH))
    parser.add_argument("--summary-path", default=str(SUMMARY_PATH))
    parser.add_argument("--n-estimators", type=int, default=N_ESTIMATORS)
    parser.add_argument("--n-bootstrap", type=int, default=N_BOOTSTRAP)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--force", action="store_true", help="Rebuild even if the output exists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_isolation_forest(
        train_panel_path=Path(args.train_panel_path),
        test_panel_path=Path(args.test_panel_path),
        out_path=Path(args.out_path),
        scores_path=Path(args.scores_path),
        summary_path=Path(args.summary_path),
        n_estimators=args.n_estimators,
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
        force=args.force,
    )


if __name__ == "__main__":
    main()
