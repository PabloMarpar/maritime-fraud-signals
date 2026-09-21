"""Downloader for the Global Fishing Watch (GFW) Events API -- encounter events, fetched as an
independent cross-check for :mod:`detect.sts` (see ``docs/DECISIONS.md``, task P2-7).

**Endpoint contract**, confirmed against live GFW documentation on 2026-09-21 (see
``docs/DATA_SOURCES.md``): ``GET https://gateway.api.globalfishingwatch.org/v3/events``,
``Authorization: Bearer <token>``, ``datasets[0]=public-global-encounters-events:latest``,
``types[0]=ENCOUNTER``, ``start-date``/``end-date`` (``YYYY-MM-DD``), ``limit``/``offset``
pagination (200 observed as the practical page-size ceiling). There is no bounding-box parameter
at the raw HTTP API level -- GFW's own SDKs (``gfwr``/``gfw-api-python-client``) support a geometry
filter, the endpoint called directly here does not -- so this module fetches every page for the
date range and filters to a bounding box client-side, in :func:`filter_bbox`.

**Response shape, confirmed against a real API call on 2026-09-21** (see ``docs/DECISIONS.md``):
one page is ``{"entries": [...], "limit", "offset", "nextOffset", "total", "metadata"}``. Each
encounter is reported TWICE, once from each vessel's own point of view, as two entries sharing an
id up to a trailing ``.1``/``.2`` suffix -- but unlike this module's pre-token guess, each entry
already carries BOTH sides (``vessel`` for "this" side, ``encounter.vessel`` for the other), so the
two entries are mirrored duplicates of the same fact, not complementary halves that must be paired.
:func:`normalise_entries` therefore de-duplicates by base id and reads both MMSI-equivalents off a
single entry. Two things the pre-token guess got wrong, corrected here: the MMSI-equivalent field
is ``vessel.ssvid`` (a string), not ``vessel.mmsi``; and position is nested under
``position.lat``/``position.lon``, not top-level ``lat``/``lon``. :func:`_extract_entries` and
:func:`_normalise_entry` still raise :class:`GFWSchemaError` with the offending payload/record
attached on any further mismatch, rather than silently mis-parsing -- GFW's API is third-party and
unversioned-in-practice from this project's point of view (the ``:latest`` dataset tag observed
resolving to ``public-global-encounters-events:v4.0`` this session could move again).

**Scope caveat -- read before interpreting P2-7's agreement numbers.** GFW's encounters dataset
only covers vessel-type pairs it classifies as fishing-economy activity: fishing-fishing,
fishing-carrier, fishing-support, fishing-bunker, tanker-fishing, carrier-bunker, support-bunker
(GFW's own documented list). :mod:`detect.sts` has no such restriction, and on the real 30-day
window its gated survivors skew away from that population ("no Belts, few tankers" -- P2-4, see
``docs/STATE.md``). Low measured agreement is therefore expected to partly reflect this scope
mismatch, not a failure of either detector -- report the two together, never the number alone.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://gateway.api.globalfishingwatch.org/v3/events"
DATASET = "public-global-encounters-events:latest"
EVENT_TYPE = "ENCOUNTER"
PAGE_LIMIT = 200
TOKEN_ENV_VAR = "GFW_API_TOKEN"

REFERENCE_ROOT = Path("data/reference")
GFW_ENCOUNTERS_PATH = REFERENCE_ROOT / "gfw_encounters.parquet"


class GFWSchemaError(ValueError):
    """Raised when a fetched GFW payload/record doesn't match the shape assumed by this module.

    That shape was never confirmed against a live token (see module docstring) -- this exists so
    a mismatch fails loudly, with the offending data attached, instead of silently producing wrong
    rows.
    """


@dataclass(frozen=True)
class GFWEncounter:
    """One GFW encounter event, normalised to the same two-vessel shape as
    :class:`detect.sts.EncounterEvent`, so the two are directly comparable in P2-7.

    ``encounter_type`` is GFW's own vessel-type-pair classification (e.g. ``"fishing-fishing"``,
    ``"tanker-fishing"``) -- see module docstring's scope caveat; kept for reporting, not used in
    the matching rule itself.
    """

    gfw_event_id: str
    start_time: datetime
    end_time: datetime
    latitude: float
    longitude: float
    mmsi_a: int
    mmsi_b: int
    encounter_type: str | None


def _extract_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of raw event records inside one page's JSON payload.

    Tries the documented ``"entries"`` key first, falls back to ``"data"`` (a common alternate
    envelope name for this kind of API), and raises :class:`GFWSchemaError` naming the top-level
    keys actually present if neither is a list -- see module docstring.
    """
    for key in ("entries", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    raise GFWSchemaError(
        f"Expected a list under 'entries' or 'data', found top-level keys {sorted(payload)!r}. "
        "The GFW response envelope may not match this module's assumption -- see module "
        "docstring; inspect the raw payload and fix _extract_entries."
    )


def _fetch_page(
    client: httpx.Client, api_token: str, start: date, end: date, offset: int, limit: int
) -> dict[str, Any]:
    response = client.get(
        BASE_URL,
        params=[
            ("datasets[0]", DATASET),
            ("types[0]", EVENT_TYPE),
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            ("limit", str(limit)),
            ("offset", str(offset)),
        ],
        headers={"Authorization": f"Bearer {api_token}"},
    )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "unknown")
        raise RuntimeError(
            f"GFW API rate limit hit at offset={offset} (Retry-After={retry_after}). "
            "See docs/DATA_SOURCES.md for the documented daily/monthly caps."
        )
    response.raise_for_status()
    return response.json()


