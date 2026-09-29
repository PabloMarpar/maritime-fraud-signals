"""P4-3b: the walk-forward evaluation over every built monthly window, read through the frozen P0
protocol (``model.pooled_evaluation``).

**What is frozen, and where.** Everything this module does was pre-registered in
``docs/DECISIONS.md`` before any archive month was scored: the P0 protocol (2026-09-27), the
P4-3c static column set and logistic model (2026-09-27), P4-3's LightGBM variants and PARAMS
(2026-09-25), and the walk-forward mechanics plus the TabICLv2 challenger (2026-09-29). Nothing
here is tuned on a test month; a change to any variant is a new, separately named variant.

**Cutoffs and training rows.** One cutoff per built window W (cutoff = W's ``window_start``).
Training uses every window that ended before the cutoff (an expanding window). Each training
window keeps its own P4-0 population and gets P4-3's as-of-cutoff label: positive only if its
vessel was designated in (that window's end, cutoff), i.e. the designation was already public.
Windows are stacked row-wise, so a vessel seen in several months contributes several rows. A
cutoff with no training positive is "not scorable" for learned models and is reported as such.

**Test rows and labels.** W's own P4-0 population. Primary label: designated after W's
``window_end`` (up to the 2026-09-21 sanctions snapshot). Secondary: designated within 12 months
of ``window_end``, which gives every cutoff the same horizon.

**Scores** (higher = riskier):

- ``r2``: tanker under a flag of convenience, the project's bar. Also the primary budget: k is
  R2's own flag count in each cutoff.
- ``static_logistic``: the P4-3c model (logistic regression, C=0.3, balanced classes,
  training-median imputation, standardized) on the frozen static columns. The primary challenger.
- ``static_lightgbm``: P4-3's PARAMS on the same columns (secondary, P4-3c).
- ``lgbm_context`` / ``lgbm_detectors`` / ``lgbm_detectors_context`` /
  ``lgbm_detectors_context_noexp``: P4-3's variants over the panel's detector columns.
- ``tabicl``: TabICLv2 (P4-3i). Contexts of all training positives plus 5,000 randomly drawn
  training negatives, 10 seeds, mean P(positive). Package defaults, checkpoint pinned.
- ``rule_tanker_dest`` (descriptive only, NOT pre-registered as a variant): the unfitted P4-3c
  rule ``tanker AND (dest_russia OR dest_south_route)``, printed for context.

**Uncertainty and verdicts** come only from ``model.pooled_evaluation``: expected precision under
random tie-breaking, micro-averaged pooling over the primary cutoffs (2024-08..2025-02) next to
the ceiling, and the paired cluster bootstrap by IMO (secondary: by designation package). "A beats
B" iff the IMO-clustered interval of the pooled difference at R2's budget excludes 0. The pooled
result is also reported without the 2024-11 cutoff, the one test month examined before (P4-3,
P4-3c).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from model.evaluation import POPULATION_WHERE_SQL
from model.isolation_forest import PANEL_ROOT, Split, load_split
from model.lightgbm_risk import as_of_cutoff_labels, columns_for, fit_model
from model.pooled_evaluation import (
    Cutoff,
    cluster_bootstrap_pooled_diff,
    package_clusters,
    per_cutoff_precision,
    pooled_ceiling,
    pooled_precision,
)
from process.partitions import atomic_write_parquet

logger = logging.getLogger(__name__)

STATIC_ROOT = Path("data/processed/static")
SANCTIONS_MATCHES_PATH = Path("data/identity/sanctions_matches.parquet")
SCORES_PATH = Path("data/processed/walk_forward_scores.parquet")
SUMMARY_PATH = Path("outputs/walk_forward_summary.txt")

# P4-3c's frozen feature set (docs/DECISIONS.md 2026-09-27). is_tanker/is_foc come from the panel
# (model.isolation_forest.CONTEXT_FEATURES_SQL), imo_serial from the panel's imo, the rest from
# features.static.
STATIC_COLUMNS = (
    "is_tanker",
    "is_foc",
    "length_m",
    "width_m",
    "max_draught_m",
    "draught_range_m",
    "imo_serial",
    "dest_russia",
    "dest_south_route",
    "dest_for_orders",
    "n_destinations",
)
_FROM_STATIC_TABLE = STATIC_COLUMNS[2:6] + STATIC_COLUMNS[7:]

# P4-3's no-exposure variant drops exactly these four columns (docs/DECISIONS.md 2026-09-25).
EXPOSURE_COLUMNS = ("n_observed_hours", "n_observed_days", "total_message_count", "voyage_count")

LOGISTIC_C = 0.3
LOGISTIC_MAX_ITER = 5000

TABICL_CHECKPOINT = "tabicl-classifier-v2-20260212.ckpt"
TABICL_N_NEGATIVES = 5000
TABICL_SEEDS = tuple(range(10))

PRIMARY_FIRST_CUTOFF = date(2024, 8, 1)
EXAMINED_CUTOFF = "2024-11-01"
SECONDARY_HORIZON_DAYS = 365
BUDGETS: tuple[int | str, ...] = ("r2", 50, 100, 200)
N_BOOTSTRAP = 2000
SEED = 0


@dataclass
class Window:
    """One built window's modelling population: the panel split plus the static columns, the
    IMO (bootstrap cluster key) and the designation package (secondary cluster key)."""

    split: Split
    imo: np.ndarray
    package: np.ndarray

    @property
    def start(self) -> date:
        return self.split.window_start

    @property
    def end(self) -> date:
        return self.split.window_end


def _window_dirs(root: Path) -> list[Path]:
    """Monthly window partitions only: the 2-day P3-4/A4 validation window is skipped."""
    dirs = []
    for d in sorted(root.glob("window=*")):
        start, end = (date.fromisoformat(s) for s in d.name.removeprefix("window=").split("_"))
        if (end - start).days >= 20:
            dirs.append(d)
    return dirs


def load_window(panel_path: Path, static_path: Path, matches_path: Path) -> Window:
    """Load one window, joining the static columns and the designation package by mmsi, in
    ``load_split``'s row order (mmsi, year_month)."""
    split = load_split(panel_path.parent.name, panel_path)
    con = duckdb.connect()
    try:
        static_sql = ", ".join(f"CAST(s.{c} AS DOUBLE) AS {c}" for c in _FROM_STATIC_TABLE)
        data = con.execute(
            f"""
            WITH pop AS (
                SELECT mmsi, year_month, imo FROM read_parquet('{panel_path.as_posix()}')
                WHERE {POPULATION_WHERE_SQL}
            ),
            first_desig AS (
                SELECT imo, min(designation_date) AS d FROM read_parquet(
                    '{matches_path.as_posix()}') WHERE designation_date IS NOT NULL GROUP BY imo
            ),
            package AS (
                SELECT m.imo, strftime(f.d, '%Y-%m-%d') || '|'
                    || string_agg(DISTINCT m.source, '+' ORDER BY m.source) AS package
                FROM read_parquet('{matches_path.as_posix()}') m
                JOIN first_desig f ON m.imo = f.imo AND m.designation_date = f.d
                GROUP BY m.imo, f.d
            )
            SELECT p.mmsi, p.imo, CAST(TRY_CAST(p.imo AS BIGINT) AS DOUBLE) AS imo_serial,
                   coalesce(k.package, '') AS package, {static_sql}
            FROM pop p
            LEFT JOIN read_parquet('{static_path.as_posix()}') s ON p.mmsi = s.mmsi
            LEFT JOIN package k ON p.imo = k.imo
            ORDER BY p.mmsi, p.year_month
            """
        ).fetchnumpy()
    finally:
        con.close()
    if not np.array_equal(np.asarray(data["mmsi"]), split.mmsi):
        raise ValueError(f"{panel_path}: static join changed the row set or order")
    for c in ("imo_serial", *_FROM_STATIC_TABLE):
        split.features[c] = np.ma.filled(np.ma.asarray(data[c]).astype(float), np.nan)
    return Window(
        split=split,
        imo=np.asarray(data["imo"]).astype(str),
        package=np.asarray(data["package"]).astype(str),
    )


