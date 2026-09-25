from datetime import date

import duckdb
import numpy as np
import pytest

from model import isolation_forest as iso
from model import lightgbm_risk as gbm
from process.partitions import window_partition_path
from tests.test_isolation_forest import _write_panel


def _split_with_dates(dates: list[str | None]) -> iso.Split:
    n = len(dates)
    return iso.Split(
        name="s",
        mmsi=np.arange(n),
        year_month=np.array(["2024-06-01"] * n),
        labels=np.zeros(n, dtype=bool),
        r2=np.zeros(n, dtype=bool),
        features={},
        window_start=date(2024, 6, 1),
        window_end=date(2024, 6, 30),
        designation_date=np.array(
            [np.datetime64(d) if d else np.datetime64("NaT", "D") for d in dates], dtype="datetime64[D]"
        ),
    )


def test_as_of_cutoff_label_counts_only_designations_public_before_the_cutoff():
    split = _split_with_dates(
        [None, "2024-06-15", "2024-06-30", "2024-07-01", "2024-10-31", "2024-11-01", "2025-03-01"]
    )
    labels = gbm.as_of_cutoff_labels(split, np.datetime64("2024-11-01"))
    assert labels.tolist() == [False, False, False, True, True, False, False]


def test_context_variant_uses_only_r2_inputs():
    split = iso.Split(
        name="s", mmsi=np.arange(1), year_month=np.array(["2024-06-01"]),
        labels=np.zeros(1, bool), r2=np.zeros(1, bool),
        features={"n_gaps": np.zeros(1), "is_tanker": np.zeros(1), "is_foc": np.zeros(1)},
        window_start=None, window_end=None,
    )
    assert gbm.columns_for("context", split) == ["is_tanker", "is_foc"]
    assert gbm.columns_for("detectors", split) == ["n_gaps"]


def test_designation_date_never_enters_the_feature_matrix(tmp_path):
    path = window_partition_path(date(2024, 6, 1), date(2024, 6, 30), tmp_path / "panel")
    _write_panel(path, date(2024, 6, 1), date(2024, 6, 30), 50, seed=1)
    split = iso.load_split("train", path)
    for variant in gbm.VARIANTS:
        columns = gbm.columns_for(variant, split)
        assert not any("designat" in c or c.startswith("label") for c in columns)
    assert not any("designat" in c for c in split.features)


def test_reproduces_set_detects_a_learned_rule():
    flagged = np.array([True, False, True, False])
    assert gbm.reproduces_set(np.array([0.9, 0.1, 0.9, 0.1]), flagged)
    assert not gbm.reproduces_set(np.array([0.9, 0.95, 0.1, 0.1]), flagged)


def test_end_to_end_learns_a_planted_signal(tmp_path):
    root = tmp_path / "panel"
    train = window_partition_path(date(2024, 6, 1), date(2024, 6, 30), root)
    test = window_partition_path(date(2024, 11, 1), date(2024, 11, 30), root)
    _write_panel(train, date(2024, 6, 1), date(2024, 6, 30), 400, seed=1)
    _write_panel(test, date(2024, 11, 1), date(2024, 11, 30), 400, seed=2)
    out = tmp_path / "gbm.parquet"

    gbm.build_lightgbm(
        (train,), test, out, tmp_path / "scores.parquet", tmp_path / "summary.txt",
        n_bootstrap=50, n_stability_seeds=1, n_train_bootstrap=5,
    )

    con = duckdb.connect()
    try:
        rows = dict(
            con.execute(
                f"SELECT variant, auc FROM '{out.as_posix()}' ORDER BY variant"
            ).fetchall()
        )
        (n_pos,) = con.execute(
            f"SELECT DISTINCT n_train_pos FROM '{out.as_posix()}'"
        ).fetchone()
    finally:
        con.close()
    assert n_pos == 40
    assert rows["detectors"] > 0.95
    assert set(rows) == {"context", "detectors", "detectors_context"}


def test_refuses_a_training_window_that_overlaps_the_test_window(tmp_path):
    root = tmp_path / "panel"
    a = window_partition_path(date(2024, 6, 1), date(2024, 6, 30), root)
    _write_panel(a, date(2024, 6, 1), date(2024, 6, 30), 50, seed=1)
    with pytest.raises(ValueError, match="does not end before"):
        gbm.build_lightgbm((a,), a, tmp_path / "o.parquet", tmp_path / "s.parquet",
                           tmp_path / "t.txt", n_bootstrap=5, n_stability_seeds=1)
