"""Unit tests for features.port_visits. All fixtures are synthetic, written to tmp_path or built
as in-memory DuckDB tables; nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from features import port_visits as pv

WINDOW_START = date(2024, 8, 1)
WINDOW_END = date(2024, 8, 31)


def _ts(*args: int) -> datetime:
    """A naive datetime literal, matching ingest.gfw_port_visits's naive-UTC-wall-clock storage
    convention (see that module's _parse_gfw_timestamp docstring) -- not an oversight."""
    return datetime(*args)  # noqa: DTZ001


def _run(
    panel_rows: list[tuple[int, str | None]],
    vessel_ids_rows: list[tuple[str, str]],
    port_visits_rows: list[tuple],
    window_end: date = WINDOW_END,
) -> dict[int, dict]:
    """panel_rows: (mmsi, imo). vessel_ids_rows: (imo, gfw_vessel_id). port_visits_rows:
    (imo, end, confidence, start_anchorage_flag, start_anchorage_name)."""
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE panel (mmsi BIGINT, imo VARCHAR)")
        if panel_rows:
            con.executemany("INSERT INTO panel VALUES (?, ?)", panel_rows)
        con.execute(
            "CREATE TABLE vids (imo VARCHAR, gfw_vessel_id VARCHAR, transmission_date_from TIMESTAMP)"
        )
        if vessel_ids_rows:
            # (imo, id) rows get an identity start long before any window; (imo, id, from) rows
            # set it explicitly.
            con.executemany(
                "INSERT INTO vids VALUES (?, ?, ?)",
                [r if len(r) == 3 else (*r, datetime(2000, 1, 1)) for r in vessel_ids_rows],  # noqa: DTZ001 -- naive UTC, as stored
            )
        con.execute(
            'CREATE TABLE visits (imo VARCHAR, "end" TIMESTAMP, confidence INTEGER, '
            "start_anchorage_flag VARCHAR, start_anchorage_name VARCHAR)"
        )
        if port_visits_rows:
            con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?, ?)", port_visits_rows)

        sql = pv.port_visits_features_sql(
            "SELECT * FROM panel", "SELECT * FROM vids", "SELECT * FROM visits", window_end
        )
        cur = con.execute(sql + " ORDER BY mmsi")
        names = [d[0] for d in cur.description]
        return {row[0]: dict(zip(names, row)) for row in cur.fetchall()}
    finally:
        con.close()


# --------------------------------------------------------------------------------------------
# gfw_resolved / unresolved NaN posture
# --------------------------------------------------------------------------------------------


def test_unresolved_imo_gets_zero_and_every_pv_column_null():
    rows = _run(panel_rows=[(1, "9111111")], vessel_ids_rows=[], port_visits_rows=[])
    r = rows[1]
    assert r["gfw_resolved"] == 0
    for col in ("pv_n_total", "pv_n_rus", "pv_any_rus", "pv_n_rus_oil", "pv_days_since_rus",
                "pv_share_south", "pv_n_sanctioned_states"):
        assert r[col] is None, col


def test_null_imo_mmsi_is_treated_as_unresolved():
    """An orphaned mmsi (no imo) can never resolve -- negative case, no special code path needed."""
    rows = _run(panel_rows=[(1, None)], vessel_ids_rows=[], port_visits_rows=[])
    r = rows[1]
    assert r["gfw_resolved"] == 0
    assert r["pv_n_total"] is None


def test_resolved_with_zero_visits_gets_real_zeros_not_null():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[],
    )
    r = rows[1]
    assert r["gfw_resolved"] == 1
    assert r["pv_n_total"] == 0
    assert r["pv_n_rus"] == 0
    assert r["pv_any_rus"] is False
    assert r["pv_n_rus_oil"] == 0
    assert r["pv_days_since_rus"] == 270
    assert r["pv_share_south"] == pytest.approx(0.0)
    assert r["pv_n_sanctioned_states"] == 0


# --------------------------------------------------------------------------------------------
# temporal filters: the 270-day lookback and the strict end < window_end leakage guard
# --------------------------------------------------------------------------------------------


def test_visit_ending_exactly_at_window_end_is_excluded():
    """The temporal-leakage guard: end == window_end must not count (strict '<')."""
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 31), 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 0
    assert rows[1]["pv_any_rus"] is False


def test_visit_ending_after_window_end_is_excluded():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 9, 15), 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 0


def test_visit_ending_just_before_window_end_is_included():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 30, 23, 59, 59), 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 1


def test_visit_within_270_days_is_included():
    # window_end - 269 days is well inside the lookback.
    end = datetime.combine(WINDOW_END, datetime.min.time())
    within = end.fromordinal(end.toordinal() - 269)
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", within, 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 1


