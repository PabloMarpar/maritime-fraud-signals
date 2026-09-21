"""Downloaders for the OFAC, EU and UK sanctions lists, normalised to one shared vessel shape
with designation dates -- the labels for Phase 3's forward-looking validation (see
``docs/DECISIONS.md``: "Forward-looking validation is the headline result").

**OFAC** (``fetch_ofac``). URL: ``https://www.treasury.gov/ofac/downloads/sanctions/1.0/
sdn_advanced.xml`` -- confirmed live 2026-09-21, 302-redirects to ``sanctionslistservice.ofac.
treas.gov``, no auth, ~127MB. Parsed with a single streaming ``xml.etree.ElementTree.iterparse``
pass (never materialised as a full DOM): the four numeric type-IDs this parser depends on
(vessel ``PartySubTypeID``, "Vessel Flag" ``FeatureTypeID``, "Vessel Registration
Identification" ``IDRegDocTypeID``, "Created" ``EntryEventTypeID``) are RESOLVED from the file's
own ``<ReferenceValueSets>`` by their human-readable labels rather than hardcoded, per this
project's convention of never trusting an undocumented third-party ID to hold forever (all four
happened to equal 1 / 3 / 1626 / 1 on 2026-09-21, confirmed live, but the code does not assume
that). A vessel's IMO lives in a SEPARATE top-level ``<IDRegDocuments>`` container (joined by
``IdentityID``), and its designation date in a separate top-level ``<SanctionsEntries>``
container (joined by ``ProfileID``) -- both containers are streamed and reduced to small lookup
dicts before ``<DistinctParties>`` is reached, since file order has ``IDRegDocuments`` before
``DistinctParties`` before ``SanctionsEntries``. A profile can carry more than one
``SanctionsEntry``, and each entry more than one "Created" ``EntryEvent`` (93 of 19,579 real
profiles do, confirmed 2026-09-21) -- the EARLIEST across all of them is taken as
``designation_date``. Partial dates (year+month only, or year only) are real per OFAC's own XSD
even though none were observed in the live 2026-09-21 snapshot; day/month default to 1 when
absent, and ``designation_date_precision`` records which case applied.

**Real quirk found while verifying, not in any spec:** a vessel's primary ``Alias`` can carry
TWO ``<DocumentedName>`` siblings -- one Latin-script, one transliterated (e.g. Cyrillic) --
confirmed on real profile 34940 ("Baltic Leader" / its Cyrillic transliteration, ``ScriptID``
215=Latin vs 220=Cyrillic). The Latin-script ``NamePartValue`` is preferred when more than one
script is present; see :data:`_OFAC_LATIN_SCRIPT_ID` and :func:`_extract_ofac_vessel`.

**EU** (``fetch_eu``). The official endpoint (``webgate.ec.europa.eu/fsd/fsf/...``) returns HTTP
403 without a registered access token -- confirmed by direct curl and a cookie/session handshake
attempt on 2026-09-21, both failed; registering for one is out of scope for this project. This
module instead uses the OpenSanctions mirror of the same official EU FSF feed: ``https://
data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv`` -- confirmed reachable
2026-09-21 (307-redirects to a dated CDN URL each time; the stable ``data.opensanctions.org`` URL
is always hit fresh, never the redirect target). See ``docs/DECISIONS.md`` for the substitution
reasoning and ``docs/DATA_SOURCES.md`` for the confirmed CSV schema. Vessel rows are
``schema == "Vessel"``; IMO is extracted from the ``identifiers`` column by regex (``IMO\\d+``,
robust to the column holding a ``;``-separated mix of identifier types); designation date is
extracted from the trailing ``YYYY-MM-DD`` in the free-text ``sanctions`` column (the EARLIEST
one found, if several) -- ``first_seen`` is deliberately never used for this, since that is
OpenSanctions' own ingestion timestamp, not a designation date (using it would be exactly the
kind of temporal leak ``CLAUDE.md`` warns about). A row with no parseable date is logged and
skipped, not guessed. Real snapshot confirmed 2026-09-21: only 2 vessel rows in the entire feed.

**UK** (``fetch_uk``). URL: ``https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv`` --
confirmed reachable 2026-09-21, no auth, ~50MB, no redirect. The first line is
``Report Date: DD-Mon-YYYY``, not the header, and is skipped before ``csv.DictReader`` sees the
real 58-column header. A vessel row is ``Designation Type == "Ship"`` (NOT ``Type of entity``,
which is empty for every ship row -- confirmed on real data). A single vessel (``Unique ID``)
spans multiple rows: one per address line (confirmed real) AND, per the task spec, potentially
one per sanctions regime, each with its own ``Date Designated`` -- rows are grouped by
``Unique ID`` and the EARLIEST ``Date Designated`` across the group is taken, with ``program``
tied to that specific earliest row's ``Regime Name``, not an arbitrary one. **Real quirk found
while verifying, not in the task spec:** the vessel NAME also varies row-to-row within a group --
each row is one ``Name 6`` value (``Name type`` is ``"Primary name"`` for exactly one row per
group, ``"Alias"`` for the rest, confirmed on real data, e.g. Unique ID ``DPR0075``: primary name
"Petrel 8" plus three aliases). ``vessel_name`` uses the ``"Primary name"`` row, falling back to
the group's first row if none is marked primary. **Second quirk:** ``IMO number`` is usually
``IMO1234567`` (no space) but was observed WITHOUT the prefix on at least one real row
(``8628597``, Unique ID DPR0076) -- the ``IMO`` prefix is stripped if present rather than
required, then the remainder is checked for digits-only exactly like the other two sources.

**Landing.** All three sources normalise to :class:`SanctionedVessel` and land as one combined
table, ``data/reference/sanctions.parquet``, with a ``source`` column distinguishing origin --
same convention as ``ingest/gfw.py``/``ingest/landmask.py``. No separate per-source Parquet file
is written: ``WHERE source = 'ofac'`` (etc.) on the combined table is cheap and sufficient for
debugging one source in isolation, so a second set of files was judged not worth the extra
moving part.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import httpx

logger = logging.getLogger(__name__)

OFAC_URL = "https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml"
EU_URL = "https://data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv"
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"

REFERENCE_ROOT = Path("data/reference")
SANCTIONS_PATH = REFERENCE_ROOT / "sanctions.parquet"

# Confirmed 2026-09-21: OFAC's primary Alias can carry a Latin-script DocumentedName alongside a
# transliterated one (e.g. Cyrillic); Latin is preferred when both are present. See module
# docstring's OFAC section.
_OFAC_LATIN_SCRIPT_ID = "215"

_EU_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EU_IMO_RE = re.compile(r"IMO(\d+)")

_EU_REQUIRED_COLUMNS = {
    "id",
    "schema",
    "name",
    "countries",
    "identifiers",
    "sanctions",
    "program_ids",
}
_UK_REQUIRED_COLUMNS = {
    "Unique ID",
    "Designation Type",
    "Date Designated",
    "IMO number",
    "Current believed flag of ship",
    "Regime Name",
    "Name 6",
    "Name type",
}


class SanctionsSchemaError(ValueError):
    """Base class for a fetched sanctions payload/record not matching the shape confirmed against
    live data on 2026-09-21 (see module docstring) -- raised with the offending data/context
    attached, rather than silently mis-parsing or dropping a row.
    """


class OFACSchemaError(SanctionsSchemaError):
    """The OFAC ``sdn_advanced.xml`` structure does not match what was confirmed on 2026-09-21."""


class EUSchemaError(SanctionsSchemaError):
    """The EU FSF (OpenSanctions mirror) CSV structure does not match what was confirmed on
    2026-09-21."""


class UKSchemaError(SanctionsSchemaError):
    """The UK Sanctions List CSV structure does not match what was confirmed on 2026-09-21."""


@dataclass(frozen=True)
class SanctionedVessel:
    """One sanctioned vessel, normalised the same way regardless of source."""

    source: str  # "ofac" | "eu" | "uk"
    source_id: str  # the source's own unique id/ref, for traceability
    vessel_name: str
    imo: str | None  # bare digits, e.g. "9187629" -- None if absent
    flag: str | None
    program: str  # sanctions programme/regime name, free text
    designation_date: date  # earliest known designation date
    designation_date_precision: str  # "day" | "month" | "year"


def _stream_download(client: httpx.Client, url: str, dest_path: Path) -> None:
    """Stream url's response body to dest_path, never buffering it whole in memory."""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as fh:
            fh.writelines(response.iter_bytes())
    logger.info("Downloaded %s", url)


