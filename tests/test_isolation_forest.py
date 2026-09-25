from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pytest

from model import isolation_forest as iso
from process.partitions import window_partition_path


def _split(features: dict[str, list[float]], labels=None, r2=None) -> iso.Split:
    n = len(next(iter(features.values())))
    return iso.Split(
        name="s",
        mmsi=np.arange(n),
        year_month=np.array(["2024-06-01"] * n),
        labels=np.zeros(n, dtype=bool) if labels is None else np.asarray(labels, dtype=bool),
        r2=np.zeros(n, dtype=bool) if r2 is None else np.asarray(r2, dtype=bool),
        features={k: np.asarray(v, dtype=float) for k, v in features.items()},
        window_start=None,
        window_end=None,
    )


def test_feature_selection_drops_ids_labels_and_non_numeric():
    described = [
        ("mmsi", "BIGINT"),
        ("imo", "VARCHAR"),
        ("is_orphaned", "BOOLEAN"),
        ("ship_type", "VARCHAR"),
        ("n_gaps", "BIGINT"),
        ("rate_gaps_per_voyage", "DOUBLE"),
        ("is_reused", "BOOLEAN"),
        ("label_is_sanctioned_ever", "BOOLEAN"),
        ("window_end", "DATE"),
    ]
    assert iso.select_feature_columns(described) == ["n_gaps", "rate_gaps_per_voyage", "is_reused"]


def test_feature_selection_refuses_unprefixed_label_column():
    with pytest.raises(ValueError, match="n_sanctions_sources"):
        iso.select_feature_columns([("n_sanctions_sources", "BIGINT")])


def test_preprocessor_imputes_with_training_median_not_test_median():
    train = _split({"a": [1.0, 3.0, np.nan, 5.0]})
    test = _split({"a": [np.nan, 100.0, 100.0]})
    prep = iso.Preprocessor.fit(train, ["a"])
    x = prep.transform(test)
    assert x[0, 0] == pytest.approx(np.log1p(3.0))


def test_preprocessor_skips_log_for_columns_with_negative_training_values():
    train = _split({"neg": [-2.0, 0.0, 2.0], "pos": [0.0, 1.0, 9.0]})
    prep = iso.Preprocessor.fit(train, ["neg", "pos"])
    x = prep.transform(train)
    assert x[:, 0].tolist() == [-2.0, 0.0, 2.0]
    assert x[:, 1] == pytest.approx(np.log1p([0.0, 1.0, 9.0]))


def test_precision_at_k_uses_top_scores():
    labels = np.array([True, False, True, False])
    scores = np.array([0.9, 0.1, 0.8, 0.5])
    assert iso.precision_at_k(labels, scores, 2) == 1.0
    assert iso.precision_at_k(labels, scores, 3) == pytest.approx(2 / 3)
    assert iso.precision_at_k(labels, scores, 5) is None


def test_paired_bootstrap_detects_a_model_that_ranks_perfectly():
    rng = np.random.default_rng(0)
    n = 400
    labels = np.zeros(n, dtype=bool)
    labels[:40] = True
    r2 = np.zeros(n, dtype=bool)
    r2[20:80] = True  # 20 of 60 flagged are positive: precision 1/3
    scores = labels.astype(float) + rng.uniform(0, 0.1, n)
    lo, _hi = iso.paired_bootstrap_precision_diff(labels, scores, r2, 300, rng)
    assert lo > 0


def test_forest_ranks_injected_outliers_highest():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, size=(500, 4))
    x[:10] += 8
    forest = iso.fit_forest(x, seed=0, n_estimators=200)
    scores = iso.risk_scores(forest, x)
    assert set(np.argsort(-scores)[:10]) == set(range(10))


def _write_panel(path: Path, start: date, end: date, n: int, seed: int) -> None:
    """A tiny panel with the columns load_population/add_tanker_foc_columns need, where
    positives are tankers with an extreme n_draught_change_unexplained."""
    rng = np.random.default_rng(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, year_month DATE, imo VARCHAR, is_orphaned BOOLEAN, "
            "ship_type VARCHAR, flag_country VARCHAR, n_gaps BIGINT, "
            "n_draught_change_unexplained BIGINT, rate_gaps_per_voyage DOUBLE, "
            "label_is_sanctioned_as_of_window_end BOOLEAN, "
            "label_is_sanctioned_after_window_end BOOLEAN, window_start DATE, window_end DATE)"
        )
        rows = []
        for i in range(n):
            positive = i < n // 10
            rows.append(
                (
                    100000000 + i,
                    date(start.year, start.month, 1),
                    f"9{i:06d}",
                    False,
                    "Tanker" if positive or i % 5 == 0 else "Cargo",
                    "Panama" if i % 2 == 0 else "Denmark",
                    int(rng.poisson(2)),
                    int(20 + rng.poisson(5)) if positive else int(rng.poisson(0.2)),
                    None if i % 7 == 0 else float(rng.uniform(0, 1)),
                    False,
                    positive,
                    start,
                    end,
                )
            )
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' * 13)})", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def test_end_to_end_writes_metrics_scores_and_summary(tmp_path):
    root = tmp_path / "panel"
    train = window_partition_path(date(2024, 6, 1), date(2024, 6, 30), root)
    test = window_partition_path(date(2024, 11, 1), date(2024, 11, 30), root)
    _write_panel(train, date(2024, 6, 1), date(2024, 6, 30), 300, seed=1)
    _write_panel(test, date(2024, 11, 1), date(2024, 11, 30), 300, seed=2)
    out = tmp_path / "if.parquet"
    scores = tmp_path / "scores.parquet"
    summary = tmp_path / "summary.txt"

    iso.build_isolation_forest(
        train, test, out, scores, summary,
        n_estimators=100, n_bootstrap=50, n_stability_seeds=2,
    )

    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT variant, split, n_pop, n_pos, auc, verdict FROM '{out.as_posix()}' "
            "ORDER BY variant, split"
        ).fetchall()
        (n_scores,) = con.execute(f"SELECT count(*) FROM '{scores.as_posix()}'").fetchone()
    finally:
        con.close()
    assert [(r[0], r[1]) for r in rows] == [
        ("detectors", "test"),
        ("detectors", "train"),
        ("detectors_context", "test"),
        ("detectors_context", "train"),
    ]
    assert all(r[2] == 300 and r[3] == 30 for r in rows)
    assert all(r[4] > 0.9 for r in rows)
    assert n_scores == 4 * 300
    assert "Primary metric" in summary.read_text(encoding="utf-8")


def test_refuses_a_test_window_that_is_not_later(tmp_path):
    root = tmp_path / "panel"
    a = window_partition_path(date(2024, 6, 1), date(2024, 6, 30), root)
    _write_panel(a, date(2024, 6, 1), date(2024, 6, 30), 50, seed=1)
    with pytest.raises(ValueError, match="does not start after"):
        iso.build_isolation_forest(a, a, tmp_path / "o.parquet", tmp_path / "s.parquet",
                                   tmp_path / "t.txt", n_estimators=10, n_bootstrap=5,
                                   n_stability_seeds=1)
