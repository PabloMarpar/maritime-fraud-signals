"""Unit tests for ingest.sanctions. All HTTP is mocked; nothing here touches the network.

Synthetic fixtures mirror the real structures confirmed live on 2026-09-21 (see
ingest.sanctions's module docstring): OFAC's XML has reference-value-set lookups plus separate
top-level IDRegDocuments/SanctionsEntries containers joined by IdentityID/ProfileID; the EU CSV
(OpenSanctions mirror) embeds its designation date as free text in 'sanctions'; the UK CSV has a
non-header first line and repeats a vessel across multiple rows (one per address/alias/regime).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import duckdb
import httpx
import pytest

from ingest import sanctions

# ---------------------------------------------------------------------------
# OFAC fixtures
# ---------------------------------------------------------------------------

_OFAC_REFERENCE_VALUE_SETS = """
<ReferenceValueSets>
  <PartySubTypeValues>
    <PartySubType ID="1" PartyTypeID="4">Vessel</PartySubType>
  </PartySubTypeValues>
  <FeatureTypeValues>
    <FeatureType ID="3">Vessel Flag</FeatureType>
  </FeatureTypeValues>
  <IDRegDocTypeValues>
    <IDRegDocType ID="1626">Vessel Registration Identification</IDRegDocType>
  </IDRegDocTypeValues>
  <EntryEventTypeValues>
    <EntryEventType ID="1">Created</EntryEventType>
  </EntryEventTypeValues>
</ReferenceValueSets>
"""

# Reference value sets missing the "Vessel" PartySubType label -- the negative case for dynamic
# reference-ID resolution (see ingest.sanctions._resolve_ofac_reference_ids).
_OFAC_REFERENCE_VALUE_SETS_NO_VESSEL_LABEL = """
<ReferenceValueSets>
  <PartySubTypeValues>
    <PartySubType ID="1" PartyTypeID="4">Something Else</PartySubType>
  </PartySubTypeValues>
  <FeatureTypeValues>
    <FeatureType ID="3">Vessel Flag</FeatureType>
  </FeatureTypeValues>
  <IDRegDocTypeValues>
    <IDRegDocType ID="1626">Vessel Registration Identification</IDRegDocType>
  </IDRegDocTypeValues>
  <EntryEventTypeValues>
    <EntryEventType ID="1">Created</EntryEventType>
  </EntryEventTypeValues>
</ReferenceValueSets>
"""


def _ofac_name_part(text: str, script_id: str = "215") -> str:
    return (
        "<DocumentedName>"
        f'<DocumentedNamePart><NamePartValue ScriptID="{script_id}">{text}</NamePartValue></DocumentedNamePart>'
        "</DocumentedName>"
    )


def _ofac_profile(
    profile_id: str,
    identity_id: str,
    name_documented_names: str,
    flag: str | None = "Panama",
) -> str:
    feature = ""
    if flag is not None:
        feature = f"""
        <Feature FeatureTypeID="3">
          <FeatureVersion><VersionDetail>{flag}</VersionDetail></FeatureVersion>
          <IdentityReference IdentityID="{identity_id}" />
        </Feature>
        """
    return f"""
    <DistinctParty>
      <Profile ID="{profile_id}" PartySubTypeID="1">
        <Identity ID="{identity_id}" Primary="true">
          <Alias Primary="true">
            {name_documented_names}
          </Alias>
        </Identity>
        {feature}
      </Profile>
    </DistinctParty>
    """


def _ofac_idreg(identity_id: str, imo: str) -> str:
    return f"""
    <IDRegDocument IDRegDocTypeID="1626" IdentityID="{identity_id}">
      <IDRegistrationNo>IMO {imo}</IDRegistrationNo>
    </IDRegDocument>
    """


def _ofac_entry_event(year: str, month: str | None = None, day: str | None = None) -> str:
    month_xml = f"<Month>{month}</Month>" if month is not None else ""
    day_xml = f"<Day>{day}</Day>" if day is not None else ""
    return f"""
    <EntryEvent EntryEventTypeID="1">
      <Date><Year>{year}</Year>{month_xml}{day_xml}</Date>
    </EntryEvent>
    """


def _ofac_sanctions_entry(profile_id: str, events_xml: str, program: str = "CUBA") -> str:
    return f"""
    <SanctionsEntry ProfileID="{profile_id}">
      {events_xml}
      <SanctionsMeasure><Comment>{program}</Comment></SanctionsMeasure>
    </SanctionsEntry>
    """


def _ofac_document(
    idreg_xml: str = "",
    profiles_xml: str = "",
    entries_xml: str = "",
    reference_value_sets: str = _OFAC_REFERENCE_VALUE_SETS,
) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <Sanctions>
      {reference_value_sets}
      <Locations></Locations>
      <IDRegDocuments>{idreg_xml}</IDRegDocuments>
      <DistinctParties>{profiles_xml}</DistinctParties>
      <ProfileRelationships></ProfileRelationships>
      <SanctionsEntries>{entries_xml}</SanctionsEntries>
      <SanctionsEntryLinks></SanctionsEntryLinks>
    </Sanctions>
    """


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_ofac_xml_normal_vessel(tmp_path):
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "9187629"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("MAR AZUL"), flag="Cuba"),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("1989", "1", "5"), program="CUBA"),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert len(result) == 1
    v = result[0]
    assert v.source == "ofac"
    assert v.source_id == "p-1"
    assert v.vessel_name == "MAR AZUL"
    assert v.imo == "9187629"
    assert v.flag == "Cuba"
    assert v.program == "CUBA"
    assert v.designation_date == date(1989, 1, 5)
    assert v.designation_date_precision == "day"


