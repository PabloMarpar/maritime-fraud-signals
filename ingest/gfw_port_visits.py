"""Downloader for GFW port-visit history, used by ``features.port_visits`` (task P4-11) as a
global-reach cross-check for Russian oil-terminal port calls that Danish-only AIS coverage cannot
see directly.

**Two-stage fetch**, both against GFW API v3 (``gateway.api.globalfishingwatch.org``), confirmed
against live calls on 2026-09-29 (see ``docs/DECISIONS.md``'s P4-11 pre-registration):

1. **Identity**: ``GET /v3/vessels/search``, ``where=imo='<imo>'``,
   ``datasets[0]=public-global-vessel-identity:latest``. One IMO can return several top-level
   entries -- flag/MMSI changes over the vessel's life, confirmed live (one real IMO returned 4
   entries, each a different flag/MMSI combination spanning a different date range) -- each
   carrying its own ``selfReportedInfo`` list. This module collects every ``selfReportedInfo``
   record whose own ``imo`` field equals the queried IMO (the ``where`` clause already restricts
   results this way server-side -- confirmed from the live ``metadata.query`` echo,
   ``"(selfReportedInfo.imo='...' OR registryInfo.imo='...')"`` -- the client-side check below is a
   belt-and-braces guard against a record matched only via ``registryInfo.imo``, which the real
   query also OR's in). No ``offset``/pagination on this endpoint -- confirmed live, passing
   ``offset`` at all raises a 422 ``"property offset should not exist"`` -- so ``limit`` is set
   generously (:data:`SEARCH_LIMIT`) and a truncated result (more entries than fit) is logged
   loudly rather than silently dropped.
2. **Port visits**: ``GET /v3/events``, ``datasets[0]=public-global-port-visits-events:latest``,
   one ``vessels[i]=<gfw_vessel_id>`` per id -- confirmed live that several ids, even from
   different IMOs, can be batched into one call, with each returned entry's own ``vessel.id``
   mapping it back to its id unambiguously. **Hard server-side cap of 20 ids per call**, confirmed
   live by binary search (see :data:`EVENTS_BATCH_SIZE`) -- not a URL-length issue, the error is a
   generic 422 that never names the real limit; an initial real run at 100 failed every batch
   before this was found (see ``docs/STATE.md``'s P4-11 entry). Paged with ``limit``/``offset``
   together (confirmed live: passing ``limit`` without ``offset`` here also 422s, the opposite
   quirk from the search endpoint) until ``nextOffset`` is ``null``. The
   requested ``start-date``/``end-date`` is a broad fetch window (2023-06-01..2025-03-01, matching
   the pre-registration); GFW returns any event whose interval *overlaps* that range (confirmed
   live: one still-open visit was returned with ``start`` before ``start-date``), not one strictly
   contained in it -- :mod:`features.port_visits` re-filters precisely against each panel window's
   own cutoff, this module only bounds the fetch.

**Resumability.** Both stages cache one Parquet file per IMO under :data:`CACHE_ROOT` --
``vessel_ids/<imo>.parquet`` (possibly zero rows, meaning "resolved, nothing found") and
``port_visits/<imo>.parquet`` (possibly zero rows, meaning "resolved ids, no visits in range").
A rerun skips any IMO whose cache file already exists; :func:`build_gfw_port_visits` only
aggregates the two final tables from whatever is cached, so a partial run (killed, rate-limited,
whatever) is always safe to resume and cheap to finish.

**Identity matching rule (analyst-review fix, ``docs/DECISIONS.md`` 2026-09-29).** A record with
no self-reported IMO that was matched only through the entry's ``registryInfo`` is stored with
``match_basis='registry_only'`` for auditing but never used: registry data is compiled with later
knowledge. ``vessel_ids.parquet`` carries ``use_for_features`` (own self-reported IMO equals the
queried imo, not a placeholder IMO, id not claimed by more than one imo); features read only those
rows, and ``port_visits.parquet`` holds only their events.

**Ownership fields excluded by design.** ``registryInfo``/``registryOwners`` (ownership,
tonnage, gear type) are never stored here -- P4-3f already found the ownership-network data source
unusable for this archive period (no link predates any cutoff); only the self-reported identity
fields that carry dates (for P4-12's pre-cutoff flag history) and the port-visit events themselves
are kept.
"""

