"""Unit tests for ingest.gfw. All HTTP is mocked; nothing here touches the network.

The response shape used here matches the real GFW Events API, confirmed against a live call on
2026-09-21 (see ingest.gfw's module docstring and docs/DECISIONS.md): each encounter is reported
twice, mirrored, as '.1'/'.2' entries sharing a base id, and each entry already carries both
vessels' ssvid (own under 'vessel', the other under 'encounter.vessel').
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import httpx
import pytest

from ingest import gfw

START = date(2024, 6, 1)
END = date(2024, 6, 2)


def _entry(
    base_id: str,
    side: int,
    own_ssvid: int,
    other_ssvid: int,
    start: str,
    end: str,
    lat: float,
    lon: float,
    encounter_type: str = "fishing-fishing",
) -> dict:
    return {
        "id": f"{base_id}.{side}",
        "type": "encounter",
        "start": start,
        "end": end,
        "position": {"lat": lat, "lon": lon},
        "vessel": {"id": f"gfw-{own_ssvid}", "ssvid": str(own_ssvid), "flag": "DK", "type": "fishing"},
        "encounter": {
            "vessel": {"id": f"gfw-{other_ssvid}", "ssvid": str(other_ssvid), "flag": "DK", "type": "fishing"},
            "type": encounter_type,
            "medianDistanceKilometers": 0.3,
            "medianSpeedKnots": 0.1,
        },
    }


def _mirrored_pair(
    base_id: str, mmsi_a: int, mmsi_b: int, start: str, end: str, lat=55.0, lon=12.0, encounter_type="fishing-fishing"
) -> list[dict]:
    return [
        _entry(base_id, 1, mmsi_a, mmsi_b, start, end, lat, lon, encounter_type),
        _entry(base_id, 2, mmsi_b, mmsi_a, start, end, lat, lon, encounter_type),
    ]


def _client_returning(pages: list[list[dict]], calls: list[str] | None = None) -> httpx.Client:
    """A client whose transport serves one page of entries per call, in order, then an empty page."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        idx = state["n"]
        state["n"] += 1
        page = pages[idx] if idx < len(pages) else []
        return httpx.Response(200, json={"entries": page})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_raw_entries_single_page():
    entries = _mirrored_pair("evt-1", 219000001, 219000002, "2024-06-01T10:00:00.000Z", "2024-06-01T12:00:00.000Z")
    client = _client_returning([entries])

    result = gfw.fetch_raw_entries(START, END, api_token="tok", client=client, page_limit=200)

    assert result == entries


def test_fetch_raw_entries_paginates_until_short_page():
    page_limit = 2
    full_page = [
        _entry(f"evt-{i}", 1, i, i + 1000, "2024-06-01T10:00:00.000Z", "2024-06-01T12:00:00.000Z", 0, 0)
        for i in range(page_limit)
    ]
    short_page = [_entry("evt-last", 1, 999, 998, "2024-06-01T10:00:00.000Z", "2024-06-01T12:00:00.000Z", 0, 0)]
    calls: list[str] = []
    client = _client_returning([full_page, short_page], calls=calls)

    result = gfw.fetch_raw_entries(START, END, api_token="tok", client=client, page_limit=page_limit)

    assert len(result) == page_limit + len(short_page)
    assert len(calls) == 2
    assert "offset=0" in calls[0]
    assert "offset=2" in calls[1]


def test_fetch_raw_entries_sends_auth_header_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"entries": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    gfw.fetch_raw_entries(START, END, api_token="secret-token", client=client)

    assert seen["auth"] == "Bearer secret-token"
    assert "datasets%5B0%5D=public-global-encounters-events%3Alatest" in seen["url"] or \
        "datasets[0]=public-global-encounters-events:latest" in seen["url"]
    assert "start-date=2024-06-01" in seen["url"]
    assert "end-date=2024-06-02" in seen["url"]


def test_fetch_raw_entries_raises_clear_error_on_rate_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "60"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="rate limit"):
        gfw.fetch_raw_entries(START, END, api_token="tok", client=client)


def test_extract_entries_falls_back_to_data_key():
    payload = {"data": [{"id": "x"}]}
    assert gfw._extract_entries(payload) == [{"id": "x"}]


def test_extract_entries_raises_on_unrecognised_envelope():
    with pytest.raises(gfw.GFWSchemaError):
        gfw._extract_entries({"unexpected_key": []})


