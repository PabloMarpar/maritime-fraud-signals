"""P4-3j: our own vessel encoder -- a self-supervised transformer over a vessel-month's hourly
track tokens and detector events, frozen, then used as 16 extra columns for the usual heads.

Design pre-registered in ``docs/DECISIONS.md`` (2026-09-29, "P4-3j ... detailed design") before
any embedding met a label. The governing constraint is the label count: 23-428 training positives
per cutoff against ~20k unlabelled vessel-months in the pretraining period. So all of the
network's capacity is spent on label-free objectives, and what finally meets a label is the same
small head as every other model (``model.walk_forward``).

**Never sees a label.** The pretraining population is every panel vessel-month with a valid IMO
in 2024-04..2024-07, not filtered by sanctions status; nothing in this module reads a label
column. **Never sees the future of a cutoff.** It trains only on those four windows (the first
primary cutoff is 2024-08-01), its standardization statistics come from them, and the next-month
targets are built only between them (April->May, May->June, June->July).

**Inputs** are ``features.vessel_tokens``' tables: :data:`features.vessel_tokens.TRACK_CHANNELS`
(no latitude, longitude, raw COG or identity), the hour index, and event tokens. ``region_id`` is
read only to build next-month targets.

**Objectives (equal weights):**

1. Masked-span reconstruction of the track channels (:data:`RECON_CHANNELS`).
2. Same-vessel contrastive (InfoNCE): two views of one IMO -- another month when it exists, else
   the same month -- each a random contiguous crop with token dropout; one pair per IMO per batch.
3. Next month, from the embedding: observed at all (BCE), distribution over 12 coarse regions
   (soft cross-entropy), median draught / the vessel's max draught in the period (MSE).

**Output:** ``data/processed/embeddings/seed=<s>/window=<w>/part-0.parquet`` -- mmsi and
``emb_00..emb_15`` for every vessel-month with a valid IMO in every built window, computed by the
frozen encoder in eval mode on the full sequence (no crop, no dropout, no mask).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
import zlib
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path

import duckdb
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from features.vessel_tokens import (
    EVENT_ROOT,
    EVENT_TYPES,
    N_REGIONS,
    TRACK_CHANNELS,
    TRACK_ROOT,
)
from process.partitions import atomic_write_parquet

logger = logging.getLogger(__name__)

PANEL_ROOT = Path("data/processed/panel")
EMBEDDING_ROOT = Path("data/processed/embeddings")
LOG_ROOT = Path("outputs")

PRETRAIN_FIRST = date(2024, 4, 1)
PRETRAIN_LAST = date(2024, 7, 31)

EMB_DIM = 16
D_MODEL = 64
N_HEADS = 4
FF_DIM = 128
N_TRACK_LAYERS = 2
N_EVENT_LAYERS = 1
N_LATENTS = 4
DROPOUT = 0.1

MASK_RATE = 0.15
MASK_SPAN = (3, 8)
CROP_RANGE = (0.6, 1.0)
TOKEN_DROPOUT = 0.1
TEMPERATURE = 0.1
LR = 3e-4
WEIGHT_DECAY = 0.01
BATCH_SIZE = 128
MAX_EPOCHS = 40
PATIENCE = 3
VAL_MOD = 10  # 1 in 10 IMOs held out, by CRC32 of the IMO

# Not reconstructed: derivable from the neighbouring tokens' positions/draughts.
RECON_CHANNELS = tuple(c for c in TRACK_CHANNELS if c not in ("log_dt_h", "draught_delta"))
_RECON_IDX = [TRACK_CHANNELS.index(c) for c in RECON_CHANNELS]
_DRAUGHT = TRACK_CHANNELS.index("draught")
_DRAUGHT_MISSING = TRACK_CHANNELS.index("draught_missing")
NO_EVENT = len(EVENT_TYPES)  # the always-present "no event" token's type id
EMB_COLUMNS = tuple(f"emb_{i:02d}" for i in range(EMB_DIM))


# --------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------


@dataclass
class VesselMonth:
    window: str
    mmsi: int
    imo: str
    track: np.ndarray  # [T, C] raw channels, float32
    hours: np.ndarray  # [T] int
    region: np.ndarray  # [T] int (targets only)
    ev_type: np.ndarray  # [E] int
    ev_hours: np.ndarray  # [E] int
    ev_num: np.ndarray  # [E, 2] float32


def window_names(panel_root: Path = PANEL_ROOT) -> list[str]:
    """Monthly windows (the 2-day validation window is skipped), in time order."""
    out = []
    for d in sorted(panel_root.glob("window=*")):
        s, e = (date.fromisoformat(x) for x in d.name.removeprefix("window=").split("_"))
        if (e - s).days >= 20:
            out.append(d.name.removeprefix("window="))
    return out


def window_bounds(name: str) -> tuple[date, date]:
    s, e = name.split("_")
    return date.fromisoformat(s), date.fromisoformat(e)


def load_window(
    name: str,
    panel_root: Path = PANEL_ROOT,
    track_root: Path = TRACK_ROOT,
    event_root: Path = EVENT_ROOT,
) -> list[VesselMonth]:
    """Every vessel-month with a valid IMO and at least one track token in window `name`."""
    con = duckdb.connect()
    try:
        roster = dict(
            con.execute(
                f"SELECT mmsi, any_value(imo) FROM read_parquet("
                f"'{(panel_root / f'window={name}' / 'part-0.parquet').as_posix()}') "
                "WHERE imo IS NOT NULL GROUP BY mmsi"
            ).fetchall()
        )
        cols = ", ".join(TRACK_CHANNELS)
        tr = con.execute(
            f"SELECT mmsi, hour_idx, region_id, {cols} FROM read_parquet("
            f"'{(track_root / f'window={name}' / 'part-0.parquet').as_posix()}') ORDER BY mmsi, hour_idx"
        ).fetchnumpy()
        ev = con.execute(
            f"SELECT mmsi, type, hour_idx, log_duration_h, score FROM read_parquet("
            f"'{(event_root / f'window={name}' / 'part-0.parquet').as_posix()}') ORDER BY mmsi, hour_idx"
        ).fetchnumpy()
    finally:
        con.close()
    type_id = {t: i for i, t in enumerate(EVENT_TYPES)}
    mm = np.asarray(tr["mmsi"])
    feats = np.column_stack([np.asarray(tr[c], dtype=np.float32) for c in TRACK_CHANNELS])
    hours = np.asarray(tr["hour_idx"], dtype=np.int64)
    region = np.asarray(tr["region_id"], dtype=np.int64)
    ev_mm = np.asarray(ev["mmsi"])
    ev_t = np.array([type_id.get(str(t), type_id["other"]) for t in ev["type"]], dtype=np.int64)
    ev_h = np.asarray(ev["hour_idx"], dtype=np.int64)
    ev_n = np.column_stack(
        [np.asarray(ev["log_duration_h"], dtype=np.float32), np.asarray(ev["score"], dtype=np.float32)]
    ) if len(ev_mm) else np.zeros((0, 2), np.float32)

    def spans(keys: np.ndarray) -> dict[int, tuple[int, int]]:
        if len(keys) == 0:
            return {}
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        ends = np.r_[starts[1:], len(keys)]
        return {int(keys[s]): (int(s), int(e)) for s, e in zip(starts, ends, strict=True)}

    tr_spans, ev_spans = spans(mm), spans(ev_mm)
    out = []
    for m, (a, b) in tr_spans.items():
        if m not in roster:
            continue
        ea, eb = ev_spans.get(m, (0, 0))
        out.append(
            VesselMonth(
                window=name, mmsi=m, imo=str(roster[m]), track=feats[a:b], hours=hours[a:b],
                region=region[a:b], ev_type=ev_t[ea:eb], ev_hours=ev_h[ea:eb], ev_num=ev_n[ea:eb],
            )
        )
    return out


@dataclass
class Standardizer:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, months: list[VesselMonth]) -> Standardizer:
        x = np.concatenate([m.track for m in months]).astype(np.float64)
        mean, std = x.mean(0), x.std(0)
        return cls(mean.astype(np.float32), np.where(std < 1e-6, 1.0, std).astype(np.float32))

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.std


def is_validation(imo: str) -> bool:
    return zlib.crc32(imo.encode()) % VAL_MOD == 0


@dataclass
class NextTarget:
    present: float
    regions: np.ndarray  # [N_REGIONS], sums to 1 when present
    laden: float  # NaN when unknown


def next_month_targets(by_window: dict[str, list[VesselMonth]]) -> dict[tuple[str, str], NextTarget]:
    """(source window, imo) -> targets from the NEXT window in `by_window` (all pretraining
    windows). The last window has no target -- nothing after it may be read."""
    names = sorted(by_window)
    max_draught: dict[str, float] = {}
    for ms in by_window.values():
        for m in ms:
            known = m.track[m.track[:, _DRAUGHT_MISSING] == 0, _DRAUGHT]
            if len(known):
                max_draught[m.imo] = max(max_draught.get(m.imo, 0.0), float(known.max()))
    out: dict[tuple[str, str], NextTarget] = {}
    for src, nxt in pairwise(names):
        nxt_by_imo: dict[str, list[VesselMonth]] = {}
        for m in by_window[nxt]:
            nxt_by_imo.setdefault(m.imo, []).append(m)
        for m in by_window[src]:
            if (src, m.imo) in out:
                continue
            ms = nxt_by_imo.get(m.imo, [])
            if not ms:
                out[(src, m.imo)] = NextTarget(0.0, np.zeros(N_REGIONS, np.float32), float("nan"))
                continue
            reg = np.bincount(np.concatenate([x.region for x in ms]), minlength=N_REGIONS)
            tracks = np.concatenate([x.track for x in ms])
            known = tracks[tracks[:, _DRAUGHT_MISSING] == 0, _DRAUGHT]
            md = max_draught.get(m.imo, 0.0)
            laden = float(np.clip(np.median(known) / md, 0, 1.2)) if len(known) and md > 0 else float("nan")
            out[(src, m.imo)] = NextTarget(1.0, (reg / reg.sum()).astype(np.float32), laden)
    return out


# --------------------------------------------------------------------------------------------
# Views and batching
# --------------------------------------------------------------------------------------------


def crop_and_drop(n: int, rng: np.random.Generator, crop=CROP_RANGE, drop=TOKEN_DROPOUT) -> np.ndarray:
    """Indices of a random contiguous crop (crop[0]-crop[1] of the tokens) with token dropout;
    always at least one token."""
    keep = max(1, round(n * rng.uniform(*crop)))
    start = int(rng.integers(0, n - keep + 1))
    idx = np.arange(start, start + keep)
    if keep > 1:
        idx = idx[rng.random(keep) >= drop]
    return idx if len(idx) else np.array([start])


def span_mask(n: int, rng: np.random.Generator, rate=MASK_RATE, span=MASK_SPAN) -> np.ndarray:
    """Boolean mask covering ~rate of n tokens in spans of span[0]-span[1]; never every token."""
    mask = np.zeros(n, dtype=bool)
    target = round(n * rate)
    while mask.sum() < target:
        length = int(rng.integers(span[0], span[1] + 1))
        s = int(rng.integers(0, max(1, n - length + 1)))
        mask[s:s + length] = True
    if mask.all():
        mask[int(rng.integers(0, n))] = False
    return mask


def collate(views: list[tuple[np.ndarray, np.ndarray, VesselMonth]], std: Standardizer, device, masks=None):
    """Pad a list of (track indices, event indices, month) views into tensors."""
    b = len(views)
    t_len = max(len(ti) for ti, _e, _m in views)
    e_len = 1 + max(len(ei) for _t, ei, _m in views)
    c = len(TRACK_CHANNELS)
    track = np.zeros((b, t_len, c), np.float32)
    hours = np.zeros((b, t_len), np.int64)
    t_pad = np.ones((b, t_len), bool)
    mask = np.zeros((b, t_len), bool)
    ev_type = np.full((b, e_len), NO_EVENT, np.int64)
    ev_hours = np.zeros((b, e_len), np.int64)
    ev_num = np.zeros((b, e_len, 2), np.float32)
    e_pad = np.ones((b, e_len), bool)
    for i, (ti, ei, m) in enumerate(views):
        track[i, : len(ti)] = std(m.track[ti])
        hours[i, : len(ti)] = m.hours[ti]
        t_pad[i, : len(ti)] = False
        if masks is not None:
            mask[i, : len(ti)] = masks[i]
        e_pad[i, 0] = False  # the "no event" token, always present
        ev_type[i, 1 : 1 + len(ei)] = m.ev_type[ei]
        ev_hours[i, 1 : 1 + len(ei)] = m.ev_hours[ei]
        ev_num[i, 1 : 1 + len(ei)] = m.ev_num[ei]
        e_pad[i, 1 : 1 + len(ei)] = False
    as_t = lambda a: torch.from_numpy(a).to(device)
    return {
        "track": as_t(track), "hours": as_t(hours), "t_pad": as_t(t_pad), "mask": as_t(mask),
        "ev_type": as_t(ev_type), "ev_hours": as_t(ev_hours), "ev_num": as_t(ev_num),
        "e_pad": as_t(e_pad),
    }


def imo_unique_batches(anchors: list[int], imos: list[str], batch_size: int, rng) -> list[list[int]]:
    """Shuffle anchors into batches with at most one anchor per IMO (a second anchor of the same
    IMO in a batch would be a false negative for the contrastive loss)."""
    order = list(rng.permutation(anchors))
    batches: list[list[int]] = []
    while order:
        batch, seen, rest = [], set(), []
        for a in order:
            if len(batch) < batch_size and imos[a] not in seen:
                batch.append(a)
                seen.add(imos[a])
            else:
                rest.append(a)
        batches.append(batch)
        order = rest
    return batches


# --------------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------------


class HourEncoding(nn.Module):
    """Sinusoidal encoding of the hour index (periods from 2 h to ~2,000 h)."""

    def __init__(self, d: int) -> None:
        super().__init__()
        periods = torch.exp(torch.linspace(math.log(2.0), math.log(2000.0), d // 2))
        self.register_buffer("freq", 2 * math.pi / periods)

    def forward(self, hours: torch.Tensor) -> torch.Tensor:
        a = hours.float()[..., None] * self.freq
        return torch.cat([torch.sin(a), torch.cos(a)], dim=-1)


def _layer(d: int) -> nn.TransformerEncoderLayer:
    return nn.TransformerEncoderLayer(
        d, N_HEADS, FF_DIM, DROPOUT, batch_first=True, norm_first=True, activation="gelu"
    )


class VesselEncoder(nn.Module):
    def __init__(self, n_track: int = len(TRACK_CHANNELS), d: int = D_MODEL, emb_dim: int = EMB_DIM) -> None:
        super().__init__()
        self.track_in = nn.Linear(n_track, d)
        self.mask_token = nn.Parameter(torch.zeros(d))
        self.hour = HourEncoding(d)
        self.track_enc = nn.TransformerEncoder(_layer(d), N_TRACK_LAYERS, enable_nested_tensor=False)
        self.ev_type = nn.Embedding(len(EVENT_TYPES) + 1, d)
        self.ev_num = nn.Linear(2, d)
        self.event_enc = nn.TransformerEncoder(_layer(d), N_EVENT_LAYERS, enable_nested_tensor=False)
        self.latents = nn.Parameter(torch.randn(N_LATENTS, d) * 0.02)
        self.fuse = nn.MultiheadAttention(d, N_HEADS, dropout=DROPOUT, batch_first=True)
        self.fuse_norm = nn.LayerNorm(d)
        self.fuse_ff = nn.Sequential(nn.LayerNorm(d), nn.Linear(d, FF_DIM), nn.GELU(), nn.Linear(FF_DIM, d))
        self.out = nn.Sequential(nn.Linear(N_LATENTS * d, emb_dim), nn.LayerNorm(emb_dim))
        # Self-supervised heads (discarded after pretraining).
        self.recon = nn.Linear(d, len(RECON_CHANNELS))
        self.proj = nn.Sequential(nn.Linear(emb_dim, 64), nn.GELU(), nn.Linear(64, 32))
        self.next_head = nn.Sequential(nn.Linear(emb_dim, 64), nn.GELU(), nn.Linear(64, 2 + N_REGIONS))

    def forward(self, batch: dict) -> tuple[torch.Tensor, torch.Tensor]:
        """(embedding [B, emb_dim], track hidden states [B, T, d])."""
        x = self.track_in(batch["track"])
        x = torch.where(batch["mask"][..., None], self.mask_token.to(x.dtype), x)
        x = x + self.hour(batch["hours"])
        h = self.track_enc(x, src_key_padding_mask=batch["t_pad"])
        e = self.ev_type(batch["ev_type"]) + self.ev_num(batch["ev_num"]) + self.hour(batch["ev_hours"])
        g = self.event_enc(e, src_key_padding_mask=batch["e_pad"])
        mem = torch.cat([h, g], dim=1)
        mem_pad = torch.cat([batch["t_pad"], batch["e_pad"]], dim=1)
        q = self.latents.unsqueeze(0).expand(x.shape[0], -1, -1)
        z, _ = self.fuse(q, mem, mem, key_padding_mask=mem_pad, need_weights=False)
        z = self.fuse_norm(z + q)
        z = z + self.fuse_ff(z)
        return self.out(z.flatten(1)), h


# --------------------------------------------------------------------------------------------
# Training
# --------------------------------------------------------------------------------------------


def info_nce(p1: torch.Tensor, p2: torch.Tensor, temperature: float = TEMPERATURE) -> torch.Tensor:
    p1, p2 = F.normalize(p1, dim=-1), F.normalize(p2, dim=-1)
    logits = p1 @ p2.T / temperature
    labels = torch.arange(len(p1), device=p1.device)
    return 0.5 * (F.cross_entropy(logits, labels) + F.cross_entropy(logits.T, labels))


def _losses(model, months, batch_idx, pairs, targets, std, device, rng) -> dict[str, torch.Tensor]:
    anchors = [months[i] for i in batch_idx]
    v1, masks = [], []
    for m in anchors:
        ti = crop_and_drop(len(m.hours), rng)
        v1.append((ti, np.arange(len(m.ev_type)), m))
        masks.append(span_mask(len(ti), rng))
    v2 = []
    for i in batch_idx:
        m = months[int(rng.choice(pairs[i]))]
        v2.append((crop_and_drop(len(m.hours), rng), np.arange(len(m.ev_type)), m))
    b1 = collate(v1, std, device, masks)
    b2 = collate(v2, std, device)
    emb1, h1 = model(b1)
    emb2, _ = model(b2)

    # 1. masked reconstruction
    sel = b1["mask"] & ~b1["t_pad"]
    pred = model.recon(h1[sel])
    true = b1["track"][sel][:, _RECON_IDX]
    l_rec = F.mse_loss(pred.float(), true.float()) if sel.any() else emb1.sum() * 0
    # 2. contrastive
    l_con = info_nce(model.proj(emb1).float(), model.proj(emb2).float())
    # 3. next month
    out = model.next_head(emb1).float()
    tg = [targets.get((m.window, m.imo)) for m in anchors]
    has = torch.tensor([t is not None for t in tg], device=device)
    l_next = emb1.sum() * 0
    if has.any():
        present = torch.tensor([t.present if t else 0.0 for t in tg], device=device)
        l_next = l_next + F.binary_cross_entropy_with_logits(out[has, 0], present[has])
        pres = has & (present > 0)
        if pres.any():
            reg = torch.tensor(np.stack([t.regions if t else np.zeros(N_REGIONS, np.float32) for t in tg]), device=device)
            l_next = l_next - (reg[pres] * F.log_softmax(out[pres, 2:], dim=-1)).sum(-1).mean()
            laden = torch.tensor([t.laden if t else float("nan") for t in tg], device=device)
            ok = pres & ~torch.isnan(laden)
            if ok.any():
                l_next = l_next + F.mse_loss(out[ok, 1], laden[ok])
    return {"recon": l_rec, "contrast": l_con, "next": l_next}


def _pairs(months: list[VesselMonth], idx: list[int]) -> dict[int, list[int]]:
    """For each anchor, the candidate second views: other months of the same IMO within `idx`,
    else itself."""
    by_imo: dict[str, list[int]] = {}
    for i in idx:
        by_imo.setdefault(months[i].imo, []).append(i)
    return {i: ([j for j in by_imo[months[i].imo] if months[j].window != months[i].window] or [i]) for i in idx}


def pretrain(
    months: list[VesselMonth],
    seed: int = 0,
    device: str | None = None,
    max_epochs: int = MAX_EPOCHS,
    batch_size: int = BATCH_SIZE,
    log: list | None = None,
) -> tuple[VesselEncoder, Standardizer]:
    """Self-supervised pretraining on `months` (all from the pretraining period)."""
    for m in months:
        if not (PRETRAIN_FIRST <= window_bounds(m.window)[0] and window_bounds(m.window)[1] <= PRETRAIN_LAST):
            raise ValueError(f"{m.window} is outside the pretraining period")
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    std = Standardizer.fit(months)
    by_window: dict[str, list[VesselMonth]] = {}
    for m in months:
        by_window.setdefault(m.window, []).append(m)
    targets = next_month_targets(by_window)
    train_idx = [i for i, m in enumerate(months) if not is_validation(m.imo)]
    val_idx = [i for i, m in enumerate(months) if is_validation(m.imo)]
    train_pairs, val_pairs = _pairs(months, train_idx), _pairs(months, val_idx)
    imos = [m.imo for m in months]

    model = VesselEncoder().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    amp = device == "cuda"
    best, best_state, bad = float("inf"), None, 0
    for epoch in range(max_epochs):
        t0 = time.time()
        model.train()
        tr = {"recon": 0.0, "contrast": 0.0, "next": 0.0}
        batches = imo_unique_batches(train_idx, imos, batch_size, rng)
        for b in batches:
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=amp):
                ls = _losses(model, months, b, train_pairs, targets, std, device, rng)
            loss = ls["recon"] + ls["contrast"] + ls["next"]
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            for k, v in ls.items():
                tr[k] += float(v.detach()) / len(batches)
        model.eval()
        vrng = np.random.default_rng(10_000)  # identical validation views every epoch
        va = {"recon": 0.0, "contrast": 0.0, "next": 0.0}
        vb = imo_unique_batches(val_idx, imos, batch_size, vrng)
        with torch.no_grad(), torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=amp):
            for b in vb:
                ls = _losses(model, months, b, val_pairs, targets, std, device, vrng)
                for k, v in ls.items():
                    va[k] += float(v) / len(vb)
        total = sum(va.values())
        row = {"epoch": epoch, "train": tr, "val": va, "val_total": total, "secs": round(time.time() - t0, 1)}
        logger.info("epoch %d: train %s | val %s total %.4f (%.0fs)", epoch,
                    {k: round(v, 3) for k, v in tr.items()}, {k: round(v, 3) for k, v in va.items()},
                    total, row["secs"])
        if log is not None:
            log.append(row)
        if total < best - 1e-4:
            best, bad = total, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= PATIENCE:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    return model, std


@torch.no_grad()
def embed(model: VesselEncoder, months: list[VesselMonth], std: Standardizer, device: str, batch_size: int = 256) -> np.ndarray:
    """Frozen, eval-mode embeddings on the full sequences, in `months` order."""
    model.eval()
    out = np.zeros((len(months), EMB_DIM), np.float32)
    order = np.argsort([len(m.hours) for m in months])
    for s in range(0, len(order), batch_size):
        idx = order[s : s + batch_size]
        views = [(np.arange(len(months[i].hours)), np.arange(len(months[i].ev_type)), months[i]) for i in idx]
        emb, _ = model(collate(views, std, device))
        out[idx] = emb.float().cpu().numpy()
    return out


def _write_embeddings(name: str, months: list[VesselMonth], emb: np.ndarray, out_root: Path) -> Path:
    out = out_root / f"window={name}" / "part-0.parquet"
    con = duckdb.connect()
    try:
        import pandas as pd

        df = pd.DataFrame(emb, columns=list(EMB_COLUMNS))
        df.insert(0, "mmsi", np.array([m.mmsi for m in months], dtype=np.int64))
        con.register("e", df)
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(con, "SELECT * FROM e ORDER BY mmsi", out)
    finally:
        con.close()
    return out


def build_embeddings(seed: int = 0, out_root: Path = EMBEDDING_ROOT, log_root: Path = LOG_ROOT, device: str | None = None) -> Path:
    """Pretrain on 2024-04..07 with `seed`, then embed every built window."""
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    names = window_names()
    pre = [n for n in names if PRETRAIN_FIRST <= window_bounds(n)[0] and window_bounds(n)[1] <= PRETRAIN_LAST]
    months = [m for n in pre for m in load_window(n)]
    logger.info("Pretraining on %s: %d vessel-months, %d IMOs", pre, len(months), len({m.imo for m in months}))
    log: list = []
    model, std = pretrain(months, seed=seed, device=device, log=log)
    root = out_root / f"seed={seed}"
    for n in names:
        ms = load_window(n)
        _write_embeddings(n, ms, embed(model, ms, std, device), root)
        logger.info("Embedded %s: %d vessel-months", n, len(ms))
    log_root.mkdir(parents=True, exist_ok=True)
    (log_root / f"vessel_encoder_seed{seed}.json").write_text(
        json.dumps({"pretrain_windows": pre, "n_months": len(months),
                    "n_params": sum(p.numel() for p in model.parameters()), "epochs": log}, indent=1),
        encoding="utf-8",
    )
    torch.save({"state": model.state_dict(), "mean": std.mean, "std": std.std}, root / "encoder.pt")
    return root


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="P4-3j: pretrain the vessel encoder and embed every window")
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    build_embeddings(seed=a.seed)


if __name__ == "__main__":
    main()