# ---------------------------------------------------------------------------
# OFAC
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    """Strip the XML namespace prefix ElementTree keeps on every tag, e.g. '{ns}Profile' -> 'Profile'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_local(elem: ET.Element, tag_name: str) -> Iterator[ET.Element]:
    """Yield every descendant of elem (elem itself included) whose local tag name matches, in
    document order -- namespace-agnostic, so callers never need to know/hardcode the OFAC
    namespace URI for a simple child/descendant lookup.
    """
    for e in elem.iter():
        if _local(e.tag) == tag_name:
            yield e


def _child_text(elem: ET.Element, tag_name: str) -> str | None:
    """First direct child of elem with the given local tag name, its stripped text (or None)."""
    for child in elem:
        if _local(child.tag) == tag_name:
            return (child.text or "").strip()
    return None


@dataclass(frozen=True)
class _OFACTypeIDs:
    vessel_party_subtype_id: str
    vessel_flag_feature_type_id: str
    vessel_imo_doc_type_id: str
    created_entry_event_type_id: str


def _resolve_ofac_reference_ids(context: Iterator[tuple[str, ET.Element]]) -> _OFACTypeIDs:
    """Consume ``<ReferenceValueSets>`` from an in-progress iterparse stream, resolving the four
    numeric type-IDs this parser depends on from their own human-readable labels (confirmed
    2026-09-21 to be 1 / 3 / 1626 / 1 respectively) rather than hardcoding them -- see module
    docstring. Raises :class:`OFACSchemaError` naming whichever label could not be found, rather
    than falling back to the hardcoded values silently.
    """
    vessel_party_subtype_id: str | None = None
    vessel_flag_feature_type_id: str | None = None
    vessel_imo_doc_type_id: str | None = None
    created_entry_event_type_id: str | None = None

    # Deliberately does NOT elem.clear() every non-matching element here (an earlier version did,
    # and it was a real bug: iterparse fires "end" bottom-up, so clearing every intermediate tag
    # indiscriminately destroys a row's own children before the row's *own* end event -- e.g. an
    # <Identity> end event would clear itself, wiping its <Alias> descendant, before the
    # enclosing <Profile>'s end event ever runs). ReferenceValueSets is small (a bounded
    # enumeration, not per-vessel data), so it is cleared once, in bulk, when it closes.
    for _event, elem in context:
        tag = _local(elem.tag)
        text = (elem.text or "").strip()
        if tag == "PartySubType" and elem.get("PartyTypeID") == "4" and text == "Vessel":
            vessel_party_subtype_id = elem.get("ID")
        elif tag == "FeatureType" and text == "Vessel Flag":
            vessel_flag_feature_type_id = elem.get("ID")
        elif tag == "IDRegDocType" and text == "Vessel Registration Identification":
            vessel_imo_doc_type_id = elem.get("ID")
        elif tag == "EntryEventType" and text == "Created":
            created_entry_event_type_id = elem.get("ID")
        elif tag == "ReferenceValueSets":
            elem.clear()
            break

    missing = [
        label
        for label, value in (
            ("PartySubType 'Vessel' (PartyTypeID=4)", vessel_party_subtype_id),
            ("FeatureType 'Vessel Flag'", vessel_flag_feature_type_id),
            ("IDRegDocType 'Vessel Registration Identification'", vessel_imo_doc_type_id),
            ("EntryEventType 'Created'", created_entry_event_type_id),
        )
        if value is None
    ]
    if missing:
        raise OFACSchemaError(
            "Could not resolve the following reference-value-set ID(s) from "
            f"<ReferenceValueSets>: {', '.join(missing)}. OFAC's own reference vocabulary has "
            "changed since this module was last verified (2026-09-21) -- see module docstring."
        )
    assert vessel_party_subtype_id is not None
    assert vessel_flag_feature_type_id is not None
    assert vessel_imo_doc_type_id is not None
    assert created_entry_event_type_id is not None
    return _OFACTypeIDs(
        vessel_party_subtype_id=vessel_party_subtype_id,
        vessel_flag_feature_type_id=vessel_flag_feature_type_id,
        vessel_imo_doc_type_id=vessel_imo_doc_type_id,
        created_entry_event_type_id=created_entry_event_type_id,
    )


def _consume_until(context: Iterator[tuple[str, ET.Element]], tag_name: str) -> None:
    """Advance context, clearing every element, until (and including) tag_name's closing tag."""
    for _event, elem in context:
        tag = _local(elem.tag)
        elem.clear()
        if tag == tag_name:
            return
    raise OFACSchemaError(
        f"Reached end of file without finding a closing </{tag_name}> -- the OFAC XML structure "
        "has changed since this module was last verified (2026-09-21), see module docstring."
    )


