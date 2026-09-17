"""Unit tests for pipeline.manifest. All fixtures are synthetic, written to tmp_path;
nothing here touches data/.
"""

from __future__ import annotations

import json
from datetime import date

from pipeline import manifest

DAY = date(2024, 6, 5)
DAY2 = date(2024, 6, 6)


def test_load_returns_empty_dict_when_missing(tmp_path):
    path = tmp_path / "manifest.json"
    assert manifest.load(path) == {}


def test_record_then_load_round_trips(tmp_path):
    path = tmp_path / "manifest.json"

    manifest.record(path, DAY, raw_rows=100, raw_bytes=1000, downloaded_at="2024-06-05T00:00:00+00:00")

    loaded = manifest.load(path)
    assert loaded == {
        "2024-06-05": {
            "raw_rows": 100,
            "raw_bytes": 1000,
            "downloaded_at": "2024-06-05T00:00:00+00:00",
        }
    }


def test_record_merges_fields_across_separate_calls(tmp_path):
    """Two record() calls for the same day (e.g. download step, then clean step) merge."""
    path = tmp_path / "manifest.json"

    manifest.record(path, DAY, raw_rows=100, downloaded_at="t1")
    manifest.record(path, DAY, clean_rows=90, cleaned_at="t2")

    entry = manifest.load(path)["2024-06-05"]
    assert entry == {
        "raw_rows": 100,
        "downloaded_at": "t1",
        "clean_rows": 90,
        "cleaned_at": "t2",
    }


def test_record_does_not_disturb_other_days(tmp_path):
    path = tmp_path / "manifest.json"

    manifest.record(path, DAY, raw_rows=100)
    manifest.record(path, DAY2, raw_rows=200)

    loaded = manifest.load(path)
    assert loaded["2024-06-05"]["raw_rows"] == 100
    assert loaded["2024-06-06"]["raw_rows"] == 200


def test_day_state_absent_when_no_entry(tmp_path):
    path = tmp_path / "manifest.json"
    assert manifest.day_state(path, DAY) == "absent"


def test_day_state_raw_only(tmp_path):
    path = tmp_path / "manifest.json"
    manifest.record(path, DAY, raw_rows=100, raw_bytes=1000, downloaded_at="t1")
    assert manifest.day_state(path, DAY) == "raw_only"


def test_day_state_clean(tmp_path):
    path = tmp_path / "manifest.json"
    manifest.record(path, DAY, raw_rows=100, downloaded_at="t1")
    manifest.record(path, DAY, clean_rows=90, cleaned_at="t2")
    assert manifest.day_state(path, DAY) == "clean"


def test_day_state_reduced(tmp_path):
    path = tmp_path / "manifest.json"
    manifest.record(path, DAY, raw_rows=100, downloaded_at="t1")
    manifest.record(path, DAY, clean_rows=90, cleaned_at="t2")
    manifest.record(path, DAY, clean_discarded_at="t3")
    assert manifest.day_state(path, DAY) == "reduced"


def test_day_state_negative_raw_only_is_not_clean(tmp_path):
    """Negative case: a day with only raw fields must not be reported as clean."""
    path = tmp_path / "manifest.json"
    manifest.record(path, DAY, raw_rows=100, downloaded_at="t1")
    assert manifest.day_state(path, DAY) != "clean"
    assert manifest.day_state(path, DAY) != "reduced"


def test_record_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "manifest.json"
    manifest.record(path, DAY, raw_rows=100)
    assert path.exists()


def test_record_leaves_no_tmp_file_and_valid_json(tmp_path):
    """Atomicity: a normal write leaves the .tmp file gone and the real file parseable."""
    path = tmp_path / "manifest.json"

    manifest.record(path, DAY, raw_rows=100, downloaded_at="t1")

    tmp_sibling = path.with_suffix(".tmp")
    assert not tmp_sibling.exists()
    with open(path, encoding="utf-8") as fh:
        parsed = json.load(fh)  # raises if not valid JSON
    assert parsed["2024-06-05"]["raw_rows"] == 100


def test_manifest_json_is_indented_and_sorted(tmp_path):
    """Readable diffs: indent=2 and sorted day keys."""
    path = tmp_path / "manifest.json"

    manifest.record(path, DAY2, raw_rows=200)
    manifest.record(path, DAY, raw_rows=100)

    text = path.read_text(encoding="utf-8")
    assert "  " in text  # indented, not compact
    keys = list(json.loads(text).keys())
    assert keys == sorted(keys)


def test_summary_counts_each_state(tmp_path):
    path = tmp_path / "manifest.json"
    day3 = date(2024, 6, 7)
    day4 = date(2024, 6, 8)

    manifest.record(path, DAY, raw_rows=100, downloaded_at="t1")  # raw_only
    manifest.record(path, DAY2, raw_rows=100, downloaded_at="t1")
    manifest.record(path, DAY2, clean_rows=90, cleaned_at="t2")  # clean
    manifest.record(path, day3, raw_rows=100, downloaded_at="t1")
    manifest.record(path, day3, clean_rows=90, cleaned_at="t2")
    manifest.record(path, day3, clean_discarded_at="t3")  # reduced
    manifest.record(path, day4, raw_rows=100, downloaded_at="t1")  # raw_only

    result = manifest.summary(path)
    assert result == {"total_days": 4, "raw_only": 2, "clean": 1, "reduced": 1}


def test_summary_empty_manifest(tmp_path):
    path = tmp_path / "manifest.json"
    assert manifest.summary(path) == {"total_days": 0, "raw_only": 0, "clean": 0, "reduced": 0}
