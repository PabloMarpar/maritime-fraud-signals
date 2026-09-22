"""Unit tests for features.panel. All fixtures are synthetic, written to tmp_path; nothing here
touches data/.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from features import panel
from process.partitions import window_partition_path

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


def _write_liveness(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, cell_hour). Only the two columns _build_exposure actually reads -- the real
    table also carries cell_lat/cell_lon/is_class_a/etc, unused here, same minimal-fixture posture
    as every other _write_* helper in this file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, cell_hour TIMESTAMP)")
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _write_ship_type_reference(path: Path, rows: list[tuple]) -> None:
    """rows: (mmsi, ship_type). Real grain also carries type_of_mobile/n_messages (P3-4/A2's
    process.ship_type), but _build_ship_type doesn't filter by type_of_mobile, so a placeholder
    value and n_messages=1 per row is enough to exercise the same mode-by-count resolution --
    repeat a row to simulate a higher count, matching the old direct-clean-partition-scan
    fixture's convention.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (mmsi BIGINT, ship_type VARCHAR, type_of_mobile VARCHAR, "
            "n_messages BIGINT)"
        )
        if rows:
            con.executemany("INSERT INTO t VALUES (?, ?, 'Class A', 1)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


class _Paths:
    """Bundles every path build_panel needs, all under one tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.mmsi_imo = tmp_path / "identity" / "mmsi_imo.parquet"
        self.sanctions_matches = tmp_path / "identity" / "sanctions_matches.parquet"
        self.voyages = tmp_path / "tracks" / "voyages.parquet"
        self.detect_root = tmp_path / "detect"
        self.ship_type_root = tmp_path / "reference" / "ship_type"
        self.liveness = tmp_path / "coverage" / "liveness.parquet"
        self.out_path = tmp_path / "processed" / "vessel_month_panel.parquet"

    @property
    def gaps(self) -> Path:
        return self.detect_root / "gaps.parquet"

    # spoofing/sts/identity_anomalies/behaviour are window-partitioned as of P3-4/A2, and
    # build_panel reads them via a `window=*` glob under detect_root -- any concrete window
    # directory matches that glob, so tests write to this fixed one (the same window every test
    # in this file uses).
    @property
    def spoofing(self) -> Path:
        return window_partition_path(WINDOW_START, WINDOW_END, self.detect_root / "spoofing")

    @property
    def sts(self) -> Path:
        return window_partition_path(WINDOW_START, WINDOW_END, self.detect_root / "sts")

    @property
    def identity_anomalies(self) -> Path:
        return window_partition_path(
            WINDOW_START, WINDOW_END, self.detect_root / "identity_anomalies"
        )

    @property
    def behaviour(self) -> Path:
        return window_partition_path(WINDOW_START, WINDOW_END, self.detect_root / "behaviour")

    @property
    def ship_type(self) -> Path:
        """Exact single-window path -- build_panel resolves this itself, not a glob, so the
        fixture must land at exactly the path it will compute."""
        return window_partition_path(WINDOW_START, WINDOW_END, self.ship_type_root)


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
    ship_type_rows: list[tuple] | None = None,
    liveness_rows: list[tuple] | None = None,
) -> None:
    _write_mmsi_imo(p.mmsi_imo, mmsi_imo_rows)
    _write_sanctions_matches(p.sanctions_matches, sanctions_rows or [])
    _write_voyages(p.voyages, voyage_rows or [])
    _write_gaps(p.gaps, gap_rows or [])
    _write_spoofing(p.spoofing, spoofing_rows or [])
    _write_sts(p.sts, sts_rows or [])
    _write_identity_anomalies(p.identity_anomalies, identity_rows or [])
    _write_behaviour(p.behaviour, behaviour_rows or [])
    _write_ship_type_reference(p.ship_type, ship_type_rows or [])
    _write_liveness(p.liveness, liveness_rows or [])


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
        ship_type_reference_root=p.ship_type_root,
        liveness_path=p.liveness,
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
        "n_observed_hours",
        "n_observed_days",
    ):
        assert row[count_col] == 0, count_col
    for mean_col in (
        "mean_gap_probability",
        "max_gap_duration_hours",
        "mean_sts_confidence",
        "max_sts_confidence",
    ):
        assert row[mean_col] is None, mean_col
    # Zero voyages and zero observed days (no liveness rows) make those two denominators 0, so
    # those rate columns must be NULL ("undefined"), never a divide-by-zero infinity or a silent
    # 0. total_message_count is 50 (nonzero, from mmsi_imo_rows), so the per-1,000-messages rate
    # has a defined (nonzero) denominator and correctly reads 0.0 -- zero events over known
    # exposure, a real measured rate, not a missing one.
    for count_col in panel.RATE_BASE_COLUMNS:
        short = count_col.removeprefix("n_")
        assert row[f"rate_{short}_per_voyage"] is None, short
        assert row[f"rate_{short}_per_observed_day"] is None, short
        assert row[f"rate_{short}_per_1000_messages"] == pytest.approx(0.0), short
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
        ship_type_rows=[(mmsi, "Tanker"), (mmsi, "Tanker"), (mmsi, "Cargo")],
        # 4 distinct cell_hour values spanning 3 distinct dates -> n_observed_hours=4,
        # n_observed_days=3.
        liveness_rows=[
            (mmsi, _ts(date(2024, 6, 2), 0)),
            (mmsi, _ts(date(2024, 6, 2), 1)),
            (mmsi, _ts(date(2024, 6, 3), 0)),
            (mmsi, _ts(date(2024, 6, 4), 0)),
        ],
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

    assert row["n_observed_hours"] == 4
    assert row["n_observed_days"] == 3
    # voyage_count=2, n_observed_days=3, total_message_count=50 (0.05 thousand) -- every rate
    # column is the raw count divided by the matching denominator, exactly, never truncated or
    # otherwise transformed.
    assert row["rate_gaps_per_voyage"] == pytest.approx(2 / 2)
    assert row["rate_gaps_per_observed_day"] == pytest.approx(2 / 3)
    assert row["rate_gaps_per_1000_messages"] == pytest.approx(2 / 0.05)
    assert row["rate_gaps_high_probability_per_voyage"] == pytest.approx(1 / 2)
    assert row["rate_gaps_high_probability_per_observed_day"] == pytest.approx(1 / 3)
    assert row["rate_gaps_high_probability_per_1000_messages"] == pytest.approx(1 / 0.05)
    assert row["rate_spoofing_events_total_per_voyage"] == pytest.approx(3 / 2)
    assert row["rate_spoofing_events_total_per_observed_day"] == pytest.approx(3 / 3)
    assert row["rate_spoofing_events_total_per_1000_messages"] == pytest.approx(3 / 0.05)
    assert row["rate_sts_episodes_per_voyage"] == pytest.approx(2 / 2)
    assert row["rate_sts_episodes_per_observed_day"] == pytest.approx(2 / 3)
    assert row["rate_sts_episodes_per_1000_messages"] == pytest.approx(2 / 0.05)
    assert row["rate_identity_anomalies_total_per_voyage"] == pytest.approx(2 / 2)
    assert row["rate_identity_anomalies_total_per_observed_day"] == pytest.approx(2 / 3)
    assert row["rate_identity_anomalies_total_per_1000_messages"] == pytest.approx(2 / 0.05)
    assert row["rate_destination_course_mismatch_per_voyage"] == pytest.approx(1 / 2)
    assert row["rate_destination_course_mismatch_per_observed_day"] == pytest.approx(1 / 3)
    assert row["rate_destination_course_mismatch_per_1000_messages"] == pytest.approx(1 / 0.05)
    assert row["rate_draught_change_unexplained_per_voyage"] == pytest.approx(0.0)
    assert row["rate_draught_change_unexplained_per_observed_day"] == pytest.approx(0.0)
    assert row["rate_draught_change_unexplained_per_1000_messages"] == pytest.approx(0.0)

    # other_mmsi has no liveness rows of its own -> exposure coalesces to 0, so its own rate
    # columns (denominators all 0) must be NULL, not an error or a copy of mmsi's.
    other_row = rows[other_mmsi]
    assert other_row["n_observed_hours"] == 0
    assert other_row["n_observed_days"] == 0
    assert other_row["rate_sts_episodes_per_observed_day"] is None

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


def test_raises_when_liveness_path_missing(tmp_path):
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000016, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    _write_sanctions_matches(p.sanctions_matches, [])
    _write_voyages(p.voyages, [])
    # liveness.parquet deliberately not written.
    with pytest.raises(FileNotFoundError):
        _run(p)


def test_raises_when_a_detector_table_missing(tmp_path):
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000015, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    _write_sanctions_matches(p.sanctions_matches, [])
    _write_voyages(p.voyages, [])
    _write_liveness(p.liveness, [])
    _write_ship_type_reference(p.ship_type, [])
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

    # P4-0's exposure/rate columns must land as ordinary features, never under the label_ prefix
    # -- wired in here so a future rename can't silently move one into the label contract above.
    expected_rate_columns = {
        f"rate_{col.removeprefix('n_')}_{suffix}"
        for col in panel.RATE_BASE_COLUMNS
        for suffix in ("per_voyage", "per_observed_day", "per_1000_messages")
    }
    assert expected_rate_columns <= non_label_columns
    assert {"n_observed_hours", "n_observed_days"} <= non_label_columns


def test_raises_when_ship_type_reference_missing(tmp_path):
    """P3-4/A2's hard-blocker fix: _build_ship_type now requires process.ship_type's reference
    for this exact window, not a direct clean-partition scan."""
    p = _Paths(tmp_path)
    _write_mmsi_imo(
        p.mmsi_imo, [(219000016, VALID_IMO_A, 5, date(2024, 6, 1), date(2024, 6, 5), False, False)]
    )
    _write_sanctions_matches(p.sanctions_matches, [])
    _write_voyages(p.voyages, [])
    _write_liveness(p.liveness, [])
    # p.ship_type deliberately never written.
    with pytest.raises(FileNotFoundError, match="ship_type"):
        _run(p)
