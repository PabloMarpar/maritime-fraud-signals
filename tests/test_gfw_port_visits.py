"""Unit tests for ingest.gfw_port_visits. All HTTP is mocked; nothing here touches the network.

Response shapes match real GFW API v3 calls made while writing this module (see its module
docstring): /v3/vessels/search returns {"entries": [...]} where each entry carries its own
selfReportedInfo list (each with its own "id", the gfw_vessel_id); /v3/events returns
{"entries": [...], "nextOffset": ...} where each port-visit entry carries "vessel": {"id": ...}
and a "port_visit" sub-object with startAnchorage/endAnchorage.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pytest

from ingest import gfw_port_visits as gpv

# Captured before any test monkeypatches httpx.Client itself, so the patched constructor below
# can still build a real client around a MockTransport without recursing into its own patch.
_RealClient = httpx.Client


_UNSET = object()


def _search_entry(
    imo: str,
    vessel_id: str,
    sri_imo: str | None = _UNSET,
    ssvid: str = "219000001",
    shipname: str = "TEST SHIP",
    flag: str = "DNK",
    callsign: str = "OXAB",
    date_from: str = "2023-01-01T00:00:00Z",
    date_to: str = "2024-01-01T00:00:00Z",
    registry_imo: str | None = None,
) -> dict:
    resolved_sri_imo = imo if sri_imo is _UNSET else sri_imo
    return {
        "registryInfo": [{"imo": registry_imo}] if registry_imo else [],
        "selfReportedInfo": [
            {
                "id": vessel_id,
                "ssvid": ssvid,
                "shipname": shipname,
                "flag": flag,
                "callsign": callsign,
                "imo": resolved_sri_imo,
                "transmissionDateFrom": date_from,
                "transmissionDateTo": date_to,
            }
        ],
    }


def _search_client(payload: dict, calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _port_visit_entry(
    vessel_id: str,
    event_id: str,
    start: str,
    end: str,
    confidence: str = "4",
    visit_id: str = "visit-1",
    start_anchorage_name: str = "PRIMORSK",
    start_anchorage_flag: str = "RUS",
    event_type: str = "port_visit",
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "start": start,
        "end": end,
        "vessel": {"id": vessel_id, "name": "TEST SHIP", "ssvid": "219000001"},
        "port_visit": {
            "visitId": visit_id,
            "confidence": confidence,
            "durationHrs": 12.5,
            "startAnchorage": {
                "anchorageId": "anch-1",
                "name": start_anchorage_name,
                "flag": start_anchorage_flag,
                "lat": 60.1,
                "lon": 28.7,
                "atDock": True,
            },
            "endAnchorage": {
                "anchorageId": "anch-2",
                "name": "SOME OTHER PORT",
                "flag": "DNK",
            },
        },
    }


def _events_client(pages: list[dict], calls: list[str] | None = None) -> httpx.Client:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        idx = state["n"]
        state["n"] += 1
        payload = pages[idx] if idx < len(pages) else {"entries": [], "nextOffset": None}
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------------------------
# load_api_token
# --------------------------------------------------------------------------------------------


def test_load_api_token_prefers_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GFW_API_TOKEN", "from-env")
    assert gpv.load_api_token(env_path=tmp_path / "missing.env") == "from-env"


def test_load_api_token_falls_back_to_dotenv_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_VAR=x\nGFW_API_TOKEN=from-file\n", encoding="utf-8")
    assert gpv.load_api_token(env_path=env_file) == "from-file"


def test_load_api_token_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("GFW_API_TOKEN", raising=False)
    assert gpv.load_api_token(env_path=tmp_path / "missing.env") is None


# --------------------------------------------------------------------------------------------
# identity normalisation
# --------------------------------------------------------------------------------------------


def test_self_reported_identities_keeps_own_imo_match():
    payload = {"entries": [_search_entry("9195717", "vid-1")]}
    result = gpv._self_reported_identities("9195717", payload)
    assert len(result) == 1
    assert result[0].gfw_vessel_id == "vid-1"
    assert result[0].ssvid == "219000001"
    assert result[0].transmission_date_from == datetime(2023, 1, 1)  # noqa: DTZ001 -- naive UTC, see _parse_gfw_timestamp


def test_self_reported_identities_collects_several_entries_one_imo():
    """A real IMO returned 4 top-level entries (flag/MMSI changes) -- see module docstring."""
    payload = {
        "entries": [
            _search_entry("9195717", "vid-1", flag="NLD"),
            _search_entry("9195717", "vid-2", flag="CYP"),
            _search_entry("9195717", "vid-3", flag="PLW"),
        ]
    }
    result = gpv._self_reported_identities("9195717", payload)
    assert {r.gfw_vessel_id for r in result} == {"vid-1", "vid-2", "vid-3"}


def test_self_reported_identities_drops_entry_with_different_imo():
    """Negative case: a selfReportedInfo record for a DIFFERENT imo must not be kept, even if it
    arrives alongside a matching one in the same payload."""
    payload = {
        "entries": [
            _search_entry("9195717", "vid-1"),
            _search_entry("9195717", "vid-wrong", sri_imo="1234567"),
        ]
    }
    result = gpv._self_reported_identities("9195717", payload)
    assert [r.gfw_vessel_id for r in result] == ["vid-1"]


def test_self_reported_identities_keeps_entry_matched_via_registry_imo():
    """A selfReportedInfo record with no own imo field is kept if the entry's registryInfo imo
    matches -- the 'or that the API matched on imo' clause in the task spec."""
    payload = {
        "entries": [_search_entry("9195717", "vid-1", sri_imo=None, registry_imo="9195717")]
    }
    result = gpv._self_reported_identities("9195717", payload)
    assert [r.gfw_vessel_id for r in result] == ["vid-1"]


def test_self_reported_identities_drops_entry_matched_via_wrong_registry_imo():
    payload = {
        "entries": [_search_entry("9195717", "vid-1", sri_imo=None, registry_imo="1234567")]
    }
    result = gpv._self_reported_identities("9195717", payload)
    assert result == []


def test_self_reported_identities_empty_when_no_entries():
    assert gpv._self_reported_identities("9195717", {"entries": [], "total": 0}) == []


def test_resolve_vessel_ids_sends_where_and_dataset_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"entries": [_search_entry("9195717", "vid-1")]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = gpv.resolve_vessel_ids("9195717", api_token="secret", client=client)

    assert len(result) == 1
    assert seen["auth"] == "Bearer secret"
    assert "imo%3D%279195717%27" in seen["url"] or "imo='9195717'" in seen["url"]
    assert "datasets%5B0%5D=public-global-vessel-identity%3Alatest" in seen["url"] or \
        "datasets[0]=public-global-vessel-identity:latest" in seen["url"]


# --------------------------------------------------------------------------------------------
# port-visit event normalisation
# --------------------------------------------------------------------------------------------


def test_parse_port_visit_entry_normalises_fields():
    id_to_imo = {"vid-1": "9195717"}
    entry = _port_visit_entry(
        "vid-1", "evt-1", "2024-06-01T00:00:00.000Z", "2024-06-03T00:00:00.000Z"
    )
    event = gpv._parse_port_visit_entry(id_to_imo, entry)
    assert event is not None
    assert event.imo == "9195717"
    assert event.gfw_vessel_id == "vid-1"
    assert event.event_id == "evt-1"
    assert event.confidence == 4
    assert event.start_anchorage_name == "PRIMORSK"
    assert event.start_anchorage_flag == "RUS"
    assert event.start == datetime(2024, 6, 1)  # noqa: DTZ001 -- naive UTC, see _parse_gfw_timestamp
    assert event.end == datetime(2024, 6, 3)  # noqa: DTZ001 -- naive UTC, see _parse_gfw_timestamp


def test_parse_port_visit_entry_returns_none_for_unknown_vessel_id():
    entry = _port_visit_entry("vid-unknown", "evt-1", "2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z")
    assert gpv._parse_port_visit_entry({}, entry) is None


def test_parse_port_visit_entry_returns_none_for_non_port_visit_type():
    id_to_imo = {"vid-1": "9195717"}
    entry = _port_visit_entry(
        "vid-1", "evt-1", "2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z", event_type="encounter"
    )
    assert gpv._parse_port_visit_entry(id_to_imo, entry) is None


def test_parse_port_visit_entry_handles_missing_confidence():
    id_to_imo = {"vid-1": "9195717"}
    entry = _port_visit_entry("vid-1", "evt-1", "2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z")
    entry["port_visit"]["confidence"] = None
    event = gpv._parse_port_visit_entry(id_to_imo, entry)
    assert event.confidence is None


def test_fetch_port_visits_batch_paginates_on_next_offset():
    page0 = {
        "entries": [
            _port_visit_entry("vid-1", "evt-1", "2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z")
        ],
        "nextOffset": 200,
    }
    page1 = {
        "entries": [
            _port_visit_entry("vid-1", "evt-2", "2024-07-01T00:00:00Z", "2024-07-02T00:00:00Z")
        ],
        "nextOffset": None,
    }
    calls: list[str] = []
    client = _events_client([page0, page1], calls=calls)

    entries = gpv.fetch_port_visits_batch(["vid-1"], api_token="tok", client=client)

    assert [e["id"] for e in entries] == ["evt-1", "evt-2"]
    assert len(calls) == 2
    assert "offset=0" in calls[0]
    assert "offset=200" in calls[1]


def test_fetch_port_visits_batch_sends_one_param_per_vessel_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"entries": [], "nextOffset": None})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gpv.fetch_port_visits_batch(["vid-1", "vid-2"], api_token="tok", client=client)

    assert "vessels%5B0%5D=vid-1" in seen["url"] or "vessels[0]=vid-1" in seen["url"]
    assert "vessels%5B1%5D=vid-2" in seen["url"] or "vessels[1]=vid-2" in seen["url"]


def test_fetch_port_visits_batch_chunks_ids_over_the_server_cap(monkeypatch):
    """Real bug found in the P4-11 run: one real imo had 26 selfReportedInfo ids (repeated
    flag/MMSI changes) -- more than the server's hard 20-id cap on this endpoint (confirmed live
    by binary search, see EVENTS_BATCH_SIZE). fetch_port_visits_batch must split internally
    rather than ever sending more than EVENTS_BATCH_SIZE ids in one call."""
    monkeypatch.setattr(gpv, "EVENTS_BATCH_SIZE", 2)
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = [v for k, v in request.url.params.multi_items() if k.startswith("vessels[")]
        calls.append(ids)
        return httpx.Response(200, json={"entries": [], "nextOffset": None})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gpv.fetch_port_visits_batch(["a", "b", "c", "d", "e"], api_token="tok", client=client)

    assert calls == [["a", "b"], ["c", "d"], ["e"]]


# --------------------------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------------------------


def test_get_with_retries_succeeds_after_transient_5xx(monkeypatch):
    monkeypatch.setattr(gpv.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = gpv._get_with_retries(client, "https://example.test/x", [], "tok")

    assert result == {"ok": True}
    assert calls["n"] == 3


def test_get_with_retries_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(gpv.time, "sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="Exhausted"):
        gpv._get_with_retries(client, "https://example.test/x", [], "tok")


def test_get_with_retries_does_not_retry_a_real_4xx(monkeypatch):
    monkeypatch.setattr(gpv.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        gpv._get_with_retries(client, "https://example.test/x", [], "tok")
    assert calls["n"] == 1


# --------------------------------------------------------------------------------------------
# batching
# --------------------------------------------------------------------------------------------


def test_pack_batches_keeps_one_imos_ids_together():
    imo_to_ids = {"imoA": ["a1", "a2"], "imoB": ["b1"], "imoC": ["c1", "c2", "c3"]}
    batches = gpv._pack_batches(imo_to_ids, batch_size=3)
    # imoA (2) + imoB (1) fit in one batch of 3; imoC (3) starts a new one.
    assert batches == [(["imoA", "imoB"], ["a1", "a2", "b1"]), (["imoC"], ["c1", "c2", "c3"])]


def test_pack_batches_keeps_an_oversized_single_imo_whole():
    imo_to_ids = {"imoA": ["a1", "a2", "a3", "a4"]}
    batches = gpv._pack_batches(imo_to_ids, batch_size=2)
    assert batches == [(["imoA"], ["a1", "a2", "a3", "a4"])]


def test_pack_batches_empty_input():
    assert gpv._pack_batches({}, batch_size=100) == []


# --------------------------------------------------------------------------------------------
# end-to-end: resolve + fetch + aggregate, with a real tmp cache tree
# --------------------------------------------------------------------------------------------


def _make_panel(tmp_path: Path, window: str, imos: list[str | None]) -> None:
    part_dir = tmp_path / "panel" / f"window={window}"
    part_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (mmsi BIGINT, imo VARCHAR)")
        con.executemany("INSERT INTO t VALUES (?, ?)", [(i, imo) for i, imo in enumerate(imos)])
        con.execute(f"COPY t TO '{(part_dir / 'part-0.parquet').as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def test_distinct_imos_excludes_null_and_stray_window(tmp_path):
    panel_root = tmp_path / "panel"
    _make_panel(tmp_path, "2024-06-01_2024-06-30", ["1111111", "2222222", None])
    _make_panel(tmp_path, gpv.STRAY_WINDOW.removeprefix("window="), ["9999999"])

    result = gpv.distinct_imos(panel_root=panel_root)

    assert result == ["1111111", "2222222"]
    assert "9999999" not in result


def test_resolve_all_vessel_ids_writes_one_cache_file_per_imo(tmp_path, monkeypatch):
    monkeypatch.setattr(gpv, "VESSEL_IDS_CACHE", tmp_path / "vessel_ids")
    payload = {"entries": [_search_entry("1111111", "vid-a")]}

    def handler(request: httpx.Request) -> httpx.Response:
        # both imos resolve to the same canned payload for simplicity; imo isn't echoed by the
        # mock server, only used by resolve_vessel_ids's own imo argument for filtering.
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, "Client", lambda timeout=60.0: _RealClient(transport=httpx.MockTransport(handler)))

    gpv.resolve_all_vessel_ids(["1111111", "2222222"], api_token="tok", workers=2)

    assert (tmp_path / "vessel_ids" / "1111111.parquet").exists()
    assert (tmp_path / "vessel_ids" / "2222222.parquet").exists()


def test_resolve_all_vessel_ids_skips_already_cached_imo(tmp_path, monkeypatch):
    cache_dir = tmp_path / "vessel_ids"
    monkeypatch.setattr(gpv, "VESSEL_IDS_CACHE", cache_dir)
    gpv._write_rows_parquet([], gpv.VESSEL_IDS_SCHEMA, cache_dir / "1111111.parquet")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"entries": []})

    monkeypatch.setattr(httpx, "Client", lambda timeout=60.0: _RealClient(transport=httpx.MockTransport(handler)))

    gpv.resolve_all_vessel_ids(["1111111"], api_token="tok", workers=1)

    assert calls == []  # already cached -- rerun must not re-fetch it


def test_fetch_all_port_visits_marks_zero_id_imo_done_without_a_call(tmp_path, monkeypatch):
    monkeypatch.setattr(gpv, "VESSEL_IDS_CACHE", tmp_path / "vessel_ids")
    monkeypatch.setattr(gpv, "PORT_VISITS_CACHE", tmp_path / "port_visits")
    gpv._write_rows_parquet([], gpv.VESSEL_IDS_SCHEMA, tmp_path / "vessel_ids" / "1111111.parquet")
    (tmp_path / "vessel_ids").mkdir(exist_ok=True)

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"entries": [], "nextOffset": None})

    monkeypatch.setattr(httpx, "Client", lambda timeout=60.0: _RealClient(transport=httpx.MockTransport(handler)))

    gpv.fetch_all_port_visits(["1111111"], api_token="tok", workers=1)

    assert calls == []
    assert (tmp_path / "port_visits" / "1111111.parquet").exists()


def test_fetch_all_port_visits_fetches_and_maps_events_back_to_imo(tmp_path, monkeypatch):
    vessel_ids_cache = tmp_path / "vessel_ids"
    port_visits_cache = tmp_path / "port_visits"
    monkeypatch.setattr(gpv, "VESSEL_IDS_CACHE", vessel_ids_cache)
    monkeypatch.setattr(gpv, "PORT_VISITS_CACHE", port_visits_cache)
    gpv._write_rows_parquet(
        [("1111111", "vid-1", None, None, None, None, None, None)],
        gpv.VESSEL_IDS_SCHEMA,
        vessel_ids_cache / "1111111.parquet",
    )

    events_payload = {
        "entries": [
            _port_visit_entry("vid-1", "evt-1", "2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z")
        ],
        "nextOffset": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=events_payload)

    monkeypatch.setattr(httpx, "Client", lambda timeout=60.0: _RealClient(transport=httpx.MockTransport(handler)))

    gpv.fetch_all_port_visits(["1111111"], api_token="tok", workers=1)

    out_path = port_visits_cache / "1111111.parquet"
    assert out_path.exists()
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT imo, event_id, confidence FROM '{out_path.as_posix()}'").fetchall()
    finally:
        con.close()
    assert rows == [("1111111", "evt-1", 4)]


def test_build_gfw_port_visits_end_to_end_and_idempotent(tmp_path, monkeypatch):
    panel_root = tmp_path / "panel"
    _make_panel(tmp_path, "2024-06-01_2024-06-30", ["1111111", "2222222"])
    monkeypatch.setattr(gpv, "VESSEL_IDS_CACHE", tmp_path / "cache" / "vessel_ids")
    monkeypatch.setattr(gpv, "PORT_VISITS_CACHE", tmp_path / "cache" / "port_visits")
    monkeypatch.setattr(gpv, "CACHE_ROOT", tmp_path / "cache")

    search_calls: list[str] = []
    events_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "vessels/search" in url:
            search_calls.append(url)
            if "1111111" in url:
                return httpx.Response(200, json={"entries": [_search_entry("1111111", "vid-1")]})
            return httpx.Response(200, json={"entries": []})
        events_calls.append(url)
        return httpx.Response(
            200,
            json={
                "entries": [
                    _port_visit_entry(
                        "vid-1", "evt-1", "2024-06-01T00:00:00Z", "2024-06-02T00:00:00Z"
                    )
                ],
                "nextOffset": None,
            },
        )

    monkeypatch.setattr(httpx, "Client", lambda timeout=60.0: _RealClient(transport=httpx.MockTransport(handler)))

    vessel_ids_out = tmp_path / "vessel_ids.parquet"
    port_visits_out = tmp_path / "port_visits.parquet"
    gpv.build_gfw_port_visits(
        panel_root=panel_root,
        vessel_ids_out=vessel_ids_out,
        port_visits_out=port_visits_out,
        api_token="tok",
        workers=2,
    )

    con = duckdb.connect()
    try:
        vessel_rows = con.execute(f"SELECT imo, gfw_vessel_id FROM '{vessel_ids_out.as_posix()}'").fetchall()
        visit_rows = con.execute(f"SELECT imo, event_id FROM '{port_visits_out.as_posix()}'").fetchall()
    finally:
        con.close()
    assert vessel_rows == [("1111111", "vid-1")]
    assert visit_rows == [("1111111", "evt-1")]

    n_search_calls_first_run = len(search_calls)
    n_events_calls_first_run = len(events_calls)

    # rerun: fully cached, must not re-fetch anything.
    gpv.build_gfw_port_visits(
        panel_root=panel_root,
        vessel_ids_out=vessel_ids_out,
        port_visits_out=port_visits_out,
        api_token="tok",
        workers=2,
    )
    assert len(search_calls) == n_search_calls_first_run
    assert len(events_calls) == n_events_calls_first_run
