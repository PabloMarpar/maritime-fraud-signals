"""Synthetic tests for model.vessel_encoder (P4-3j). CPU only, tiny data."""

from __future__ import annotations

import numpy as np
import pytest
import torch

from features.vessel_tokens import N_REGIONS, TRACK_CHANNELS
from model import vessel_encoder as ve


def _month(window: str, mmsi: int, imo: str, n: int, seed: int, draught: float = 8.0, region: int = 0, n_ev: int = 0):
    rng = np.random.default_rng(seed)
    track = rng.normal(size=(n, len(TRACK_CHANNELS))).astype(np.float32)
    track[:, ve._DRAUGHT] = draught
    track[:, ve._DRAUGHT_MISSING] = 0
    return ve.VesselMonth(
        window=window, mmsi=mmsi, imo=imo, track=track,
        hours=np.sort(rng.choice(700, size=n, replace=False)).astype(np.int64),
        region=np.full(n, region, np.int64),
        ev_type=np.zeros(n_ev, np.int64), ev_hours=np.arange(n_ev, dtype=np.int64),
        ev_num=np.zeros((n_ev, 2), np.float32),
    )


APR, MAY, JUN = "2024-04-01_2024-04-30", "2024-05-01_2024-05-31", "2024-06-01_2024-06-30"


def test_inputs_never_include_position_cog_or_identity():
    forbidden = {"latitude", "longitude", "lat", "lon", "cog", "heading", "mmsi", "imo", "region_id"}
    assert not forbidden & set(TRACK_CHANNELS)
    assert ve.VesselEncoder().track_in.in_features == len(TRACK_CHANNELS)


def test_next_month_targets_come_only_from_the_following_window():
    by_window = {
        APR: [_month(APR, 1, "9000001", 5, 0, draught=10.0, region=3)],
        MAY: [_month(MAY, 1, "9000001", 4, 1, draught=5.0, region=7)],
        JUN: [_month(JUN, 2, "9000002", 3, 2)],
    }
    t = ve.next_month_targets(by_window)
    apr = t[(APR, "9000001")]
    assert apr.present == 1.0 and apr.regions[7] == pytest.approx(1.0)
    assert apr.laden == pytest.approx(0.5)  # median 5 m / max 10 m over the period
    assert t[(MAY, "9000001")].present == 0.0  # not seen in June
    assert (JUN, "9000002") not in t  # the last window has no next month to read


def test_pretraining_rejects_windows_after_the_period():
    with pytest.raises(ValueError, match="outside the pretraining period"):
        ve.pretrain([_month("2024-08-01_2024-08-31", 1, "9000001", 5, 0)], max_epochs=1, device="cpu")


def test_batches_hold_each_imo_at_most_once_and_cover_every_anchor():
    imos = ["a", "a", "a", "b", "c", "c"]
    batches = ve.imo_unique_batches(list(range(6)), imos, 4, np.random.default_rng(0))
    assert sorted(i for b in batches for i in b) == list(range(6))
    for b in batches:
        assert len({imos[i] for i in b}) == len(b)


def test_views_and_masks_are_never_empty_or_total():
    rng = np.random.default_rng(0)
    for n in (1, 2, 7, 300):
        assert len(ve.crop_and_drop(n, rng)) >= 1
        m = ve.span_mask(n, rng)
        assert not m.all() or n == 0
    assert ve.span_mask(1000, rng).mean() == pytest.approx(ve.MASK_RATE, abs=0.02)


def test_embedding_does_not_depend_on_batch_padding():
    torch.manual_seed(0)
    model = ve.VesselEncoder().eval()
    short, long_ = _month(APR, 1, "9000001", 4, 0, n_ev=2), _month(APR, 2, "9000002", 50, 1, n_ev=9)
    std = ve.Standardizer.fit([short, long_])
    alone = ve.embed(model, [short], std, "cpu")
    padded = ve.embed(model, [short, long_], std, "cpu")
    assert padded.shape == (2, ve.EMB_DIM)
    np.testing.assert_allclose(alone[0], padded[0], atol=1e-5)


def test_pretraining_smoke_runs_and_embeds():
    months = [
        _month(w, 10 + i, f"90000{i:02d}", 6 + i, s, region=i % N_REGIONS, n_ev=i % 3)
        for s, (w, i) in enumerate([(w, i) for w in (APR, MAY, JUN) for i in range(12)])
    ]
    log: list = []
    model, std = ve.pretrain(months, seed=0, device="cpu", max_epochs=2, batch_size=8, log=log)
    assert len(log) >= 1 and np.isfinite(log[-1]["val_total"])
    emb = ve.embed(model, months, std, "cpu")
    assert emb.shape == (len(months), ve.EMB_DIM) and np.isfinite(emb).all()