def _collect_ofac_imo_by_identity(
    context: Iterator[tuple[str, ET.Element]], imo_doc_type_id: str
) -> dict[str, str]:
    """Consume ``<IDRegDocuments>``, returning {IdentityID: bare-digit IMO} for every vessel
    registration document whose registration number parses as digits after stripping the 'IMO '
    prefix -- non-numeric or missing registration numbers are simply left out (None downstream),
    per module docstring, not an error.
    """
    imo_by_identity: dict[str, str] = {}
    for _event, elem in context:
        tag = _local(elem.tag)
        if tag == "IDRegDocument":
            if elem.get("IDRegDocTypeID") == imo_doc_type_id:
                identity_id = elem.get("IdentityID")
                reg_no = _child_text(elem, "IDRegistrationNo") or ""
                imo = reg_no.removeprefix("IMO").strip()
                if identity_id is not None and imo.isdigit() and identity_id not in imo_by_identity:
                    imo_by_identity[identity_id] = imo
            elem.clear()  # safe: IDRegDocument is a row, its own end event, no pending ancestor read
        elif tag == "IDRegDocuments":
            elem.clear()
            break
        # else: deliberately not cleared here -- see _resolve_ofac_reference_ids's comment on why
        # clearing every intermediate tag is unsafe; IDRegDocument's own children (Comment,
        # IDRegistrationNo, ...) must still exist when IDRegDocument's own end event is handled
        # above, and they are freed together by elem.clear() on IDRegDocument itself.
    return imo_by_identity