def test_normalise_entries_dedupes_mirrored_pair_into_one_encounter():
    entries = _mirrored_pair(
        "evt-1", 219000001, 219000002, "2024-06-01T10:00:00.000Z", "2024-06-01T12:00:00.000Z", lat=55.5, lon=12.5
    )

    encounters = gfw.normalise_entries(entries)

    assert len(encounters) == 1
    e = encounters[0]
    assert e.gfw_event_id == "evt-1"
    assert {e.mmsi_a, e.mmsi_b} == {219000001, 219000002}
    assert e.start_time == datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert e.end_time == datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert e.latitude == 55.5
    assert e.longitude == 12.5
    assert e.encounter_type == "fishing-fishing"


def test_normalise_entries_handles_a_single_unmirrored_entry():
    entries = [_entry("evt-1", 1, 111, 222, "2024-06-01T10:00:00.000Z", "2024-06-01T12:00:00.000Z", 55.0, 12.0)]

    encounters = gfw.normalise_entries(entries)

    assert len(encounters) == 1
    assert {encounters[0].mmsi_a, encounters[0].mmsi_b} == {111, 222}


def test_normalise_entries_raises_on_id_without_suffix():
    entries = [{"id": "no-suffix-here", "start": "x", "end": "y"}]
    with pytest.raises(gfw.GFWSchemaError):
        gfw.normalise_entries(entries)


def test_normalise_entries_raises_on_missing_ssvid():
    entries = [{"id": "evt-1.1", "start": "x", "end": "y", "position": {"lat": 0, "lon": 0}, "vessel": {}}]
    with pytest.raises(gfw.GFWSchemaError):
        gfw.normalise_entries(entries)


def test_normalise_entries_raises_on_missing_position():
    entries = [
        {
            "id": "evt-1.1",
            "start": "x",
            "end": "y",
            "vessel": {"ssvid": "1"},
            "encounter": {"vessel": {"ssvid": "2"}},
        }
    ]
    with pytest.raises(gfw.GFWSchemaError):
        gfw.normalise_entries(entries)


def test_filter_bbox_keeps_only_inside_points():
    entries = _mirrored_pair("in", 1, 2, "2024-06-01T00:00:00.000Z", "2024-06-01T02:00:00.000Z", lat=55.0, lon=12.0) + \
        _mirrored_pair("out", 3, 4, "2024-06-01T00:00:00.000Z", "2024-06-01T02:00:00.000Z", lat=10.0, lon=10.0)
    encounters = gfw.normalise_entries(entries)

    kept = gfw.filter_bbox(encounters, min_lat=50.0, max_lat=60.0, min_lon=8.0, max_lon=16.0)

    assert [e.gfw_event_id for e in kept] == ["in"]


def test_build_gfw_encounters_writes_parquet_and_is_idempotent(tmp_path):
    entries = _mirrored_pair(
        "evt-1", 219000001, 219000002, "2024-06-01T10:00:00.000Z", "2024-06-01T12:00:00.000Z", lat=55.5, lon=12.5
    )
    calls: list[str] = []
    client = _client_returning([entries], calls=calls)
    out_path = tmp_path / "gfw_encounters.parquet"

    result = gfw.build_gfw_encounters(START, END, api_token="tok", out_path=out_path, client=client)

    assert result == out_path
    assert out_path.exists()
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT gfw_event_id, mmsi_a, mmsi_b, encounter_type FROM '{out_path.as_posix()}'"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("evt-1", 219000001, 219000002, "fishing-fishing")]

    calls_after_first = len(calls)
    gfw.build_gfw_encounters(START, END, api_token="tok", out_path=out_path, client=client)
    assert len(calls) == calls_after_first, "re-running without --force must not re-fetch"


def test_build_gfw_encounters_applies_bbox(tmp_path):
    entries = _mirrored_pair("in", 1, 2, "2024-06-01T00:00:00.000Z", "2024-06-01T02:00:00.000Z", lat=55.0, lon=12.0) + \
        _mirrored_pair("out", 3, 4, "2024-06-01T00:00:00.000Z", "2024-06-01T02:00:00.000Z", lat=10.0, lon=10.0)
    client = _client_returning([entries])
    out_path = tmp_path / "gfw_encounters.parquet"

    gfw.build_gfw_encounters(
        START, END, api_token="tok", bbox=(50.0, 60.0, 8.0, 16.0), out_path=out_path, client=client
    )

    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT gfw_event_id FROM '{out_path.as_posix()}'").fetchall()
    finally:
        con.close()
    assert rows == [("in",)]