def test_visit_older_than_270_days_is_excluded():
    end = datetime.combine(WINDOW_END, datetime.min.time())
    too_old = end.fromordinal(end.toordinal() - 271)
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", too_old, 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 0


def test_visit_exactly_270_days_before_window_end_is_included():
    """>= window_end - 270 days is inclusive at the boundary."""
    end = datetime.combine(WINDOW_END, datetime.min.time())
    boundary = end.fromordinal(end.toordinal() - 270)
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", boundary, 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 1


# --------------------------------------------------------------------------------------------
# confidence filter
# --------------------------------------------------------------------------------------------


def test_confidence_below_threshold_is_excluded():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 2, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 0


def test_confidence_at_threshold_is_included():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 3, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_total"] == 1


# --------------------------------------------------------------------------------------------
# RUS / oil-terminal matching
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["PRIMORSK", "UST LUGA", "UST-LUGA", "UST LUGA TERMINAL", "VYSOTSK", "NOVOROSSIYSK",
     "SHESKHARIS", "TAMAN", "TUAPSE", "KAVKAZ", "KOZMINO", "NAKHODKA", "DE KASTRI", "DE-KASTRI",
     "MURMANSK", "SABETTA", "SAINT PETERSBURG", "ST PETERSBURG", "ST. PETERSBURG"],
)
def test_oil_terminal_names_match(name):
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 4, "RUS", name)],
    )
    assert rows[1]["pv_n_rus_oil"] == 1, name


@pytest.mark.parametrize("name", ["KALININGRAD", "PIRAEUS", "ROTTERDAM", "USTKA"])
def test_non_oil_terminal_names_do_not_match(name):
    """Negative case: a RUS visit at a non-oil-terminal port counts toward pv_n_rus but not
    pv_n_rus_oil -- and USTKA (a real Polish port) must not accidentally match UST.?LUGA."""
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 4, "RUS", name)],
    )
    assert rows[1]["pv_n_rus_oil"] == 0, name
    assert rows[1]["pv_n_rus"] == 1, name


def test_non_rus_visit_to_an_oil_terminal_named_port_does_not_count_as_rus_oil():
    """Defensive: pv_n_rus_oil requires BOTH the RUS flag and the name match, not name alone."""
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 4, "EST", "PRIMORSK")],
    )
    assert rows[1]["pv_n_rus"] == 0
    assert rows[1]["pv_n_rus_oil"] == 0


def test_pv_any_rus_true_with_at_least_one_rus_visit():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 4, "RUS", "MURMANSK")],
    )
    assert rows[1]["pv_any_rus"] is True


def test_pv_days_since_rus_counts_from_most_recent_rus_visit():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[
            ("9111111", _ts(2024, 8, 1), 4, "RUS", "PRIMORSK"),   # 30 days before window_end
            ("9111111", _ts(2024, 8, 21), 4, "RUS", "MURMANSK"),  # 10 days before window_end
        ],
    )
    assert rows[1]["pv_days_since_rus"] == 10


def test_pv_days_since_rus_ignores_non_rus_visits():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 30), 4, "TUR", "ALIAGA")],
    )
    assert rows[1]["pv_days_since_rus"] == 270  # no RUS visit at all -> default


# --------------------------------------------------------------------------------------------
# south route share / sanctioned states
# --------------------------------------------------------------------------------------------


def test_pv_share_south_is_the_fraction_of_qualifying_visits():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[
            ("9111111", _ts(2024, 8, 5), 4, "TUR", "ALIAGA"),
            ("9111111", _ts(2024, 8, 10), 4, "IND", "SIKKA"),
            ("9111111", _ts(2024, 8, 15), 4, "DNK", "SKAGEN"),
            ("9111111", _ts(2024, 8, 20), 4, "DNK", "SKAGEN"),
        ],
    )
    assert rows[1]["pv_n_total"] == 4
    assert rows[1]["pv_share_south"] == pytest.approx(0.5)


def test_pv_n_sanctioned_states_counts_distinct_states_not_visits():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[
            ("9111111", _ts(2024, 8, 5), 4, "IRN", "BANDAR ABBAS"),
            ("9111111", _ts(2024, 8, 10), 4, "IRN", "BANDAR ABBAS"),  # repeat -- same state
            ("9111111", _ts(2024, 8, 15), 4, "VEN", "MARACAIBO"),
            ("9111111", _ts(2024, 8, 20), 4, "DNK", "SKAGEN"),  # not sanctioned
        ],
    )
    assert rows[1]["pv_n_sanctioned_states"] == 2


def test_pv_n_sanctioned_states_zero_when_none_visited():
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1")],
        port_visits_rows=[("9111111", _ts(2024, 8, 5), 4, "DNK", "SKAGEN")],
    )
    assert rows[1]["pv_n_sanctioned_states"] == 0


