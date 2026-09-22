"""Unit tests for features.panel. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from features import panel

WINDOW_START = date(2024, 6, 1)
WINDOW_END = date(2024, 6, 30)
YEAR_MONTH = date(2024, 6, 1)


def _ts(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001


def _valid_imo(prefix: int) -> str:
    """A checksum-valid 7-digit IMO from a 6-digit prefix, same formula as VALID_IMO_SQL."""
    digits = f"{prefix:06d}"
    weights = (7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    return f"{digits}{total % 10}"


VALID_IMO_A = _valid_imo(100000)
VALID_IMO_B = _valid_imo(200000)
VALID_IMO_C = _valid_imo(300000)


def _write_mmsi_imo(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, imo, message_count, first_seen, last_seen, is_orphaned, is_reused)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, imo VARCHAR, message_count BIGINT, "
            "first_seen DATE, last_seen DATE, is_orphaned BOOLEAN, is_reused BOOLEAN)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_sanctions_matches(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, imo, source, source_id, designation_date)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, imo VARCHAR, source VARCHAR, source_id VARCHAR, "
            "designation_date DATE)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_voyages(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, voyage_seq, voyage_id, start_time)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, voyage_seq BIGINT, voyage_id VARCHAR, "
            "start_time TIMESTAMP)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_gaps(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, gap_end, duration_hours, probability)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, gap_end TIMESTAMP, duration_hours DOUBLE, "
            "probability DOUBLE)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_spoofing(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, kind, event_time)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, kind VARCHAR, event_time TIMESTAMP)")
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_sts(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi_a, mmsi_b, end_time, confidence)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi_a BIGINT, mmsi_b BIGINT, end_time TIMESTAMP, confidence DOUBLE)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_identity_anomalies(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, kind, knowable_at)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, kind VARCHAR, knowable_at TIMESTAMP)")
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_behaviour(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, kind, knowable_at)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, kind VARCHAR, knowable_at TIMESTAMP)")
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_clean_partition(root: Path, day: date, rows: list[tuple]) -> None:
    """rows: (mmsi, ship_type)."""
    partition_dir = root / f"date={day.isoformat()}"
    partition_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = partition_dir / "part-0.parquet"
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE clean (mmsi BIGINT, ship_type VARCHAR)")
        if rows:
            con.executemany("INSERT INTO clean VALUES (?, ?)", rows)
        con.execute(f"COPY clean TO '{parquet_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


class _Paths:
    """Bundles every path build_panel needs, all under one tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.mmsi_imo = tmp_path / "identity" / "mmsi_imo.parquet"
        self.sanctions_matches = tmp_path / "identity" / "sanctions_matches.parquet"
        self.voyages = tmp_path / "tracks" / "voyages.parquet"
        self.detect_root = tmp_path / "detect"
        self.clean_root = tmp_path / "clean" / "ais_dk"
        self.out_path = tmp_path / "processed" / "vessel_month_panel.parquet"

    @property
    def gaps(self) -> Path:
        return self.detect_root / "gaps.parquet"

    @property
    def spoofing(self) -> Path:
        return self.detect_root / "spoofing.parquet"

    @property
    def sts(self) -> Path:
        return self.detect_root / "sts.parquet"

    @property
    def identity_anomalies(self) -> Path:
        return self.detect_root / "identity_anomalies.parquet"

    @property
    def behaviour(self) -> Path:
        return self.detect_root / "behaviour.parquet"


def _build_minimal_inputs(
    p: _Paths,
    mmsi_imo_rows: list[tuple],
    sanctions_rows: list[tuple] | None = None,
    voyage_rows: list[tuple] | None = None,
    gap_rows: list[tuple] | None = None,
    spoofing_rows: list[tuple] | None = None,
    sts_rows: list[tuple] | None = None,
    identity_rows: list[tuple] | None = None,
    behaviour_rows: list[tuple] | None = None,
    clean_rows: list[tuple] | None = None,
) -> None:
    _write_mmsi_imo(p.mmsi_imo, mmsi_imo_rows)
    _write_sanctions_matches(p.sanctions_matches, sanctions_rows or [])
    _write_voyages(p.voyages, voyage_rows or [])
    _write_gaps(p.gaps, gap_rows or [])
    _write_spoofing(p.spoofing, spoofing_rows or [])
    _write_sts(p.sts, sts_rows or [])
    _write_identity_anomalies(p.identity_anomalies, identity_rows or [])
    _write_behaviour(p.behaviour, behaviour_rows or [])
    _write_clean_partition(p.clean_root, WINDOW_START, clean_rows or [])