def test_parse_ofac_xml_vessel_missing_imo(tmp_path):
    """Negative case: a vessel with no matching IDRegDocument at all lands imo=None, not an error."""
    xml = _ofac_document(
        idreg_xml="",  # no IDRegDocument for id-1
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("NO IMO VESSEL")),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2001", "6", "1")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert len(result) == 1
    assert result[0].imo is None


def test_parse_ofac_xml_multiple_designation_events_takes_earliest(tmp_path):
    events = _ofac_entry_event("2010", "6", "16") + _ofac_entry_event("1995", "5", "23")
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("MULTI EVENT VESSEL")),
        entries_xml=_ofac_sanctions_entry("p-1", events),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].designation_date == date(1995, 5, 23)
    assert result[0].designation_date_precision == "day"


def test_parse_ofac_xml_multiple_sanctions_entries_takes_earliest(tmp_path):
    """A profile can carry more than one <SanctionsEntry> element, not just >1 EntryEvent inside one."""
    entries = _ofac_sanctions_entry("p-1", _ofac_entry_event("2015", "3", "2")) + _ofac_sanctions_entry(
        "p-1", _ofac_entry_event("2008", "9", "9")
    )
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("MULTI ENTRY VESSEL")),
        entries_xml=entries,
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].designation_date == date(2008, 9, 9)


def test_parse_ofac_xml_partial_date_year_month_only(tmp_path):
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("PARTIAL DATE VESSEL")),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2005", "7")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].designation_date == date(2005, 7, 1)
    assert result[0].designation_date_precision == "month"


def test_parse_ofac_xml_partial_date_year_only(tmp_path):
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("YEAR ONLY VESSEL")),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("1999")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].designation_date == date(1999, 1, 1)
    assert result[0].designation_date_precision == "year"


def test_parse_ofac_xml_prefers_latin_script_name(tmp_path):
    """Real quirk (profile 34940, confirmed 2026-09-21): a primary Alias can carry both a Latin
    and a transliterated DocumentedName. Latin (ScriptID 215) must win."""
    names = _ofac_name_part("Baltic Leader", script_id="215") + _ofac_name_part(
        "Cyrillic Transliteration", script_id="220"
    )
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", names),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2022", "3", "24")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].vessel_name == "Baltic Leader"


def test_parse_ofac_xml_falls_back_to_first_name_when_no_latin_script(tmp_path):
    """Negative case for the Latin-preference rule: if no ScriptID=215 part exists, the first
    (only) name part found is used rather than crashing."""
    names = _ofac_name_part("Only Cyrillic Name", script_id="220")
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", names),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2022", "3", "24")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].vessel_name == "Only Cyrillic Name"