# --------------------------------------------------------------------------------------------
# multi-vessel / multi-id-per-imo joins
# --------------------------------------------------------------------------------------------


def test_two_mmsi_sharing_one_imo_get_the_same_features():
    """A reused/re-flagged mmsi joins by imo, not gfw_vessel_id -- both panel rows for the same
    imo must see the same port-visit history."""
    rows = _run(
        panel_rows=[(1, "9111111"), (2, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1"), ("9111111", "vid-2")],
        port_visits_rows=[("9111111", _ts(2024, 8, 20), 4, "RUS", "PRIMORSK")],
    )
    assert rows[1]["pv_n_rus_oil"] == rows[2]["pv_n_rus_oil"] == 1


# --------------------------------------------------------------------------------------------
# panel_window_dirs
# --------------------------------------------------------------------------------------------


def test_panel_window_dirs_skips_the_two_day_validation_window(tmp_path):
    panel_root = tmp_path / "panel"
    (panel_root / "window=2024-06-01_2024-06-30").mkdir(parents=True)
    (panel_root / "window=2024-06-10_2024-06-11").mkdir(parents=True)
    (panel_root / "window=2024-11-01_2024-11-30").mkdir(parents=True)

    result = pv.panel_window_dirs(panel_root)

    names = [d.name for _s, _e, d in result]
    assert "window=2024-06-10_2024-06-11" not in names
    assert set(names) == {"window=2024-06-01_2024-06-30", "window=2024-11-01_2024-11-30"}


def test_panel_window_dirs_keeps_real_windows_only(tmp_path):
    panel_root = tmp_path / "panel"
    (panel_root / "window=2024-06-01_2024-06-30").mkdir(parents=True)
    result = pv.panel_window_dirs(panel_root)
    assert len(result) == 1
    start, end, _d = result[0]
    assert start == date(2024, 6, 1) and end == date(2024, 6, 30)


# --------------------------------------------------------------------------------------------
# distinct_rus_anchorage_names
# --------------------------------------------------------------------------------------------


def test_distinct_rus_anchorage_names_reports_matched_flag(tmp_path):
    con = duckdb.connect()
    out_path = tmp_path / "port_visits.parquet"
    try:
        con.execute(
            'CREATE TABLE t (imo VARCHAR, gfw_vessel_id VARCHAR, event_id VARCHAR, '
            'visit_id VARCHAR, "start" TIMESTAMP, "end" TIMESTAMP, confidence INTEGER, '
            "duration_hrs DOUBLE, start_anchorage_id VARCHAR, start_anchorage_name VARCHAR, "
            "start_anchorage_flag VARCHAR, start_anchorage_lat DOUBLE, start_anchorage_lon DOUBLE, "
            "start_anchorage_at_dock BOOLEAN, end_anchorage_id VARCHAR, end_anchorage_name VARCHAR, "
            "end_anchorage_flag VARCHAR)"
        )
        con.execute(
            "INSERT INTO t (imo, \"start\", \"end\", confidence, start_anchorage_name, "
            "start_anchorage_flag) VALUES "
            "('1', '2024-08-01', '2024-08-02', 4, 'PRIMORSK', 'RUS'), "
            "('2', '2024-08-01', '2024-08-02', 4, 'KALININGRAD', 'RUS'), "
            "('3', '2024-08-01', '2024-08-02', 4, 'PIRAEUS', 'GRC')"
        )
        con.execute(f"COPY t TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()

    result = pv.distinct_rus_anchorage_names(out_path)

    by_name = dict(result)
    assert by_name["PRIMORSK"] is True
    assert by_name["KALININGRAD"] is False
    assert "PIRAEUS" not in by_name  # not RUS-flagged, excluded


# --------------------------------------------------------------------------------------------
# build_port_visits_features: real parquet round-trip
# --------------------------------------------------------------------------------------------


def _write_panel(path: Path, rows: list[tuple[int, str | None]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, imo VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_vessel_ids(path: Path, rows: list[tuple]) -> None:
    """rows: (imo, gfw_vessel_id) or (imo, gfw_vessel_id, use_for_features)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (imo VARCHAR, gfw_vessel_id VARCHAR, use_for_features BOOLEAN, "
            "transmission_date_from TIMESTAMP)"
        )
        con.executemany(
            "INSERT INTO t VALUES (?, ?, ?, ?)",
            [(*((*r, True) if len(r) == 2 else r), datetime(2000, 1, 1)) for r in rows],  # noqa: DTZ001 -- naive UTC, as stored
        )
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_port_visits(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            'CREATE TABLE t (imo VARCHAR, "end" TIMESTAMP, confidence INTEGER, '
            "start_anchorage_flag VARCHAR, start_anchorage_name VARCHAR)"
        )
        con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _read(out_path: Path) -> dict[int, dict]:
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM '{out_path.as_posix()}' ORDER BY mmsi")
        names = [d[0] for d in cur.description]
        return {row[0]: dict(zip(names, row)) for row in cur.fetchall()}
    finally:
        con.close()


def test_build_port_visits_features_writes_parquet_and_is_idempotent(tmp_path):
    panel_dir = tmp_path / "panel" / "window=2024-08-01_2024-08-31"
    _write_panel(panel_dir / "part-0.parquet", [(1, "9111111"), (2, None)])
    vessel_ids_path = tmp_path / "vessel_ids.parquet"
    _write_vessel_ids(vessel_ids_path, [("9111111", "vid-1")])
    port_visits_path = tmp_path / "port_visits.parquet"
    _write_port_visits(
        port_visits_path, [("9111111", _ts(2024, 8, 20), 4, "RUS", "PRIMORSK")]
    )

    out_path = pv.build_port_visits_features(
        WINDOW_START, WINDOW_END, panel_dir,
        vessel_ids_path=vessel_ids_path, port_visits_path=port_visits_path,
        out_root=tmp_path / "out",
    )

    rows = _read(out_path)
    assert rows[1]["gfw_resolved"] == 1
    assert rows[1]["pv_n_rus_oil"] == 1
    assert rows[2]["gfw_resolved"] == 0
    assert rows[2]["pv_n_total"] is None
    assert rows[1]["window_start"] == WINDOW_START
    assert rows[1]["window_end"] == WINDOW_END

    mtime_before = out_path.stat().st_mtime_ns
    pv.build_port_visits_features(
        WINDOW_START, WINDOW_END, panel_dir,
        vessel_ids_path=vessel_ids_path, port_visits_path=port_visits_path,
        out_root=tmp_path / "out",
    )
    assert out_path.stat().st_mtime_ns == mtime_before  # re-run without force is a no-op


def test_build_port_visits_features_ignores_identities_not_marked_for_features(tmp_path):
    """A registry-only / shared identity (use_for_features false) must leave the imo unresolved."""
    panel_dir = tmp_path / "panel" / "window=2024-08-01_2024-08-31"
    _write_panel(panel_dir / "part-0.parquet", [(1, "9111111"), (2, "9222222")])
    vessel_ids_path = tmp_path / "vessel_ids.parquet"
    _write_vessel_ids(vessel_ids_path, [("9111111", "vid-1", False), ("9222222", "vid-2")])
    port_visits_path = tmp_path / "port_visits.parquet"
    _write_port_visits(port_visits_path, [("9222222", _ts(2024, 8, 20), 4, "RUS", "PRIMORSK")])

    out_path = pv.build_port_visits_features(
        WINDOW_START, WINDOW_END, panel_dir,
        vessel_ids_path=vessel_ids_path, port_visits_path=port_visits_path,
        out_root=tmp_path / "out",
    )
    rows = _read(out_path)
    assert rows[1]["gfw_resolved"] == 0
    assert rows[2]["gfw_resolved"] == 1


def test_build_port_visits_features_tolerates_missing_reference_files(tmp_path):
    """Before ingest.gfw_port_visits has ever run, both reference parquet files are absent --
    every mmsi must still get a well-formed (unresolved) row, not a crash."""
    panel_dir = tmp_path / "panel" / "window=2024-08-01_2024-08-31"
    _write_panel(panel_dir / "part-0.parquet", [(1, "9111111")])

    out_path = pv.build_port_visits_features(
        WINDOW_START, WINDOW_END, panel_dir,
        vessel_ids_path=tmp_path / "does_not_exist" / "vessel_ids.parquet",
        port_visits_path=tmp_path / "does_not_exist" / "port_visits.parquet",
        out_root=tmp_path / "out",
    )

    rows = _read(out_path)
    assert rows[1]["gfw_resolved"] == 0
    assert rows[1]["pv_n_total"] is None


def test_identity_starting_after_window_end_does_not_resolve_the_imo():
    """analyst-review 2026-09-30: an IMO whose only kept identity starts after window_end is not
    resolved as of window_end (gfw_resolved 0, every pv_* NULL)."""
    rows = _run(
        panel_rows=[(1, "9111111")],
        vessel_ids_rows=[("9111111", "vid-1", datetime(2024, 9, 5))],  # noqa: DTZ001 -- naive UTC, as stored
        port_visits_rows=[],
    )
    assert rows[1]["gfw_resolved"] == 0
    assert rows[1]["pv_n_total"] is None