def fetch_raw_entries(
    start: date,
    end: date,
    api_token: str,
    client: httpx.Client | None = None,
    page_limit: int = PAGE_LIMIT,
) -> list[dict[str, Any]]:
    """Fetch every raw encounter-event record for ``[start, end]``, paginating until exhausted.

    Returns the raw list of dicts exactly as GFW returns them -- no normalisation, no bbox filter
    -- so a caller or test can inspect the true shape before anything downstream trusts
    :func:`_normalise_pair`'s assumptions about it. Stops when a page returns fewer than
    ``page_limit`` entries, rather than depending on an unverified "total"/"nextOffset" field.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    entries: list[dict[str, Any]] = []
    try:
        offset = 0
        while True:
            payload = _fetch_page(client, api_token, start, end, offset, page_limit)
            page = _extract_entries(payload)
            entries.extend(page)
            if len(page) < page_limit:
                break
            offset += page_limit
    finally:
        if owns_client:
            client.close()
    return entries


def _normalise_entry(base_id: str, record: dict[str, Any]) -> GFWEncounter:
    """Build a :class:`GFWEncounter` from a single raw entry -- one entry already carries both
    vessels (``vessel``, the reporting side; ``encounter.vessel``, the other side), see module
    docstring. Raises :class:`GFWSchemaError` naming the base id and the record if the expected
    keys are missing, rather than guessing.
    """
    try:
        own_ssvid = record["vessel"]["ssvid"]
        other_ssvid = record["encounter"]["vessel"]["ssvid"]
        position = record["position"]
        return GFWEncounter(
            gfw_event_id=base_id,
            start_time=datetime.fromisoformat(record["start"].replace("Z", "+00:00")),
            end_time=datetime.fromisoformat(record["end"].replace("Z", "+00:00")),
            latitude=float(position["lat"]),
            longitude=float(position["lon"]),
            mmsi_a=int(own_ssvid),
            mmsi_b=int(other_ssvid),
            encounter_type=record.get("encounter", {}).get("type"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GFWSchemaError(
            f"Entry for encounter {base_id!r} did not match the confirmed shape "
            "(record['vessel']['ssvid'], record['encounter']['vessel']['ssvid'], "
            f"record['position']['lat'/'lon'], record['start'/'end']): {record!r}"
        ) from exc


def normalise_entries(entries: list[dict[str, Any]]) -> list[GFWEncounter]:
    """De-duplicate raw entries by base id (each encounter arrives twice, mirrored -- see module
    docstring) and normalise one :class:`GFWEncounter` per encounter.

    Raises :class:`GFWSchemaError` if an id has no recognisable ``<base>.<side>`` suffix, which
    would indicate the real shape has changed since this module was last checked against a live
    call.
    """
    by_base_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or "." not in raw_id:
            raise GFWSchemaError(
                f"Entry id {raw_id!r} has no '<base>.<side>' suffix -- the confirmed shape does "
                "not hold any more. See module docstring."
            )
        base_id = raw_id.rsplit(".", 1)[0]
        by_base_id.setdefault(base_id, entry)  # keep the first copy seen; both are mirrors

    return [_normalise_entry(base_id, record) for base_id, record in by_base_id.items()]


def filter_bbox(
    encounters: list[GFWEncounter],
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
) -> list[GFWEncounter]:
    """Keep only encounters whose position falls inside the given bounding box.

    Client-side filter: the raw HTTP API has no bbox/geometry parameter (see module docstring).
    """
    return [
        e for e in encounters if min_lat <= e.latitude <= max_lat and min_lon <= e.longitude <= max_lon
    ]


def build_gfw_encounters(
    start: date,
    end: date,
    api_token: str,
    bbox: tuple[float, float, float, float] | None = None,
    out_path: Path = GFW_ENCOUNTERS_PATH,
    client: httpx.Client | None = None,
    force: bool = False,
) -> Path:
    """Fetch, normalise, optionally bbox-filter, and write GFW encounters for ``[start, end]``.

    Idempotent: if ``out_path`` already exists, this is a no-op unless ``force=True``. ``bbox``,
    if given, is ``(min_lat, max_lat, min_lon, max_lon)``.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to re-fetch)", out_path
        )
        return out_path

    raw_entries = fetch_raw_entries(start, end, api_token, client=client)
    logger.info("Fetched %d raw entry record(s)", len(raw_entries))

    encounters = normalise_entries(raw_entries)
    logger.info("Normalised into %d encounter(s)", len(encounters))

    if bbox is not None:
        min_lat, max_lat, min_lon, max_lon = bbox
        encounters = filter_bbox(encounters, min_lat, max_lat, min_lon, max_lon)
        logger.info("%d encounter(s) remain after bbox filter %s", len(encounters), bbox)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _gfw_encounters "
            "(gfw_event_id VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP, "
            "latitude DOUBLE, longitude DOUBLE, mmsi_a BIGINT, mmsi_b BIGINT, "
            "encounter_type VARCHAR)"
        )
        if encounters:
            con.executemany(
                "INSERT INTO _gfw_encounters VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.gfw_event_id,
                        e.start_time,
                        e.end_time,
                        e.latitude,
                        e.longitude,
                        e.mmsi_a,
                        e.mmsi_b,
                        e.encounter_type,
                    )
                    for e in encounters
                ],
            )
        built_at = datetime.now(timezone.utc)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at "
            "FROM _gfw_encounters ORDER BY start_time) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch GFW encounter events for a date range, for cross-checking against "
        "detect.sts (P2-7). Reads the API token from the GFW_API_TOKEN environment variable."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LAT", "MAX_LAT", "MIN_LON", "MAX_LON"),
        help="Restrict to this bounding box (client-side filter, see module docstring)",
    )
    parser.add_argument(
        "--out-path", default=str(GFW_ENCOUNTERS_PATH), help="Output path for the encounters table"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    api_token = os.environ.get(TOKEN_ENV_VAR)
    if not api_token:
        raise SystemExit(
            f"{TOKEN_ENV_VAR} is not set. Get a free token at "
            "https://globalfishingwatch.org/our-apis/tokens, then set it in your environment "
            "(e.g. via a .env file, never committed) before running this module."
        )
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    bbox = tuple(args.bbox) if args.bbox else None
    build_gfw_encounters(
        start,
        end,
        api_token=api_token,
        bbox=bbox,
        out_path=Path(args.out_path),
        force=args.force,
    )


if __name__ == "__main__":
    main()