def _read_panel(out_path: Path) -> list[dict]:
    con = duckdb.connect()
    try:
        cols = [
            c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{out_path.as_posix()}'").fetchall()
        ]
        rows = con.execute(f"SELECT * FROM '{out_path.as_posix()}' ORDER BY mmsi").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        con.close()


def _run(p: _Paths, force: bool = False) -> Path:
    return panel.build_panel(
        WINDOW_START,
        WINDOW_END,
        mmsi_imo_path=p.mmsi_imo,
        sanctions_matches_path=p.sanctions_matches,
        voyages_path=p.voyages,
        detect_root=p.detect_root,
        clean_root=p.clean_root,
        out_path=p.out_path,
        force=force,
    )


def test_vessel_with_zero_detector_events_gets_zero_not_missing_row(tmp_path):
    p = _Paths(tmp_path)
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (219000001, VALID_IMO_A, 50, date(2024, 6, 1), date(2024, 6, 30), False, False)
        ],
    )

    _run(p)
    rows = _read_panel(p.out_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["mmsi"] == 219000001
    assert row["year_month"] == YEAR_MONTH
    assert row["imo"] == VALID_IMO_A
    for count_col in (
        "n_gaps",
        "n_gaps_high_probability",
        "n_impossible_speed",
        "n_on_land",
        "n_synthetic_circle",
        "n_simultaneous_position",
        "n_spoofing_events_total",
        "n_sts_episodes",
        "n_no_valid_imo",
        "n_name_change",
        "n_identity_anomalies_total",
        "n_destination_course_mismatch",
        "n_draught_change_unexplained",
        "voyage_count",
        "label_n_sanctions_sources",
    ):
        assert row[count_col] == 0, count_col
    for mean_col in (
        "mean_gap_probability",
        "max_gap_duration_hours",
        "mean_sts_confidence",
        "max_sts_confidence",
    ):
        assert row[mean_col] is None, mean_col
    assert row["label_is_sanctioned_ever"] is False
    assert row["label_is_sanctioned_as_of_window_end"] is False
    assert row["label_is_sanctioned_after_window_end"] is False
    assert row["label_earliest_designation_date"] is None


def test_vessel_with_events_across_all_detectors_aggregates_correctly(tmp_path):
    p = _Paths(tmp_path)
    mmsi = 219000002
    other_mmsi = 219000099
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (mmsi, VALID_IMO_A, 50, date(2024, 6, 1), date(2024, 6, 30), False, False),
            (other_mmsi, VALID_IMO_B, 10, date(2024, 6, 1), date(2024, 6, 5), False, False),
        ],
        voyage_rows=[
            (mmsi, 1, "v1", _ts(date(2024, 6, 2))),
            (mmsi, 2, "v2", _ts(date(2024, 6, 10))),
        ],
        gap_rows=[
            (mmsi, _ts(date(2024, 6, 3)), 5.0, 0.8),  # high probability
            (mmsi, _ts(date(2024, 6, 4)), 30.0, 0.3),  # low probability
        ],
        spoofing_rows=[
            (mmsi, "impossible_speed", _ts(date(2024, 6, 5))),
            (mmsi, "impossible_speed", _ts(date(2024, 6, 6))),
            (mmsi, "on_land", _ts(date(2024, 6, 7))),
        ],
        sts_rows=[
            (mmsi, other_mmsi, _ts(date(2024, 6, 8)), 0.6),  # mmsi as mmsi_a
            (other_mmsi, mmsi, _ts(date(2024, 6, 9)), 0.4),  # mmsi as mmsi_b
        ],
        identity_rows=[
            (mmsi, "name_change", _ts(date(2024, 6, 10))),
            (mmsi, "callsign_flapping", _ts(date(2024, 6, 11))),
        ],
        behaviour_rows=[
            (mmsi, "destination_course_mismatch", _ts(date(2024, 6, 12))),
        ],
        clean_rows=[(mmsi, "Tanker"), (mmsi, "Tanker"), (mmsi, "Cargo")],
    )

    _run(p)
    rows = {row["mmsi"]: row for row in _read_panel(p.out_path)}
    row = rows[mmsi]

    assert row["voyage_count"] == 2
    assert row["ship_type"] == "Tanker"
    assert row["n_gaps"] == 2
    assert row["n_gaps_high_probability"] == 1
    assert row["mean_gap_probability"] == pytest.approx(0.55)
    assert row["max_gap_duration_hours"] == pytest.approx(30.0)
    assert row["n_impossible_speed"] == 2
    assert row["n_on_land"] == 1
    assert row["n_synthetic_circle"] == 0
    assert row["n_spoofing_events_total"] == 3
    assert row["n_sts_episodes"] == 2
    assert row["mean_sts_confidence"] == pytest.approx(0.5)
    assert row["max_sts_confidence"] == pytest.approx(0.6)
    assert row["n_name_change"] == 1
    assert row["n_callsign_flapping"] == 1
    assert row["n_identity_anomalies_total"] == 2
    assert row["n_destination_course_mismatch"] == 1
    assert row["n_draught_change_unexplained"] == 0

    # other_mmsi is the counterparty on both sts episodes (so it does count those), but has its
    # own row with every OTHER detector's counts correctly zeroed, not a missing row.
    other_row = rows[other_mmsi]
    assert other_row["n_sts_episodes"] == 2
    assert other_row["n_gaps"] == 0
    assert other_row["n_spoofing_events_total"] == 0
    assert other_row["n_identity_anomalies_total"] == 0


