"""Unit tests for ingest.wikidata_ships. All HTTP is mocked; nothing here touches the network.

The response shape used here matches a real WDQS SPARQL JSON result, confirmed against a live
query on 2026-09-23 (see ingest.wikidata_ships's module docstring): bindings under
results.bindings, each a dict of {var: {"type", "value", ...}}, with OPTIONAL variables simply
absent from a binding rather than present with a null value.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx
import pytest

from ingest import wikidata_ships as wd


def _binding(item: str, imo: str, p729: str | None = None, p571: str | None = None) -> dict:
    b = {
        "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{item}"},
        "imo": {"type": "literal", "value": imo},
    }
    if p729 is not None:
        b["p729"] = {"type": "literal", "value": p729, "datatype": "dateTime"}
    if p571 is not None:
        b["p571"] = {"type": "literal", "value": p571, "datatype": "dateTime"}
    return b


def _client_returning(pages: list[list[dict]], calls: list[str] | None = None) -> httpx.Client:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        idx = state["n"]
        state["n"] += 1
        page = pages[idx] if idx < len(pages) else []
        return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": page}})

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- fetch_raw_rows -----------------------------------------------------------------------


def test_fetch_raw_rows_single_page():
    bindings = [_binding("Q1", "9187629", p729="1993-01-01T00:00:00Z")]
    client = _client_returning([bindings])

    result = wd.fetch_raw_rows(client=client, page_limit=200)

    assert len(result) == 1
    assert result[0].wikidata_id == "Q1"
    assert result[0].imo_raw == "9187629"
    assert result[0].service_entry == datetime(1993, 1, 1, tzinfo=timezone.utc)
    assert result[0].inception is None


def test_fetch_raw_rows_falls_back_to_p571_when_p729_absent():
    bindings = [_binding("Q2", "1234567", p571="1968-06-15T00:00:00Z")]
    client = _client_returning([bindings])

    result = wd.fetch_raw_rows(client=client, page_limit=200)

    assert result[0].service_entry is None
    assert result[0].inception == datetime(1968, 6, 15, tzinfo=timezone.utc)


def test_fetch_raw_rows_treats_unknown_value_date_as_no_date():
    # A real Wikidata "somevalue" (unknown value) statement surfaces as a blank-node genid URI,
    # not a date literal -- confirmed live 2026-09-23 against Q12329788's P729. Must be treated
    # as "no date", not raise WikidataSchemaError.
    bindings = [
        {
            "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q12329788"},
            "imo": {"type": "literal", "value": "7427166"},
            "p729": {
                "type": "uri",
                "value": "http://www.wikidata.org/.well-known/genid/8bb86ece3f563c2b7f1e27c2a4145536",
            },
        }
    ]
    client = _client_returning([bindings])

    result = wd.fetch_raw_rows(client=client, page_limit=200)

    assert result[0].service_entry is None


def test_fetch_raw_rows_handles_missing_dates_entirely():
    bindings = [_binding("Q3", "7654321")]
    client = _client_returning([bindings])

    result = wd.fetch_raw_rows(client=client, page_limit=200)

    assert result[0].service_entry is None
    assert result[0].inception is None


def test_fetch_raw_rows_paginates_until_short_page():
    page_limit = 2
    full_page = [_binding(f"Q{i}", f"100000{i}") for i in range(page_limit)]
    short_page = [_binding("Qlast", "9999999")]
    calls: list[str] = []
    client = _client_returning([full_page, short_page], calls=calls)

    result = wd.fetch_raw_rows(client=client, page_limit=page_limit)

    assert len(result) == page_limit + len(short_page)
    assert len(calls) == 2
    assert "OFFSET+0" in calls[0]
    assert "OFFSET+2" in calls[1]


def test_fetch_raw_rows_sends_descriptive_user_agent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"results": {"bindings": []}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    wd.fetch_raw_rows(client=client)

    assert "maritime-fraud-signals" in seen["user_agent"]


def test_fetch_raw_rows_raises_clear_error_on_rate_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="rate limit"):
        wd.fetch_raw_rows(client=client)


def test_extract_bindings_raises_on_unrecognised_envelope():
    with pytest.raises(wd.WikidataSchemaError):
        wd._extract_bindings({"unexpected_key": []})


def test_parse_binding_raises_on_missing_imo():
    with pytest.raises(wd.WikidataSchemaError):
        wd._parse_binding({"item": {"value": "http://www.wikidata.org/entity/Q1"}})


# --- build_ship_build_year (reconciliation) -----------------------------------------------


def _read(path: Path) -> list[dict]:
    con = duckdb.connect()
    try:
        cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{path.as_posix()}'").fetchall()]
        rows = con.execute(f"SELECT * FROM '{path.as_posix()}'").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        con.close()


def _row_for(rows: list[dict], imo: str) -> dict:
    for r in rows:
        if r["imo"] == imo:
            return r
    raise AssertionError(f"no reconciled row for imo={imo}")


def test_build_ship_build_year_uses_p729_over_p571(tmp_path):
    bindings = [_binding("Q1", "9187629", p729="1993-01-01T00:00:00Z", p571="1980-01-01T00:00:00Z")]
    client = _client_returning([bindings])
    out_path = tmp_path / "out.parquet"
    raw_path = tmp_path / "raw.parquet"

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)

    row = _row_for(_read(out_path), "9187629")
    assert row["build_year"] == 1993
    assert row["date_source"] == "P729"
    assert row["has_date_conflict"] is True  # 1993 vs 1980 disagree by > 1 year


def test_build_ship_build_year_drops_checksum_invalid_imo(tmp_path):
    # "1193046": a known checksum-invalid IMO (process.identity's own docstring example) --
    # (1*7+1*6+9*5+3*4+0*3+4*2) % 10 == 8, but the 7th digit is 6, so it fails validation.
    bindings = [_binding("Q1", "1193046", p729="2000-01-01T00:00:00Z")]
    client = _client_returning([bindings])
    out_path = tmp_path / "out.parquet"
    raw_path = tmp_path / "raw.parquet"

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)

    assert _read(out_path) == []
    assert _read(raw_path) != []  # the raw row still lands, for auditability


def test_build_ship_build_year_agrees_within_one_year_keeps_earliest(tmp_path):
    bindings = [
        _binding("Q1", "9187629", p729="2000-01-01T00:00:00Z"),
        _binding("Q2", "9187629", p729="2001-01-01T00:00:00Z"),  # same imo, different item
    ]
    client = _client_returning([bindings])
    out_path = tmp_path / "out.parquet"
    raw_path = tmp_path / "raw.parquet"

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)

    row = _row_for(_read(out_path), "9187629")
    assert row["build_year"] == 2000  # earliest of the two agreeing years
    assert row["n_wikidata_items"] == 2
    assert row["is_ambiguous"] is False


def test_build_ship_build_year_disagreeing_items_are_ambiguous(tmp_path):
    bindings = [
        _binding("Q1", "9187629", p729="1990-01-01T00:00:00Z"),
        _binding("Q2", "9187629", p729="2010-01-01T00:00:00Z"),  # same imo, wildly different item
    ]
    client = _client_returning([bindings])
    out_path = tmp_path / "out.parquet"
    raw_path = tmp_path / "raw.parquet"

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)

    row = _row_for(_read(out_path), "9187629")
    assert row["build_year"] is None
    assert row["is_ambiguous"] is True
    assert row["n_wikidata_items"] == 2


def test_build_ship_build_year_no_date_is_not_ambiguous(tmp_path):
    bindings = [_binding("Q1", "9187629")]  # valid imo, no p729/p571 at all
    client = _client_returning([bindings])
    out_path = tmp_path / "out.parquet"
    raw_path = tmp_path / "raw.parquet"

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)

    row = _row_for(_read(out_path), "9187629")
    assert row["build_year"] is None
    assert row["is_ambiguous"] is False


def test_build_ship_build_year_is_idempotent_unless_forced(tmp_path):
    bindings = [_binding("Q1", "9187629", p729="1993-01-01T00:00:00Z")]
    calls: list[str] = []
    client = _client_returning([bindings], calls=calls)
    out_path = tmp_path / "out.parquet"
    raw_path = tmp_path / "raw.parquet"

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)
    n_calls_after_first = len(calls)

    wd.build_ship_build_year(out_path=out_path, raw_out_path=raw_path, client=client, page_limit=200)
    assert len(calls) == n_calls_after_first, "re-running without --force must not re-fetch"
