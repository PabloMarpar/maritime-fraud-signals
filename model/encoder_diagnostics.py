"""Label-free diagnostics of the P4-3j embeddings (``model.vessel_encoder``).

Reads no sanctions column. It answers "did the encoder learn anything at all?" separately from
"does it help predict designations?" (``model.walk_forward``), so a null on the second question
cannot be blamed on a broken encoder without evidence:

- **Collapse:** per-dimension spread and the effective rank of the embedding covariance
  (exp of the entropy of its normalised eigenvalues; 16 = every dimension used equally).
- **Linear probes** for ship type (tanker, passenger), 5-fold AUC. Ship type is never an encoder
  input, so recovering it means the track tokens carry it.
- **Same-vessel retrieval** between two months the encoder never trained on (2024-08 -> 2024-09):
  for each IMO in both, the rank of its own September embedding among all September embeddings
  by cosine similarity (random ~ n/2).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler

EMBEDDING_ROOT = Path("data/processed/embeddings")
PANEL_ROOT = Path("data/processed/panel")
PROBE_WINDOWS = ("2024-06-01_2024-06-30", "2024-11-01_2024-11-30")
RETRIEVAL_WINDOWS = ("2024-08-01_2024-08-31", "2024-09-01_2024-09-30")
N_DIM = 16


def _load(con: duckdb.DuckDBPyConnection, root: Path, window: str) -> dict:
    cols = ", ".join(f"e.emb_{i:02d}" for i in range(N_DIM))
    return con.execute(
        f"""SELECT e.mmsi, p.imo, coalesce(p.ship_type, '') = 'Tanker' AS tanker,
            coalesce(p.ship_type, '') = 'Passenger' AS passenger, {cols}
        FROM read_parquet('{(root / f'window={window}' / 'part-0.parquet').as_posix()}') e
        JOIN (SELECT mmsi, any_value(imo) AS imo, any_value(ship_type) AS ship_type
              FROM read_parquet('{(PANEL_ROOT / f'window={window}' / 'part-0.parquet').as_posix()}')
              WHERE imo IS NOT NULL GROUP BY mmsi) p USING (mmsi)"""
    ).fetchnumpy()


def _matrix(d: dict) -> np.ndarray:
    return np.column_stack([d[f"emb_{i:02d}"] for i in range(N_DIM)])


def effective_rank(x: np.ndarray) -> float:
    ev = np.clip(np.linalg.eigvalsh(np.cov(x.T)), 1e-12, None)
    p = ev / ev.sum()
    return float(np.exp(-(p * np.log(p)).sum()))


def retrieval_ranks(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Row i of `a` and row i of `b` are the same vessel; rank of the true match (0 = best)."""
    a = a / np.linalg.norm(a, axis=1, keepdims=True)
    b = b / np.linalg.norm(b, axis=1, keepdims=True)
    sim = a @ b.T
    return (sim > np.diag(sim)[:, None]).sum(1)


def run(seed: int) -> list[str]:
    root = EMBEDDING_ROOT / f"seed={seed}"
    con = duckdb.connect()
    out = [f"P4-3j embedding diagnostics, encoder seed {seed} (no sanctions column read)"]
    for w in PROBE_WINDOWS:
        d = _load(con, root, w)
        x = _matrix(d)
        out.append(
            f"{w}: n={len(x)}, per-dim std min {x.std(0).min():.3f} / median "
            f"{np.median(x.std(0)):.3f}, effective rank {effective_rank(x):.2f} of {N_DIM}"
        )
        xs = StandardScaler().fit_transform(x)
        for name in ("tanker", "passenger"):
            y = np.asarray(d[name], bool)
            auc = cross_val_score(LogisticRegression(max_iter=2000), xs, y, cv=5, scoring="roc_auc").mean()
            out.append(f"  probe {name}: prevalence {y.mean():.3f}, 5-fold AUC {auc:.3f}")
    a, b = (_load(con, root, w) for w in RETRIEVAL_WINDOWS)
    ia, ib = np.asarray(a["imo"]).astype(str), np.asarray(b["imo"]).astype(str)
    common = sorted(set(ia) & set(ib))
    ua, ub = {k: i for i, k in enumerate(ia)}, {k: i for i, k in enumerate(ib)}
    ranks = retrieval_ranks(_matrix(a)[[ua[k] for k in common]], _matrix(b)[[ub[k] for k in common]])
    out.append(
        f"{RETRIEVAL_WINDOWS[0][:7]} -> {RETRIEVAL_WINDOWS[1][:7]} same-IMO retrieval among "
        f"{len(common)}: top-1 {np.mean(ranks == 0):.3f}, top-10 {np.mean(ranks < 10):.3f}, "
        f"median rank {int(np.median(ranks))} (random ~{len(common) // 2})"
    )
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Label-free diagnostics of the P4-3j embeddings")
    p.add_argument("--seed", type=int, default=0)
    print("\n".join(run(p.parse_args(argv).seed)))


if __name__ == "__main__":
    main()