def _extract_ofac_vessel(elem: ET.Element, type_ids: _OFACTypeIDs) -> dict:
    """Extract {profile_id, identity_id, vessel_name, flag} from one vessel <Profile>.

    Raises :class:`OFACSchemaError` if the primary Identity/Alias/name is missing -- every real
    SDN vessel profile confirmed on 2026-09-21 has one; a profile without it means the assumed
    shape no longer holds, not that this vessel simply lacks a name.
    """
    profile_id = elem.get("ID")
    identity = next((e for e in _iter_local(elem, "Identity") if e.get("Primary") == "true"), None)
    if identity is None:
        raise OFACSchemaError(
            f"Vessel profile {profile_id!r} has no primary <Identity> -- see module docstring."
        )
    identity_id = identity.get("ID")

    alias = next((e for e in _iter_local(identity, "Alias") if e.get("Primary") == "true"), None)
    if alias is None:
        raise OFACSchemaError(
            f"Vessel profile {profile_id!r} (identity {identity_id!r}) has no primary <Alias> -- "
            "see module docstring."
        )

    name_parts: list[tuple[str | None, str]] = []
    for documented_name in _iter_local(alias, "DocumentedName"):
        for name_part in _iter_local(documented_name, "NamePartValue"):
            text = (name_part.text or "").strip()
            if text:
                name_parts.append((name_part.get("ScriptID"), text))
    if not name_parts:
        raise OFACSchemaError(
            f"Vessel profile {profile_id!r} (identity {identity_id!r}) has a primary Alias with "
            "no NamePartValue text -- see module docstring."
        )
    # Prefer the Latin-script name when more than one script is present -- see module docstring's
    # "real quirk" note (profile 34940, confirmed 2026-09-21).
    latin = next((text for script_id, text in name_parts if script_id == _OFAC_LATIN_SCRIPT_ID), None)
    vessel_name = latin if latin is not None else name_parts[0][1]

    flag: str | None = None
    for feature in _iter_local(elem, "Feature"):
        if feature.get("FeatureTypeID") != type_ids.vessel_flag_feature_type_id:
            continue
        if not any(
            ref.get("IdentityID") == identity_id for ref in _iter_local(feature, "IdentityReference")
        ):
            continue
        detail = next(_iter_local(feature, "VersionDetail"), None)
        detail_text = (detail.text or "").strip() if detail is not None else ""
        if detail_text:
            flag = detail_text
            break

    return {
        "profile_id": profile_id,
        "identity_id": identity_id,
        "vessel_name": vessel_name,
        "flag": flag,
    }