def test_parse_ofac_xml_vessel_with_no_flag_feature(tmp_path):
    """Negative case: a vessel profile with no Vessel Flag Feature at all lands flag=None."""
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("NO FLAG VESSEL"), flag=None),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2022", "3", "24")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert result[0].flag is None


def test_parse_ofac_xml_ignores_non_vessel_profiles(tmp_path):
    non_vessel_profile = """
    <DistinctParty>
      <Profile ID="p-2" PartySubTypeID="99">
        <Identity ID="id-2" Primary="true">
          <Alias Primary="true">
            <DocumentedName><DocumentedNamePart><NamePartValue>A PERSON</NamePartValue></DocumentedNamePart></DocumentedName>
          </Alias>
        </Identity>
      </Profile>
    </DistinctParty>
    """
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("REAL VESSEL")) + non_vessel_profile,
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2022", "3", "24"))
        + _ofac_sanctions_entry("p-2", _ofac_entry_event("2020", "1", "1")),
    )
    path = _write(tmp_path, "sdn.xml", xml)

    result = sanctions.parse_ofac_xml(path)

    assert [v.source_id for v in result] == ["p-1"]


def test_parse_ofac_xml_raises_on_unresolvable_reference_ids(tmp_path):
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("VESSEL")),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("2022", "3", "24")),
        reference_value_sets=_OFAC_REFERENCE_VALUE_SETS_NO_VESSEL_LABEL,
    )
    path = _write(tmp_path, "sdn.xml", xml)

    with pytest.raises(sanctions.OFACSchemaError, match="Vessel"):
        sanctions.parse_ofac_xml(path)


def test_parse_ofac_xml_raises_when_vessel_has_no_designation(tmp_path):
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "1234567"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("UNDESIGNATED VESSEL")),
        entries_xml="",  # no SanctionsEntry at all for p-1
    )
    path = _write(tmp_path, "sdn.xml", xml)

    with pytest.raises(sanctions.OFACSchemaError, match="no matching"):
        sanctions.parse_ofac_xml(path)


def test_parse_ofac_xml_raises_when_no_primary_identity(tmp_path):
    bad_profile = """
    <DistinctParty>
      <Profile ID="p-1" PartySubTypeID="1">
        <Identity ID="id-1" Primary="false">
          <Alias Primary="true">
            <DocumentedName><DocumentedNamePart><NamePartValue>X</NamePartValue></DocumentedNamePart></DocumentedName>
          </Alias>
        </Identity>
      </Profile>
    </DistinctParty>
    """
    xml = _ofac_document(profiles_xml=bad_profile)
    path = _write(tmp_path, "sdn.xml", xml)

    with pytest.raises(sanctions.OFACSchemaError, match="primary"):
        sanctions.parse_ofac_xml(path)


def test_fetch_ofac_downloads_and_parses(tmp_path):
    xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "9187629"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("MAR AZUL")),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("1989", "1", "5")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml.encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sanctions.fetch_ofac(client=client, tmp_dir=tmp_path)

    assert len(result) == 1
    assert result[0].imo == "9187629"


# ---------------------------------------------------------------------------
# EU fixtures
# ---------------------------------------------------------------------------

_EU_COLUMNS = [
    "id",
    "schema",
    "name",
    "aliases",
    "birth_date",
    "countries",
    "addresses",
    "identifiers",
    "sanctions",
    "phones",
    "emails",
    "program_ids",
    "dataset",
    "first_seen",
    "last_seen",
    "last_change",
]


def _eu_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EU_COLUMNS)
    writer.writeheader()
    for row in rows:
        full = {col: "" for col in _EU_COLUMNS}
        full.update(row)
        writer.writerow(full)
    return buf.getvalue()


def test_parse_eu_csv_normal_vessel_row(tmp_path):
    text = _eu_csv(
        [
            {
                "id": "NK-1",
                "schema": "Vessel",
                "name": "New Konk",
                "countries": "kp",
                "identifiers": "IMO9036387",
                "sanctions": "PRK - 2022/2429 (OJ L318I) - 2022-12-12",
                "program_ids": "EU-PRK",
                "first_seen": "2099-01-01T00:00:00",  # deliberately far-future: must be ignored
            }
        ]
    )
    path = _write(tmp_path, "eu.csv", text)

    result = sanctions._parse_eu_csv(path)

    assert len(result) == 1
    v = result[0]
    assert v.source == "eu"
    assert v.source_id == "NK-1"
    assert v.vessel_name == "New Konk"
    assert v.imo == "9036387"
    assert v.flag == "kp"
    assert v.program == "EU-PRK"
    assert v.designation_date == date(2022, 12, 12)
    assert v.designation_date_precision == "day"


