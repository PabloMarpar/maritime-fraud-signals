"""P4-3: supervised risk scoring with LightGBM, trained on earlier window(s) with an as-of-cutoff
label and evaluated out-of-time against the P4-1 baseline, exactly like ``model.isolation_forest``.

**The cutoff and the label -- read this first.** The test window's panel is scored as of its own
end; the model is trained as of ``cutoff = test window_start``. A training row is positive only if
its vessel was designated in ``(its own window_end, cutoff)`` -- i.e. the designation was already
public when the model was trained. Every other training row is negative, including vessels that
will be designated later (the model cannot know that yet; excluding them would itself use future
information). The naive label, ``label_is_sanctioned_after_window_end``, would leak the test
period's outcomes: on the real June window 121 of its 147 forward positives were designated after
2024-11-30, i.e. they ARE November-period outcomes (see ``model.isolation_forest``'s docstring and
``docs/DECISIONS.md``). With only June as a training window this leaves **16 training positives**
(all tankers) -- the binding constraint on this module, stated in every summary it writes.

``designation_date`` comes from the 2026-09-21 sanctions-list snapshot, so "already public at the
cutoff" is approximate: a vessel delisted before the snapshot is missing entirely. The label
horizon also differs: training positives were designated within ~4 months of their window, test
positives up to ~22 months after theirs (until the snapshot).

**Cutoffs.** ``train_panel_paths`` accepts several earlier windows; each keeps its own population
(``model.evaluation``'s P4-0 filter against its own window_end) and its own as-of-cutoff label,
then they are pooled. With the two real windows built so far there is exactly ONE cutoff (train
June -> test November) -- not a rolling evaluation; more cutoffs need more windows, not code.

**Comparison (primary, pre-set).** Identical to ``model.isolation_forest``: precision at k = R2's
own flag count on the test window, paired row-bootstrap CI of the difference, same verdict rule.
A model whose top-k set is exactly R2's flagged set gets ``reproduces_r2`` instead (its CI is
[0, 0] by construction, not evidence). **Read the ceiling with it:** k = 574 and only
``min(n_test_pos, k)`` positives exist, so the best achievable precision@k -- and hence the
largest achievable gain over R2 -- is printed in every summary; on the real November window it
is 0.242, i.e. at most +0.038 over R2.

**Uncertainty.** The test-row bootstrap CIs are conditional on the one fitted model. A separate
training-row bootstrap (refit on each resample, :data:`N_TRAIN_BOOTSTRAP` times) reports how much
precision@k moves with which positives happened to be in training -- with 16 positives this is
the larger source of variance. The seed spread covers only LightGBM's own subsampling.

**Secondary, added after the first run (post hoc, not pre-registered).** A paired test-row
bootstrap of the AUC difference ``detectors_context - context``: the fair question "do detectors
add ranking signal on top of R2's two inputs?". AUC against R2 itself is not a fair comparison --
R2 is binary, and even ``is_tanker`` alone has a higher AUC. Out-of-fold AUC on the training rows
(stratified, grouped by mmsi) is in-sample context only. Scores are ``risk_score``, not
probabilities: trained at 0.33% prevalence, tested at 3.1%, never calibrated (that is P4-4).

**Variants:** ``context`` (is_tanker, is_foc -- R2's own two inputs, so a learned R2),
``detectors`` (every numeric panel column, see ``model.isolation_forest``), ``detectors_context``
(both). Beating R2 at the matched budget is the project's bar.

**Hyperparameters** are fixed and conservative: shallow trees (``num_leaves=4``, ``max_depth=2``),
slow learning, row/column subsampling. No imputation or scaling -- LightGBM handles NaN natively and
trees are invariant to monotone transforms. The variants and hyperparameters were written in the
same session as the first real run, so "not tuned on the test window" is the author's statement,
not independently verifiable; the commit that introduces this module freezes them for the next
window.

**No GPU.** ~5k rows; a fit takes well under a second on CPU. LightGBM's GPU path only pays off at
hundreds of thousands of rows.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold

from model.evaluation import bootstrap_auc_ci
from model.isolation_forest import (
    CONTEXT_FEATURES_SQL,
    TEST_PANEL_PATH,
    TRAIN_PANEL_PATH,
    Split,
    _subset_auc,
    load_split,
    paired_bootstrap_precision_diff,
    precision_at_k,
    variant_columns,
)
from process.partitions import atomic_write_parquet, git_sha

logger = logging.getLogger(__name__)

OUT_PATH = Path("data/processed/lightgbm.parquet")
SCORES_PATH = Path("data/processed/lightgbm_scores.parquet")
SUMMARY_PATH = Path("outputs/lightgbm_summary.txt")

SEED = 0
N_STABILITY_SEEDS = 10
N_BOOTSTRAP = 2000
N_CV_FOLDS = 5
N_TRAIN_BOOTSTRAP = 200
N_TOP_FEATURES = 8

PARAMS = {
    "objective": "binary",
    "n_estimators": 300,
    "learning_rate": 0.03,
    "num_leaves": 4,
    "max_depth": 2,
    "min_child_samples": 10,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "verbose": -1,
}
VARIANTS = ("context", "detectors", "detectors_context")


def as_of_cutoff_labels(split: Split, cutoff: np.datetime64) -> np.ndarray:
    """True only for vessels designated after the split's own window_end and strictly before
    `cutoff` -- the designations already public when a model is trained at `cutoff`."""
    d = split.designation_date
    known = ~np.isnat(d)
    after_window = np.zeros(len(d), dtype=bool)
    after_window[known] = (d[known] > np.datetime64(split.window_end, "D")) & (d[known] < cutoff)
    return after_window


def columns_for(variant: str, split: Split) -> list[str]:
    if variant == "context":
        return list(CONTEXT_FEATURES_SQL)
    return variant_columns(variant, split)


def matrix(splits: list[Split], columns: list[str]) -> np.ndarray:
    return np.vstack([np.column_stack([s.features[c] for c in columns]) for s in splits])


def fit_model(x: np.ndarray, y: np.ndarray, seed: int) -> lgb.LGBMClassifier:
    return lgb.LGBMClassifier(**PARAMS, random_state=seed, n_jobs=-1).fit(x, y)


def paired_bootstrap_auc_diff(
    labels: np.ndarray,
    scores_a: np.ndarray,
    scores_b: np.ndarray,
    n_bootstrap: int,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    """(point, 2.5%, 97.5%) of AUC(a) - AUC(b), resampling test rows jointly for both scores."""
    point = float(roc_auc_score(labels, scores_a) - roc_auc_score(labels, scores_b))
    n = len(labels)
    diffs = []
    for _ in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        lab = labels[idx]
        if lab.all() or not lab.any():
            continue
        diffs.append(roc_auc_score(lab, scores_a[idx]) - roc_auc_score(lab, scores_b[idx]))
    return point, float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def train_bootstrap_precision(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    labels: np.ndarray,
    k: int,
    n_resamples: int,
    seed: int,
    rng: np.random.Generator,
) -> tuple[float | None, float | None]:
    """2.5%/97.5% of precision@k when the model is refit on training rows resampled with
    replacement -- the variance from WHICH positives were in training, which the test-row
    bootstrap (conditional on one fitted model) cannot see."""
    precisions = []
    n = len(y_train)
    for i in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        if not y_train[idx].any():
            continue
        scores = fit_model(x_train[idx], y_train[idx], seed + i).predict_proba(x_test)[:, 1]
        precisions.append(precision_at_k(labels, scores, k))
    if not precisions:
        return None, None
    return float(np.percentile(precisions, 2.5)), float(np.percentile(precisions, 97.5))


def reproduces_set(scores: np.ndarray, flagged: np.ndarray) -> bool:
    """True if the top-|flagged| rows by score are exactly the flagged rows."""
    k = int(flagged.sum())
    top = np.argsort(-scores, kind="stable")[:k]
    return bool(flagged[top].all())


def out_of_fold_auc(x: np.ndarray, y: np.ndarray, groups: np.ndarray, seed: int) -> float | None:
    """Stratified, mmsi-grouped out-of-fold AUC on the training rows -- in-sample context only."""
    n_splits = min(N_CV_FOLDS, int(y.sum()))
    if n_splits < 2:
        return None
    oof = np.zeros(len(y))
    folds = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    for fit_idx, pred_idx in folds.split(x, y, groups):
        oof[pred_idx] = fit_model(x[fit_idx], y[fit_idx], seed).predict_proba(x[pred_idx])[:, 1]
    return float(roc_auc_score(y, oof))


RESULT_COLUMNS = (
    ("variant", "VARCHAR"),
    ("n_features", "BIGINT"),
    ("n_train", "BIGINT"),
    ("n_train_pos", "BIGINT"),
    ("train_oof_auc", "DOUBLE"),
    ("n_test", "BIGINT"),
    ("n_test_pos", "BIGINT"),
    ("prevalence", "DOUBLE"),
    ("auc", "DOUBLE"),
    ("auc_ci_low", "DOUBLE"),
    ("auc_ci_high", "DOUBLE"),
    ("auc_seen_in_train", "DOUBLE"),
    ("auc_unseen_in_train", "DOUBLE"),
    ("k_matched", "BIGINT"),
    ("precision_r2", "DOUBLE"),
    ("lift_r2", "DOUBLE"),
    ("precision_at_k", "DOUBLE"),
    ("lift_at_k", "DOUBLE"),
    ("diff_ci_low", "DOUBLE"),
    ("diff_ci_high", "DOUBLE"),
    ("precision_at_k_seed_min", "DOUBLE"),
    ("precision_at_k_seed_max", "DOUBLE"),
    ("precision_ceiling", "DOUBLE"),
    ("train_boot_precision_low", "DOUBLE"),
    ("train_boot_precision_high", "DOUBLE"),
    ("auc_diff_vs_context", "DOUBLE"),
    ("auc_diff_vs_context_ci_low", "DOUBLE"),
    ("auc_diff_vs_context_ci_high", "DOUBLE"),
    ("top_features", "VARCHAR"),
    ("verdict", "VARCHAR"),
)


def evaluate_variant(
    variant: str,
    train: list[Split],
    test: Split,
    seed: int,
    n_bootstrap: int,
    n_stability_seeds: int,
    n_train_bootstrap: int,
    rng: np.random.Generator,
) -> tuple[dict, np.ndarray]:
    cutoff = np.datetime64(test.window_start, "D")
    columns = columns_for(variant, train[0])
    x_train = matrix(train, columns)
    y_train = np.concatenate([as_of_cutoff_labels(s, cutoff) for s in train])
    groups = np.concatenate([s.mmsi for s in train])
    x_test = matrix([test], columns)
    labels, r2 = test.labels, test.r2

    model = fit_model(x_train, y_train, seed)
    scores = model.predict_proba(x_test)[:, 1]
    seed_scores = [
        fit_model(x_train, y_train, seed + 1 + i).predict_proba(x_test)[:, 1]
        for i in range(n_stability_seeds)
    ]

    prevalence = float(labels.mean())
    auc = float(roc_auc_score(labels, scores))
    auc_lo, auc_hi = bootstrap_auc_ci(scores[labels], scores[~labels], n_bootstrap, rng)
    k = int(r2.sum())
    precision_r2 = float(labels[r2].mean())
    prec_k = precision_at_k(labels, scores, k)
    diff_lo, diff_hi = paired_bootstrap_precision_diff(labels, scores, r2, n_bootstrap, rng)
    seed_precisions = [precision_at_k(labels, s, k) for s in seed_scores]
    seen = np.isin(test.mmsi, groups)

    gains = model.booster_.feature_importance(importance_type="gain")
    order = np.argsort(-gains)[:N_TOP_FEATURES]
    top_features = ", ".join(f"{columns[i]}={gains[i]:.0f}" for i in order if gains[i] > 0)

    boot_lo, boot_hi = train_bootstrap_precision(
        x_train, y_train, x_test, labels, k, n_train_bootstrap, seed, rng
    )

    if reproduces_set(scores, r2):
        verdict = "reproduces_r2"
    elif diff_lo > 0:
        verdict = "beats_baseline"
    elif diff_hi < 0:
        verdict = "loses_to_baseline"
    else:
        verdict = "no_significant_difference"
    row = {
        "variant": variant,
        "n_features": len(columns),
        "n_train": len(y_train),
        "n_train_pos": int(y_train.sum()),
        "train_oof_auc": out_of_fold_auc(x_train, y_train, groups, seed),
        "n_test": len(labels),
        "n_test_pos": int(labels.sum()),
        "prevalence": prevalence,
        "auc": auc,
        "auc_ci_low": auc_lo,
        "auc_ci_high": auc_hi,
        "auc_seen_in_train": _subset_auc(labels, scores, seen),
        "auc_unseen_in_train": _subset_auc(labels, scores, ~seen),
        "k_matched": k,
        "precision_r2": precision_r2,
        "lift_r2": precision_r2 / prevalence,
        "precision_at_k": prec_k,
        "lift_at_k": prec_k / prevalence,
        "diff_ci_low": diff_lo,
        "diff_ci_high": diff_hi,
        "precision_at_k_seed_min": min(seed_precisions),
        "precision_at_k_seed_max": max(seed_precisions),
        "precision_ceiling": min(int(labels.sum()), k) / k,
        "train_boot_precision_low": boot_lo,
        "train_boot_precision_high": boot_hi,
        "auc_diff_vs_context": None,
        "auc_diff_vs_context_ci_low": None,
        "auc_diff_vs_context_ci_high": None,
        "top_features": top_features,
        "verdict": verdict,
    }
    return row, scores


def _fmt(value: float | None, spec: str = ".3f") -> str:
    return "n/a" if value is None else format(value, spec)


def _write_summary(path: Path, rows: list[dict], train: list[Split], test: Split) -> None:
    windows = ", ".join(f"{s.window_start}..{s.window_end}" for s in train)
    lines = [
        "P4-3 LightGBM vs the R2 baseline (tanker + flag of convenience)",
        "=" * 72,
        f"Trained on {windows} with labels as of cutoff {test.window_start};",
        f"tested out-of-time on {test.window_start}..{test.window_end}.",
        f"TRAINING POSITIVES: {rows[0]['n_train_pos']} of {rows[0]['n_train']} rows -- a",
        "training label counts only designations already public at the cutoff.",
        "Primary metric: precision at k = R2's own flag count on the test window.",
        (
            f"Ceiling at k={rows[0]['k_matched']}: only {rows[0]['n_test_pos']} positives exist, so "
            f"the best possible precision is {rows[0]['precision_ceiling']:.3f} -- at most "
            f"{rows[0]['precision_ceiling'] - rows[0]['precision_r2']:+.3f} over R2."
        ),
        "",
        (
            f"{'variant':<19}{'n_feat':>7}{'oof_auc':>9}{'auc':>7}{'k':>5}{'prec_R2':>9}"
            f"{'prec@k':>8}{'diff 95% CI':>18}  verdict"
        ),
    ]
    for r in rows:
        ci = f"[{r['diff_ci_low']:+.3f},{r['diff_ci_high']:+.3f}]"
        lines.append(
            f"{r['variant']:<19}{r['n_features']:>7}{_fmt(r['train_oof_auc']):>9}{r['auc']:>7.3f}"
            f"{r['k_matched']:>5}{r['precision_r2']:>9.3f}{r['precision_at_k']:>8.3f}{ci:>18}"
            f"  {r['verdict']}"
        )
        lines.append(
            f"    lift@k {r['lift_at_k']:.2f}x vs R2 {r['lift_r2']:.2f}x; auc CI "
            f"[{r['auc_ci_low']:.3f}, {r['auc_ci_high']:.3f}]; seen/unseen auc "
            f"{_fmt(r['auc_seen_in_train'])}/{_fmt(r['auc_unseen_in_train'])}; prec@k across "
            f"seeds {r['precision_at_k_seed_min']:.3f}-{r['precision_at_k_seed_max']:.3f}"
        )
        lines.append(
            f"    prec@k under training-row resampling (refit): "
            f"{_fmt(r['train_boot_precision_low'])}-{_fmt(r['train_boot_precision_high'])}"
            + (
                ""
                if r["auc_diff_vs_context"] is None
                else f"; [post hoc] auc - context auc {r['auc_diff_vs_context']:+.3f} "
                f"[{r['auc_diff_vs_context_ci_low']:+.3f}, {r['auc_diff_vs_context_ci_high']:+.3f}]"
            )
        )
        lines.append(f"    top gain: {r['top_features'] or '(no splits)'}")
    lines += [
        "",
        "Test-row CIs are conditional on the one fitted model; the training-row resampling line",
        "shows how much the result depends on which positives happened to be in training.",
        "reproduces_r2: the model's top-k set IS R2's flagged set (a learned R2, CI [0,0]).",
        "[post hoc] lines were added after the first run and are not pre-registered evidence.",
        "oof_auc is out-of-fold on the training rows (in-sample context, very noisy with this",
        "few positives). Scores are risk scores, not probabilities (uncalibrated, see P4-4).",
        "Labels measure who was caught, not who evaded (label bias, see README).",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_lightgbm(
    train_panel_paths: tuple[Path, ...] = (TRAIN_PANEL_PATH,),
    test_panel_path: Path = TEST_PANEL_PATH,
    out_path: Path = OUT_PATH,
    scores_path: Path = SCORES_PATH,
    summary_path: Path = SUMMARY_PATH,
    n_bootstrap: int = N_BOOTSTRAP,
    n_stability_seeds: int = N_STABILITY_SEEDS,
    n_train_bootstrap: int = N_TRAIN_BOOTSTRAP,
    seed: int = SEED,
    force: bool = False,
) -> Path:
    """Train on `train_panel_paths`, evaluate on `test_panel_path`, write metrics + scores +
    summary. Idempotent unless force=True. Raises ValueError if any training window does not end
    before the test window starts, or if no training row is positive as of the cutoff."""
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to rebuild)", out_path)
        return out_path

    train = [load_split("train", p) for p in train_panel_paths]
    test = load_split("test", test_panel_path)
    for s in train:
        if not s.window_end < test.window_start:
            raise ValueError(
                f"Training window {s.window_start}..{s.window_end} does not end before test "
                f"window {test.window_start}..{test.window_end}"
            )
    cutoff = np.datetime64(test.window_start, "D")
    n_pos = int(sum(as_of_cutoff_labels(s, cutoff).sum() for s in train))
    if n_pos == 0:
        raise ValueError(f"No training row is positive as of cutoff {test.window_start}")

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    score_rows: list[tuple] = []
    scores_by_variant: dict[str, np.ndarray] = {}
    for variant in VARIANTS:
        row, scores = evaluate_variant(
            variant, train, test, seed, n_bootstrap, n_stability_seeds, n_train_bootstrap, rng
        )
        rows.append(row)
        scores_by_variant[variant] = scores
        logger.info(
            "%s: %d train positives, auc %.3f, precision@%d %.3f vs R2 %.3f -> %s",
            variant, row["n_train_pos"], row["auc"], row["k_matched"], row["precision_at_k"],
            row["precision_r2"], row["verdict"],
        )
        score_rows.extend(
            (variant, int(m), str(ym), float(s), bool(lab), bool(r))
            for m, ym, s, lab, r in zip(
                test.mmsi, test.year_month, scores, test.labels, test.r2, strict=True
            )
        )

    for row in rows:
        if row["variant"] == "context":
            continue
        point, lo, hi = paired_bootstrap_auc_diff(
            test.labels, scores_by_variant[row["variant"]], scores_by_variant["context"],
            n_bootstrap, rng,
        )
        row["auc_diff_vs_context"] = point
        row["auc_diff_vs_context_ci_low"] = lo
        row["auc_diff_vs_context_ci_high"] = hi

    _write_summary(summary_path, rows, train, test)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    train_windows = ";".join(f"{s.window_start}_{s.window_end}" for s in train)
    provenance = (
        f"'{train_windows}' AS train_windows, "
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
        atomic_write_parquet(con, f"SELECT *, {provenance} FROM _results ORDER BY variant", out_path)
        con.execute(
            "CREATE TEMP TABLE _scores (variant VARCHAR, mmsi BIGINT, year_month DATE, "
            "risk_score DOUBLE, label BOOLEAN, r2 BOOLEAN)"
        )
        con.executemany("INSERT INTO _scores VALUES (?, ?, ?, ?, ?, ?)", score_rows)
        atomic_write_parquet(
            con,
            f"SELECT *, {provenance} FROM _scores ORDER BY variant, risk_score DESC, mmsi",
            scores_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P4-3: LightGBM trained with as-of-cutoff labels, evaluated out-of-time."
    )
    parser.add_argument(
        "--train-panel-path", action="append", default=None,
        help="Training panel (repeatable; default: the June window)",
    )
    parser.add_argument("--test-panel-path", default=str(TEST_PANEL_PATH))
    parser.add_argument("--out-path", default=str(OUT_PATH))
    parser.add_argument("--scores-path", default=str(SCORES_PATH))
    parser.add_argument("--summary-path", default=str(SUMMARY_PATH))
    parser.add_argument("--n-bootstrap", type=int, default=N_BOOTSTRAP)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--force", action="store_true", help="Rebuild even if the output exists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    train_paths = tuple(Path(p) for p in (args.train_panel_path or [TRAIN_PANEL_PATH]))
    build_lightgbm(
        train_panel_paths=train_paths,
        test_panel_path=Path(args.test_panel_path),
        out_path=Path(args.out_path),
        scores_path=Path(args.scores_path),
        summary_path=Path(args.summary_path),
        n_bootstrap=args.n_bootstrap,
        seed=args.seed,
        force=args.force,
    )


if __name__ == "__main__":
    main()