def _collect_ofac_vessel_profiles(
    context: Iterator[tuple[str, ET.Element]], type_ids: _OFACTypeIDs
) -> list[dict]:
    """Consume ``<DistinctParties>``, extracting one dict per vessel Profile (PartySubTypeID
    matching the resolved vessel type)."""
    vessels: list[dict] = []
    for _event, elem in context:
        tag = _local(elem.tag)
        if tag == "Profile":
            # Extract BEFORE clearing -- elem.clear() here recursively frees this Profile's whole
            # subtree (Identity, Alias, Feature, ...), which is exactly what we want once (and
            # only once) _extract_ofac_vessel has read everything it needs from it.
            if elem.get("PartySubTypeID") == type_ids.vessel_party_subtype_id:
                vessels.append(_extract_ofac_vessel(elem, type_ids))
            elem.clear()
        elif tag == "DistinctParties":
            elem.clear()
            break
        # else: not cleared -- see _resolve_ofac_reference_ids's comment. In particular, a
        # Profile's own descendants (Identity, Alias, DocumentedName, Feature, ...) each fire
        # their own "end" event here before Profile's; clearing them individually would wipe the
        # data _extract_ofac_vessel still needs to read off the still-open Profile element.
    return vessels


def _extract_ofac_earliest_designation(
    entry: ET.Element, created_event_type_id: str
) -> tuple[date, str, str] | None:
    """From one <SanctionsEntry>, return (earliest Created designation date, precision, program),
    or None if it has no "Created" EntryEvent at all (some non-Created entry types exist and are
    not designation events). Raises :class:`OFACSchemaError` if a "Created" event has no
    parseable Year -- day/month may legitimately be absent (partial dates), Year may not.
    """
    profile_id = entry.get("ProfileID")
    earliest: tuple[date, str] | None = None
    for event in _iter_local(entry, "EntryEvent"):
        if event.get("EntryEventTypeID") != created_event_type_id:
            continue
        date_elem = next(_iter_local(event, "Date"), None)
        if date_elem is None:
            raise OFACSchemaError(
                f"SanctionsEntry for profile {profile_id!r} has a 'Created' EntryEvent with no "
                "<Date> -- see module docstring."
            )
        year_text = _child_text(date_elem, "Year")
        month_text = _child_text(date_elem, "Month")
        day_text = _child_text(date_elem, "Day")
        if not year_text or not year_text.isdigit():
            raise OFACSchemaError(
                f"SanctionsEntry for profile {profile_id!r} has a 'Created' EntryEvent with no "
                f"valid Year (got {year_text!r}) -- see module docstring."
            )
        year = int(year_text)
        # Partial dates are real per OFAC's own XSD (none observed live on 2026-09-21, but the
        # task spec calls for handling them): missing/zero Month/Day fall back to 1, and
        # precision records which case applied -- see module docstring.
        if month_text and month_text.isdigit() and int(month_text) >= 1:
            month = int(month_text)
            if day_text and day_text.isdigit() and int(day_text) >= 1:
                day = int(day_text)
                precision = "day"
            else:
                day = 1
                precision = "month"
        else:
            month = 1
            day = 1
            precision = "year"
        candidate = (date(year, month, day), precision)
        if earliest is None or candidate[0] < earliest[0]:
            earliest = candidate

    if earliest is None:
        return None

    program = "UNKNOWN"
    for measure in _iter_local(entry, "SanctionsMeasure"):
        comment = _child_text(measure, "Comment")
        if comment:
            program = comment
            break

    designation_date, precision = earliest
    return designation_date, precision, program