def test_orphaned_mmsi_has_no_imo_and_no_possible_sanctions_label(tmp_path):
    p = _Paths(tmp_path)
    mmsi = 219000003
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[(mmsi, None, 15, date(2024, 6, 1), date(2024, 6, 20), True, False)],
    )

    _run(p)
    rows = _read_panel(p.out_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["is_orphaned"] is True
    assert row["is_reused"] is False
    assert row["imo"] is None
    assert row["label_is_sanctioned_ever"] is False
    assert row["label_is_sanctioned_as_of_window_end"] is False
    assert row["label_is_sanctioned_after_window_end"] is False
    assert row["label_n_sanctions_sources"] == 0


def test_reused_mmsi_picks_most_recently_seen_imo_as_representative(tmp_path):
    p = _Paths(tmp_path)
    mmsi = 219000004
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (mmsi, VALID_IMO_A, 10, date(2024, 6, 1), date(2024, 6, 5), False, True),
            (mmsi, VALID_IMO_B, 20, date(2024, 6, 10), date(2024, 6, 20), False, True),
        ],
    )

    _run(p)
    rows = _read_panel(p.out_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["is_reused"] is True
    assert row["imo"] == VALID_IMO_B  # later last_seen wins
    assert row["total_message_count"] == 30  # summed across both rows


def test_sanctions_label_three_way_split(tmp_path):
    p = _Paths(tmp_path)
    mmsi_never = 219000005
    mmsi_before = 219000006
    mmsi_after = 219000007
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (mmsi_never, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False),
            (mmsi_before, VALID_IMO_B, 5, date(2024, 6, 1), date(2024, 6, 5), False, False),
            (mmsi_after, VALID_IMO_C, 5, date(2024, 6, 1), date(2024, 6, 5), False, False),
        ],
        sanctions_rows=[
            # mmsi_before: designated before window_end -- already sanctioned.
            (mmsi_before, VALID_IMO_B, "ofac", "SDN-1", date(2024, 1, 1)),
            # mmsi_after: designated after window_end, corroborated by 2 sources.
            (mmsi_after, VALID_IMO_C, "ofac", "SDN-2", date(2024, 12, 1)),
            (mmsi_after, VALID_IMO_C, "uk", "DPR-1", date(2024, 12, 15)),
        ],
    )

    _run(p)
    rows = {row["mmsi"]: row for row in _read_panel(p.out_path)}

    never = rows[mmsi_never]
    assert never["label_is_sanctioned_ever"] is False
    assert never["label_is_sanctioned_as_of_window_end"] is False
    assert never["label_is_sanctioned_after_window_end"] is False
    assert never["label_earliest_designation_date"] is None
    assert never["label_n_sanctions_sources"] == 0

    before = rows[mmsi_before]
    assert before["label_is_sanctioned_ever"] is True
    assert before["label_is_sanctioned_as_of_window_end"] is True
    assert before["label_is_sanctioned_after_window_end"] is False
    assert before["label_earliest_designation_date"] == date(2024, 1, 1)
    assert before["label_n_sanctions_sources"] == 1

    after = rows[mmsi_after]
    assert after["label_is_sanctioned_ever"] is True
    assert after["label_is_sanctioned_as_of_window_end"] is False
    assert after["label_is_sanctioned_after_window_end"] is True
    assert after["label_earliest_designation_date"] == date(2024, 12, 1)  # earliest of two sources
    assert after["label_n_sanctions_sources"] == 2