from __future__ import annotations

import argparse
import glob as glob_module
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import httpx

from process.partitions import atomic_write_parquet, git_sha

logger = logging.getLogger(__name__)

SEARCH_URL = "https://gateway.api.globalfishingwatch.org/v3/vessels/search"
EVENTS_URL = "https://gateway.api.globalfishingwatch.org/v3/events"
IDENTITY_DATASET = "public-global-vessel-identity:latest"
PORT_VISITS_DATASET = "public-global-port-visits-events:latest"
TOKEN_ENV_VAR = "GFW_API_TOKEN"
ENV_PATH = Path(".env")

# Pre-registered in docs/DECISIONS.md's P4-11 entry -- the longest range the GFW port-visits
# dataset and the panel's earliest window jointly allow.
FETCH_START = date(2023, 6, 1)
FETCH_END = date(2025, 3, 1)

# Real IMOs observed return at most a handful of selfReportedInfo entries (4, in the probe used to
# write this module); generous headroom over that, and the search endpoint has no pagination to
# fall back on if it's too low (see module docstring) -- a truncated result is logged, not hidden.
SEARCH_LIMIT = 25
EVENTS_PAGE_LIMIT = 200
# vessel ids per /v3/events call, mixing several imos' ids -- NOT a URL-length limit, a hard
# server-side cap confirmed live by binary search: 20 ids succeeds, 21+ always 422s
# ("each value in vessels must be a string" / "vessels must be an array" -- a generic message
# that does not name the real limit, discovered only empirically). A first real run at
# EVENTS_BATCH_SIZE=100 (2026-09-29) failed every single batch on this before being corrected --
# see docs/STATE.md's P4-11 entry.
EVENTS_BATCH_SIZE = 20

DEFAULT_WORKERS = 8

# Retry/backoff style mirrors ingest.dma: doubling from 2s, capped at 60s.
MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0

PANEL_ROOT = Path("data/processed/panel")
REFERENCE_ROOT = Path("data/reference/gfw")
CACHE_ROOT = REFERENCE_ROOT / "cache"
VESSEL_IDS_CACHE = CACHE_ROOT / "vessel_ids"
PORT_VISITS_CACHE = CACHE_ROOT / "port_visits"
VESSEL_IDS_PATH = REFERENCE_ROOT / "vessel_ids.parquet"
PORT_VISITS_PATH = REFERENCE_ROOT / "port_visits.parquet"

# The stray 2-day validation window (P3-4/A4, see docs/STATE.md's "Open questions") that sits
# alongside every real window-partitioned tree -- excluded so it never contributes an imo twice
# (harmless either way since we dedupe, but excluding it matches the task spec exactly).
STRAY_WINDOW = "window=2024-06-10_2024-06-11"

VESSEL_IDS_SCHEMA: list[tuple[str, str]] = [
    ("imo", "VARCHAR"),
    ("gfw_vessel_id", "VARCHAR"),
    ("ssvid", "VARCHAR"),
    ("shipname", "VARCHAR"),
    ("flag", "VARCHAR"),
    ("callsign", "VARCHAR"),
    ("transmission_date_from", "TIMESTAMP"),
    ("transmission_date_to", "TIMESTAMP"),
    # The record's OWN self-reported IMO (null when it has none) and how it matched the queried
    # IMO: 'self_reported_imo' (own IMO equals it) or 'registry_only' (no own IMO; matched only
    # through the entry's registryInfo, which is compiled with later knowledge). Only the former
    # is ever used by the features -- see ``use_for_features`` in
    # _aggregate_vessel_ids.
    ("sri_imo", "VARCHAR"),
    ("match_basis", "VARCHAR"),
]

MATCH_SELF_REPORTED = "self_reported_imo"
MATCH_REGISTRY_ONLY = "registry_only"

# Placeholder IMOs seen in the data (many unrelated vessels report them): excluded entirely.
PLACEHOLDER_IMOS: frozenset[str] = frozenset({"1234567", "5555555"})