def _collect_ofac_designations(
    context: Iterator[tuple[str, ET.Element]], created_event_type_id: str
) -> dict[str, tuple[date, str, str]]:
    """Consume ``<SanctionsEntries>``, returning {ProfileID: (earliest designation date,
    precision, program)} -- a profile can carry more than one <SanctionsEntry> (93 of 19,579 real
    profiles did on 2026-09-21), so the earliest across ALL of a profile's entries/events wins."""
    designations: dict[str, tuple[date, str, str]] = {}
    for _event, elem in context:
        tag = _local(elem.tag)
        if tag == "SanctionsEntry":
            profile_id = elem.get("ProfileID")
            extracted = _extract_ofac_earliest_designation(elem, created_event_type_id)
            if profile_id is not None and extracted is not None:
                existing = designations.get(profile_id)
                if existing is None or extracted[0] < existing[0]:
                    designations[profile_id] = extracted
            elem.clear()
        elif tag == "SanctionsEntries":
            elem.clear()
            break
        # else: not cleared -- see _resolve_ofac_reference_ids's comment; a SanctionsEntry's own
        # EntryEvent/Date/SanctionsMeasure/Comment children must survive until SanctionsEntry's
        # own end event is handled above.
    return designations


def parse_ofac_xml(path: Path) -> list[SanctionedVessel]:
    """Stream-parse one OFAC ``sdn_advanced.xml`` file into normalised :class:`SanctionedVessel`
    records. A single ``iterparse`` pass over the file (never a full in-memory DOM): reference-ID
    resolution, then IMO lookup, then vessel profiles, then designation dates, in that order,
    matching the file's own top-level section order -- see module docstring.

    Raises :class:`OFACSchemaError` if a vessel profile has no matching designation event at all;
    every real SDN profile is expected to have one (it is what makes it a listing).
    """
    context = ET.iterparse(str(path), events=("end",))
    try:
        type_ids = _resolve_ofac_reference_ids(context)
        _consume_until(context, "Locations")
        imo_by_identity = _collect_ofac_imo_by_identity(context, type_ids.vessel_imo_doc_type_id)
        vessels = _collect_ofac_vessel_profiles(context, type_ids)
        _consume_until(context, "ProfileRelationships")
        designations = _collect_ofac_designations(context, type_ids.created_entry_event_type_id)
    finally:
        # The rest of the document (SanctionsEntryLinks) is never read; close() releases the
        # underlying file handle explicitly rather than relying on the iterator being exhausted
        # (iterparse keeps a filename source open until either happens) -- matters on Windows,
        # where a still-open handle blocks the caller's TemporaryDirectory cleanup.
        context.close()

    result: list[SanctionedVessel] = []
    for v in vessels:
        designation = designations.get(v["profile_id"])
        if designation is None:
            raise OFACSchemaError(
                f"Vessel profile {v['profile_id']!r} ({v['vessel_name']!r}) has no matching "
                "<SanctionsEntry> with a 'Created' EntryEvent -- see module docstring."
            )
        designation_date, precision, program = designation
        result.append(
            SanctionedVessel(
                source="ofac",
                source_id=v["profile_id"],
                vessel_name=v["vessel_name"],
                imo=imo_by_identity.get(v["identity_id"]),
                flag=v["flag"],
                program=program,
                designation_date=designation_date,
                designation_date_precision=precision,
            )
        )
    return result


def fetch_ofac(client: httpx.Client | None = None, tmp_dir: Path | None = None) -> list[SanctionedVessel]:
    """Download and parse OFAC's SDN vessel list. See module docstring."""
    owns_client = client is None
    client = client or httpx.Client(timeout=180.0, follow_redirects=True)
    try:
        with tempfile.TemporaryDirectory(prefix="ofac-sdn-", dir=tmp_dir) as tmp:
            xml_path = Path(tmp) / "sdn_advanced.xml"
            _stream_download(client, OFAC_URL, xml_path)
            vessels = parse_ofac_xml(xml_path)
    finally:
        if owns_client:
            client.close()
    logger.info("OFAC: parsed %d vessel(s)", len(vessels))
    return vessels


# ---------------------------------------------------------------------------
# EU (OpenSanctions mirror of the official FSF feed -- see module docstring)
# ---------------------------------------------------------------------------


