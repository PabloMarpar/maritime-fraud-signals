"""Unit tests for ingest.sanctions_v2. All HTTP is mocked; nothing here touches the network or
data/. Synthetic fixtures mirror the real structures confirmed live 2026-09-29 (see
ingest.sanctions_v2's module docstring): the DMA CSV is semicolon-delimited with a trailing-space
IMO column and unrelated DPRK rows mixed in; the Canada SEMA XML is a flat <record> list where a
non-ship record simply has no ShipIMONumber element; the NZ register's 'Ships' sheet has a real
per-row 'Date of Sanction' and a 'Record Deleted Flag'.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import duckdb
import httpx
import openpyxl
import pytest

from ingest import sanctions_v2 as sv2

# Real IMO numbers (verified check digit), same constants tests/test_identity.py and
# tests/test_sanctions_match.py use.
VALID_IMO_A = "9074729"
VALID_IMO_B = "9264386"
VALID_IMO_C = "9806847"
VALID_IMO_D = "2000004"
INVALID_IMO = "1234568"  # 7 digits, checksum fails -- same fixture test_sanctions_match.py uses


def _write(tmp_path: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


# ---------------------------------------------------------------------------
# EU vessels (DMA)
# ---------------------------------------------------------------------------

_DMA_HEADER = "Vessel name;IMO number ;Date of application;Subject to;;;;;;;;;;;;;;;\n"


def _dma_row(name: str, imo: str, applied: str, subject: str) -> str:
    return f"{name};{imo};{applied};{subject};;;;;;;;;;;;;;;\n"


def _dma_csv(rows: list[str]) -> str:
    return _DMA_HEADER + "".join(rows)


def test_parse_eu_vessels_csv_normal_row(tmp_path):
    text = _dma_csv(
        [_dma_row("Angara", VALID_IMO_A, "25.06.2024", "Article 3s (Council Regulation 833/2014)")]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, counts = sv2._parse_eu_vessels_csv(path)

    assert len(result) == 1
    v = result[0]
    assert v.source == "eu_vessels"
    assert v.vessel_name == "Angara"
    assert v.imo == VALID_IMO_A
    assert v.program == "Article 3s (Council Regulation 833/2014)"
    assert v.designation_date == date(2024, 6, 25)
    assert v.designation_date_precision == "day"
    assert counts == {"total": 1, "no_date": 0, "invalid_imo": 0, "after_cap": 0, "kept": 1}


def test_parse_eu_vessels_csv_filters_non_mare_subject(tmp_path):
    """Negative case: the same DMA file also carries unrelated DPRK ship designations
    (Council Regulation 2017/1509) -- must not be landed as an 'eu_vessels' EU-MARE record."""
    text = _dma_csv(
        [
            _dma_row("Angara", VALID_IMO_A, "25.06.2024", "Article 3s (Council Regulation 833/2014)"),
            _dma_row("DPRK Ship", VALID_IMO_B, "01.01.2018", "Port entry ban (Council Regulation 2017/1509)"),
        ]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, counts = sv2._parse_eu_vessels_csv(path)

    assert [v.imo for v in result] == [VALID_IMO_A]
    assert counts["total"] == 1


def test_parse_eu_vessels_csv_multiple_dates_takes_earliest(tmp_path):
    """A vessel re-listed under a later package keeps its earliest designation date and that
    row's own program text (3 of 674 real rows did on 2026-09-29)."""
    text = _dma_csv(
        [
            _dma_row("Re-listed", VALID_IMO_A, "24.07.2026", "Article 3s (Council Regulation 833/2014)"),
            _dma_row("Re-listed", VALID_IMO_A, "25.06.2024", "Article 3s (Council Regulation 833/2014)"),
        ]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, _ = sv2._parse_eu_vessels_csv(path)

    assert len(result) == 1
    assert result[0].designation_date == date(2024, 6, 25)


def test_parse_eu_vessels_csv_excludes_invalid_imo(tmp_path):
    text = _dma_csv(
        [_dma_row("Bad IMO", INVALID_IMO, "25.06.2024", "Article 3s (Council Regulation 833/2014)")]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, counts = sv2._parse_eu_vessels_csv(path)

    assert result == []
    assert counts["invalid_imo"] == 1
    assert counts["kept"] == 0


def test_parse_eu_vessels_csv_excludes_after_cap(tmp_path):
    text = _dma_csv(
        [_dma_row("Too New", VALID_IMO_A, "22.09.2026", "Article 3s (Council Regulation 833/2014)")]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, counts = sv2._parse_eu_vessels_csv(path)

    assert result == []
    assert counts["after_cap"] == 1


def test_parse_eu_vessels_csv_on_cap_date_is_kept(tmp_path):
    """The cap excludes strictly-after 2026-09-21, not on-or-after (v1's own real fetch date)."""
    text = _dma_csv(
        [_dma_row("On Cap", VALID_IMO_A, "21.09.2026", "Article 3s (Council Regulation 833/2014)")]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, counts = sv2._parse_eu_vessels_csv(path)

    assert len(result) == 1
    assert counts["kept"] == 1


def test_parse_eu_vessels_csv_excludes_no_date(tmp_path):
    text = _dma_csv(
        [_dma_row("No Date", VALID_IMO_A, "", "Article 3s (Council Regulation 833/2014)")]
    )
    path = _write(tmp_path, "dma.csv", text, encoding="utf-8-sig")

    result, counts = sv2._parse_eu_vessels_csv(path)

    assert result == []
    assert counts["no_date"] == 1


def test_parse_eu_vessels_csv_raises_on_missing_columns(tmp_path):
    path = _write(tmp_path, "dma.csv", "Vessel name;Date\nX;2024\n", encoding="utf-8-sig")

    with pytest.raises(sv2.EUVesselsSchemaError):
        sv2._parse_eu_vessels_csv(path)


def test_discover_eu_vessels_csv_url_finds_absolute_link():
    html = '<a href="https://www.dma.dk/Media/123/List240726.csv">Download</a>'
    url = sv2._discover_eu_vessels_csv_url(html, sv2.EU_VESSELS_PAGE_URL)
    assert url == "https://www.dma.dk/Media/123/List240726.csv"


def test_discover_eu_vessels_csv_url_resolves_relative_link():
    html = '<a href="/Media/123/List240726.csv">Download</a>'
    url = sv2._discover_eu_vessels_csv_url(html, sv2.EU_VESSELS_PAGE_URL)
    assert url == "https://www.dma.dk/Media/123/List240726.csv"


def test_discover_eu_vessels_csv_url_raises_when_no_csv_link():
    with pytest.raises(sv2.EUVesselsSchemaError):
        sv2._discover_eu_vessels_csv_url("<a href='/Media/123/List.pdf'>PDF only</a>", "http://x")


def test_fetch_eu_vessels_downloads_and_parses(tmp_path):
    page_html = '<html><a href="/Media/1/List.csv">CSV</a></html>'
    csv_text = _dma_csv(
        [_dma_row("Angara", VALID_IMO_A, "25.06.2024", "Article 3s (Council Regulation 833/2014)")]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == sv2.EU_VESSELS_PAGE_URL:
            return httpx.Response(200, text=page_html)
        if str(request.url) == "https://www.dma.dk/Media/1/List.csv":
            return httpx.Response(200, content=csv_text.encode("utf-8-sig"))
        raise AssertionError(f"unexpected URL requested: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result, counts = sv2.fetch_eu_vessels(client=client, tmp_dir=tmp_path)

    assert len(result) == 1
    assert result[0].imo == VALID_IMO_A
    assert counts["kept"] == 1


# ---------------------------------------------------------------------------
# Canada SEMA
# ---------------------------------------------------------------------------


def _ca_record(
    name: str | None,
    imo: str | None,
    listing_date: str | None,
    schedule: str = "1.1",
    item: str = "1",
    country: str = "Russia / Russie",
) -> str:
    name_xml = f"<EntityOrShip-EntiteOuNavire>{name}</EntityOrShip-EntiteOuNavire>" if name else ""
    imo_xml = (
        f"<ShipIMONumber-NumeroOMIDuNavire>{imo}</ShipIMONumber-NumeroOMIDuNavire>" if imo else ""
    )
    date_xml = (
        f"<DateOfListing-DateDinscription>{listing_date}</DateOfListing-DateDinscription>"
        if listing_date
        else ""
    )
    return f"""
    <record>
      <Country-Pays>{country}</Country-Pays>
      {name_xml}
      <TitleOrShipType-TitreOuTypeDeNavire>General Cargo</TitleOrShipType-TitreOuTypeDeNavire>
      {imo_xml}
      <Schedule-Annexe>{schedule}</Schedule-Annexe>
      <Item-NumeroDarticle>{item}</Item-NumeroDarticle>
      {date_xml}
    </record>
    """


def _ca_document(records_xml: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><data-set>{records_xml}</data-set>'


def test_parse_ca_sema_xml_normal_ship_record(tmp_path):
    xml = _ca_document(_ca_record("Balitiyskiy III", VALID_IMO_A, "2025-02-21"))
    path = _write(tmp_path, "sema.xml", xml)

    result, counts = sv2.parse_ca_sema_xml(path)

    assert len(result) == 1
    v = result[0]
    assert v.source == "ca"
    assert v.vessel_name == "Balitiyskiy III"
    assert v.imo == VALID_IMO_A
    assert v.program == "SEMA Schedule 1.1"
    assert v.designation_date == date(2025, 2, 21)
    assert counts["kept"] == 1


def test_parse_ca_sema_xml_ignores_non_ship_records(tmp_path):
    """Negative case: an individual/entity record has no ShipIMONumber element at all."""
    person = """
    <record>
      <Country-Pays>Belarus / Belarus</Country-Pays>
      <LastName-NomDeFamille>Atabekov</LastName-NomDeFamille>
      <Schedule-Annexe>1, Part 1</Schedule-Annexe>
      <Item-NumeroDarticle>1</Item-NumeroDarticle>
      <DateOfListing-DateDinscription>2020-09-28</DateOfListing-DateDinscription>
    </record>
    """
    xml = _ca_document(person + _ca_record("Musa Jalil", VALID_IMO_B, "2025-02-21"))
    path = _write(tmp_path, "sema.xml", xml)

    result, _ = sv2.parse_ca_sema_xml(path)

    assert [v.imo for v in result] == [VALID_IMO_B]


def test_parse_ca_sema_xml_excludes_invalid_imo(tmp_path):
    """Real quirk confirmed 2026-09-29: some Iran-individual records carry non-numeric text in
    the ShipIMONumber field, a GAC data-entry bug -- caught by the same checksum gate."""
    xml = _ca_document(_ca_record(None, "Not An IMO Number", "2026-09-22", country="Iran"))
    path = _write(tmp_path, "sema.xml", xml)

    result, counts = sv2.parse_ca_sema_xml(path)

    assert result == []
    assert counts["invalid_imo"] == 1


def test_parse_ca_sema_xml_excludes_after_cap(tmp_path):
    xml = _ca_document(_ca_record("Too New", VALID_IMO_A, "2026-09-22"))
    path = _write(tmp_path, "sema.xml", xml)

    result, counts = sv2.parse_ca_sema_xml(path)

    assert result == []
    assert counts["after_cap"] == 1


def test_parse_ca_sema_xml_excludes_no_date(tmp_path):
    xml = _ca_document(_ca_record("No Date", VALID_IMO_A, None))
    path = _write(tmp_path, "sema.xml", xml)

    result, counts = sv2.parse_ca_sema_xml(path)

    assert result == []
    assert counts["no_date"] == 1


def test_parse_ca_sema_xml_multiple_records_same_imo_takes_earliest(tmp_path):
    xml = _ca_document(
        _ca_record("Dup A", VALID_IMO_A, "2025-06-13", item="2")
        + _ca_record("Dup B", VALID_IMO_A, "2025-02-21", item="1")
    )
    path = _write(tmp_path, "sema.xml", xml)

    result, _ = sv2.parse_ca_sema_xml(path)

    assert len(result) == 1
    assert result[0].designation_date == date(2025, 2, 21)
    assert result[0].vessel_name == "Dup B"


def test_parse_ca_sema_xml_raises_on_malformed_xml(tmp_path):
    path = _write(tmp_path, "sema.xml", "<data-set><record>not closed")

    with pytest.raises(sv2.CASchemaError):
        sv2.parse_ca_sema_xml(path)


def test_fetch_ca_downloads_and_parses(tmp_path):
    xml = _ca_document(_ca_record("Balitiyskiy III", VALID_IMO_A, "2025-02-21"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml.encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result, counts = sv2.fetch_ca(client=client, tmp_dir=tmp_path)

    assert len(result) == 1
    assert result[0].imo == VALID_IMO_A
    assert counts["kept"] == 1


# ---------------------------------------------------------------------------
# New Zealand MFAT Russia Sanctions Register
# ---------------------------------------------------------------------------

_NZ_COLUMNS = [
    "Type",
    "Unique Identifier",
    "IMO Number",
    "Name of Ship as of Date of Sanction",
    "Date of Sanction",
    "Alias/Alternate Names",
    "Date of Additional Sanction",
    "Sanction Status",
    "Travel Ban",
    "Asset Freeze",
    "Aircraft Ban",
    "Ship Ban",
    "Service Prohibition",
    "Dealing with Securities",
    "Date Record Deleted",
    "Record Deleted Flag",
    "Reason for Deletion",
]


def _nz_workbook(rows: list[dict], sheet_name: str = "Ships") -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(sheet_name)
    ws.append(_NZ_COLUMNS)
    for row in rows:
        ws.append([row.get(col) for col in _NZ_COLUMNS])
    return wb


def _write_nz_xlsx(tmp_path: Path, rows: list[dict], sheet_name: str = "Ships") -> Path:
    path = tmp_path / "nz.xlsx"
    _nz_workbook(rows, sheet_name=sheet_name).save(path)
    return path


def test_parse_nz_register_xlsx_normal_row(tmp_path):
    path = _write_nz_xlsx(
        tmp_path,
        [
            {
                "Type": "Ship",
                "Unique Identifier": "SHP-1",
                "IMO Number": int(VALID_IMO_A),
                "Name of Ship as of Date of Sanction": "Andaman Skies",
                "Date of Sanction": date(2025, 6, 19),
                "Ship Ban": "Yes",
            }
        ],
    )

    result, counts = sv2.parse_nz_register_xlsx(path)

    assert len(result) == 1
    v = result[0]
    assert v.source == "nz"
    assert v.source_id == "SHP-1"
    assert v.vessel_name == "Andaman Skies"
    assert v.imo == VALID_IMO_A
    assert v.designation_date == date(2025, 6, 19)
    assert counts["kept"] == 1
    assert counts["deleted"] == 0


def test_parse_nz_register_xlsx_excludes_deleted_record(tmp_path):
    path = _write_nz_xlsx(
        tmp_path,
        [
            {
                "Unique Identifier": "SHP-2",
                "IMO Number": int(VALID_IMO_A),
                "Name of Ship as of Date of Sanction": "Deleted Ship",
                "Date of Sanction": date(2025, 6, 19),
                "Record Deleted Flag": "Yes",
            }
        ],
    )

    result, counts = sv2.parse_nz_register_xlsx(path)

    assert result == []
    assert counts["deleted"] == 1


def test_parse_nz_register_xlsx_excludes_invalid_imo(tmp_path):
    path = _write_nz_xlsx(
        tmp_path,
        [
            {
                "Unique Identifier": "SHP-3",
                "IMO Number": int(INVALID_IMO),
                "Name of Ship as of Date of Sanction": "Bad IMO",
                "Date of Sanction": date(2025, 6, 19),
            }
        ],
    )

    result, counts = sv2.parse_nz_register_xlsx(path)

    assert result == []
    assert counts["invalid_imo"] == 1


def test_parse_nz_register_xlsx_excludes_after_cap(tmp_path):
    path = _write_nz_xlsx(
        tmp_path,
        [
            {
                "Unique Identifier": "SHP-4",
                "IMO Number": int(VALID_IMO_A),
                "Name of Ship as of Date of Sanction": "Too New",
                "Date of Sanction": date(2026, 9, 22),
            }
        ],
    )

    result, counts = sv2.parse_nz_register_xlsx(path)

    assert result == []
    assert counts["after_cap"] == 1


def test_parse_nz_register_xlsx_excludes_no_date(tmp_path):
    path = _write_nz_xlsx(
        tmp_path,
        [
            {
                "Unique Identifier": "SHP-5",
                "IMO Number": int(VALID_IMO_A),
                "Name of Ship as of Date of Sanction": "No Date",
                "Date of Sanction": None,
            }
        ],
    )

    result, counts = sv2.parse_nz_register_xlsx(path)

    assert result == []
    assert counts["no_date"] == 1


def test_parse_nz_register_xlsx_raises_on_missing_sheet(tmp_path):
    path = _write_nz_xlsx(tmp_path, [], sheet_name="Not Ships")

    with pytest.raises(sv2.NZSchemaError, match="Ships"):
        sv2.parse_nz_register_xlsx(path)


def test_parse_nz_register_xlsx_raises_on_missing_columns(tmp_path):
    path = tmp_path / "nz.xlsx"
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Ships")
    ws.append(["Type", "Unique Identifier"])
    wb.save(path)

    with pytest.raises(sv2.NZSchemaError, match="columns"):
        sv2.parse_nz_register_xlsx(path)


def test_fetch_nz_downloads_and_parses(tmp_path):
    xlsx_path = tmp_path / "src.xlsx"
    _nz_workbook(
        [
            {
                "Unique Identifier": "SHP-1",
                "IMO Number": int(VALID_IMO_A),
                "Name of Ship as of Date of Sanction": "Andaman Skies",
                "Date of Sanction": date(2025, 6, 19),
            }
        ]
    ).save(xlsx_path)
    content = xlsx_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result, counts = sv2.fetch_nz(client=client, tmp_dir=tmp_path)

    assert len(result) == 1
    assert result[0].imo == VALID_IMO_A
    assert counts["kept"] == 1


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def _write_v1(path: Path, rows: list[tuple]) -> None:
    """rows: (source, source_id, vessel_name, imo, flag, program, designation_date,
    designation_date_precision, built_at)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE sanctions (source VARCHAR, source_id VARCHAR, vessel_name VARCHAR, "
            "imo VARCHAR, flag VARCHAR, program VARCHAR, designation_date DATE, "
            "designation_date_precision VARCHAR, built_at TIMESTAMP)"
        )
        con.executemany("INSERT INTO sanctions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY sanctions TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def _empty_client() -> httpx.Client:
    """A client whose every response is empty/no-op -- for landing tests that don't care about
    each source's own content, just that v1 rows pass through unchanged and new rows land too."""
    dma_page = '<html><a href="/Media/1/List.csv">CSV</a></html>'
    dma_csv = _dma_csv(
        [_dma_row("New EU Ship", VALID_IMO_B, "25.06.2024", "Article 3s (Council Regulation 833/2014)")]
    )
    ca_xml = _ca_document(_ca_record("New CA Ship", VALID_IMO_C, "2025-02-21"))
    nz_bytes = io.BytesIO()
    _nz_workbook(
        [
            {
                "Unique Identifier": "SHP-9",
                "IMO Number": int(VALID_IMO_D),
                "Name of Ship as of Date of Sanction": "New NZ Ship",
                "Date of Sanction": date(2025, 6, 19),
            }
        ]
    ).save(nz_bytes)
    nz_content = nz_bytes.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == sv2.EU_VESSELS_PAGE_URL:
            return httpx.Response(200, text=dma_page)
        if url == "https://www.dma.dk/Media/1/List.csv":
            return httpx.Response(200, content=dma_csv.encode("utf-8-sig"))
        if url == sv2.CA_SEMA_URL:
            return httpx.Response(200, content=ca_xml.encode("utf-8"))
        if url == sv2.NZ_REGISTER_URL:
            return httpx.Response(200, content=nz_content)
        raise AssertionError(f"unexpected URL requested: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_build_sanctions_v2_passes_v1_rows_unchanged_and_adds_new_sources(tmp_path):
    v1_path = tmp_path / "sanctions.parquet"
    out_path = tmp_path / "sanctions_v2.parquet"
    _write_v1(
        v1_path,
        [
            (
                "ofac",
                "p-1",
                "OFAC VESSEL",
                VALID_IMO_A,
                "Panama",
                "CUBA",
                date(1989, 1, 5),
                "day",
                "2026-09-21 19:22:00",
            )
        ],
    )
    client = _empty_client()

    result_path, counts = sv2.build_sanctions_v2(
        v1_path=v1_path, out_path=out_path, client=client, tmp_dir=tmp_path
    )

    assert result_path == out_path
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT source, source_id, vessel_name, imo, designation_date "
            f"FROM '{out_path.as_posix()}' ORDER BY source"
        ).fetchall()
    finally:
        con.close()
    assert rows == [
        ("ca", "ca-1.1-1", "New CA Ship", VALID_IMO_C, date(2025, 2, 21)),
        ("eu_vessels", "dma-eu-9264386", "New EU Ship", VALID_IMO_B, date(2024, 6, 25)),
        ("nz", "SHP-9", "New NZ Ship", VALID_IMO_D, date(2025, 6, 19)),
        ("ofac", "p-1", "OFAC VESSEL", VALID_IMO_A, date(1989, 1, 5)),
    ]
    assert counts["eu_vessels"]["kept"] == 1
    assert counts["ca"]["kept"] == 1
    assert counts["nz"]["kept"] == 1


def test_build_sanctions_v2_requires_v1_to_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        sv2.build_sanctions_v2(
            v1_path=tmp_path / "missing.parquet", out_path=tmp_path / "out.parquet"
        )


def test_build_sanctions_v2_is_idempotent_by_default(tmp_path):
    v1_path = tmp_path / "sanctions.parquet"
    out_path = tmp_path / "sanctions_v2.parquet"
    _write_v1(
        v1_path,
        [("ofac", "p-1", "V", VALID_IMO_A, None, "P", date(2020, 1, 1), "day", "2026-09-21 00:00:00")],
    )
    client = _empty_client()
    sv2.build_sanctions_v2(v1_path=v1_path, out_path=out_path, client=client, tmp_dir=tmp_path)

    calls: list[str] = []
    original_transport = client._transport

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return original_transport.handle_request(request)

    client._transport = httpx.MockTransport(counting_handler)
    sv2.build_sanctions_v2(v1_path=v1_path, out_path=out_path, client=client, tmp_dir=tmp_path)
    assert calls == [], "re-running without --force must not re-fetch"


def test_build_sanctions_v2_force_rebuilds(tmp_path):
    v1_path = tmp_path / "sanctions.parquet"
    out_path = tmp_path / "sanctions_v2.parquet"
    _write_v1(
        v1_path,
        [("ofac", "p-1", "V", VALID_IMO_A, None, "P", date(2020, 1, 1), "day", "2026-09-21 00:00:00")],
    )
    client = _empty_client()
    sv2.build_sanctions_v2(v1_path=v1_path, out_path=out_path, client=client, tmp_dir=tmp_path)

    calls: list[str] = []
    original_transport = client._transport

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return original_transport.handle_request(request)

    client._transport = httpx.MockTransport(counting_handler)
    sv2.build_sanctions_v2(
        v1_path=v1_path, out_path=out_path, client=client, tmp_dir=tmp_path, force=True
    )
    assert len(calls) == 4, "force=True must re-fetch all three new sources (DMA page + csv, CA, NZ)"
