"""Unit tests for process.partitions. All fixtures are synthetic, written to tmp_path."""

from __future__ import annotations

from datetime import date

from process import partitions

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)
DAY3 = date(2024, 6, 7)


def test_daterange_is_inclusive_of_both_ends():
    assert list(partitions.daterange(DAY, DAY3)) == [DAY, DAY2, DAY3]


def test_daterange_single_day():
    assert list(partitions.daterange(DAY, DAY)) == [DAY]


def test_partition_path_shape(tmp_path):
    path = partitions.partition_path(DAY, tmp_path)
    assert path == tmp_path / "date=2024-06-05" / "part-0.parquet"


def test_existing_partitions_finds_only_days_present(tmp_path):
    for day in (DAY, DAY3):
        partition_dir = tmp_path / f"date={day.isoformat()}"
        partition_dir.mkdir(parents=True)
        (partition_dir / "part-0.parquet").touch()

    found = partitions.existing_partitions(DAY, DAY3, tmp_path)

    assert [day for day, _path in found] == [DAY, DAY3]


def test_existing_partitions_warns_on_missing_day(tmp_path, caplog):
    partition_dir = tmp_path / f"date={DAY.isoformat()}"
    partition_dir.mkdir(parents=True)
    (partition_dir / "part-0.parquet").touch()

    with caplog.at_level("WARNING"):
        found = partitions.existing_partitions(DAY, DAY2, tmp_path)

    assert [day for day, _path in found] == [DAY]
    assert "No clean partition for 2024-06-06" in caplog.text


def test_existing_partitions_empty_when_nothing_exists(tmp_path):
    assert partitions.existing_partitions(DAY, DAY3, tmp_path) == []
