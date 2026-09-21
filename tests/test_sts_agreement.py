"""Unit tests for detect.sts_agreement."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from detect import sts_agreement as agr

UTC = timezone.utc


def _event(source_id, mmsi_a, mmsi_b, start, end):
    return agr.MatchableEvent(
        source_id=source_id, mmsi_a=mmsi_a, mmsi_b=mmsi_b, start_time=start, end_time=end
    )


def test_intervals_overlap_true_for_overlapping_windows():
    a_start, a_end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    b_start, b_end = datetime(2024, 6, 1, 11, tzinfo=UTC), datetime(2024, 6, 1, 13, tzinfo=UTC)
    assert agr.intervals_overlap(a_start, a_end, b_start, b_end, tolerance_minutes=0)


def test_intervals_overlap_false_for_disjoint_windows_beyond_tolerance():
    a_start, a_end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 11, tzinfo=UTC)
    b_start, b_end = datetime(2024, 6, 1, 14, tzinfo=UTC), datetime(2024, 6, 1, 15, tzinfo=UTC)
    assert not agr.intervals_overlap(a_start, a_end, b_start, b_end, tolerance_minutes=30)


def test_intervals_overlap_true_when_gap_is_within_tolerance():
    a_start, a_end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 11, tzinfo=UTC)
    b_start, b_end = datetime(2024, 6, 1, 11, 20, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    assert agr.intervals_overlap(a_start, a_end, b_start, b_end, tolerance_minutes=30)


def test_is_match_requires_same_pair_regardless_of_side_order():
    start, end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    a = _event("s1", 111, 222, start, end)
    b = _event("g1", 222, 111, start, end)  # reversed order
    assert agr.is_match(a, b, tolerance_minutes=0)


def test_is_match_false_for_different_pair_even_with_overlapping_time():
    start, end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    a = _event("s1", 111, 222, start, end)
    b = _event("g1", 111, 333, start, end)
    assert not agr.is_match(a, b, tolerance_minutes=0)


def test_is_match_false_for_same_pair_but_non_overlapping_time():
    a = _event("s1", 111, 222, datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 11, tzinfo=UTC))
    b = _event("g1", 111, 222, datetime(2024, 6, 2, 10, tzinfo=UTC), datetime(2024, 6, 2, 11, tzinfo=UTC))
    assert not agr.is_match(a, b, tolerance_minutes=30)


def test_match_all_many_to_many():
    start, end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    sts_events = [
        _event("s-matched", 111, 222, start, end),
        _event("s-unmatched", 333, 444, start, end),
    ]
    gfw_events = [
        _event("g-matched-1", 111, 222, start, end),
        _event("g-matched-2", 222, 111, start, end),  # also matches s-matched
        _event("g-unmatched", 555, 666, start, end),
    ]

    result = agr.match_all(sts_events, gfw_events, tolerance_minutes=0)

    assert result.matched_sts_ids == frozenset({"s-matched"})
    assert result.matched_gfw_ids == frozenset({"g-matched-1", "g-matched-2"})


def test_match_all_empty_inputs():
    result = agr.match_all([], [], tolerance_minutes=30)
    assert result.matched_sts_ids == frozenset()
    assert result.matched_gfw_ids == frozenset()


def _write_sts_parquet(path, rows):
    """rows: list of (encounter_id, mmsi_a, mmsi_b, start, end, ship_type_a, ship_type_b)."""
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (encounter_id VARCHAR, mmsi_a BIGINT, mmsi_b BIGINT, "
            "start_time TIMESTAMP, end_time TIMESTAMP, ship_type_a VARCHAR, ship_type_b VARCHAR)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_gfw_parquet(path, rows):
    """rows: list of (gfw_event_id, mmsi_a, mmsi_b, start, end)."""
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (gfw_event_id VARCHAR, mmsi_a BIGINT, mmsi_b BIGINT, "
            "start_time TIMESTAMP, end_time TIMESTAMP)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def test_build_agreement_end_to_end(tmp_path):
    start, end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    other_start = datetime(2024, 6, 5, 10, tzinfo=UTC)
    other_end = datetime(2024, 6, 5, 12, tzinfo=UTC)

    sts_path = tmp_path / "sts.parquet"
    gfw_path = tmp_path / "gfw_encounters.parquet"
    out_path = tmp_path / "agreement.parquet"

    _write_sts_parquet(
        sts_path,
        [
            ("s-matched", 111, 222, start, end, "Fishing", "Cargo"),
            ("s-unmatched", 333, 444, other_start, other_end, "Tanker", "Tanker"),
        ],
    )
    _write_gfw_parquet(gfw_path, [("g1", 111, 222, start, end)])

    result_path = agr.build_agreement(sts_path=sts_path, gfw_path=gfw_path, out_path=out_path)

    assert result_path == out_path
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT encounter_id, in_gfw_scope, matched_by_gfw FROM '{out_path.as_posix()}' "
            "ORDER BY encounter_id"
        ).fetchall()
    finally:
        con.close()
    assert rows == [
        ("s-matched", True, True),
        ("s-unmatched", False, False),
    ]


def test_build_agreement_is_idempotent(tmp_path):
    start, end = datetime(2024, 6, 1, 10, tzinfo=UTC), datetime(2024, 6, 1, 12, tzinfo=UTC)
    sts_path = tmp_path / "sts.parquet"
    gfw_path = tmp_path / "gfw_encounters.parquet"
    out_path = tmp_path / "agreement.parquet"
    _write_sts_parquet(sts_path, [("s1", 1, 2, start, end, "Tanker", "Tanker")])
    _write_gfw_parquet(gfw_path, [])

    agr.build_agreement(sts_path=sts_path, gfw_path=gfw_path, out_path=out_path)
    first_mtime = out_path.stat().st_mtime_ns

    agr.build_agreement(sts_path=sts_path, gfw_path=gfw_path, out_path=out_path)

    assert out_path.stat().st_mtime_ns == first_mtime, "re-running without --force must not rebuild"


def test_build_agreement_raises_when_sts_path_missing(tmp_path):
    gfw_path = tmp_path / "gfw_encounters.parquet"
    _write_gfw_parquet(gfw_path, [])
    with pytest.raises(FileNotFoundError):
        agr.build_agreement(sts_path=tmp_path / "missing.parquet", gfw_path=gfw_path, out_path=tmp_path / "out.parquet")


def test_build_agreement_raises_when_gfw_path_missing(tmp_path):
    sts_path = tmp_path / "sts.parquet"
    _write_sts_parquet(sts_path, [])
    with pytest.raises(FileNotFoundError):
        agr.build_agreement(sts_path=sts_path, gfw_path=tmp_path / "missing.parquet", out_path=tmp_path / "out.parquet")
