"""Pooled walk-forward evaluation: the P0 protocol frozen in ``docs/DECISIONS.md`` (2026-09-27).

**Why this exists.** With 16-140 positives per cutoff, precision@50 has a binomial half-width of
roughly +-14 points, so no single cutoff can separate two good models. The walk-forward
evaluation (P4-3b) therefore pools every monthly cutoff, and this module is the one place that
does it, so every model is compared the same way. It computes no features and fits no models: it
takes per-cutoff test labels, cluster keys and scores that callers have already produced.

**What it computes.**

- **Expected precision at a budget.** Ties at the cutoff boundary are broken uniformly at random
  *in expectation*: rows scoring strictly above the boundary count in full, and the tied group
  at the boundary contributes its mean label times the remaining slots. A binary rule such as R2
  therefore scores its flagged-set precision at any k up to its flag count. That is the fair
  reading, unlike ``model.baseline``'s mmsi-order tie-break, whose artifact is documented there.
- **Budgets.** An ``int`` is a fixed k (the pre-registered 50/100/200). A ``str`` names a binary
  score in the same cutoff, and k is that score's flagged count (``score > 0``). This is the
  primary matched budget from ``CLAUDE.md``: R2's own alert count.
- **Pooling.** A micro-average: total expected hits over all cutoffs divided by total budget. The
  pooled ceiling, sum(min(n_pos, k)) / sum(k), goes next to it, because at R2's budget the
  ceiling is often close to the achieved value.
- **Uncertainty.** A paired *cluster* bootstrap over the union of clusters in all cutoffs. One
  resample draws clusters with replacement, and each drawn cluster's rows enter every cutoff with
  the same multiplicity. With clusters = IMO, a tanker seen in eight months is one piece of
  evidence, not eight. With the designation-package clustering (:func:`package_clusters`),
  vessels sanctioned in the same action move together. Matched budgets are recomputed inside
  every resample, and both scores see the same resample (paired).

Everything is numpy over arrays the caller has already reduced to one row per vessel per
cutoff, a few thousand rows each, so no data leaves DuckDB in bulk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

CI_LOW_PCT = 2.5
CI_HIGH_PCT = 97.5

Budget = int | str


@dataclass(frozen=True)
class Cutoff:
    """One test month: its labels, the cluster key of every row, and every score to compare
    (higher = riskier). All arrays are aligned row by row."""

    name: str
    labels: np.ndarray
    clusters: np.ndarray
    scores: Mapping[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        n = len(self.labels)
        if len(self.clusters) != n:
            raise ValueError(f"{self.name}: {len(self.clusters)} cluster keys for {n} labels")
        for name, s in self.scores.items():
            if len(s) != n:
                raise ValueError(f"{self.name}: score {name!r} has {len(s)} rows, expected {n}")


class _Ranked:
    """One score's descending order and tie levels, computed once and reused across bootstrap
    resamples (only the row weights change between resamples)."""

    def __init__(self, labels: np.ndarray, scores: np.ndarray) -> None:
        self.order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")
        s = np.asarray(scores, dtype=float)[self.order]
        self.labels = np.asarray(labels, dtype=float)[self.order]
        self.starts = np.flatnonzero(np.r_[True, s[1:] != s[:-1]]) if len(s) else np.array([], int)

    def hits(self, weights: np.ndarray, k: float) -> float | None:
        """Expected hits among the top-k units of weight; None if k is not in (0, total]."""
        w = weights[self.order]
        if k <= 0 or k > w.sum():
            return None
        level_w = np.add.reduceat(w, self.starts)
        level_h = np.add.reduceat(w * self.labels, self.starts)
        cum_w = np.cumsum(level_w)
        # First level whose cumulative weight reaches k. It always has positive weight: a
        # zero-weight level leaves the cumulative sum unchanged, so it cannot be the first to
        # reach k.
        i = int(np.searchsorted(cum_w, k, side="left"))
        before_w = cum_w[i - 1] if i > 0 else 0.0
        before_h = level_h[:i].sum()
        return float(before_h + (k - before_w) * level_h[i] / level_w[i])


def expected_hits_at_k(
    labels: np.ndarray, scores: np.ndarray, k: float, weights: np.ndarray | None = None
) -> float | None:
    """Expected positives among the k highest-scoring rows, ties at the boundary broken uniformly
    at random. `weights` are row multiplicities (e.g. bootstrap counts; default 1 each). None if
    k is not in (0, total weight]."""
    w = np.ones(len(labels)) if weights is None else np.asarray(weights, dtype=float)
    return _Ranked(labels, scores).hits(w, float(k))


def expected_precision_at_k(
    labels: np.ndarray, scores: np.ndarray, k: float, weights: np.ndarray | None = None
) -> float | None:
    hits = expected_hits_at_k(labels, scores, k, weights)
    return None if hits is None else hits / k


def _budget_k(cutoff: Cutoff, budget: Budget, weights: np.ndarray) -> float:
    if isinstance(budget, str):
        return float(weights[np.asarray(cutoff.scores[budget]) > 0].sum())
    return float(budget)


def _pooled(
    cutoffs: Sequence[Cutoff],
    ranked: Sequence[_Ranked],
    budget: Budget,
    weights: Sequence[np.ndarray],
) -> tuple[float, float] | None:
    """(total expected hits, total budget) over cutoffs where the budget is valid."""
    hits_total = k_total = 0.0
    for c, r, w in zip(cutoffs, ranked, weights):
        k = _budget_k(c, budget, w)
        hits = r.hits(w, k)
        if hits is None:
            continue
        hits_total += hits
        k_total += k
    return None if k_total == 0 else (hits_total, k_total)


def pooled_precision(cutoffs: Sequence[Cutoff], score: str, budget: Budget) -> float | None:
    """Micro-averaged expected precision of `score` at `budget` over every cutoff."""
    ranked = [_Ranked(c.labels, c.scores[score]) for c in cutoffs]
    ones = [np.ones(len(c.labels)) for c in cutoffs]
    pooled = _pooled(cutoffs, ranked, budget, ones)
    return None if pooled is None else pooled[0] / pooled[1]


def pooled_ceiling(cutoffs: Sequence[Cutoff], budget: Budget) -> float | None:
    """sum(min(n_pos, k)) / sum(k): the best any score could do at this budget."""
    best = total = 0.0
    for c in cutoffs:
        k = _budget_k(c, budget, np.ones(len(c.labels)))
        if k <= 0 or k > len(c.labels):
            continue
        best += min(float(np.sum(c.labels)), k)
        total += k
    return None if total == 0 else best / total


def per_cutoff_precision(
    cutoffs: Sequence[Cutoff], score: str, budget: Budget
) -> list[dict[str, object]]:
    """One row per cutoff: name, k, n_rows, n_pos, precision (None if k is invalid), ceiling."""
    rows = []
    for c in cutoffs:
        ones = np.ones(len(c.labels))
        k = _budget_k(c, budget, ones)
        n_pos = int(np.sum(c.labels))
        precision = expected_precision_at_k(c.labels, c.scores[score], k) if k > 0 else None
        rows.append(
            {
                "cutoff": c.name,
                "k": k,
                "n_rows": len(c.labels),
                "n_pos": n_pos,
                "precision": precision,
                "ceiling": min(n_pos, k) / k if k > 0 else None,
            }
        )
    return rows


def _cluster_index(cutoffs: Sequence[Cutoff]) -> tuple[int, list[np.ndarray]]:
    """Map every row to an index into the union of cluster keys across all cutoffs."""
    keys = np.unique(np.concatenate([np.asarray(c.clusters).astype(str) for c in cutoffs]))
    return len(keys), [
        np.searchsorted(keys, np.asarray(c.clusters).astype(str)) for c in cutoffs
    ]


def _draw(n_clusters: int, idx: Sequence[np.ndarray], rng: np.random.Generator) -> list[np.ndarray]:
    counts = np.bincount(rng.integers(0, n_clusters, size=n_clusters), minlength=n_clusters)
    return [counts[i].astype(float) for i in idx]


def resample_weights(
    cutoffs: Sequence[Cutoff], rng: np.random.Generator
) -> list[np.ndarray]:
    """One cluster-bootstrap draw: per-row multiplicities in every cutoff, where rows sharing a
    cluster key get the same multiplicity in every cutoff."""
    n_clusters, idx = _cluster_index(cutoffs)
    return _draw(n_clusters, idx, rng)


def cluster_bootstrap_pooled_diff(
    cutoffs: Sequence[Cutoff],
    score_a: str,
    score_b: str,
    budget: Budget,
    n_bootstrap: int,
    rng: np.random.Generator,
) -> tuple[float | None, float, float]:
    """(point, 2.5%, 97.5%) of pooled precision(score_a) - pooled precision(score_b) at `budget`,
    under the paired cluster bootstrap described in the module docstring."""
    ranked_a = [_Ranked(c.labels, c.scores[score_a]) for c in cutoffs]
    ranked_b = [_Ranked(c.labels, c.scores[score_b]) for c in cutoffs]

    def diff(weights: Sequence[np.ndarray]) -> float | None:
        a = _pooled(cutoffs, ranked_a, budget, weights)
        b = _pooled(cutoffs, ranked_b, budget, weights)
        if a is None or b is None:
            return None
        return a[0] / a[1] - b[0] / b[1]

    point = diff([np.ones(len(c.labels)) for c in cutoffs])
    n_clusters, idx = _cluster_index(cutoffs)
    diffs = []
    for _ in range(n_bootstrap):
        d = diff(_draw(n_clusters, idx, rng))
        if d is not None:
            diffs.append(d)
    if not diffs:
        return (point, float("nan"), float("nan"))
    return (
        point,
        float(np.percentile(diffs, CI_LOW_PCT)),
        float(np.percentile(diffs, CI_HIGH_PCT)),
    )


def package_clusters(
    imo: np.ndarray, is_positive: np.ndarray, package: np.ndarray
) -> np.ndarray:
    """The secondary clustering: a positive's key is its designation package (e.g.
    ``f"{source}|{designation_date}"`` of its earliest designation), and a negative's key is its
    IMO. Vessels designated in the same action therefore resample together."""
    imo = np.asarray(imo).astype(str)
    package = np.asarray(package).astype(str)
    return np.where(np.asarray(is_positive, dtype=bool), "pkg:" + package, "imo:" + imo)
