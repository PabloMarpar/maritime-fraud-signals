"""Contamination gate for the GFW-derived features (``features.port_visits``, ``features.history``),
committed after the ``analyst-review`` of the push before unsealing (``docs/DECISIONS.md``,
2026-09-29). It replaces the ad-hoc P4-11 check.

**Why.** GFW's identity and port-visit data are compiled with later knowledge; a vessel may be
*better covered* because it was later sanctioned. If coverage or richness differs between future
positives and never-sanctioned vessels, a GFW feature can predict the label through data
availability rather than behaviour.

**Population.** Non-sealed IMOs only (``model.sealed_split.groups`` != sealed; sealed labels are
never touched). Distinct IMOs across ``data/processed/panel_v2/window=*`` rows with ``imo IS NOT
NULL AND NOT label_is_sanctioned_as_of_window_end`` (already designated by the window is not
"future"). *Positive* = any row with ``label_is_sanctioned_after_window_end``; *negative* = not
``label_is_sanctioned_ever``; the rest (ever-sanctioned but never flagged forward) are dropped.

**(a) Coverage.** Resolution rate (at least one kept GFW identity) and any-port-visit rate (at
least one kept event ending in 2023-06-01..2025-02-28 with confidence >= 3) per class; difference
(positive - negative) with a 95% bootstrap interval. **GO iff |difference| <= 0.05** for both.

**(b) Richness.** Per IMO: number of kept GFW identities, ``pv_n_total`` and ``hist_n_mmsi_730d``.
The last two are taken from the **last window the IMO appears in** (the panel window with the
latest ``window_end`` containing it; max over that window's mmsi), unresolved IMOs counting as 0.
AUC of each (positive vs negative) with a 95% bootstrap interval; **FLAG** if the whole interval
lies outside [0.45, 0.55]. A flag is reported as a documented limitation, not an automatic NO-GO
(these are also legitimate signals).

**(c) Registry-only share.** The share of GFW identity rows dropped as ``registry_only`` (matched
only through ``registryInfo``, never used), by class, plus the placeholder / shared-id counts.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import rankdata

from model.sealed_split import SEALED, groups

logger = logging.getLogger(__name__)

PANEL_V2_ROOT = Path("data/processed/panel_v2")
PORT_VISITS_FEATURES_ROOT = Path("data/processed/port_visits")
HISTORY_FEATURES_ROOT = Path("data/processed/history")
VESSEL_IDS_PATH = Path("data/reference/gfw/vessel_ids.parquet")
PORT_VISITS_PATH = Path("data/reference/gfw/port_visits.parquet")
OUT_PATH = Path("outputs/gfw_gate.txt")

VISIT_START = "2023-06-01"
VISIT_END = "2025-02-28"
MIN_CONFIDENCE = 3
MAX_RATE_DIFF = 0.05
AUC_BAND = (0.45, 0.55)
N_BOOTSTRAP = 2000
BOOTSTRAP_SEED = 0


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """P(pos > neg) + 0.5 P(pos == neg), via ranks."""
    n_pos, n_neg = len(pos), len(neg)
    ranks = rankdata(np.concatenate([pos, neg]))
    return float((ranks[:n_pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def bootstrap_ci(
    pos: np.ndarray, neg: np.ndarray, stat, n_bootstrap: int = N_BOOTSTRAP, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    """95% percentile interval of ``stat(pos, neg)``, resampling each class independently."""
    rng = np.random.default_rng(seed)
    vals = np.empty(n_bootstrap)
    for i in range(n_bootstrap):
        vals[i] = stat(
            pos[rng.integers(0, len(pos), len(pos))], neg[rng.integers(0, len(neg), len(neg))]
        )
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def rate_diff(pos: np.ndarray, neg: np.ndarray) -> float:
    return float(np.mean(pos) - np.mean(neg))


@dataclass(frozen=True)
class RateResult:
    name: str
    rate_pos: float
    rate_neg: float
    diff: float
    ci: tuple[float, float]

    @property
    def go(self) -> bool:
        return abs(self.diff) <= MAX_RATE_DIFF


@dataclass(frozen=True)
class AucResult:
    name: str
    median_pos: float
    median_neg: float
    auc: float
    ci: tuple[float, float]

    @property
    def flagged(self) -> bool:
        return self.ci[0] > AUC_BAND[1] or self.ci[1] < AUC_BAND[0]


def rate_result(name: str, pos: np.ndarray, neg: np.ndarray, n_bootstrap: int, seed: int) -> RateResult:
    pos, neg = pos.astype(float), neg.astype(float)
    return RateResult(
        name, float(pos.mean()), float(neg.mean()), rate_diff(pos, neg),
        bootstrap_ci(pos, neg, rate_diff, n_bootstrap, seed),
    )


def auc_result(name: str, pos: np.ndarray, neg: np.ndarray, n_bootstrap: int, seed: int) -> AucResult:
    pos, neg = pos.astype(float), neg.astype(float)
    return AucResult(
        name, float(np.median(pos)), float(np.median(neg)), auc(pos, neg),
        bootstrap_ci(pos, neg, auc, n_bootstrap, seed),
    )


def load_population(
    panel_root: Path = PANEL_V2_ROOT,
    vessel_ids_path: Path = VESSEL_IDS_PATH,
    port_visits_path: Path = PORT_VISITS_PATH,
    port_visits_features_root: Path = PORT_VISITS_FEATURES_ROOT,
    history_root: Path = HISTORY_FEATURES_ROOT,
) -> pd.DataFrame:
    """One row per non-sealed classified IMO: ``positive`` (bool), ``resolved``, ``any_visit``,
    ``n_identities``, ``n_registry_only_rows``, ``n_id_rows``, ``pv_n_total`` and
    ``hist_n_mmsi_730d`` (last window), aggregates only -- no sealed IMO's label is read past the
    initial per-IMO aggregation, which is discarded for sealed IMOs before anything is reported."""
    con = duckdb.connect()
    try:
        panel = (panel_root / "window=*" / "part-0.parquet").as_posix()
        imo_df = con.execute(
            f"""
            SELECT imo,
                   bool_or(coalesce(label_is_sanctioned_after_window_end, false)) AS fwd,
                   bool_or(coalesce(label_is_sanctioned_ever, false)) AS ever
            FROM read_parquet('{panel}')
            WHERE imo IS NOT NULL AND NOT coalesce(label_is_sanctioned_as_of_window_end, false)
            GROUP BY imo
            """
        ).df()
        imo_df = imo_df[groups(imo_df["imo"].to_numpy()) != SEALED].copy()
        imo_df["positive"] = imo_df["fwd"]
        imo_df = imo_df[imo_df["fwd"] | ~imo_df["ever"]].drop(columns=["fwd", "ever"])
        con.register("pop", imo_df)

        ids = con.execute(
            f"""
            SELECT imo, count(*) AS n_id_rows,
                   count(*) FILTER (WHERE use_for_features) AS n_identities,
                   count(*) FILTER (WHERE match_basis = 'registry_only') AS n_registry_only_rows
            FROM read_parquet('{vessel_ids_path.as_posix()}') GROUP BY imo
            """
        ).df()
        visits = con.execute(
            f"""
            SELECT DISTINCT imo, true AS any_visit
            FROM read_parquet('{port_visits_path.as_posix()}')
            WHERE confidence >= {MIN_CONFIDENCE}
              AND "end" >= TIMESTAMP '{VISIT_START}' AND "end" < TIMESTAMP '{VISIT_END}' + INTERVAL 1 DAY
            """
        ).df()
        # Last window per imo, then the feature value in that window (max over its mmsi).
        last = con.execute(
            f"""
            WITH pv AS (
                SELECT imo, mmsi, CAST(split_part("window", '_', 2) AS DATE) AS window_end
                FROM read_parquet('{panel}', hive_partitioning = true) WHERE imo IS NOT NULL
            ),
            last_w AS (SELECT imo, max(window_end) AS window_end FROM pv GROUP BY imo)
            SELECT l.imo, max(f.pv_n_total) AS pv_n_total, max(h.hist_n_mmsi_730d) AS hist_n_mmsi_730d
            FROM last_w l
            JOIN pv ON pv.imo = l.imo AND pv.window_end = l.window_end
            LEFT JOIN read_parquet('{(port_visits_features_root / "window=*" / "part-0.parquet").as_posix()}') f
                   ON f.mmsi = pv.mmsi AND f.window_end = l.window_end
            LEFT JOIN read_parquet('{(history_root / "window=*" / "part-0.parquet").as_posix()}') h
                   ON h.mmsi = pv.mmsi AND h.window_end = l.window_end
            GROUP BY l.imo
            """
        ).df()
    finally:
        con.close()
    df = (
        imo_df.merge(ids, on="imo", how="left")
        .merge(visits, on="imo", how="left")
        .merge(last, on="imo", how="left")
    )
    for col in ("n_id_rows", "n_identities", "n_registry_only_rows", "pv_n_total", "hist_n_mmsi_730d"):
        df[col] = df[col].fillna(0).astype(float)
    df["any_visit"] = df["any_visit"].fillna(False).astype(bool)
    df["resolved"] = df["n_identities"] > 0
    return df


def evaluate(df: pd.DataFrame, n_bootstrap: int = N_BOOTSTRAP, seed: int = BOOTSTRAP_SEED) -> dict:
    p, n = df[df["positive"]], df[~df["positive"]]
    if len(p) < 2 or len(n) < 2:
        raise ValueError("gate needs at least 2 positive and 2 negative IMOs")
    rates = [
        rate_result("resolved (>=1 kept GFW identity)", p["resolved"].to_numpy(),
                    n["resolved"].to_numpy(), n_bootstrap, seed),
        rate_result("any port visit (2023-06-01..2025-02-28, conf>=3)", p["any_visit"].to_numpy(),
                    n["any_visit"].to_numpy(), n_bootstrap, seed),
    ]
    aucs = [
        auc_result(col, p[col].to_numpy(), n[col].to_numpy(), n_bootstrap, seed)
        for col in ("n_identities", "pv_n_total", "hist_n_mmsi_730d")
    ]
    dropped = {}
    for label, sub in (("positive", p), ("negative", n)):
        rows = float(sub["n_id_rows"].sum())
        reg = float(sub["n_registry_only_rows"].sum())
        dropped[label] = {
            "id_rows": int(rows), "registry_only_rows": int(reg),
            "share": reg / rows if rows else float("nan"),
            "imos_with_registry_only": int((sub["n_registry_only_rows"] > 0).sum()),
        }
    return {
        "n_pos": len(p), "n_neg": len(n), "rates": rates, "aucs": aucs, "dropped": dropped,
        "go": all(r.go for r in rates), "flagged": [a.name for a in aucs if a.flagged],
    }


def shared_and_placeholder_counts(vessel_ids_path: Path = VESSEL_IDS_PATH) -> dict[str, int]:
    """Label-free file-level counts: identity rows, ids claimed by several imos, placeholder-imo
    rows, registry-only rows, kept rows."""
    con = duckdb.connect()
    try:
        row = con.execute(
            f"""
            SELECT count(*), count(DISTINCT gfw_vessel_id) FILTER (WHERE id_shared),
                   count(*) FILTER (WHERE imo IN ('1234567', '5555555')),
                   count(*) FILTER (WHERE match_basis = 'registry_only'),
                   count(*) FILTER (WHERE use_for_features), count(DISTINCT imo)
            FROM read_parquet('{vessel_ids_path.as_posix()}')
            """
        ).fetchone()
    finally:
        con.close()
    keys = ("id_rows", "shared_ids", "placeholder_imo_rows", "registry_only_rows", "kept_rows", "imos")
    return dict(zip(keys, row))


def format_report(result: dict, file_counts: dict[str, int]) -> str:
    lines = [
        "GFW contamination gate (non-sealed IMOs; positive = forward-sanctioned, negative = never)",
        f"IMOs: {result['n_pos']} positive, {result['n_neg']} negative",
        "",
        "(a) coverage  [GO iff |diff| <= 0.05]",
    ]
    for r in result["rates"]:
        lines.append(
            f"  {r.name}: pos {r.rate_pos:.3f} neg {r.rate_neg:.3f} diff {r.diff:+.3f} "
            f"95% CI [{r.ci[0]:+.3f}, {r.ci[1]:+.3f}] -> {'GO' if r.go else 'NO-GO'}"
        )
    lines.append(f"  VERDICT: {'GO' if result['go'] else 'NO-GO'}")
    lines += ["", "(b) richness  [FLAG iff the AUC interval lies outside [0.45, 0.55]]"]
    lines.append(
        "  pv_n_total / hist_n_mmsi_730d: per IMO, from the last window it appears in; "
        "unresolved = 0"
    )
    for a in result["aucs"]:
        lines.append(
            f"  {a.name}: median pos {a.median_pos:g} neg {a.median_neg:g} AUC {a.auc:.3f} "
            f"95% CI [{a.ci[0]:.3f}, {a.ci[1]:.3f}] -> {'FLAG' if a.flagged else 'ok'}"
        )
    lines.append(f"  FLAGGED: {', '.join(result['flagged']) or 'none'}")
    lines += ["", "(c) identities dropped as registry-only (matched only via registryInfo)"]
    for label, d in result["dropped"].items():
        lines.append(
            f"  {label}: {d['registry_only_rows']} of {d['id_rows']} identity rows "
            f"({100 * d['share']:.1f}%); {d['imos_with_registry_only']} IMOs affected"
        )
    lines += ["", "file-level counts (all IMOs, label-free)"]
    lines += [f"  {k}: {v}" for k, v in file_counts.items()]
    return "\n".join(lines) + "\n"


def run_gate(
    out_path: Path = OUT_PATH,
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
    panel_root: Path = PANEL_V2_ROOT,
    vessel_ids_path: Path = VESSEL_IDS_PATH,
    port_visits_path: Path = PORT_VISITS_PATH,
    port_visits_features_root: Path = PORT_VISITS_FEATURES_ROOT,
    history_root: Path = HISTORY_FEATURES_ROOT,
) -> str:
    df = load_population(
        panel_root, vessel_ids_path, port_visits_path, port_visits_features_root, history_root
    )
    report = format_report(evaluate(df, n_bootstrap, seed), shared_and_placeholder_counts(vessel_ids_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GFW feature contamination gate.")
    parser.add_argument("--out-path", default=str(OUT_PATH))
    parser.add_argument("--n-bootstrap", type=int, default=N_BOOTSTRAP)
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    print(run_gate(Path(args.out_path), args.n_bootstrap, args.seed))


if __name__ == "__main__":
    main()