def _parse_eu_csv(path: Path) -> list[SanctionedVessel]:
    """Parse the EU FSF (OpenSanctions mirror) CSV into normalised :class:`SanctionedVessel`
    records. See module docstring for the column-by-column mapping and the deliberate choice to
    extract the designation date from the free-text 'sanctions' column, never from 'first_seen'.
    """
    vessels: list[SanctionedVessel] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = set(reader.fieldnames or [])
        if not _EU_REQUIRED_COLUMNS.issubset(fieldnames):
            raise EUSchemaError(
                f"Expected columns {sorted(_EU_REQUIRED_COLUMNS)} in the EU FSF CSV, found "
                f"{sorted(fieldnames)} -- see module docstring."
            )
        for row in reader:
            if row.get("schema") != "Vessel":
                continue
            source_id = row["id"]
            name = (row.get("name") or "").strip()
            if not name:
                raise EUSchemaError(f"EU FSF vessel row {source_id!r} has no name -- see module docstring.")

            imo_match = _EU_IMO_RE.search(row.get("identifiers") or "")
            imo = imo_match.group(1) if imo_match else None

            flag = (row.get("countries") or "").strip() or None

            sanctions_text = row.get("sanctions") or ""
            dates = _EU_DATE_RE.findall(sanctions_text)
            if not dates:
                logger.warning(
                    "EU FSF vessel row %s (%s) has no parseable date in 'sanctions' (%r) -- skipping",
                    source_id,
                    name,
                    sanctions_text,
                )
                continue
            designation_date = min(date.fromisoformat(d) for d in dates)

            program = (row.get("program_ids") or "").strip() or "UNKNOWN"

            vessels.append(
                SanctionedVessel(
                    source="eu",
                    source_id=source_id,
                    vessel_name=name,
                    imo=imo,
                    flag=flag,
                    program=program,
                    designation_date=designation_date,
                    designation_date_precision="day",
                )
            )
    return vessels


def fetch_eu(client: httpx.Client | None = None, tmp_dir: Path | None = None) -> list[SanctionedVessel]:
    """Download and parse the EU FSF vessel list (OpenSanctions mirror). See module docstring."""
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        with tempfile.TemporaryDirectory(prefix="eu-fsf-", dir=tmp_dir) as tmp:
            csv_path = Path(tmp) / "eu_fsf_targets.csv"
            _stream_download(client, EU_URL, csv_path)
            vessels = _parse_eu_csv(csv_path)
    finally:
        if owns_client:
            client.close()
    logger.info("EU: parsed %d vessel(s)", len(vessels))
    return vessels


# ---------------------------------------------------------------------------
# UK
# ---------------------------------------------------------------------------