def test_parse_eu_csv_vessel_missing_imo(tmp_path):
    text = _eu_csv(
        [
            {
                "id": "NK-2",
                "schema": "Vessel",
                "name": "No IMO Vessel",
                "countries": "kp",
                "identifiers": "",
                "sanctions": "PRK - 2022/2429 (OJ L318I) - 2022-12-12",
                "program_ids": "EU-PRK",
            }
        ]
    )
    path = _write(tmp_path, "eu.csv", text)

    result = sanctions._parse_eu_csv(path)

    assert result[0].imo is None


def test_parse_eu_csv_multiple_sanction_dates_takes_earliest(tmp_path):
    text = _eu_csv(
        [
            {
                "id": "NK-3",
                "schema": "Vessel",
                "name": "Re-Designated Vessel",
                "countries": "kp",
                "identifiers": "IMO1111111",
                "sanctions": "PRK - 2022/2429 (OJ L318I) - 2022-12-12; PRK - 2021/111 - 2021-05-01",
                "program_ids": "EU-PRK",
            }
        ]
    )
    path = _write(tmp_path, "eu.csv", text)

    result = sanctions._parse_eu_csv(path)

    assert result[0].designation_date == date(2021, 5, 1)


def test_parse_eu_csv_skips_row_with_no_parseable_date(tmp_path):
    text = _eu_csv(
        [
            {
                "id": "NK-4",
                "schema": "Vessel",
                "name": "No Date Vessel",
                "countries": "kp",
                "identifiers": "IMO2222222",
                "sanctions": "PRK - no date here",
                "program_ids": "EU-PRK",
            },
            {
                "id": "NK-5",
                "schema": "Vessel",
                "name": "Has Date Vessel",
                "countries": "kp",
                "identifiers": "IMO3333333",
                "sanctions": "PRK - 2020/1 - 2020-01-15",
                "program_ids": "EU-PRK",
            },
        ]
    )
    path = _write(tmp_path, "eu.csv", text)

    result = sanctions._parse_eu_csv(path)

    assert [v.source_id for v in result] == ["NK-5"]


def test_parse_eu_csv_ignores_non_vessel_rows(tmp_path):
    text = _eu_csv(
        [
            {
                "id": "NK-6",
                "schema": "Person",
                "name": "A Person",
                "countries": "ru",
                "identifiers": "",
                "sanctions": "UKR - 2023/1 - 2023-01-01",
                "program_ids": "EU-UKR",
            }
        ]
    )
    path = _write(tmp_path, "eu.csv", text)

    result = sanctions._parse_eu_csv(path)

    assert result == []


def test_parse_eu_csv_raises_on_missing_columns(tmp_path):
    path = _write(tmp_path, "eu.csv", "id,schema,name\nNK-1,Vessel,X\n")

    with pytest.raises(sanctions.EUSchemaError):
        sanctions._parse_eu_csv(path)


