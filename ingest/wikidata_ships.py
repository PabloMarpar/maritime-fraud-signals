"""Downloader + reconciler for Wikidata's ship build-year data -- the source that unblocks P4-1's
naive-baseline age term (see ``docs/DECISIONS.md``'s P4-1 entry). GFW's vessel registry carries no
build-year field, and Equasis/IMO GISIS both prohibit bulk/automated extraction in their terms of
use; Wikidata is CC0 and its public SPARQL endpoint needs no token.

**Endpoint and query, confirmed live 2026-09-23.** ``GET https://query.wikidata.org/sparql`` with
``query``/``format=json``. **Wikimedia's User-Agent policy is enforced in practice, not just
documented**: an unlabelled request gets HTTP 403 (confirmed live) -- :data:`_HEADERS` sends a
descriptive User-Agent naming this project and its repository. The query pulls the WHOLE Wikidata
ship set, not just this project's own IMO list: a query scoped to our own IMOs would make the fetch
aware of the population it will later be checked for contamination against (see
``model.build_year_gate``), and a blind full pull cannot be. Confirmed real count 2026-09-23:
~96,500 items carry an IMO number (``wdt:P458``), of which ~93,000 also carry a date
(``wdt:P729`` "service entry" or ``wdt:P571`` "inception"). Paginated ``ORDER BY ?item LIMIT
:data:`PAGE_LIMIT` OFFSET n`` (unordered offset pagination on a result this size is not
reproducible across pages); a 10,000-row page took ~8-13s in testing, well inside WDQS's ~60s
query timeout.

**Real quirks confirmed against a live query, not assumed:**

* 17 of ~96,500 raw IMO literals are not exactly 7 digits -- Wikidata's ``P458`` is free text, not
  format-constrained. These are dropped by re-validating the IMO check digit with
  :data:`process.identity.VALID_IMO_SQL` (the same SQL every other IMO-bearing source in this
  project is validated with), not trusted as-is.
* A meaningful number of items carry only ``P571`` (inception), no ``P729`` (service entry) --
  the ``P571`` fallback path is real, not a hypothetical.
* A small number of distinct IMO values are attached to more than one Wikidata item (duplicate
  ship entries, or two items that happen to share a mis-keyed IMO). See reconciliation below.
* Wikidata date statements carry their own precision (year-only statements render as
  ``YYYY-01-01T00:00:00Z``); since this module only ever extracts the YEAR component, that lost
  month/day precision never affects :data:`build_year`.
* A ``P729``/``P571``/``P458`` statement marked "unknown value" (Wikidata's "somevalue") surfaces
  in the SPARQL binding as a blank-node genid URI (``type != "literal"``), not a date/IMO literal
  -- confirmed live 2026-09-23 (real example: Q12329788, whose P729 has both a real-dated
  statement AND a separate "unknown value" one, so the same item appears twice in the raw rows).
  :func:`_parse_optional_date` treats this the same as the OPTIONAL being absent; an IMO in this
  shape is left as the literal genid string, which simply fails :data:`process.identity.
  VALID_IMO_SQL`'s digit regex downstream like any other malformed value, so it needs no special
  case in :func:`_parse_binding`.

**Reconciliation** (``_resolve_r3_availability`` and friends in ``model.baseline`` consume the
output; this module only produces it). Per Wikidata item: ``date_source`` is ``"P729"`` if present,
else ``"P571"``; ``has_date_conflict`` flags when both are present and disagree by more than a
year. Per IMO (an IMO can map to more than one item): if every item's resolved year agrees within
±1 year, ``build_year`` is their minimum (the earliest, most conservative reading); if they
disagree by more than a year, ``build_year`` is NULL and ``is_ambiguous`` is true -- never an
arbitrary pick between conflicting sources. An IMO with no date at all also gets ``build_year``
NULL, but with ``is_ambiguous`` false (no data, not disagreement).

**Output.** Two files: ``data/reference/wikidata_ships_raw.parquet`` (every raw (item, imo, p729,
p571) row, unreconciled, for auditability) and ``data/reference/ship_build_year.parquet`` (one row
per valid-checksum IMO: ``imo, build_year, date_source, n_wikidata_items, is_ambiguous,
has_date_conflict, retrieved_at, git_sha``) -- the file ``model.baseline`` actually joins against.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import httpx

from process.identity import VALID_IMO_SQL
from process.partitions import atomic_write_parquet, git_sha

logger = logging.getLogger(__name__)

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = (
    "maritime-fraud-signals/1.0 "
    "(https://github.com/PabloMarpar/maritime-fraud-signals; research project)"
)
PAGE_LIMIT = 10_000

BASE_QUERY = """SELECT ?item ?imo ?p729 ?p571 WHERE {
  ?item wdt:P458 ?imo .
  OPTIONAL { ?item wdt:P729 ?p729 }
  OPTIONAL { ?item wdt:P571 ?p571 }
} ORDER BY ?item"""

REFERENCE_ROOT = Path("data/reference")
RAW_PATH = REFERENCE_ROOT / "wikidata_ships_raw.parquet"
BUILD_YEAR_PATH = REFERENCE_ROOT / "ship_build_year.parquet"


class WikidataSchemaError(ValueError):
    """Raised when a fetched WDQS payload/binding doesn't match the shape confirmed live
    2026-09-23 (see module docstring) -- fails loudly with the offending data attached, rather
    than silently mis-parsing a public, unversioned, third-party endpoint.
    """


@dataclass(frozen=True)
class WikidataShipRow:
    """One raw (item, imo, dates) row exactly as WDQS returns it -- unreconciled, unvalidated."""

    wikidata_id: str
    imo_raw: str
    service_entry: datetime | None  # P729
    inception: datetime | None  # P571


def _extract_bindings(payload: Any) -> list[dict[str, Any]]:
    try:
        bindings = payload["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise WikidataSchemaError(
            f"Expected results.bindings in the SPARQL JSON payload, got: {payload!r}"
        ) from exc
    if not isinstance(bindings, list):
        raise WikidataSchemaError(f"results.bindings was not a list: {bindings!r}")
    return bindings


def _parse_optional_date(value: dict[str, Any] | None) -> datetime | None:
    if value is None:
        return None
    if value.get("type") != "literal":
        # A P729/P571 statement marked "unknown value" (Wikidata's "somevalue") surfaces here as
        # a blank-node genid URI, not a date literal -- confirmed live 2026-09-23 (real example:
        # Q12329788). A real, documented case of the RDF data model, not a malformed response;
        # treated the same as the OPTIONAL being absent entirely, not an error.
        return None
    raw = value.get("value")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WikidataSchemaError(f"Unparseable date literal {raw!r}") from exc


def _parse_binding(binding: dict[str, Any]) -> WikidataShipRow:
    try:
        item_uri = binding["item"]["value"]
        imo_raw = binding["imo"]["value"]
    except (KeyError, TypeError) as exc:
        raise WikidataSchemaError(f"Binding missing item/imo value: {binding!r}") from exc
    wikidata_id = item_uri.rsplit("/", 1)[-1]
    return WikidataShipRow(
        wikidata_id=wikidata_id,
        imo_raw=imo_raw,
        service_entry=_parse_optional_date(binding.get("p729")),
        inception=_parse_optional_date(binding.get("p571")),
    )


def _fetch_page(client: httpx.Client, offset: int, limit: int) -> dict[str, Any]:
    response = client.get(
        SPARQL_URL,
        params={"query": f"{BASE_QUERY} LIMIT {limit} OFFSET {offset}", "format": "json"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "unknown")
        raise RuntimeError(
            f"WDQS rate limit hit at offset={offset} (Retry-After={retry_after})."
        )
    response.raise_for_status()
    return response.json()


def fetch_raw_rows(
    client: httpx.Client | None = None, page_limit: int = PAGE_LIMIT
) -> list[WikidataShipRow]:
    """Fetch every (item, imo, p729, p571) row from WDQS, paginating until exhausted.

    Stops when a page returns fewer than page_limit rows, mirroring ingest.gfw.fetch_raw_entries
    -- no dependency on an unconfirmed "total" field.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=120.0)
    rows: list[WikidataShipRow] = []
    try:
        offset = 0
        while True:
            payload = _fetch_page(client, offset, page_limit)
            bindings = _extract_bindings(payload)
            rows.extend(_parse_binding(b) for b in bindings)
            if len(bindings) < page_limit:
                break
            offset += page_limit
    finally:
        if owns_client:
            client.close()
    return rows


