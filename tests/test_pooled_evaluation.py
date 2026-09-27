"""Unit tests for model.pooled_evaluation. Synthetic arrays only; nothing here touches data/."""

from __future__ import annotations

import numpy as np
import pytest

from model import pooled_evaluation as pe


def _cutoff(name, labels, clusters=None, **scores):
    labels = np.asarray(labels, dtype=bool)
    if clusters is None:
        clusters = [f"{name}-{i}" for i in range(len(labels))]
    return pe.Cutoff(
        name, labels, np.asarray(clusters), {k: np.asarray(v, float) for k, v in scores.items()}
    )


def test_expected_hits_without_ties_is_plain_top_k_count():
    labels = np.array([1, 0, 1, 0, 1], bool)
    scores = np.array([5, 4, 3, 2, 1], float)
    assert pe.expected_hits_at_k(labels, scores, 3) == pytest.approx(2.0)
    assert pe.expected_precision_at_k(labels, scores, 2) == pytest.approx(0.5)


def test_binary_rule_scores_its_flagged_set_precision_at_any_smaller_k():
    # 4 flagged rows, 1 of them positive -> 0.25 at every k <= 4, regardless of row order.
    labels = np.array([0, 0, 0, 1, 1, 0], bool)
    rule = np.array([1, 1, 1, 1, 0, 0], float)
    for k in (1, 2, 3, 4):
        assert pe.expected_precision_at_k(labels, rule, k) == pytest.approx(0.25)


def test_ties_straddling_the_boundary_use_the_tied_group_mean():
    labels = np.array([1, 0, 1, 1, 0], bool)
    scores = np.array([9, 5, 5, 5, 5], float)
    # Top row (hit) + 1 slot from a tied group of 4 with 2 hits -> 1 + 0.5.
    assert pe.expected_hits_at_k(labels, scores, 2) == pytest.approx(1.5)


def test_integer_weights_equal_duplicated_rows():
    labels = np.array([1, 0, 1, 0], bool)
    scores = np.array([4, 3, 3, 1], float)
    weights = np.array([2, 1, 3, 0], float)
    dup_labels = np.repeat(labels, weights.astype(int))
    dup_scores = np.repeat(scores, weights.astype(int))
    for k in (1, 2, 3, 4, 5, 6):
        assert pe.expected_hits_at_k(labels, scores, k, weights) == pytest.approx(
            pe.expected_hits_at_k(dup_labels, dup_scores, k)
        )


def test_budget_outside_the_population_is_none():
    labels = np.array([1, 0], bool)
    scores = np.array([1, 0], float)
    assert pe.expected_hits_at_k(labels, scores, 3) is None
    assert pe.expected_hits_at_k(labels, scores, 0) is None


def test_pooled_precision_is_a_micro_average_over_cutoffs():
    a = _cutoff("a", [1, 1, 0, 0], m=[4, 3, 2, 1])  # k=2 -> 2 hits
    b = _cutoff("b", [0, 0, 1, 1, 0, 0], m=[6, 5, 4, 3, 2, 1])  # k=2 -> 0 hits
    assert pe.pooled_precision([a, b], "m", 2) == pytest.approx(0.5)


def test_matched_budget_uses_each_cutoffs_own_rule_count():
    a = _cutoff("a", [1, 0, 1, 0], m=[4, 3, 2, 1], r2=[1, 1, 1, 0])  # k=3: 2 hits
    b = _cutoff("b", [1, 0], m=[2, 1], r2=[1, 0])  # k=1: 1 hit
    assert pe.pooled_precision([a, b], "m", "r2") == pytest.approx(3 / 4)
    assert pe.pooled_precision([a, b], "r2", "r2") == pytest.approx(3 / 4)


def test_pooled_ceiling():
    a = _cutoff("a", [1, 0, 0, 0], r2=[1, 1, 1, 1])  # k=4, 1 positive
    b = _cutoff("b", [1, 1, 1], r2=[1, 1, 0])  # k=2, 3 positives
    assert pe.pooled_ceiling([a, b], "r2") == pytest.approx((1 + 2) / (4 + 2))
    assert pe.pooled_ceiling([a, b], 2) == pytest.approx((1 + 2) / (2 + 2))


def test_per_cutoff_rows_report_k_positives_precision_and_ceiling():
    rows = pe.per_cutoff_precision([_cutoff("a", [1, 0, 0], m=[3, 2, 1])], "m", 2)
    assert rows == [
        {"cutoff": "a", "k": 2.0, "n_rows": 3, "n_pos": 1, "precision": 0.5, "ceiling": 0.5}
    ]


def test_rows_sharing_a_cluster_get_the_same_weight_in_every_cutoff():
    a = _cutoff("a", [0, 0, 0], clusters=["v1", "v2", "v3"])
    b = _cutoff("b", [0, 0, 0], clusters=["v3", "v1", "v4"])
    rng = np.random.default_rng(0)
    for _ in range(50):
        wa, wb = pe.resample_weights([a, b], rng)
        assert wa[0] == wb[1]  # v1
        assert wa[2] == wb[0]  # v3


def test_bootstrap_of_a_score_against_itself_is_exactly_zero():
    a = _cutoff("a", [1, 0, 1, 0, 0, 1], m=[6, 5, 4, 3, 2, 1])
    point, lo, hi = pe.cluster_bootstrap_pooled_diff(
        [a], "m", "m", 3, n_bootstrap=100, rng=np.random.default_rng(0)
    )
    assert (point, lo, hi) == (0.0, 0.0, 0.0)


def test_bootstrap_detects_a_clearly_better_score():
    rng = np.random.default_rng(1)
    cutoffs = []
    for m in range(4):
        labels = rng.random(400) < 0.1
        perfect = labels + rng.random(400) * 0.1
        noise = rng.random(400)
        clusters = [f"v{i}" for i in range(400)]  # same vessels every month
        cutoffs.append(_cutoff(str(m), labels, clusters, good=perfect, bad=noise))
    point, lo, _hi = pe.cluster_bootstrap_pooled_diff(
        cutoffs, "good", "bad", 20, n_bootstrap=200, rng=np.random.default_rng(0)
    )
    assert point > 0.5 and lo > 0


def test_bootstrap_is_reproducible_with_a_seed():
    a = _cutoff(
        "a", [1, 0, 1, 0, 0, 1, 0, 0], m=[8, 7, 6, 5, 4, 3, 2, 1], r2=[1, 0, 1, 1, 0, 0, 1, 0]
    )
    run = lambda: pe.cluster_bootstrap_pooled_diff(
        [a], "m", "r2", "r2", n_bootstrap=50, rng=np.random.default_rng(7)
    )
    assert run() == run()


def test_package_clusters_group_positives_by_package_and_negatives_by_imo():
    keys = pe.package_clusters(
        imo=np.array(["1", "2", "3"]),
        is_positive=np.array([True, True, False]),
        package=np.array(["OFAC|2025-01-10", "OFAC|2025-01-10", ""]),
    )
    assert list(keys) == ["pkg:OFAC|2025-01-10", "pkg:OFAC|2025-01-10", "imo:3"]


def test_misaligned_arrays_are_rejected():
    with pytest.raises(ValueError):
        pe.Cutoff("x", np.array([True, False]), np.array(["a"]), {})
    with pytest.raises(ValueError):
        pe.Cutoff("x", np.array([True]), np.array(["a"]), {"m": np.array([1.0, 2.0])})