def test_fetch_eu_downloads_and_parses(tmp_path):
    text = _eu_csv(
        [
            {
                "id": "NK-1",
                "schema": "Vessel",
                "name": "New Konk",
                "countries": "kp",
                "identifiers": "IMO9036387",
                "sanctions": "PRK - 2022/2429 - 2022-12-12",
                "program_ids": "EU-PRK",
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=text.encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sanctions.fetch_eu(client=client, tmp_dir=tmp_path)

    assert len(result) == 1
    assert result[0].imo == "9036387"


# ---------------------------------------------------------------------------
# UK fixtures
# ---------------------------------------------------------------------------

_UK_COLUMNS = [
    "Unique ID",
    "Designation Type",
    "Date Designated",
    "IMO number",
    "Current believed flag of ship",
    "Regime Name",
    "Name 6",
    "Name type",
]


def _uk_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    buf.write("Report Date: 21-Sep-2026\n")
    writer = csv.DictWriter(buf, fieldnames=_UK_COLUMNS)
    writer.writeheader()
    for row in rows:
        full = {col: "" for col in _UK_COLUMNS}
        full.update(row)
        writer.writerow(full)
    return buf.getvalue()


def test_parse_uk_csv_normal_vessel_row(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0075",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "IMO9562233",
                "Current believed flag of ship": "Comoros",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Petrel 8",
                "Name type": "Primary name",
            }
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert len(result) == 1
    v = result[0]
    assert v.source == "uk"
    assert v.source_id == "DPR0075"
    assert v.vessel_name == "Petrel 8"
    assert v.imo == "9562233"
    assert v.flag == "Comoros"
    assert v.program == "DPRK Regulations"
    assert v.designation_date == date(2017, 10, 3)
    assert v.designation_date_precision == "day"


def test_parse_uk_csv_vessel_missing_imo(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0076",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "",
                "Current believed flag of ship": "St Kitts and Nevis",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Hao Fan 6",
                "Name type": "Primary name",
            }
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert result[0].imo is None


def test_parse_uk_csv_imo_without_prefix_still_parses(tmp_path):
    """Real quirk confirmed 2026-09-21 (DPR0076, '8628597'): the 'IMO' prefix isn't always present."""
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0076",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "8628597",
                "Current believed flag of ship": "St Kitts and Nevis",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Hao Fan 6",
                "Name type": "Primary name",
            }
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert result[0].imo == "8628597"


def test_parse_uk_csv_multiple_regime_rows_takes_earliest_designation_and_matching_program(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0999",
                "Designation Type": "Ship",
                "Date Designated": "01/06/2020",
                "IMO number": "IMO1112223",
                "Current believed flag of ship": "Panama",
                "Regime Name": "Later Regime",
                "Name 6": "Multi Regime Vessel",
                "Name type": "Primary name",
            },
            {
                "Unique ID": "DPR0999",
                "Designation Type": "Ship",
                "Date Designated": "01/06/2018",
                "IMO number": "IMO1112223",
                "Current believed flag of ship": "Panama",
                "Regime Name": "Earlier Regime",
                "Name 6": "Multi Regime Vessel",
                "Name type": "Primary name",
            },
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert len(result) == 1
    assert result[0].designation_date == date(2018, 6, 1)
    assert result[0].program == "Earlier Regime"


def test_parse_uk_csv_multiple_alias_rows_prefers_primary_name(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0075",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "IMO9562233",
                "Current believed flag of ship": "Comoros",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Jal Vahini",
                "Name type": "Alias",
            },
            {
                "Unique ID": "DPR0075",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "IMO9562233",
                "Current believed flag of ship": "Comoros",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Petrel 8",
                "Name type": "Primary name",
            },
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert len(result) == 1
    assert result[0].vessel_name == "Petrel 8"


def test_parse_uk_csv_falls_back_to_first_row_name_when_none_marked_primary(tmp_path):
    """Negative case for the primary-name preference: no row is marked 'Primary name'."""
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0080",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "IMO9999999",
                "Current believed flag of ship": "Comoros",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Only Alias",
                "Name type": "Alias",
            }
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert result[0].vessel_name == "Only Alias"


def test_parse_uk_csv_ignores_non_ship_rows(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "PER001",
                "Designation Type": "Individual",
                "Date Designated": "03/10/2017",
                "IMO number": "",
                "Current believed flag of ship": "",
                "Regime Name": "DPRK Regulations",
                "Name 6": "A Person",
                "Name type": "Primary name",
            }
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    result = sanctions._parse_uk_csv(path)

    assert result == []


def test_parse_uk_csv_raises_when_first_line_is_not_report_date(tmp_path):
    path = _write(tmp_path, "uk.csv", "Unique ID,Designation Type\nDPR1,Ship\n")

    with pytest.raises(sanctions.UKSchemaError, match="Report Date"):
        sanctions._parse_uk_csv(path)


def test_parse_uk_csv_raises_on_missing_columns(tmp_path):
    path = _write(tmp_path, "uk.csv", "Report Date: 21-Sep-2026\nUnique ID,Designation Type\nDPR1,Ship\n")

    with pytest.raises(sanctions.UKSchemaError, match="columns"):
        sanctions._parse_uk_csv(path)


def test_parse_uk_csv_raises_when_ship_has_no_date_designated(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0090",
                "Designation Type": "Ship",
                "Date Designated": "",
                "IMO number": "IMO1234567",
                "Current believed flag of ship": "Panama",
                "Regime Name": "DPRK Regulations",
                "Name 6": "No Date Vessel",
                "Name type": "Primary name",
            }
        ]
    )
    path = _write(tmp_path, "uk.csv", text)

    with pytest.raises(sanctions.UKSchemaError, match="Date Designated"):
        sanctions._parse_uk_csv(path)