PORT_VISITS_SCHEMA: list[tuple[str, str]] = [
    ("imo", "VARCHAR"),
    ("gfw_vessel_id", "VARCHAR"),
    ("event_id", "VARCHAR"),
    ("visit_id", "VARCHAR"),
    ("start", "TIMESTAMP"),
    ("end", "TIMESTAMP"),
    ("confidence", "INTEGER"),
    ("duration_hrs", "DOUBLE"),
    ("start_anchorage_id", "VARCHAR"),
    ("start_anchorage_name", "VARCHAR"),
    ("start_anchorage_flag", "VARCHAR"),
    ("start_anchorage_lat", "DOUBLE"),
    ("start_anchorage_lon", "DOUBLE"),
    ("start_anchorage_at_dock", "BOOLEAN"),
    ("end_anchorage_id", "VARCHAR"),
    ("end_anchorage_name", "VARCHAR"),
    ("end_anchorage_flag", "VARCHAR"),
]


@dataclass(frozen=True)
class SelfReportedIdentity:
    imo: str
    gfw_vessel_id: str
    ssvid: str | None
    shipname: str | None
    flag: str | None
    callsign: str | None
    transmission_date_from: datetime | None
    transmission_date_to: datetime | None
    sri_imo: str | None = None
    match_basis: str = MATCH_SELF_REPORTED


@dataclass(frozen=True)
class PortVisitEvent:
    imo: str
    gfw_vessel_id: str
    event_id: str
    visit_id: str | None
    start: datetime
    end: datetime
    confidence: int | None
    duration_hrs: float | None
    start_anchorage_id: str | None
    start_anchorage_name: str | None
    start_anchorage_flag: str | None
    start_anchorage_lat: float | None
    start_anchorage_lon: float | None
    start_anchorage_at_dock: bool | None
    end_anchorage_id: str | None
    end_anchorage_name: str | None
    end_anchorage_flag: str | None


def load_api_token(env_path: Path = ENV_PATH, var: str = TOKEN_ENV_VAR) -> str | None:
    """Read the GFW token from the environment first, then a dotenv file -- never logged.

    Mirrors ``ingest.aisstream.load_api_key``'s manual ``.env`` parsing (no third-party dotenv
    dependency in ``requirements.txt``).
    """
    value = os.environ.get(var)
    if value:
        return value
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, raw_value = line.partition("=")
        if name.strip() == var:
            cleaned = raw_value.strip().strip('"').strip("'")
            return cleaned or None
    return None


def distinct_imos(panel_root: Path = PANEL_ROOT) -> list[str]:
    """Every distinct non-null imo across every panel window, excluding the stray 2-day window and
    the placeholder IMOs (:data:`PLACEHOLDER_IMOS`)."""
    pattern = (panel_root / "window=*" / "part-0.parquet").as_posix()
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT DISTINCT imo FROM read_parquet('{pattern}', filename=true) "
            f"WHERE imo IS NOT NULL AND filename NOT LIKE '%{STRAY_WINDOW}%'"
        ).fetchall()
    finally:
        con.close()
    return sorted(r[0] for r in rows if r[0] not in PLACEHOLDER_IMOS)


def _parse_gfw_timestamp(value: Any) -> datetime | None:
    """Parse a GFW ISO timestamp (always UTC, suffixed 'Z') into a **naive** datetime holding UTC
    wall-clock time -- not a tz-aware one. DuckDB's Parquet ``TIMESTAMP`` type (used throughout
    this module and by :mod:`features.port_visits`'s window-boundary comparisons) is itself
    timezone-naive; handing the DuckDB Python driver a tz-AWARE datetime silently converts it to
    the local machine's timezone before stripping the tzinfo (confirmed live: a UTC midnight
    became 02:00 on this machine's Europe timezone), which would shift every stored timestamp and
    corrupt the exact-day boundaries the 270-day lookback and the ``end < window_end``
    leakage guard depend on. Stripping the 'Z' and parsing as naive avoids the conversion
    entirely, so the stored value is UTC wall-clock time, unchanged."""
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.removesuffix("Z"))