def build_ship_build_year(
    out_path: Path = BUILD_YEAR_PATH,
    raw_out_path: Path = RAW_PATH,
    client: httpx.Client | None = None,
    page_limit: int = PAGE_LIMIT,
    force: bool = False,
) -> Path:
    """Fetch, land raw, and reconcile Wikidata ship build years into out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. See module
    docstring for the reconciliation rule.
    """
    if out_path.exists() and not force:
        logger.info("%s already exists, skipping (pass force=True / --force to re-fetch)", out_path)
        return out_path

    rows = fetch_raw_rows(client=client, page_limit=page_limit)
    logger.info("Fetched %d raw Wikidata ship row(s)", len(rows))

    retrieved_at = datetime.now(timezone.utc)
    retrieved_at_sql = f"TIMESTAMP '{retrieved_at.strftime('%Y-%m-%d %H:%M:%S.%f')}'"
    sha = git_sha()

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _raw (wikidata_id VARCHAR, imo_raw VARCHAR, "
            "p729 TIMESTAMP, p571 TIMESTAMP)"
        )
        if rows:
            con.executemany(
                "INSERT INTO _raw VALUES (?, ?, ?, ?)",
                [(r.wikidata_id, r.imo_raw, r.service_entry, r.inception) for r in rows],
            )

        atomic_write_parquet(
            con,
            "SELECT *, "
            f"{retrieved_at_sql} AS retrieved_at, '{sha}' AS git_sha "
            "FROM _raw ORDER BY wikidata_id",
            raw_out_path,
        )

        # Re-validate the IMO check digit -- P458 is free text, not format-constrained (17 of
        # ~96,500 real values fail this). VALID_IMO_SQL's text assumes a column literally named
        # `imo`, hence the aliasing subquery.
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _valid AS "
            "SELECT * FROM ("
            "  SELECT wikidata_id, imo_raw AS imo, p729, p571 FROM _raw WHERE imo_raw IS NOT NULL"
            f") WHERE {VALID_IMO_SQL}"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _resolved AS "
            "SELECT imo, "
            "CASE WHEN p729 IS NOT NULL THEN 'P729' WHEN p571 IS NOT NULL THEN 'P571' END "
            "  AS date_source, "
            "(p729 IS NOT NULL AND p571 IS NOT NULL "
            " AND abs(EXTRACT(year FROM p729) - EXTRACT(year FROM p571)) > 1) AS has_date_conflict, "
            "EXTRACT(year FROM COALESCE(p729, p571))::INTEGER AS build_year_candidate "
            "FROM _valid"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _by_imo AS "
            "SELECT imo, count(*) AS n_wikidata_items, "
            "min(build_year_candidate) AS min_year, max(build_year_candidate) AS max_year, "
            "bool_or(has_date_conflict) AS has_date_conflict, "
            "CASE WHEN bool_or(date_source = 'P729') THEN 'P729' "
            "     WHEN bool_or(date_source = 'P571') THEN 'P571' ELSE NULL END AS date_source "
            "FROM _resolved GROUP BY imo"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _final AS "
            "SELECT imo, n_wikidata_items, date_source, has_date_conflict, "
            "CASE WHEN max_year IS NULL THEN NULL "
            "     WHEN max_year - min_year <= 1 THEN min_year ELSE NULL END AS build_year, "
            "(max_year IS NOT NULL AND max_year - min_year > 1) AS is_ambiguous "
            "FROM _by_imo"
        )

        (n_imo, n_with_year, n_ambiguous) = con.execute(
            "SELECT count(*), count(build_year), count(*) FILTER (WHERE is_ambiguous) FROM _final"
        ).fetchone()
        logger.info(
            "Reconciled %d distinct valid-IMO Wikidata ship(s): %d with a usable build_year, "
            "%d ambiguous (conflicting items, excluded)",
            n_imo,
            n_with_year,
            n_ambiguous,
        )

        atomic_write_parquet(
            con,
            f"SELECT *, {retrieved_at_sql} AS retrieved_at, '{sha}' AS git_sha "
            "FROM _final ORDER BY imo",
            out_path,
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and reconcile Wikidata ship build years, for P4-1's naive-baseline "
        "age term."
    )
    parser.add_argument("--out-path", default=str(BUILD_YEAR_PATH), help="Output path for the reconciled table")
    parser.add_argument("--raw-out-path", default=str(RAW_PATH), help="Output path for the raw fetched rows")
    parser.add_argument(
        "--page-limit", type=int, default=PAGE_LIMIT, help="Rows per SPARQL page"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_ship_build_year(
        out_path=Path(args.out_path),
        raw_out_path=Path(args.raw_out_path),
        page_limit=args.page_limit,
        force=args.force,
    )


if __name__ == "__main__":
    main()
