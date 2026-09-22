"""Unit tests for detect.identity_anomalies. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from detect import identity_anomalies as ia
from process import ship_type

DAY = date(2024, 6, 1)
DAY2 = date(2024, 6, 2)
DAY3 = date(2024, 6, 3)
DAY4 = date(2024, 6, 4)
DAY5 = date(2024, 6, 5)


def _ts(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001


def _window_end(day: date) -> datetime:
    """Matches build_identity_events' own end-of-day computation for the `end` argument."""
    return datetime.combine(day, datetime.max.time())


def _valid_imo(prefix: int) -> str:
    """A checksum-valid 7-digit IMO from a 6-digit prefix, same formula as VALID_IMO_SQL."""
    digits = f"{prefix:06d}"
    weights = (7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    return f"{digits}{total % 10}"


VALID_IMO_A = _valid_imo(100000)
VALID_IMO_B = _valid_imo(200000)
VALID_IMO_C = _valid_imo(300000)


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """Write a synthetic clean partition as Parquet via DuckDB, Hive-style.

    rows is a list of (mmsi, timestamp, imo, name, callsign, ship_type, type_of_mobile) tuples.
    """
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE clean (mmsi BIGINT, timestamp TIMESTAMP, imo VARCHAR, name VARCHAR, "
            "callsign VARCHAR, ship_type VARCHAR, type_of_mobile VARCHAR)"
        )
        con.executemany("INSERT INTO clean VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _steady_rows(
    mmsi: int,
    days: list[date],
    per_day: int,
    imo: str | None,
    name: str = "TESTSHIP",
    callsign: str = "TESTCALL",
    ship_type: str = "Tanker",
    mobile: str = "Class A",
) -> list[tuple]:
    """N identical static messages per day, spread over `days` -- a vessel with a stable identity
    except possibly a missing IMO."""
    rows = []
    for day in days:
        for i in range(per_day):
            rows.append((mmsi, _ts(day, hour=i % 24, minute=(i * 7) % 60), imo, name, callsign,
                         ship_type, mobile))
    return rows


def _write_partitions_by_day(root: Path, rows_by_day: dict[date, list[tuple]]) -> None:
    for day, rows in rows_by_day.items():
        _write_clean_partition(root, day, rows)


def _read_events(path: Path) -> list[dict]:
    con = duckdb.connect()
    try:
        cur = con.execute(
            f"SELECT * FROM read_parquet('{path.as_posix()}') ORDER BY mmsi, kind, event_time"
        )
        col_names = [d[0] for d in cur.description]
        return [dict(zip(col_names, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Pure functions: classify_value_intervals
# ---------------------------------------------------------------------------


def _iv(value: str, min_ts: datetime, max_ts: datetime, n: int = 1) -> ia.ValueInterval:
    return ia.ValueInterval(value=value, min_ts=min_ts, max_ts=max_ts, n=n)


def test_classify_requires_at_least_two_intervals():
    with pytest.raises(ValueError, match="at least 2"):
        ia.classify_value_intervals([_iv("A", _ts(DAY, 0), _ts(DAY, 1))])


def test_classify_two_disjoint_intervals_is_clean_switch():
    intervals = [
        _iv("A", _ts(DAY, 0), _ts(DAY, 10)),
        _iv("B", _ts(DAY2, 0), _ts(DAY2, 10)),
    ]
    pattern = ia.classify_value_intervals(intervals)
    assert pattern.pattern == "clean_switch"
    assert pattern.n_overlaps == 0
    assert pattern.transitions == [("A", "B", _ts(DAY2, 0))]


def test_classify_two_overlapping_intervals_is_flapping():
    intervals = [
        _iv("A", _ts(DAY, 0), _ts(DAY, 20)),
        _iv("B", _ts(DAY, 10), _ts(DAY2, 6)),
    ]
    pattern = ia.classify_value_intervals(intervals)
    assert pattern.pattern == "flapping"
    assert pattern.n_overlaps == 1


def test_classify_touching_intervals_counts_as_flapping():
    """Equality (B starts exactly when A ends) counts as overlap, not a clean switch -- pinned."""
    intervals = [
        _iv("A", _ts(DAY, 0), _ts(DAY, 10)),
        _iv("B", _ts(DAY, 10), _ts(DAY, 20)),
    ]
    pattern = ia.classify_value_intervals(intervals)
    assert pattern.pattern == "flapping"


def test_classify_three_sequential_values_is_clean_switch_with_two_transitions():
    intervals = [
        _iv("A", _ts(DAY, 0), _ts(DAY, 5)),
        _iv("B", _ts(DAY2, 0), _ts(DAY2, 5)),
        _iv("C", _ts(DAY3, 0), _ts(DAY3, 5)),
    ]
    pattern = ia.classify_value_intervals(intervals)
    assert pattern.pattern == "clean_switch"
    assert pattern.n_overlaps == 0
    assert pattern.transitions == [
        ("A", "B", _ts(DAY2, 0)),
        ("B", "C", _ts(DAY3, 0)),
    ]


def test_classify_three_values_one_nonadjacent_overlap_is_flapping():
    """A and C overlap even though B sits strictly between them in min_ts order."""
    intervals = [
        _iv("A", _ts(DAY, 0), _ts(DAY5, 23)),  # spans the whole window
        _iv("B", _ts(DAY2, 0), _ts(DAY2, 5)),
        _iv("C", _ts(DAY3, 0), _ts(DAY4, 5)),
    ]
    pattern = ia.classify_value_intervals(intervals)
    assert pattern.pattern == "flapping"
    assert pattern.n_overlaps >= 1


def test_classify_is_deterministic_for_tied_min_ts():
    intervals = [
        _iv("A", _ts(DAY, 0), _ts(DAY, 5)),
        _iv("B", _ts(DAY, 0), _ts(DAY, 5)),
    ]
    first = ia.classify_value_intervals(intervals)
    second = ia.classify_value_intervals(intervals)
    assert first == second


def test_kind_base_confidence_covers_exactly_the_emitted_kinds():
    assert set(ia.KIND_BASE_CONFIDENCE) == {
        "no_valid_imo",
        "name_change",
        "name_flapping",
        "callsign_change",
        "callsign_flapping",
        "shared_identity",
        "shared_imo",
        "reused_mmsi",
    }


def test_identity_confidence_cross_mid_bonus_only_moves_shared_imo():
    plain, _ = ia.identity_confidence("shared_imo")
    boosted, _ = ia.identity_confidence("shared_imo", cross_mid=True)
    assert boosted > plain


def test_identity_confidence_ship_type_instability_penalizes_no_valid_imo():
    stable, _ = ia.identity_confidence("no_valid_imo", n_distinct_ship_types=1)
    unstable, _ = ia.identity_confidence("no_valid_imo", n_distinct_ship_types=2)
    assert unstable < stable


def test_identity_confidence_clamped_to_unit_interval():
    confidence, _ = ia.identity_confidence("shared_imo", cross_mid=True)
    assert 0.0 <= confidence <= 1.0


# ---------------------------------------------------------------------------
# Integration: build_identity_events
# ---------------------------------------------------------------------------


def test_tanker_without_imo_is_flagged(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000001
    days = [DAY, DAY2, DAY3, DAY4]
    rows_by_day = {}
    for day in days:
        rows_by_day.setdefault(day, []).extend(
            _steady_rows(mmsi, [day], per_day=6, imo=None, ship_type="Tanker")
        )
    _write_partitions_by_day(in_root, rows_by_day)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY4, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY4, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi]

    no_imo = [e for e in events if e["kind"] == "no_valid_imo"]
    assert len(no_imo) == 1
    assert no_imo[0]["resolved_ship_type"] == "TANKER"
    # An absence claim is only knowable once the whole window has been scanned, not at last_seen.
    assert no_imo[0]["knowable_at"] == _window_end(DAY4)
    assert no_imo[0]["event_time"] == no_imo[0]["last_seen"]


def test_tanker_with_too_few_static_messages_is_not_flagged(tmp_path):
    """Absence of evidence (2 messages) must not be treated as evidence of absence."""
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000002
    rows = _steady_rows(mmsi, [DAY, DAY4], per_day=1, imo=None, ship_type="Tanker")
    _write_clean_partition(in_root, DAY, [r for r in rows if r[1].date() == DAY])
    _write_clean_partition(in_root, DAY4, [r for r in rows if r[1].date() == DAY4])
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY4, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY4, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi]

    assert not any(e["kind"] == "no_valid_imo" for e in events)


def test_sailing_vessel_without_imo_is_not_flagged(tmp_path):
    """Regression test for docs/DECISIONS.md's reframing: Sailing/Pleasure has no IMO requirement,
    so the global orphaned-MMSI population must not be scored."""
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000003
    days = [DAY, DAY2, DAY3, DAY4]
    for day in days:
        _write_clean_partition(
            in_root, day, _steady_rows(mmsi, [day], per_day=6, imo=None, ship_type="Sailing")
        )
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY4, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY4, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi]

    assert not any(e["kind"] == "no_valid_imo" for e in events)


def test_padded_and_cased_name_variants_normalize_to_one_value(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000004
    rows = [
        (mmsi, _ts(DAY, 0), VALID_IMO_A, "MAERSK", "OXAB1", "Cargo", "Class A"),
        (mmsi, _ts(DAY, 6), VALID_IMO_A, "MAERSK@@", "OXAB1", "Cargo", "Class A"),
        (mmsi, _ts(DAY, 12), VALID_IMO_A, "  maersk ", "OXAB1", "Cargo", "Class A"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi]

    assert not any(e["kind"] in ("name_change", "name_flapping") for e in events)


def test_name_change_clean_switch_emits_one_transition(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000005
    rows_day1 = [(mmsi, _ts(DAY, h), VALID_IMO_A, "OLDNAME", "OXAB2", "Cargo", "Class A")
                 for h in range(4)]
    rows_day2 = [(mmsi, _ts(DAY2, h), VALID_IMO_A, "NEWNAME", "OXAB2", "Cargo", "Class A")
                 for h in range(4)]
    _write_clean_partition(in_root, DAY, rows_day1)
    _write_clean_partition(in_root, DAY2, rows_day2)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY2, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY2, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi and e["kind"] == "name_change"]

    assert len(events) == 1
    assert events[0]["old_value"] == "OLDNAME"
    assert events[0]["new_value"] == "NEWNAME"
    assert events[0]["event_time"] == _ts(DAY2, 0)
    # Clean-switch vs flapping is a whole-window judgement -- only knowable at window_end, not at
    # the vessel's own last message (see module docstring's leakage section, point 1).
    assert events[0]["knowable_at"] == _window_end(DAY2)


def test_name_flapping_when_values_interleave(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000006
    rows = [
        (mmsi, _ts(DAY, 0), VALID_IMO_A, "ALPHA", "OXAB3", "Cargo", "Class A"),
        (mmsi, _ts(DAY, 6), VALID_IMO_A, "BRAVO", "OXAB3", "Cargo", "Class A"),
        (mmsi, _ts(DAY, 12), VALID_IMO_A, "ALPHA", "OXAB3", "Cargo", "Class A"),
        (mmsi, _ts(DAY, 18), VALID_IMO_A, "BRAVO", "OXAB3", "Cargo", "Class A"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi]

    flapping = [e for e in events if e["kind"] == "name_flapping"]
    assert len(flapping) == 1
    assert not any(e["kind"] == "name_change" for e in events)


def test_reused_mmsi_two_distinct_valid_imos(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000007
    rows_day1 = [(mmsi, _ts(DAY, h), VALID_IMO_A, "SHIPX", "OXAB4", "Cargo", "Class A")
                 for h in range(3)]
    rows_day2 = [(mmsi, _ts(DAY2, h), VALID_IMO_B, "SHIPX", "OXAB4", "Cargo", "Class A")
                 for h in range(3)]
    _write_clean_partition(in_root, DAY, rows_day1)
    _write_clean_partition(in_root, DAY2, rows_day2)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY2, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY2, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi and e["kind"] == "reused_mmsi"]

    assert len(events) == 1
    assert events[0]["old_value"] == VALID_IMO_A
    assert events[0]["new_value"] == VALID_IMO_B
    # Unlike name/callsign, this fact needs no window_end wait: it's true and fully knowable the
    # instant the second valid IMO is seen (see module docstring's leakage section, point 3).
    assert events[0]["event_time"] == _ts(DAY2, 0)
    assert events[0]["knowable_at"] == events[0]["event_time"]


def test_two_mmsi_sharing_imo_emit_symmetric_pair_with_shared_knowable_at(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi_a, mmsi_b = 219000010, 219000011
    rows_a = [(mmsi_a, _ts(DAY, h), VALID_IMO_A, "SHIPA", "OXA1", "Cargo", "Class A")
              for h in range(3)]
    rows_b = [(mmsi_b, _ts(DAY2, h), VALID_IMO_A, "SHIPB", "OXA2", "Cargo", "Class A")
              for h in range(3)]
    _write_clean_partition(in_root, DAY, rows_a)
    _write_clean_partition(in_root, DAY2, rows_b)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY2, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY2, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["kind"] == "shared_imo"]

    assert len(events) == 2
    assert {e["mmsi"] for e in events} == {mmsi_a, mmsi_b}
    assert events[0]["pair_id"] == events[1]["pair_id"]
    # The pair only becomes knowable once the LATER of the two first broadcasts it (mmsi_b, day 2).
    expected_knowable_at = _ts(DAY2, 0)
    assert events[0]["knowable_at"] == expected_knowable_at
    assert events[1]["knowable_at"] == expected_knowable_at


def test_cross_mid_pair_flags_cross_mid_and_never_emits_a_flag_change_kind(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi_dk, mmsi_no = 219000020, 259000020  # Denmark, Norway MIDs
    rows_dk = [(mmsi_dk, _ts(DAY, h), VALID_IMO_A, "SHIPDK", "OXDK", "Cargo", "Class A")
               for h in range(3)]
    rows_no = [(mmsi_no, _ts(DAY, h), VALID_IMO_A, "SHIPNO", "LNNO", "Cargo", "Class A")
               for h in range(3)]
    _write_clean_partition(in_root, DAY, rows_dk + rows_no)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = _read_events(out_path)
    shared = [e for e in events if e["kind"] == "shared_imo"]

    assert len(shared) == 2
    assert all(e["cross_mid"] for e in shared)
    assert not any(e["kind"] == "flag_change" for e in events)


def test_three_mmsi_sharing_imo_yields_six_rows_three_pairs(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsis = [219000030, 219000031, 219000032]
    rows = []
    for i, mmsi in enumerate(mmsis):
        rows.append((mmsi, _ts(DAY, i), VALID_IMO_A, f"SHIP{i}", f"OX{i}", "Cargo", "Class A"))
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["kind"] == "shared_imo"]

    assert len(events) == 6
    assert len({e["pair_id"] for e in events}) == 3


def test_oversized_shared_group_is_skipped_with_warning(tmp_path, caplog):
    """All n_mmsi broadcast the shared IMO at the SAME instant, so every pair's own
    size-as-of-knowable_at is the full group size -- every pair is skipped."""
    in_root = tmp_path / "clean" / "ais_dk"
    n_mmsi = ia.MAX_IDENTITY_GROUP_SIZE + 1
    rows = [
        (219100000 + i, _ts(DAY, 0), VALID_IMO_C, f"SHIP{i}", f"OXZ{i}", "Cargo", "Class A")
        for i in range(n_mmsi)
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    with caplog.at_level("WARNING"):
        ship_type.build_ship_type_reference(
            DAY, DAY, in_root=in_root, out_root=ship_type_root
        )
        out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    assert any("MAX_IDENTITY_GROUP_SIZE" in record.message for record in caplog.records)
    events = [e for e in _read_events(out_path) if e["kind"] == "shared_imo"]
    assert not any(e["shared_value"] == VALID_IMO_C for e in events)


def test_group_size_gate_is_evaluated_as_of_each_pairs_own_knowable_at(tmp_path):
    """Members join one at a time, each on a later day. Pairs formed while the group was still at
    or under the cap must survive even though the group later grows past it -- gating on the
    group's EVENTUAL size would leak the future into an early pair's very existence."""
    in_root = tmp_path / "clean" / "ais_dk"
    n_mmsi = ia.MAX_IDENTITY_GROUP_SIZE + 2
    base_day = date(2024, 1, 1)
    days = [date.fromordinal(base_day.toordinal() + i) for i in range(n_mmsi)]
    mmsis = [219200000 + i for i in range(n_mmsi)]
    for i, (mmsi, day) in enumerate(zip(mmsis, days, strict=True)):
        _write_clean_partition(
            in_root, day, [(mmsi, _ts(day, 0), VALID_IMO_C, f"SHIP{i}", f"OXY{i}", "Cargo",
                            "Class A")]
        )
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        days[0], days[-1], in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(days[0], days[-1], in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["shared_value"] == VALID_IMO_C]

    # The pair formed by the first two members (group size 2 at that point) must survive.
    early_pair = [e for e in events if {e["mmsi"], e["related_mmsi"]} == {mmsis[0], mmsis[1]}]
    assert len(early_pair) == 2
    # Any pair involving the (MAX_IDENTITY_GROUP_SIZE + 2)th member -- present only once the group
    # has already exceeded the cap -- must be skipped.
    assert not any(mmsis[-1] in (e["mmsi"], e["related_mmsi"]) for e in events)


def test_shared_identity_with_different_imos_emits_only_shared_identity(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi_a, mmsi_b = 219000040, 219000041
    rows = [
        (mmsi_a, _ts(DAY, 0), VALID_IMO_A, "SAMESHIP", "OXSAME", "Cargo", "Class A"),
        (mmsi_b, _ts(DAY2, 0), VALID_IMO_B, "SAMESHIP", "OXSAME", "Cargo", "Class A"),
    ]
    _write_clean_partition(in_root, DAY, [rows[0]])
    _write_clean_partition(in_root, DAY2, [rows[1]])
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY2, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY2, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] in (mmsi_a, mmsi_b)]

    assert not any(e["kind"] == "shared_imo" for e in events)
    shared_identity = [e for e in events if e["kind"] == "shared_identity"]
    assert len(shared_identity) == 2


def test_base_station_rows_are_excluded(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    mmsi = 219000050
    rows = [
        (mmsi, _ts(DAY, 0), None, "NAMEA", "CALLA", "Undefined", "Base Station"),
        (mmsi, _ts(DAY, 1), None, "NAMEB", "CALLB", "Undefined", "Base Station"),
    ]
    _write_clean_partition(in_root, DAY, rows)
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = [e for e in _read_events(out_path) if e["mmsi"] == mmsi]

    assert events == []


def test_build_identity_events_raises_when_no_partitions_exist(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    out_root = tmp_path / "detect" / "identity_anomalies"

    with pytest.raises(FileNotFoundError):
        ia.build_identity_events(DAY, DAY, in_root=in_root, out_root=out_root)


def test_build_identity_events_is_idempotent_by_default(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root, DAY, [(219000060, _ts(DAY, 0), VALID_IMO_A, "SHIP", "OX", "Cargo", "Class A")]
    )
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    first = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    first_mtime = first.stat().st_mtime_ns

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    second = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime


def test_build_identity_events_carries_provenance_columns(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root, DAY, [(219000061, _ts(DAY, 0), VALID_IMO_A, "SHIP", "OX", "Cargo", "Class A")]
    )
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = _read_events(out_path)

    assert all(e["window_start"] == DAY for e in events) or events == []
    con = duckdb.connect()
    try:
        columns = {
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{out_path.as_posix()}')"
            ).fetchall()
        }
    finally:
        con.close()
    assert {"window_start", "window_end", "built_at", "git_sha"} <= columns


def test_zero_event_run_writes_typed_empty_parquet(tmp_path):
    """A vessel with a perfectly clean, stable identity should trigger no check at all."""
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root,
        DAY,
        [(219000070, _ts(DAY, 0), VALID_IMO_A, "CLEANSHIP", "OXCLEAN", "Fishing", "Class A")],
    )
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    out_path = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    events = _read_events(out_path)

    assert events == []
    con = duckdb.connect()
    try:
        columns = [
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{out_path.as_posix()}')"
            ).fetchall()
        ]
    finally:
        con.close()
    assert "kind" in columns
    assert "knowable_at" in columns


def test_force_rebuilds_even_when_output_exists(tmp_path):
    in_root = tmp_path / "clean" / "ais_dk"
    _write_clean_partition(
        in_root, DAY, [(219000080, _ts(DAY, 0), VALID_IMO_A, "SHIP", "OX", "Cargo", "Class A")]
    )
    out_root = tmp_path / "detect" / "identity_anomalies"
    ship_type_root = tmp_path / "reference" / "ship_type"

    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    first = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root)

    first_mtime = first.stat().st_mtime_ns

    time.sleep(0.01)
    ship_type.build_ship_type_reference(
        DAY, DAY, in_root=in_root, out_root=ship_type_root
    )
    second = ia.build_identity_events(DAY, DAY, in_root=in_root, ship_type_reference_root=ship_type_root, out_root=out_root, force=True)

    assert second.stat().st_mtime_ns != first_mtime


# ---------------------------------------------------------------------------
# build_vessel_links
# ---------------------------------------------------------------------------


def _write_events_fixture(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, related_mmsi, kind, confidence, knowable_at) tuples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE events (mmsi BIGINT, related_mmsi BIGINT, kind VARCHAR, "
            "confidence DOUBLE, knowable_at TIMESTAMP)"
        )
        con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY events TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def test_build_vessel_links_groups_transitively_across_kinds(tmp_path):
    events_path = tmp_path / "detect" / "identity_anomalies.parquet"
    mmsi_a, mmsi_b, mmsi_c, mmsi_d = 100, 200, 300, 400
    ts_ab = _ts(DAY, 0)
    ts_bc = _ts(DAY2, 0)
    _write_events_fixture(
        events_path,
        [
            (mmsi_a, mmsi_b, "shared_imo", 0.8, ts_ab),
            (mmsi_b, mmsi_a, "shared_imo", 0.8, ts_ab),
            (mmsi_b, mmsi_c, "shared_identity", 0.6, ts_bc),
            (mmsi_c, mmsi_b, "shared_identity", 0.6, ts_bc),
        ],
    )
    out_path = tmp_path / "identity" / "vessel_links.parquet"

    ia.build_vessel_links(events_path=events_path, out_path=out_path)

    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT mmsi, vessel_key, knowable_at FROM read_parquet("
            f"'{out_path.as_posix()}') ORDER BY mmsi"
        ).fetchall()
    finally:
        con.close()

    linked = {mmsi: vessel_key for mmsi, vessel_key, _ in rows}
    knowable_at = {mmsi: ts for mmsi, _, ts in rows}
    assert linked[mmsi_a] == linked[mmsi_b] == linked[mmsi_c]
    assert mmsi_d not in linked
    # mmsi_b's earliest edge is A-B (ts_ab); mmsi_c only has the later B-C edge.
    assert knowable_at[mmsi_b] == ts_ab
    assert knowable_at[mmsi_c] == ts_bc


def test_build_vessel_links_raises_when_events_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        ia.build_vessel_links(
            events_path=tmp_path / "missing.parquet", out_path=tmp_path / "links.parquet"
        )


def test_build_vessel_links_is_idempotent_by_default(tmp_path):
    events_path = tmp_path / "detect" / "identity_anomalies.parquet"
    _write_events_fixture(
        events_path,
        [(1, 2, "shared_imo", 0.8, _ts(DAY, 0)), (2, 1, "shared_imo", 0.8, _ts(DAY, 0))],
    )
    out_path = tmp_path / "identity" / "vessel_links.parquet"

    first = ia.build_vessel_links(events_path=events_path, out_path=out_path)
    first_mtime = first.stat().st_mtime_ns
    second = ia.build_vessel_links(events_path=events_path, out_path=out_path)

    assert second.stat().st_mtime_ns == first_mtime