def _get_with_retries(
    client: httpx.Client, url: str, params: list[tuple[str, str]], api_token: str
) -> dict[str, Any]:
    """GET url with retries + capped exponential backoff on 429/5xx/timeout (see module
    docstring point 2 / ingest.dma's retry style). A real 4xx other than 429 is not retried.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        backoff = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
        try:
            response = client.get(
                url, params=params, headers={"Authorization": f"Bearer {api_token}"}
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc
        else:
            if response.status_code == 429:
                last_error = RuntimeError(f"429 rate limited at {url}")
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    backoff = float(retry_after)
            elif response.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} from {url}", request=response.request, response=response
                )
            else:
                response.raise_for_status()  # a real non-retryable 4xx
                return response.json()

        if attempt == MAX_ATTEMPTS:
            break
        logger.warning(
            "Transient error at %s (attempt %d/%d): %s -- retrying in %.0fs",
            url, attempt, MAX_ATTEMPTS, last_error, backoff,
        )
        time.sleep(backoff)
    raise RuntimeError(f"Exhausted {MAX_ATTEMPTS} attempts against {url}: {last_error}") from last_error


def _self_reported_identities(imo: str, payload: dict[str, Any]) -> list[SelfReportedIdentity]:
    """Every selfReportedInfo record, across every top-level entry, whose own imo matches --
    see module docstring point 1."""
    entries = payload.get("entries") or []
    total = payload.get("total")
    if isinstance(total, int) and total > len(entries):
        logger.warning(
            "imo %s: search returned %d of %d entries -- SEARCH_LIMIT=%d may be too low",
            imo, len(entries), total, SEARCH_LIMIT,
        )
    out: list[SelfReportedIdentity] = []
    for entry in entries:
        registry_imos = {r.get("imo") for r in (entry.get("registryInfo") or []) if r.get("imo")}
        for sri in entry.get("selfReportedInfo") or []:
            vessel_id = sri.get("id")
            if not vessel_id:
                continue
            sri_imo = sri.get("imo")
            if sri_imo:
                if sri_imo != imo:
                    continue
                match_basis = MATCH_SELF_REPORTED
            elif imo in registry_imos:
                match_basis = MATCH_REGISTRY_ONLY
            else:
                continue
            out.append(
                SelfReportedIdentity(
                    imo=imo,
                    gfw_vessel_id=vessel_id,
                    ssvid=sri.get("ssvid"),
                    shipname=sri.get("shipname"),
                    flag=sri.get("flag"),
                    callsign=sri.get("callsign"),
                    transmission_date_from=_parse_gfw_timestamp(sri.get("transmissionDateFrom")),
                    transmission_date_to=_parse_gfw_timestamp(sri.get("transmissionDateTo")),
                    sri_imo=sri_imo or None,
                    match_basis=match_basis,
                )
            )
    return out


def resolve_vessel_ids(imo: str, api_token: str, client: httpx.Client) -> list[SelfReportedIdentity]:
    params = [
        ("datasets[0]", IDENTITY_DATASET),
        ("where", f"imo='{imo}'"),
        ("limit", str(SEARCH_LIMIT)),
    ]
    payload = _get_with_retries(client, SEARCH_URL, params, api_token)
    return _self_reported_identities(imo, payload)


def _parse_port_visit_entry(
    id_to_imo: dict[str, str], entry: dict[str, Any]
) -> PortVisitEvent | None:
    if entry.get("type") not in (None, "port_visit"):
        return None
    vessel_id = (entry.get("vessel") or {}).get("id")
    imo = id_to_imo.get(vessel_id)
    if imo is None or not vessel_id:
        return None
    start = _parse_gfw_timestamp(entry.get("start"))
    end = _parse_gfw_timestamp(entry.get("end"))
    if start is None or end is None:
        return None
    event_id = entry.get("id")
    if not event_id:
        return None
    pv = entry.get("port_visit") or {}
    start_anchorage = pv.get("startAnchorage") or {}
    end_anchorage = pv.get("endAnchorage") or {}
    confidence_raw = pv.get("confidence")
    try:
        confidence = int(confidence_raw) if confidence_raw not in (None, "") else None
    except (TypeError, ValueError):
        confidence = None
    return PortVisitEvent(
        imo=imo,
        gfw_vessel_id=vessel_id,
        event_id=event_id,
        visit_id=pv.get("visitId"),
        start=start,
        end=end,
        confidence=confidence,
        duration_hrs=pv.get("durationHrs"),
        start_anchorage_id=start_anchorage.get("anchorageId"),
        start_anchorage_name=start_anchorage.get("name"),
        start_anchorage_flag=start_anchorage.get("flag"),
        start_anchorage_lat=start_anchorage.get("lat"),
        start_anchorage_lon=start_anchorage.get("lon"),
        start_anchorage_at_dock=start_anchorage.get("atDock"),
        end_anchorage_id=end_anchorage.get("anchorageId"),
        end_anchorage_name=end_anchorage.get("name"),
        end_anchorage_flag=end_anchorage.get("flag"),
    )


def _fetch_port_visits_page(
    ids: list[str], api_token: str, client: httpx.Client
) -> list[dict[str, Any]]:
    """Every raw port-visit event entry for AT MOST :data:`EVENTS_BATCH_SIZE` vessel ids (the
    server's own hard cap, see :data:`EVENTS_BATCH_SIZE`), paginating on ``nextOffset`` until it
    is null."""
    entries: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = [
            ("datasets[0]", PORT_VISITS_DATASET),
            ("start-date", FETCH_START.isoformat()),
            ("end-date", FETCH_END.isoformat()),
            ("limit", str(EVENTS_PAGE_LIMIT)),
            ("offset", str(offset)),
        ] + [(f"vessels[{i}]", vid) for i, vid in enumerate(ids)]
        payload = _get_with_retries(client, EVENTS_URL, params, api_token)
        page = payload.get("entries") or []
        entries.extend(page)
        next_offset = payload.get("nextOffset")
        if next_offset is None:
            break
        offset = next_offset
    return entries


def fetch_port_visits_batch(
    ids: list[str], api_token: str, client: httpx.Client
) -> list[dict[str, Any]]:
    """Fetch every raw port-visit event entry for a batch of GFW vessel ids -- any number of
    them, chunked internally into groups of at most :data:`EVENTS_BATCH_SIZE` (the server's hard
    cap; :func:`_pack_batches` keeps one imo's own ids together even when that imo alone has more
    ids than the cap, e.g. a real imo with 26 ids from repeated flag/MMSI changes -- confirmed
    live, this is what surfaces that case)."""
    entries: list[dict[str, Any]] = []
    for i in range(0, len(ids), EVENTS_BATCH_SIZE):
        entries.extend(_fetch_port_visits_page(ids[i : i + EVENTS_BATCH_SIZE], api_token, client))
    return entries


def _rows_for_vessel_ids(identities: list[SelfReportedIdentity]) -> list[tuple]:
    return [
        (i.imo, i.gfw_vessel_id, i.ssvid, i.shipname, i.flag, i.callsign,
         i.transmission_date_from, i.transmission_date_to, i.sri_imo, i.match_basis)
        for i in identities
    ]


def _rows_for_port_visits(events: list[PortVisitEvent]) -> list[tuple]:
    return [
        (e.imo, e.gfw_vessel_id, e.event_id, e.visit_id, e.start, e.end, e.confidence,
         e.duration_hrs, e.start_anchorage_id, e.start_anchorage_name, e.start_anchorage_flag,
         e.start_anchorage_lat, e.start_anchorage_lon, e.start_anchorage_at_dock,
         e.end_anchorage_id, e.end_anchorage_name, e.end_anchorage_flag)
        for e in events
    ]


def _write_rows_parquet(rows: list[tuple], schema: list[tuple[str, str]], out_path: Path) -> None:
    """Write ``rows`` (possibly empty, to preserve the schema for a later glob read) atomically."""
    con = duckdb.connect()
    try:
        cols_ddl = ", ".join(f'"{name}" {typ}' for name, typ in schema)
        con.execute(f"CREATE TABLE t ({cols_ddl})")
        if rows:
            placeholders = ", ".join(["?"] * len(schema))
            con.executemany(f"INSERT INTO t VALUES ({placeholders})", rows)
        atomic_write_parquet(con, "SELECT * FROM t", out_path)
    finally:
        con.close()


def _cached_imo_set(cache_dir: Path) -> set[str]:
    if not cache_dir.exists():
        return set()
    return {p.stem for p in cache_dir.glob("*.parquet")}


def _resolve_one_imo(imo: str, api_token: str, client: httpx.Client) -> int:
    out_path = VESSEL_IDS_CACHE / f"{imo}.parquet"
    identities = resolve_vessel_ids(imo, api_token, client)
    _write_rows_parquet(_rows_for_vessel_ids(identities), VESSEL_IDS_SCHEMA, out_path)
    return len(identities)


def resolve_all_vessel_ids(
    imos: list[str], api_token: str, workers: int = DEFAULT_WORKERS
) -> None:
    """Resolve every imo not already cached under :data:`VESSEL_IDS_CACHE`, in parallel."""
    VESSEL_IDS_CACHE.mkdir(parents=True, exist_ok=True)
    done = _cached_imo_set(VESSEL_IDS_CACHE)
    todo = [imo for imo in imos if imo not in done]
    logger.info("Resolving vessel ids: %d of %d imo(s) not yet cached", len(todo), len(imos))
    if not todo:
        return

    # httpx.Client's connection pool is safe for concurrent use from multiple threads (its
    # underlying httpcore pool is lock-protected) -- one shared client, not one per request.
    client = httpx.Client(timeout=60.0)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_resolve_one_imo, imo, api_token, client): imo for imo in todo}
            n_failed = 0
            for n_done, future in enumerate(as_completed(futures), start=1):
                imo = futures[future]
                try:
                    future.result()
                except Exception:
                    n_failed += 1
                    logger.exception("Failed to resolve imo %s -- will retry on next run", imo)
                if n_done % 500 == 0:
                    logger.info("Resolved %d/%d imo(s) (%d failed so far)", n_done, len(todo), n_failed)
    finally:
        client.close()


def _pack_batches(
    imo_to_ids: dict[str, list[str]], batch_size: int
) -> list[tuple[list[str], list[str]]]:
    """Group imos into (imos, ids) batches so each imo's own ids always stay together in one
    batch -- required so a batch's success can mark every imo in it as done -- and no batch
    exceeds ``batch_size`` ids unless a single imo alone has more (kept whole regardless; a real
    imo needed this -- see :func:`fetch_port_visits_batch`, which chunks a batch's ids into
    individual HTTP calls of at most :data:`EVENTS_BATCH_SIZE` regardless of how they were
    grouped here)."""
    batches: list[tuple[list[str], list[str]]] = []
    current_ids: list[str] = []
    current_imos: list[str] = []
    for imo, ids in imo_to_ids.items():
        if current_ids and len(current_ids) + len(ids) > batch_size:
            batches.append((current_imos, current_ids))
            current_ids, current_imos = [], []
        current_ids.extend(ids)
        current_imos.append(imo)
    if current_ids or current_imos:
        batches.append((current_imos, current_ids))
    return batches


def _fetch_and_cache_batch(
    batch_imos: list[str],
    batch_ids: list[str],
    id_to_imo: dict[str, str],
    api_token: str,
    client: httpx.Client,
) -> int:
    raw_entries = fetch_port_visits_batch(batch_ids, api_token, client)
    events_by_imo: dict[str, list[PortVisitEvent]] = {imo: [] for imo in batch_imos}
    for raw in raw_entries:
        event = _parse_port_visit_entry(id_to_imo, raw)
        if event is not None:
            events_by_imo.setdefault(event.imo, []).append(event)
    for imo in batch_imos:
        _write_rows_parquet(
            _rows_for_port_visits(events_by_imo.get(imo, [])),
            PORT_VISITS_SCHEMA,
            PORT_VISITS_CACHE / f"{imo}.parquet",
        )
    return sum(len(v) for v in events_by_imo.values())


def _load_cached_vessel_id_pairs() -> list[tuple[str, str]]:
    pattern = (VESSEL_IDS_CACHE / "*.parquet").as_posix()
    if not glob_module.glob(pattern):
        return []
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT imo, gfw_vessel_id FROM read_parquet('{pattern}')"
        ).fetchall()
    finally:
        con.close()


def fetch_all_port_visits(
    imos: list[str], api_token: str, workers: int = DEFAULT_WORKERS
) -> None:
    """Fetch port-visit events for every imo not already cached under :data:`PORT_VISITS_CACHE`.

    Requires :func:`resolve_all_vessel_ids` to have already run (reads its cache to build the
    id -> imo map). An imo resolved with zero ids is marked done immediately, no API call needed.
    """
    PORT_VISITS_CACHE.mkdir(parents=True, exist_ok=True)
    imos_set = set(imos)
    resolved_imos = _cached_imo_set(VESSEL_IDS_CACHE)
    done_imos = _cached_imo_set(PORT_VISITS_CACHE)

    imo_to_ids: dict[str, list[str]] = {}
    id_to_imo: dict[str, str] = {}
    for imo, vessel_id in _load_cached_vessel_id_pairs():
        if imo not in imos_set:
            continue
        id_to_imo[vessel_id] = imo
        imo_to_ids.setdefault(imo, []).append(vessel_id)

    n_marked_empty = 0
    for imo in imos:
        if imo in resolved_imos and imo not in done_imos and imo not in imo_to_ids:
            _write_rows_parquet([], PORT_VISITS_SCHEMA, PORT_VISITS_CACHE / f"{imo}.parquet")
            done_imos.add(imo)
            n_marked_empty += 1
    if n_marked_empty:
        logger.info("%d imo(s) resolved with zero GFW ids -- cached as zero port visits", n_marked_empty)

    todo = {imo: ids for imo, ids in imo_to_ids.items() if imo not in done_imos}
    batches = _pack_batches(todo, EVENTS_BATCH_SIZE)
    logger.info(
        "Fetching port visits: %d imo(s) across %d batch(es) not yet cached", len(todo), len(batches)
    )
    if not batches:
        return

    client = httpx.Client(timeout=60.0)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_and_cache_batch, b_imos, b_ids, id_to_imo, api_token, client): b_imos
                for b_imos, b_ids in batches
            }
            n_events = 0
            n_failed = 0
            for n_done, future in enumerate(as_completed(futures), start=1):
                b_imos = futures[future]
                try:
                    n_events += future.result()
                except Exception:
                    n_failed += 1
                    logger.exception(
                        "Failed to fetch port visits for a batch of %d imo(s) -- will retry on "
                        "next run", len(b_imos),
                    )
                if n_done % 50 == 0:
                    logger.info(
                        "Fetched %d/%d batch(es), %d events so far (%d batches failed)",
                        n_done, len(batches), n_events, n_failed,
                    )
    finally:
        client.close()


def _glob_or_none(cache_dir: Path) -> str | None:
    pattern = (cache_dir / "*.parquet").as_posix()
    return pattern if glob_module.glob(pattern) else None


def _aggregate_vessel_ids(cache_dir: Path, out_path: Path) -> None:
    """Aggregate the per-imo identity cache into ``vessel_ids.parquet``, adding
    ``id_shared`` (this gfw_vessel_id is self-reported by more than one distinct imo) and
    ``use_for_features`` -- the ONLY rows any feature may read: the record's own self-reported IMO
    equals the queried imo, the imo is not a placeholder, and the id is not shared. Registry-only,
    placeholder and shared rows stay in the file for auditing."""
    pattern = _glob_or_none(cache_dir)
    con = duckdb.connect()
    try:
        built_at = datetime.now(timezone.utc)
        sha = git_sha()
        if pattern is None:
            cols_ddl = ", ".join(f'"{name}" {typ}' for name, typ in VESSEL_IDS_SCHEMA)
            con.execute(f"CREATE TABLE ids ({cols_ddl})")
        else:
            con.execute(f"CREATE TABLE ids AS SELECT * FROM read_parquet('{pattern}')")
        placeholders = ", ".join(f"'{p}'" for p in sorted(PLACEHOLDER_IMOS))
        atomic_write_parquet(
            con,
            f"""
            WITH shared AS (
                SELECT gfw_vessel_id FROM ids
                WHERE match_basis = '{MATCH_SELF_REPORTED}' AND sri_imo = imo
                GROUP BY gfw_vessel_id HAVING count(DISTINCT imo) > 1
            )
            SELECT ids.*,
                (ids.gfw_vessel_id IN (SELECT gfw_vessel_id FROM shared)) AS id_shared,
                (ids.match_basis = '{MATCH_SELF_REPORTED}' AND ids.sri_imo = ids.imo
                 AND ids.imo NOT IN ({placeholders})
                 AND ids.gfw_vessel_id NOT IN (SELECT gfw_vessel_id FROM shared)) AS use_for_features,
                TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at,
                '{sha}' AS git_sha
            FROM ids ORDER BY ids.imo, ids.gfw_vessel_id
            """,
            out_path,
        )
    finally:
        con.close()


def _aggregate_port_visits(cache_dir: Path, vessel_ids_path: Path, out_path: Path) -> None:
    """Aggregate the per-imo event cache into ``port_visits.parquet``, keeping only events whose
    (imo, gfw_vessel_id) is a ``use_for_features`` identity in ``vessel_ids_path``. The cache is
    per imo with the id on every event, so no re-fetch is needed when the identity rule changes."""
    pattern = _glob_or_none(cache_dir)
    con = duckdb.connect()
    try:
        built_at = datetime.now(timezone.utc)
        sha = git_sha()
        if pattern is None:
            cols_ddl = ", ".join(f'"{name}" {typ}' for name, typ in PORT_VISITS_SCHEMA)
            con.execute(f"CREATE TABLE ev ({cols_ddl})")
        else:
            con.execute(f"CREATE TABLE ev AS SELECT * FROM read_parquet('{pattern}')")
        con.execute(
            "CREATE TEMP VIEW kept AS SELECT DISTINCT imo, gfw_vessel_id "
            f"FROM read_parquet('{vessel_ids_path.as_posix()}') WHERE use_for_features"
        )
        atomic_write_parquet(
            con,
            "SELECT ev.*, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{sha}' AS git_sha "
            "FROM ev JOIN kept USING (imo, gfw_vessel_id) ORDER BY ev.imo, ev.start",
            out_path,
        )
    finally:
        con.close()


def build_gfw_port_visits(
    panel_root: Path = PANEL_ROOT,
    vessel_ids_out: Path = VESSEL_IDS_PATH,
    port_visits_out: Path = PORT_VISITS_PATH,
    api_token: str | None = None,
    workers: int = DEFAULT_WORKERS,
    force: bool = False,
    refresh_identities: bool = False,
) -> tuple[Path, Path]:
    """Resolve GFW vessel ids and fetch port-visit events for every distinct imo in the panel.

    Resumable via the per-imo cache under :data:`CACHE_ROOT` (see module docstring); ``force=True``
    wipes the cache first and re-fetches everything. ``refresh_identities=True`` wipes only the
    identity cache (re-searching every imo) and keeps the per-imo port-visit event cache, which is
    re-filtered to the kept identities at aggregation time.
    """
    api_token = api_token or load_api_token()
    if not api_token:
        raise SystemExit(
            f"{TOKEN_ENV_VAR} is not set (checked the environment and {ENV_PATH}). Get a free "
            "token at https://globalfishingwatch.org/our-apis/tokens."
        )

    if force:
        shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    elif refresh_identities:
        shutil.rmtree(VESSEL_IDS_CACHE, ignore_errors=True)

    imos = distinct_imos(panel_root)
    logger.info("%d distinct imo(s) across the panel", len(imos))

    resolve_all_vessel_ids(imos, api_token, workers=workers)
    fetch_all_port_visits(imos, api_token, workers=workers)

    _aggregate_vessel_ids(VESSEL_IDS_CACHE, vessel_ids_out)
    _aggregate_port_visits(PORT_VISITS_CACHE, vessel_ids_out, port_visits_out)

    con = duckdb.connect()
    try:
        n_resolved = con.execute(
            f"SELECT count(DISTINCT imo) FROM read_parquet('{vessel_ids_out.as_posix()}')"
        ).fetchone()[0]
        n_ids = con.execute(f"SELECT count(*) FROM read_parquet('{vessel_ids_out.as_posix()}')").fetchone()[0]
        n_events = con.execute(f"SELECT count(*) FROM read_parquet('{port_visits_out.as_posix()}')").fetchone()[0]
    finally:
        con.close()
    logger.info(
        "IMOs queried %d, resolved (>=1 id) %d (%.1f%%), ids %d, port-visit events %d",
        len(imos), n_resolved, (100 * n_resolved / len(imos)) if imos else 0.0, n_ids, n_events,
    )
    return vessel_ids_out, port_visits_out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch GFW vessel-identity ids and port-visit events for every distinct "
        "imo in the panel (task P4-11). Reads GFW_API_TOKEN from the environment or .env."
    )
    parser.add_argument("--panel-root", default=str(PANEL_ROOT))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--refresh-identities", action="store_true",
        help="Wipe only the identity cache and re-search every imo; keep cached port visits",
    )
    parser.add_argument(
        "--force", action="store_true", help="Wipe the per-imo cache and re-fetch everything"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_gfw_port_visits(
        panel_root=Path(args.panel_root), workers=args.workers, force=args.force,
        refresh_identities=args.refresh_identities,
    )


if __name__ == "__main__":
    main()