def _parse_uk_date(text: str, uid: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()  # noqa: DTZ007 -- date-only field
    except ValueError as exc:
        raise UKSchemaError(
            f"UK Sanctions List ship {uid!r} has a 'Date Designated' value {text!r} that is not "
            "DD/MM/YYYY -- see module docstring."
        ) from exc


def _parse_uk_csv(path: Path) -> list[SanctionedVessel]:
    """Parse the UK Sanctions List CSV into normalised :class:`SanctionedVessel` records.

    Skips the non-header first line ('Report Date: ...'), keeps only 'Designation Type' == 'Ship'
    rows, groups by 'Unique ID' (one vessel can span many rows: one per address line and/or one
    per sanctions regime -- see module docstring), and for each group takes the EARLIEST
    'Date Designated' with 'program' tied to that same row's 'Regime Name'. The vessel name also
    varies row-to-row (one alias per row); the 'Name type' == 'Primary name' row is used, falling
    back to the group's first row if none is marked primary -- a real quirk not in the task spec,
    see module docstring.
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        first_line = fh.readline()
        if not first_line.startswith("Report Date:"):
            raise UKSchemaError(
                "Expected the UK Sanctions List CSV's first line to start with 'Report Date:', "
                f"got {first_line!r} -- see module docstring."
            )
        reader = csv.DictReader(fh)
        fieldnames = set(reader.fieldnames or [])
        if not _UK_REQUIRED_COLUMNS.issubset(fieldnames):
            raise UKSchemaError(
                f"Expected columns {sorted(_UK_REQUIRED_COLUMNS)} in the UK Sanctions List CSV, "
                f"found {sorted(fieldnames)} -- see module docstring."
            )
        rows_by_id: dict[str, list[dict]] = defaultdict(list)
        for row in reader:
            if row.get("Designation Type") != "Ship":
                continue
            rows_by_id[row["Unique ID"]].append(row)

    vessels: list[SanctionedVessel] = []
    for uid, rows in rows_by_id.items():
        dated_rows = [
            (_parse_uk_date(raw, uid), row)
            for row in rows
            if (raw := (row.get("Date Designated") or "").strip())
        ]
        if not dated_rows:
            raise UKSchemaError(
                f"UK Sanctions List ship {uid!r} has no row with a 'Date Designated' value -- "
                "see module docstring."
            )
        designation_date, earliest_row = min(dated_rows, key=lambda pair: pair[0])

        primary_row = next(
            (row for row in rows if (row.get("Name type") or "").strip() == "Primary name"), rows[0]
        )
        vessel_name = (primary_row.get("Name 6") or "").strip()
        if not vessel_name:
            raise UKSchemaError(
                f"UK Sanctions List ship {uid!r} has no usable name -- see module docstring."
            )

        imo_raw = (earliest_row.get("IMO number") or "").strip()
        # Usually 'IMO1234567' (no space) but at least one real row omits the prefix entirely
        # (confirmed 2026-09-21, Unique ID DPR0076: '8628597') -- strip if present, don't require it.
        imo_digits = imo_raw.removeprefix("IMO").strip()
        imo = imo_digits if imo_digits.isdigit() else None

        flag = (earliest_row.get("Current believed flag of ship") or "").strip() or None
        program = (earliest_row.get("Regime Name") or "").strip() or "UNKNOWN"

        vessels.append(
            SanctionedVessel(
                source="uk",
                source_id=uid,
                vessel_name=vessel_name,
                imo=imo,
                flag=flag,
                program=program,
                designation_date=designation_date,
                designation_date_precision="day",
            )
        )
    return vessels


def fetch_uk(client: httpx.Client | None = None, tmp_dir: Path | None = None) -> list[SanctionedVessel]:
    """Download and parse the UK Sanctions List's ship entries. See module docstring."""
    owns_client = client is None
    client = client or httpx.Client(timeout=180.0, follow_redirects=True)
    try:
        with tempfile.TemporaryDirectory(prefix="uk-sanctions-", dir=tmp_dir) as tmp:
            csv_path = Path(tmp) / "uk_sanctions_list.csv"
            _stream_download(client, UK_URL, csv_path)
            vessels = _parse_uk_csv(csv_path)
    finally:
        if owns_client:
            client.close()
    logger.info("UK: parsed %d vessel(s)", len(vessels))
    return vessels


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def build_sanctions(
    out_path: Path = SANCTIONS_PATH,
    client: httpx.Client | None = None,
    tmp_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Fetch all three sources and land them as one combined Parquet table, ``source`` column
    distinguishing origin. Idempotent: a no-op if out_path already exists, unless force=True.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to re-fetch)", out_path
        )
        return out_path

    vessels: list[SanctionedVessel] = []
    vessels.extend(fetch_ofac(client=client, tmp_dir=tmp_dir))
    vessels.extend(fetch_eu(client=client, tmp_dir=tmp_dir))
    vessels.extend(fetch_uk(client=client, tmp_dir=tmp_dir))
    logger.info("Total: %d sanctioned vessel record(s) across all three sources", len(vessels))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _sanctions (source VARCHAR, source_id VARCHAR, "
            "vessel_name VARCHAR, imo VARCHAR, flag VARCHAR, program VARCHAR, "
            "designation_date DATE, designation_date_precision VARCHAR)"
        )
        if vessels:
            con.executemany(
                "INSERT INTO _sanctions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        v.source,
                        v.source_id,
                        v.vessel_name,
                        v.imo,
                        v.flag,
                        v.program,
                        v.designation_date,
                        v.designation_date_precision,
                    )
                    for v in vessels
                ],
            )
        built_at = datetime.now(timezone.utc)
        con.execute(
            "COPY (SELECT *, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at "
            "FROM _sanctions ORDER BY source, designation_date) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest OFAC/EU/UK sanctions lists, landing a combined vessel table with "
        "designation dates at data/reference/sanctions.parquet."
    )
    parser.add_argument(
        "--out-path", default=str(SANCTIONS_PATH), help="Output path for the combined table"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_sanctions(out_path=Path(args.out_path), force=args.force)


if __name__ == "__main__":
    main()
