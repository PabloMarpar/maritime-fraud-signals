"""Unit tests for features.static. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from features import static

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
DAY_OUTSIDE = date(2024, 7, 1)


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """rows is a list of (mmsi, length, width, draught, destination) tuples."""
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, length DOUBLE, width DOUBLE, draught DOUBLE, "
            "destination VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(
            f"COPY clean TO '{(partition_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)"
        )
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


def _classify(destination: str | None) -> tuple[bool, bool, bool]:
    """(dest_russia, dest_south_route, dest_for_orders) for a single-message vessel."""
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, length DOUBLE, width DOUBLE, draught DOUBLE, "
                    "destination VARCHAR)")
        con.execute("INSERT INTO t VALUES (1, NULL, NULL, NULL, ?)", [destination])
        return con.execute(
            "SELECT dest_russia, dest_south_route, dest_for_orders FROM ("
            + static.static_features_sql("SELECT * FROM t") + ")"
        ).fetchone()
    finally:
        con.close()


def _build(tmp_path: Path, partitions: dict[date, list[tuple]]) -> dict[int, dict]:
    in_root = tmp_path / "clean"
    for day, rows in partitions.items():
        _write_clean_partition(in_root, day, rows)
    out = static.build_static_features(DAY, DAY2, in_root=in_root, out_root=tmp_path / "static")
    return _read(out)


def test_size_and_draught_ignore_zero_and_null(tmp_path):
    rows = _build(
        tmp_path,
        {
            DAY: [
                (1, 0.0, 0.0, 0.0, None),
                (1, 244.0, 42.0, 8.0, "PRIMORSK"),
                (1, 246.0, 44.0, None, "PRIMORSK"),
            ],
            DAY2: [(1, None, None, 14.5, "PORT SAID")],
        },
    )
    r = rows[1]
    assert r["n_messages"] == 4
    assert r["length_m"] == pytest.approx(245.0)
    assert r["width_m"] == pytest.approx(43.0)
    assert r["min_draught_m"] == pytest.approx(8.0)
    assert r["max_draught_m"] == pytest.approx(14.5)
    assert r["draught_range_m"] == pytest.approx(6.5)


def test_vessel_that_never_declares_anything_gets_nulls_and_false_flags(tmp_path):
    r = _build(tmp_path, {DAY: [(2, 0.0, None, 0.0, "Unknown"), (2, None, 0.0, None, "  ")]})[2]
    assert r["length_m"] is None and r["width_m"] is None
    assert r["min_draught_m"] is None and r["draught_range_m"] is None
    assert r["n_destinations"] == 0
    assert (r["dest_russia"], r["dest_south_route"], r["dest_for_orders"]) == (False, False, False)


def test_n_destinations_normalizes_case_and_whitespace_and_skips_unknown(tmp_path):
    r = _build(
        tmp_path,
        {DAY: [(3, None, None, None, d) for d in ("ust luga", "UST  LUGA ", "UNKNOWN", "Skagen")]},
    )[3]
    assert r["n_destinations"] == 2


def test_partitions_outside_the_window_are_not_read(tmp_path):
    rows = _build(
        tmp_path,
        {
            DAY: [(4, 180.0, 30.0, 9.0, "SKAGEN")],
            DAY_OUTSIDE: [(4, 180.0, 30.0, 9.0, "RU ULU"), (5, 250.0, 44.0, 15.0, "PRIMORSK")],
        },
    )
    assert set(rows) == {4}
    assert rows[4]["dest_russia"] is False


def test_output_carries_window_bounds(tmp_path):
    r = _build(tmp_path, {DAY: [(6, 100.0, 20.0, 5.0, "DKSKA")]})[6]
    assert r["window_start"] == DAY and r["window_end"] == DAY2


@pytest.mark.parametrize(
    "destination",
    ["RU ULU", "RUULU", "RU PRI", "RUPRI>TRTUT", "PRIMORSK", "UST-LUGA", "UST LUGA",
     "ST.PETERSBURG", "GULF OF FINLAND", "FINLAND GULF", "CV MIN > RU ULU", "VYSOTSK"],
)
def test_russian_destinations_match(destination):
    assert _classify(destination)[0] is True


@pytest.mark.parametrize(
    "destination",
    ["PORT SAID", "EGPSD", "EG PSD", "EGSUZ", "SUEZ", "INDIA FOR ORDERS", "IN SIK", "SIKKA",
     "ALIAGA", "TR ALI", "KALAMATA", "CN NGB", "FUJAIRAH"],
)
def test_south_route_destinations_match(destination):
    assert _classify(destination)[1] is True


@pytest.mark.parametrize(
    "destination", ["BALTIC FOR ORDERS", "FOR ORDERS", "SKAGEN FOR ORDER", "TALLIN OPL"]
)
def test_for_orders_destinations_match(destination):
    assert _classify(destination)[2] is True


@pytest.mark.parametrize(
    "destination",
    # Ordinary North Sea / Baltic traffic, and near-misses a loose regex would catch: a word
    # merely *containing* "RU"/"IN"/"CN", Rotterdam, Gothenburg, Skagen, Tallinn, Gdansk.
    ["NLRTM", "SE GOT", "DK SKA", "SKAGEN", "TALLINN", "PLGDN", "DEHAM", "BRUNSBUTTEL",
     "INVERNESS", "CNOSSOS", "HAMBURG", "UNKNOWN", None],
)
def test_ordinary_destinations_match_nothing(destination):
    assert _classify(destination) == (False, False, False)


def test_existing_output_is_not_rebuilt_without_force(tmp_path):
    in_root = tmp_path / "clean"
    _write_clean_partition(in_root, DAY, [(7, 100.0, 20.0, 5.0, "SKAGEN")])
    out = static.build_static_features(DAY, DAY2, in_root=in_root, out_root=tmp_path / "s")
    _write_clean_partition(in_root, DAY2, [(8, 100.0, 20.0, 5.0, "SKAGEN")])
    assert set(_read(static.build_static_features(
        DAY, DAY2, in_root=in_root, out_root=tmp_path / "s")
    )) == {7}
    static.build_static_features(DAY, DAY2, in_root=in_root, out_root=tmp_path / "s", force=True)
    assert set(_read(out)) == {7, 8}


def test_no_partitions_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        static.build_static_features(DAY, DAY2, in_root=tmp_path / "empty", out_root=tmp_path)
