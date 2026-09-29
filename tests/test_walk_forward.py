"""Synthetic tests for model.walk_forward: training-set construction under the as-of-cutoff
label, the TabICL context recipe (with a fake classifier), the secondary label and the window
discovery. The real-data check (reproducing P4-3c's June -> November numbers) lives in
docs/DECISIONS.md, not here."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import numpy as np
import pytest

from model.isolation_forest import Split
from model.walk_forward import (
    Window,
    _window_dirs,
    detector_columns,
    logistic_scores,
    secondary_labels,
    tabicl_scores,
    training_set,
)


def _window(start: date, end: date, desig: list[str | None], feature: list[float]) -> Window:
    n = len(desig)
    d = np.array(
        [np.datetime64(x, "D") if x else np.datetime64("NaT", "D") for x in desig],
        dtype="datetime64[D]",
    )
    split = Split(
        name=str(start),
        mmsi=np.arange(n),
        year_month=np.array([str(start)] * n),
        labels=np.zeros(n, dtype=bool),
        r2=np.zeros(n, dtype=bool),
        features={"f": np.asarray(feature, dtype=float)},
        window_start=start,
        window_end=end,
        designation_date=d,
    )
    return Window(
        split=split,
        imo=np.array([str(i) for i in range(n)]),
        package=np.array([""] * n),
        panel_columns=frozenset(split.features),
    )


APR = _window(date(2024, 4, 1), date(2024, 4, 30), ["2024-05-10", "2024-09-01", None], [1, 2, 3])
MAY = _window(date(2024, 5, 1), date(2024, 5, 31), ["2024-06-15", None], [4, 5])
JUN = _window(date(2024, 6, 1), date(2024, 6, 30), [None], [6])


def test_training_set_uses_only_windows_ended_before_the_cutoff():
    x, _ = training_set([APR, MAY, JUN], date(2024, 6, 1), ["f"])
    assert x[:, 0].tolist() == [1, 2, 3, 4, 5]


def test_training_label_is_as_of_the_cutoff():
    _, y = training_set([APR, MAY, JUN], date(2024, 6, 1), ["f"])
    # 2024-05-10 is public before the June cutoff; 2024-09-01 and 2024-06-15 are not yet.
    assert y.tolist() == [True, False, False, False, False]
    _, y_later = training_set([APR, MAY, JUN], date(2024, 7, 1), ["f"])
    assert y_later.tolist() == [True, False, False, True, False, False]


def test_training_set_is_empty_when_nothing_ended_before_the_cutoff():
    x, y = training_set([APR], date(2024, 4, 1), ["f"])
    assert x.shape == (0, 1) and y.size == 0


def test_secondary_label_is_a_twelve_month_horizon():
    w = _window(
        date(2024, 6, 1),
        date(2024, 6, 30),
        ["2024-06-30", "2024-07-01", "2025-06-30", "2025-07-01", None],
        [0] * 5,
    )
    assert secondary_labels(w.split).tolist() == [False, True, True, False, False]


class _FakeClassifier:
    """Records its context and scores each test row by its first feature."""

    contexts: ClassVar[list[tuple[np.ndarray, np.ndarray]]] = []

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.classes_ = np.array([0, 1])

    def fit(self, x, y):
        _FakeClassifier.contexts.append((x.copy(), y.copy()))
        return self

    def predict_proba(self, x):
        p = (x[:, 0] + self.seed) / 100.0
        return np.column_stack([1 - p, p])


def test_tabicl_contexts_keep_every_positive_and_sample_negatives():
    _FakeClassifier.contexts = []
    rng = np.random.default_rng(0)
    y = np.zeros(200, dtype=bool)
    y[:5] = True
    x = np.column_stack([np.arange(200.0), rng.normal(size=200)])
    x[7, 1] = np.nan
    out = tabicl_scores(
        x, y, np.array([[1.0, 0.0], [2.0, 0.0]]), seeds=(0, 1, 2), n_negatives=20,
        factory=_FakeClassifier,
    )
    assert len(_FakeClassifier.contexts) == 3
    for cx, cy in _FakeClassifier.contexts:
        assert cy.sum() == 5 and len(cy) == 25
        assert not np.isnan(cx).any()
    negs = [set(cx[cy == 0, 0].tolist()) for cx, cy in _FakeClassifier.contexts]
    assert negs[0] != negs[1]
    # mean over seeds 0, 1, 2 of (x0 + seed) / 100
    assert out.tolist() == pytest.approx([0.02, 0.03])


def test_tabicl_uses_all_negatives_when_fewer_than_requested():
    _FakeClassifier.contexts = []
    y = np.array([True, False, False])
    tabicl_scores(
        np.ones((3, 1)), y, np.ones((1, 1)), seeds=(0,), n_negatives=10, factory=_FakeClassifier
    )
    assert len(_FakeClassifier.contexts[0][1]) == 3


def test_logistic_ranks_the_separating_feature():
    rng = np.random.default_rng(1)
    y = np.r_[np.ones(20, bool), np.zeros(200, bool)]
    x = np.column_stack([y + rng.normal(0, 0.3, len(y)), rng.normal(size=len(y))])
    s = logistic_scores(x, y, np.array([[1.0, 0.0], [0.0, 0.0]]))
    assert s[0] > s[1]


def _panel_window_with_static_columns() -> Window:
    """A window as load_window leaves it: panel columns recorded, then static columns added to
    the same features dict."""
    w = _window(date(2024, 6, 1), date(2024, 6, 30), [None], [0])
    w.split.features.clear()
    w.split.features.update(
        {
            "n_gaps": np.zeros(1),
            "n_observed_hours": np.zeros(1),
            "voyage_count": np.zeros(1),
            "is_tanker": np.zeros(1),
            "is_foc": np.zeros(1),
        }
    )
    w = Window(w.split, w.imo, w.package, panel_columns=frozenset(w.split.features))
    for c in ("imo_serial", "length_m", "dest_russia"):
        w.split.features[c] = np.zeros(1)
    return w


def test_noexp_variant_drops_exactly_the_exposure_columns():
    w = _panel_window_with_static_columns()
    full = detector_columns([w], "detectors_context")
    noexp = detector_columns([w], "detectors_context_noexp")
    assert set(full) - set(noexp) == {"n_observed_hours", "voyage_count"}


@pytest.mark.parametrize(
    "variant", ["detectors", "detectors_context", "detectors_context_noexp"]
)
def test_detector_variants_never_see_the_static_columns(variant):
    # Regression: the first real run (97f0661) fed imo_serial/length/destination into these.
    cols = detector_columns([_panel_window_with_static_columns()], variant)
    assert not {"imo_serial", "length_m", "dest_russia"} & set(cols)
    assert "n_gaps" in cols


def test_window_dirs_skip_the_short_validation_window(tmp_path):
    for name in (
        "window=2024-06-01_2024-06-30",
        "window=2024-06-10_2024-06-11",
        "window=2025-02-01_2025-02-26",
    ):
        (tmp_path / name).mkdir()
    assert [d.name for d in _window_dirs(tmp_path)] == [
        "window=2024-06-01_2024-06-30",
        "window=2025-02-01_2025-02-26",
    ]
