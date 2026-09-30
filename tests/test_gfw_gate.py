"""Synthetic tests for model.gfw_gate."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from model import gfw_gate as g
from model.sealed_split import SEALED, groups

# Non-sealed / sealed synthetic IMOs (crc32-hash based, so found rather than hard-coded).
_ALL = [str(9000000 + i) for i in range(400)]
_GROUP = dict(zip(_ALL, groups(np.array(_ALL))))
DEV = [i for i in _ALL if _GROUP[i] != SEALED]
SEALED_IMOS = [i for i in _ALL if _GROUP[i] == SEALED]


def _pop(n_pos, n_neg, pos_vals, neg_vals, **extra):
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"positive": [True] * n_pos + [False] * n_neg})
    df["resolved"] = np.concatenate(
        [np.arange(n_pos) < round(n_pos * pos_vals), np.arange(n_neg) < round(n_neg * neg_vals)]
    )
    df["any_visit"] = df["resolved"]
    df["n_identities"] = df["resolved"].astype(float)
    df["pv_n_total"] = rng.poisson(3, n_pos + n_neg).astype(float)
    df["hist_n_mmsi_730d"] = rng.poisson(1, n_pos + n_neg).astype(float)
    df["n_id_rows"] = 2.0
    df["n_registry_only_rows"] = 1.0
    for k, v in extra.items():
        df[k] = v
    return df


def test_auc_known_values():
    assert g.auc(np.array([3.0, 4.0]), np.array([1.0, 2.0])) == 1.0
    assert g.auc(np.array([1.0, 2.0]), np.array([3.0, 4.0])) == 0.0
    assert g.auc(np.array([1.0, 1.0]), np.array([1.0, 1.0])) == 0.5


def test_equal_coverage_is_go_and_unflagged():
    r = g.evaluate(_pop(300, 300, 0.6, 0.6), n_bootstrap=200)
    assert r["go"]
    assert r["flagged"] == []


def test_coverage_gap_over_five_points_is_no_go():
    r = g.evaluate(_pop(400, 400, 0.9, 0.6), n_bootstrap=200)
    assert not r["go"]
    assert r["rates"][0].diff > 0.05


def test_richness_gap_is_flagged_only_when_interval_clears_the_band():
    df = _pop(300, 300, 0.6, 0.6)
    df.loc[df["positive"], "pv_n_total"] += 20  # every positive richer than every negative
    r = g.evaluate(df, n_bootstrap=200)
    assert "pv_n_total" in r["flagged"]
    assert "hist_n_mmsi_730d" not in r["flagged"]


def test_registry_only_share_reported_by_class():
    r = g.evaluate(_pop(50, 50, 0.6, 0.6), n_bootstrap=50)
    assert r["dropped"]["positive"]["share"] == pytest.approx(0.5)
    assert r["dropped"]["negative"]["registry_only_rows"] == 50


def _write(con, sql_table: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY {sql_table} TO '{path.as_posix()}' (FORMAT PARQUET)")


def test_load_population_filters_sealed_asof_and_unclassified(tmp_path):
    pos, neg, ever_only, designated, sealed = DEV[0], DEV[1], DEV[2], DEV[3], SEALED_IMOS[0]
    con = duckdb.connect()
    # panel_v2: (mmsi, imo, as_of, after, ever)
    rows = [
        (1, pos, False, True, True), (2, neg, False, False, False),
        (3, ever_only, False, False, True),  # ever-sanctioned but not forward: dropped
        (4, designated, True, False, True),  # already designated at window end: dropped
        (5, sealed, False, True, True),  # sealed: dropped
    ]
    con.execute(
        "CREATE TABLE p (mmsi BIGINT, imo VARCHAR, label_is_sanctioned_as_of_window_end BOOLEAN, "
        "label_is_sanctioned_after_window_end BOOLEAN, label_is_sanctioned_ever BOOLEAN)"
    )
    con.executemany("INSERT INTO p VALUES (?, ?, ?, ?, ?)", rows)
    _write(con, "p", tmp_path / "panel_v2" / "window=2024-06-01_2024-06-30" / "part-0.parquet")

    con.execute(
        "CREATE TABLE v (imo VARCHAR, gfw_vessel_id VARCHAR, match_basis VARCHAR, "
        "use_for_features BOOLEAN)"
    )
    con.executemany(
        "INSERT INTO v VALUES (?, ?, ?, ?)",
        [(pos, "a", "self_reported_imo", True), (pos, "b", "registry_only", False),
         (neg, "c", "registry_only", False)],
    )
    _write(con, "v", tmp_path / "vessel_ids.parquet")

    con.execute(
        'CREATE TABLE e (imo VARCHAR, "end" TIMESTAMP, confidence INTEGER)'
    )
    con.executemany(
        "INSERT INTO e VALUES (?, ?, ?)",
        [(pos, datetime(2024, 5, 1), 4), (neg, datetime(2024, 5, 1), 2)],  # neg: low confidence  # noqa: DTZ001
    )
    _write(con, "e", tmp_path / "port_visits.parquet")

    con.execute("CREATE TABLE f (mmsi BIGINT, pv_n_total BIGINT, window_end DATE)")
    con.executemany("INSERT INTO f VALUES (?, ?, DATE '2024-06-30')", [(1, 7), (2, None)])
    _write(con, "f", tmp_path / "pvf" / "window=2024-06-01_2024-06-30" / "part-0.parquet")
    con.execute("CREATE TABLE h (mmsi BIGINT, hist_n_mmsi_730d BIGINT, window_end DATE)")
    con.executemany("INSERT INTO h VALUES (?, ?, DATE '2024-06-30')", [(1, 2), (2, None)])
    _write(con, "h", tmp_path / "hist" / "window=2024-06-01_2024-06-30" / "part-0.parquet")
    con.close()

    df = g.load_population(
        tmp_path / "panel_v2", tmp_path / "vessel_ids.parquet", tmp_path / "port_visits.parquet",
        tmp_path / "pvf", tmp_path / "hist",
    ).set_index("imo")

    assert set(df.index) == {pos, neg}
    assert bool(df.loc[pos, "positive"]) and not bool(df.loc[neg, "positive"])
    assert df.loc[pos, "n_identities"] == 1 and df.loc[pos, "n_registry_only_rows"] == 1
    assert bool(df.loc[pos, "resolved"]) and not bool(df.loc[neg, "resolved"])
    assert bool(df.loc[pos, "any_visit"]) and not bool(df.loc[neg, "any_visit"])
    assert df.loc[pos, "pv_n_total"] == 7 and df.loc[neg, "pv_n_total"] == 0
    assert df.loc[pos, "hist_n_mmsi_730d"] == 2
