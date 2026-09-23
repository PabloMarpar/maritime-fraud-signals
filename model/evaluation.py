"""Shared evaluation population and statistics for everything that scores against the sanctions
label -- P4-0's discriminative check and P4-1's naive baseline alike.

**Why this exists.** Both modules must agree on exactly which panel rows count as positive and
negative, or a later module's "beats the baseline" claim would be comparing two different
populations without anyone noticing. Extracted from ``model.discriminative_check`` (P4-0, which
defined the population first) rather than duplicated when ``model.baseline`` (P4-1) needed the same
definition.

**Population.** Every panel row with ``imo IS NOT NULL`` (the panel's own hard modelling-population
restriction, see ``features.panel``'s docstring) AND ``NOT label_is_sanctioned_as_of_window_end`` --
vessels already under sanctions before/during this window are a different analytical question and
are excluded from BOTH classes, not folded into either one. Positive = forward-looking
(``label_is_sanctioned_after_window_end``); negative = never sanctioned at all. See
``model.discriminative_check``'s module docstring for the full reasoning.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
from sklearn.metrics import roc_auc_score

from process.foc import FOC_NAME_TO_MID_COUNTRY

POPULATION_WHERE_SQL = "imo IS NOT NULL AND NOT label_is_sanctioned_as_of_window_end"

CI_LOW_PCT = 2.5
CI_HIGH_PCT = 97.5

# Flag-of-convenience flag_country values, from process.foc's ITF-derived mapping. Sorted only for
# deterministic SQL text across runs; membership is what matters, not order.
_FOC_FLAG_VALUES = sorted(set(FOC_NAME_TO_MID_COUNTRY.values()))


def load_population(
    con: duckdb.DuckDBPyConnection, panel_path: Path, table_name: str = "_population"
) -> tuple[date, date]:
    """Create temp table `table_name` holding the P4-0 evaluation population read from
    `panel_path`, and return its (window_start, window_end).

    Raises FileNotFoundError if panel_path doesn't exist (run features.panel.build_panel first)
    and ValueError if the panel spans more than one distinct (window_start, window_end) pair --
    every caller of this function has only been validated against a single-window panel.
    """
    if not panel_path.exists():
        raise FileNotFoundError(f"No panel at {panel_path}; run features.panel.build_panel first")

    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {table_name} AS "
        f"SELECT * FROM read_parquet('{panel_path.as_posix()}') WHERE {POPULATION_WHERE_SQL}"
    )
    window_rows = con.execute(
        f"SELECT DISTINCT window_start, window_end FROM {table_name}"
    ).fetchall()
    if len(window_rows) != 1:
        raise ValueError(
            f"Expected exactly one distinct (window_start, window_end) in {panel_path}, "
            f"found {len(window_rows)} -- this module has only been validated against a "
            "single-window panel; a multi-window panel needs this check re-scoped, not "
            "blindly run over pooled windows."
        )
    return window_rows[0]


def bootstrap_auc_ci(
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


def precision_at_k(sorted_labels: Sequence[bool] | np.ndarray, k: int) -> float | None:
    """Precision among the top `k` rows of a sequence already sorted by descending priority
    (highest-risk first). None if there are fewer than k rows total -- a fixed-k cut can't be
    computed, not silently over a shorter list.
    """
    labels = np.asarray(list(sorted_labels), dtype=bool)
    if len(labels) < k:
        return None
    return float(np.mean(labels[:k]))


def n_tied_at_cutoff(sorted_scores: Sequence, k: int) -> int:
    """Count of rows sharing the same value as the row at rank k-1 (the cutoff boundary).

    For a binary rule this is typically just n_flagged whenever k < n_flagged (every flagged row
    ties on the same value) -- it does not, by itself, prove the tie-break is what determined
    precision_at_k's result. It is evidence that the top-k slice is NOT a genuine ranking among
    n_flagged equally-scored rows, so precision_at_k should be read as "precision on one arbitrary
    k-subset of the flagged rows under this rule's tie-break", not as a meaningful top-k metric on
    its own -- see model.baseline's module docstring for why its own tie-break (mmsi ascending) is
    not even a neutral arbitrary choice for this project's real flag_country distribution.
    """
    scores = list(sorted_scores)
    if not scores or k <= 0 or k > len(scores):
        return 0
    boundary = scores[k - 1]
    return sum(1 for s in scores if s == boundary)


def add_tanker_foc_columns(
    con: duckdb.DuckDBPyConnection, source_table: str, dest_table: str
) -> None:
    """Create dest_table as source_table plus r1 (tanker) and r2 (tanker + ITF flag-of-convenience)
    BOOLEAN columns -- the P4-1 baseline's own R1/R2 rules, and the subpopulation
    model.build_year_gate's G2 check restricts itself to (see that module's docstring: G2 measured
    over the WHOLE population can pass even when the coverage gap inside the tanker+FOC
    subpopulation, where R3 actually operates, is worse -- this shared definition is what lets both
    modules test the same rule instead of each re-deriving it).

    Never NULL: a NULL ship_type/flag_country fails r1/r2 (via COALESCE), it never propagates NULL.
    """
    foc_list_sql = ", ".join(f"'{v}'" for v in _FOC_FLAG_VALUES)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {dest_table} AS "
        "SELECT *, (COALESCE(ship_type, '') = 'Tanker') AS r1, "
        "(COALESCE(ship_type, '') = 'Tanker' AND COALESCE(flag_country, '') IN "
        f"({foc_list_sql})) AS r2 "
        f"FROM {source_table}"
    )