def test_fetch_uk_downloads_and_parses(tmp_path):
    text = _uk_csv(
        [
            {
                "Unique ID": "DPR0075",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "IMO9562233",
                "Current believed flag of ship": "Comoros",
                "Regime Name": "DPRK Regulations",
                "Name 6": "Petrel 8",
                "Name type": "Primary name",
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=text.encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    result = sanctions.fetch_uk(client=client, tmp_dir=tmp_path)

    assert len(result) == 1
    assert result[0].imo == "9562233"


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def _combined_client() -> httpx.Client:
    ofac_xml = _ofac_document(
        idreg_xml=_ofac_idreg("id-1", "9187629"),
        profiles_xml=_ofac_profile("p-1", "id-1", _ofac_name_part("OFAC VESSEL")),
        entries_xml=_ofac_sanctions_entry("p-1", _ofac_entry_event("1989", "1", "5")),
    )
    eu_csv = _eu_csv(
        [
            {
                "id": "NK-1",
                "schema": "Vessel",
                "name": "EU Vessel",
                "countries": "kp",
                "identifiers": "IMO9036387",
                "sanctions": "PRK - 2022/2429 - 2022-12-12",
                "program_ids": "EU-PRK",
            }
        ]
    )
    uk_csv = _uk_csv(
        [
            {
                "Unique ID": "DPR0075",
                "Designation Type": "Ship",
                "Date Designated": "03/10/2017",
                "IMO number": "IMO9562233",
                "Current believed flag of ship": "Comoros",
                "Regime Name": "DPRK Regulations",
                "Name 6": "UK Vessel",
                "Name type": "Primary name",
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == sanctions.OFAC_URL:
            return httpx.Response(200, content=ofac_xml.encode("utf-8"))
        if url == sanctions.EU_URL:
            return httpx.Response(200, content=eu_csv.encode("utf-8"))
        if url == sanctions.UK_URL:
            return httpx.Response(200, content=uk_csv.encode("utf-8"))
        raise AssertionError(f"unexpected URL requested: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_build_sanctions_writes_combined_parquet_and_is_idempotent(tmp_path):
    out_path = tmp_path / "sanctions.parquet"
    client = _combined_client()

    result = sanctions.build_sanctions(out_path=out_path, client=client, tmp_dir=tmp_path)

    assert result == out_path
    assert out_path.exists()
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT source, source_id, vessel_name, imo FROM '{out_path.as_posix()}' ORDER BY source"
        ).fetchall()
    finally:
        con.close()
    assert rows == [
        ("eu", "NK-1", "EU Vessel", "9036387"),
        ("ofac", "p-1", "OFAC VESSEL", "9187629"),
        ("uk", "DPR0075", "UK Vessel", "9562233"),
    ]

    calls: list[str] = []
    original_transport = client._transport

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return original_transport.handle_request(request)

    client._transport = httpx.MockTransport(counting_handler)
    sanctions.build_sanctions(out_path=out_path, client=client, tmp_dir=tmp_path)
    assert calls == [], "re-running without --force must not re-fetch"


def test_build_sanctions_force_refetches(tmp_path):
    out_path = tmp_path / "sanctions.parquet"
    client = _combined_client()

    sanctions.build_sanctions(out_path=out_path, client=client, tmp_dir=tmp_path)

    calls: list[str] = []
    original_transport = client._transport

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return original_transport.handle_request(request)

    client._transport = httpx.MockTransport(counting_handler)
    sanctions.build_sanctions(out_path=out_path, client=client, tmp_dir=tmp_path, force=True)

    assert len(calls) == 3, "force=True must re-fetch all three sources"