def training_set(
    windows: Sequence[Window], cutoff: date, columns: Sequence[str]
) -> tuple[np.ndarray, np.ndarray]:
    """(X, y) stacked over every window ending before `cutoff`, with as-of-cutoff labels."""
    train = [w for w in windows if w.end < cutoff]
    if not train:
        return np.empty((0, len(columns))), np.empty(0, dtype=bool)
    c = np.datetime64(cutoff, "D")
    x = np.vstack([np.column_stack([w.split.features[k] for k in columns]) for w in train])
    y = np.concatenate([as_of_cutoff_labels(w.split, c) for w in train])
    return x, y


def test_matrix(window: Window, columns: Sequence[str]) -> np.ndarray:
    return np.column_stack([window.split.features[k] for k in columns])


def _impute(x_train: np.ndarray, x_test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Median imputation with medians from the training rows only (0 for an all-NaN column)."""
    with np.errstate(all="ignore"):
        med = np.nanmedian(x_train, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    return np.where(np.isnan(x_train), med, x_train), np.where(np.isnan(x_test), med, x_test)


def logistic_scores(x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray) -> np.ndarray:
    """The P4-3c static model: median-imputed, standardized, logistic C=0.3, balanced."""
    xtr, xte = _impute(x_train, x_test)
    scaler = StandardScaler().fit(xtr)
    model = LogisticRegression(
        C=LOGISTIC_C, class_weight="balanced", max_iter=LOGISTIC_MAX_ITER
    ).fit(scaler.transform(xtr), y_train)
    return model.decision_function(scaler.transform(xte))


def lightgbm_scores(
    x_train: np.ndarray, y_train: np.ndarray, x_test: np.ndarray, seed: int = SEED
) -> np.ndarray:
    """P4-3's PARAMS; LightGBM handles NaN natively, so no imputation."""
    return fit_model(x_train, y_train, seed).predict_proba(x_test)[:, 1]


def _tabicl_factory(seed: int):
    from tabicl import TabICLClassifier

    return TabICLClassifier(checkpoint_version=TABICL_CHECKPOINT, random_state=seed)


def tabicl_scores(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    seeds: Sequence[int] = TABICL_SEEDS,
    n_negatives: int = TABICL_N_NEGATIVES,
    factory: Callable[[int], object] = _tabicl_factory,
) -> np.ndarray:
    """Mean P(positive) over one context per seed: every training positive plus `n_negatives`
    negatives drawn uniformly without replacement (all of them if fewer). Training-median
    imputation, no scaling, no oversampling (P4-3i, docs/DECISIONS.md 2026-09-29)."""
    xtr, xte = _impute(x_train, x_test)
    pos = np.flatnonzero(y_train)
    neg = np.flatnonzero(~y_train)
    out = []
    for seed in seeds:
        rng = np.random.default_rng(seed)
        chosen = neg if len(neg) <= n_negatives else rng.choice(neg, n_negatives, replace=False)
        idx = np.sort(np.concatenate([pos, chosen]))
        clf = factory(seed)
        clf.fit(xtr[idx], y_train[idx].astype(int))
        proba = clf.predict_proba(xte)
        out.append(proba[:, list(clf.classes_).index(1)])
    return np.mean(out, axis=0)


def secondary_labels(split: Split, horizon_days: int = SECONDARY_HORIZON_DAYS) -> np.ndarray:
    """Designated within `horizon_days` after the window's end."""
    d = split.designation_date
    end = np.datetime64(split.window_end, "D")
    known = ~np.isnat(d)
    out = np.zeros(len(d), dtype=bool)
    out[known] = (d[known] > end) & (d[known] <= end + np.timedelta64(horizon_days, "D"))
    return out


def detector_columns(windows: Sequence[Window], variant: str) -> list[str]:
    """P4-3's variant columns, restricted to those present in every window. The no-exposure
    variant is ``detectors_context`` minus :data:`EXPOSURE_COLUMNS`."""
    base = "detectors_context" if variant == "detectors_context_noexp" else variant
    common = set.intersection(*(set(columns_for(base, w.split)) for w in windows))
    cols = [c for c in columns_for(base, windows[-1].split) if c in common]
    if variant == "detectors_context_noexp":
        cols = [c for c in cols if c not in EXPOSURE_COLUMNS]
    return cols


def score_cutoff(
    windows: Sequence[Window], test: Window, use_tabicl: bool
) -> tuple[dict[str, np.ndarray], dict[str, object]]:
    """Every score for one test window, plus bookkeeping (training rows/positives)."""
    cutoff = test.start
    f = test.split.features
    scores: dict[str, np.ndarray] = {
        "r2": test.split.r2.astype(float),
        "rule_tanker_dest": (
            (f["is_tanker"] > 0)
            & ((np.nan_to_num(f["dest_russia"]) > 0) | (np.nan_to_num(f["dest_south_route"]) > 0))
        ).astype(float),
    }
    x_train, y_train = training_set(windows, cutoff, STATIC_COLUMNS)
    info: dict[str, object] = {
        "n_train_windows": sum(w.end < cutoff for w in windows),
        "n_train_rows": len(y_train),
        "n_train_pos": int(y_train.sum()),
    }
    if y_train.sum() == 0:
        return scores, info
    x_test = test_matrix(test, STATIC_COLUMNS)
    scores["static_logistic"] = logistic_scores(x_train, y_train, x_test)
    scores["static_lightgbm"] = lightgbm_scores(x_train, y_train, x_test)
    for variant in ("context", "detectors", "detectors_context", "detectors_context_noexp"):
        cols = detector_columns(windows, variant)
        xtr, ytr = training_set(windows, cutoff, cols)
        scores[f"lgbm_{variant}"] = lightgbm_scores(xtr, ytr, test_matrix(test, cols))
    if use_tabicl:
        scores["tabicl"] = tabicl_scores(x_train, y_train, x_test)
    return scores, info


def _fmt(v: float | None, spec: str = ".3f") -> str:
    return "  n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else format(v, spec)


def _report(
    cutoffs_primary: list[Cutoff],
    cutoffs_all: list[Cutoff],
    cutoffs_pkg: list[Cutoff],
    cutoffs_secondary: list[Cutoff],
    infos: dict[str, dict],
    models: list[str],
    comparisons: list[tuple[str, str]],
    n_bootstrap: int,
    seed: int,
) -> str:
    lines = ["P4-3b walk-forward (protocol: docs/DECISIONS.md 2026-09-27 P0, 2026-09-29)", ""]
    lines.append("Training per cutoff (expanding window, as-of-cutoff labels):")
    for name, info in infos.items():
        lines.append(
            f"  {name}: {info['n_train_windows']} window(s), {info['n_train_rows']} rows, "
            f"{info['n_train_pos']} positives; test {info['n_test']} rows, "
            f"{info['n_test_pos']} positives"
        )
    lines.append("")
    primary_names = [c.name for c in cutoffs_primary]
    lines.append(f"Primary pooled cutoffs: {', '.join(primary_names)}")
    for budget in BUDGETS:
        label = "R2's own count" if budget == "r2" else f"k={budget}"
        lines += ["", f"=== Budget: {label} ==="]
        lines.append(
            "  per cutoff (precision; all cutoffs, * = outside the primary pool):"
        )
        header = "    " + f"{'model':30s}" + "".join(f"{c.name[:7]:>9s}" for c in cutoffs_all)
        lines.append(header)
        for m in models:
            vals = []
            for c in cutoffs_all:
                if m not in c.scores:
                    vals.append(f"{'--':>9s}")
                    continue
                p = per_cutoff_precision([c], m, budget)[0]["precision"]
                vals.append(f"{_fmt(p):>9s}")
            lines.append("    " + f"{m:30s}" + "".join(vals))
        ceil = [per_cutoff_precision([c], "r2", budget)[0]["ceiling"] for c in cutoffs_all]
        lines.append("    " + f"{'(ceiling)':30s}" + "".join(f"{_fmt(v):>9s}" for v in ceil))
        lines.append(
            f"  pooled over primary cutoffs (ceiling {_fmt(pooled_ceiling(cutoffs_primary, budget))}):"
        )
        for m in models:
            if all(m in c.scores for c in cutoffs_primary):
                lines.append(f"    {m:30s} {_fmt(pooled_precision(cutoffs_primary, m, budget))}")
        lines.append("  pooled difference, paired IMO-clustered bootstrap 95% CI:")
        rng = np.random.default_rng(seed)
        for a, b in comparisons:
            if not all(a in c.scores and b in c.scores for c in cutoffs_primary):
                continue
            pt, lo, hi = cluster_bootstrap_pooled_diff(cutoffs_primary, a, b, budget, n_bootstrap, rng)
            verdict = "WINS" if lo > 0 else ("LOSES" if hi < 0 else "no significant difference")
            lines.append(
                f"    {a} - {b}: {_fmt(pt, '+.3f')} [{_fmt(lo, '+.3f')}, {_fmt(hi, '+.3f')}]"
                f"  -> {verdict}"
            )
    lines += ["", "=== Robustness at the primary budget (R2's own count) ==="]
    no_nov = [c for c in cutoffs_primary if c.name != EXAMINED_CUTOFF]
    for title, cs in (
        ("package-clustered bootstrap", cutoffs_pkg),
        ("secondary label (designated within 12 months)", cutoffs_secondary),
        (f"without {EXAMINED_CUTOFF} (examined before)", no_nov),
    ):
        lines.append(f"  {title}:")
        rng = np.random.default_rng(seed)
        for a, b in comparisons:
            if not all(a in c.scores and b in c.scores for c in cs):
                continue
            pa, pb = pooled_precision(cs, a, "r2"), pooled_precision(cs, b, "r2")
            pt, lo, hi = cluster_bootstrap_pooled_diff(cs, a, b, "r2", n_bootstrap, rng)
            lines.append(
                f"    {a} {_fmt(pa)} vs {b} {_fmt(pb)}: {_fmt(pt, '+.3f')} "
                f"[{_fmt(lo, '+.3f')}, {_fmt(hi, '+.3f')}]"
            )
    lines += [
        "",
        "Notes: rule_tanker_dest is descriptive only (not a pre-registered variant). Scores are",
        "rankings, not probabilities. Positives are vessels on the 2026-09-21 sanctions snapshot.",
    ]
    return "\n".join(lines) + "\n"


def run_walk_forward(
    panel_root: Path = PANEL_ROOT,
    static_root: Path = STATIC_ROOT,
    matches_path: Path = SANCTIONS_MATCHES_PATH,
    scores_path: Path = SCORES_PATH,
    summary_path: Path = SUMMARY_PATH,
    use_tabicl: bool = True,
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = SEED,
) -> Path:
    dirs = _window_dirs(panel_root)
    windows = [
        load_window(d / "part-0.parquet", static_root / d.name / "part-0.parquet", matches_path)
        for d in dirs
    ]
    logger.info("Loaded %d windows: %s", len(windows), ", ".join(d.name for d in dirs))

    cut_all, cut_pkg, cut_sec, infos = [], [], [], {}
    score_parts = []
    for test in windows[1:]:
        name = test.start.isoformat()
        scores, info = score_cutoff(windows, test, use_tabicl)
        labels = test.split.labels
        info.update(n_test=len(labels), n_test_pos=int(labels.sum()))
        infos[name] = info
        logger.info("cutoff %s: %s", name, info)
        cut_all.append(Cutoff(name, labels, test.imo, scores))
        cut_pkg.append(
            Cutoff(name, labels, package_clusters(test.imo, labels, test.package), scores)
        )
        cut_sec.append(Cutoff(name, secondary_labels(test.split), test.imo, scores))
        for m, s in scores.items():
            score_parts.append(
                pd.DataFrame(
                    {"cutoff": name, "model": m, "mmsi": test.split.mmsi.astype("int64"),
                     "score": np.asarray(s, dtype=float), "label": labels}
                )
            )

    primary = [c for c in cut_all if date.fromisoformat(c.name) >= PRIMARY_FIRST_CUTOFF]
    keep = {c.name for c in primary}
    models = [
        "r2", "static_logistic", "static_lightgbm", "tabicl", "lgbm_context", "lgbm_detectors",
        "lgbm_detectors_context", "lgbm_detectors_context_noexp", "rule_tanker_dest",
    ]
    models = [m for m in models if any(m in c.scores for c in cut_all)]
    comparisons = [(m, "r2") for m in models if m not in ("r2", "rule_tanker_dest")]
    if "tabicl" in models:
        comparisons.append(("tabicl", "static_logistic"))
    comparisons.append(("static_lightgbm", "static_logistic"))
    summary = _report(
        primary,
        cut_all,
        [c for c in cut_pkg if c.name in keep],
        [c for c in cut_sec if c.name in keep],
        infos,
        models,
        comparisons,
        n_bootstrap,
        seed,
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")

    con = duckdb.connect()
    try:
        con.register("s", pd.concat(score_parts, ignore_index=True))
        scores_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(con, "SELECT * FROM s ORDER BY cutoff, model, mmsi", scores_path)
    finally:
        con.close()
    logger.info("Wrote %s and %s", summary_path, scores_path)
    return summary_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--no-tabicl", action="store_true", help="Skip the TabICLv2 challenger")
    p.add_argument("--n-bootstrap", type=int, default=N_BOOTSTRAP)
    p.add_argument("--summary", default=str(SUMMARY_PATH))
    p.add_argument("--scores", default=str(SCORES_PATH))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    run_walk_forward(
        use_tabicl=not args.no_tabicl,
        n_bootstrap=args.n_bootstrap,
        summary_path=Path(args.summary),
        scores_path=Path(args.scores),
    )


if __name__ == "__main__":
    main()