def test_knowable_at_filter_excludes_event_after_month_end(tmp_path):
    """The core temporal-leakage guard: an identity_anomalies/behaviour event whose knowable_at
    is after this vessel-month's end must not be counted, even though the event's own mmsi and
    kind would otherwise match.
    """
    p = _Paths(tmp_path)
    mmsi = 219000008
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (mmsi, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 30), False, False)
        ],
        identity_rows=[
            # Knowable strictly after the window's own end -- must be excluded.
            (mmsi, "name_change", datetime(2024, 7, 1, 0, 0)),  # noqa: DTZ001
        ],
        behaviour_rows=[
            # Knowable strictly after the window's own end -- must be excluded.
            (mmsi, "draught_change_unexplained", datetime(2024, 7, 1, 0, 0)),  # noqa: DTZ001
        ],
    )

    _run(p)
    rows = _read_panel(p.out_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["n_name_change"] == 0
    assert row["n_identity_anomalies_total"] == 0
    assert row["n_draught_change_unexplained"] == 0


def test_knowable_at_filter_includes_event_within_month_negative_case(tmp_path):
    """Negative case for the above: an event knowable strictly before month end IS counted."""
    p = _Paths(tmp_path)
    mmsi = 219000009
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (mmsi, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 30), False, False)
        ],
        identity_rows=[(mmsi, "name_change", _ts(date(2024, 6, 30), 23, 0))],
    )

    _run(p)
    rows = _read_panel(p.out_path)

    assert len(rows) == 1
    assert rows[0]["n_name_change"] == 1
    assert rows[0]["n_identity_anomalies_total"] == 1


def test_flag_country_derived_from_mid(tmp_path):
    p = _Paths(tmp_path)
    mmsi = 219000010  # MID 219 -> Denmark, per process.mid.MID_COUNTRY
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (mmsi, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)
        ],
    )

    _run(p)
    rows = _read_panel(p.out_path)

    assert rows[0]["flag_country"] == "Denmark"


def test_panel_is_idempotent_by_default(tmp_path):
    p = _Paths(tmp_path)
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (219000011, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)
        ],
    )

    first = _run(p)
    first_mtime = first.stat().st_mtime_ns
    second = _run(p)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime


def test_panel_force_rebuilds(tmp_path):
    p = _Paths(tmp_path)
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (219000012, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)
        ],
    )

    _run(p)
    result = _run(p, force=True)

    assert result.exists()


def test_raises_when_mmsi_imo_path_missing(tmp_path):
    p = _Paths(tmp_path)
    _write_sanctions_matches(p.sanctions_matches, [])
    _write_voyages(p.voyages, [])
    with pytest.raises(FileNotFoundError):
        _run(p)


def test_raises_when_sanctions_matches_path_missing(tmp_path):
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000013, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    with pytest.raises(FileNotFoundError):
        _run(p)


def test_raises_when_voyages_path_missing(tmp_path):
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000014, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    _write_sanctions_matches(p.sanctions_matches, [])
    with pytest.raises(FileNotFoundError):
        _run(p)


def test_raises_when_a_detector_table_missing(tmp_path):
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000015, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    _write_sanctions_matches(p.sanctions_matches, [])
    _write_voyages(p.voyages, [])
    _write_gaps(p.gaps, [])
    _write_spoofing(p.spoofing, [])
    _write_sts(p.sts, [])
    _write_identity_anomalies(p.identity_anomalies, [])
    # behaviour.parquet deliberately not written.
    with pytest.raises(FileNotFoundError):
        _run(p)


def test_label_columns_are_the_only_ones_naming_sanctions(tmp_path):
    """Guard against a future rename accidentally re-introducing an unprefixed label column into
    the feature matrix: every sanctions-derived column must carry the `label_` prefix, and no
    other column name may even mention "sanction"/"designat", or it would silently look like a
    legitimate feature to a feature-selection step that only excludes `label_*`.
    """
    p = _Paths(tmp_path)
    _build_minimal_inputs(
        p,
        mmsi_imo_rows=[
            (219000017, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)
        ],
    )

    _run(p)
    rows = _read_panel(p.out_path)
    columns = set(rows[0].keys())

    assert {c for c in columns if c.startswith("label_")} == set(panel.LABEL_COLUMNS)
    non_label_columns = columns - set(panel.LABEL_COLUMNS)
    for column in non_label_columns:
        lowered = column.lower()
        assert "sanction" not in lowered, column
        assert "designat" not in lowered, column


def test_raises_when_no_clean_partitions_in_range(tmp_path):
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000016, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    _write_sanctions_matches(p.sanctions_matches, [])
    _write_voyages(p.voyages, [])
    _write_gaps(p.gaps, [])
    _write_spoofing(p.spoofing, [])
    _write_sts(p.sts, [])
    _write_identity_anomalies(p.identity_anomalies, [])
    _write_behaviour(p.behaviour, [])
    # p.clean_root deliberately never populated.
    with pytest.raises(FileNotFoundError):
        _run(p)
