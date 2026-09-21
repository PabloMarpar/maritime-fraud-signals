# Data sources

All sources are open, free and publicly documented. Verified reachable on 2026-09-16.

## AIS positions

### Danish Maritime Authority — historical
- **URL:** `https://s3.eu-central-1.amazonaws.com/aisdata.ais.dk/{year}/aisdk-{YYYY-MM-DD}.zip`
  (path-style S3 URL — see quirk below). Confirmed live 2026-09-16.
- **Contents:** daily files, `{year}/aisdk-YYYY-MM-DD.zip`, from 2006 onwards (older years may ship
  monthly archives instead of daily — not yet needed, revisit when Phase 3-4 widens the range).
- **Access:** direct download, no registration.
- **Quirk (superseded, kept for history):** the legacy host `http://web.ais.dk/aisdata/` documented
  here previously no longer answers on port 80 from any network tested, and its HTTPS listener
  serves a certificate (`*.govcloud.dk`) that doesn't match the hostname and then resets the
  connection. The archive has since moved to an S3 bucket, `aisdata.ais.dk`. Do not resurrect the
  `web.ais.dk` URL.
- **Quirk (current):** the bucket name contains dots, so the virtual-hosted-style URL
  (`aisdata.ais.dk.s3.eu-central-1.amazonaws.com`) fails TLS hostname verification against Amazon's
  wildcard certificate. Use the **path-style** URL (`s3.eu-central-1.amazonaws.com/aisdata.ais.dk/...`)
  instead — it verifies cleanly over HTTPS. Do not "fix" the virtual-hosted form with `verify=False`.
- **Schema quirk (confirmed live 2026-09-16):** the real CSV header's first column is literally
  `# Timestamp` (stray leading `#`, an export-tool artifact) rather than `Timestamp`.
  `ingest.dma._normalise` strips it so the landed column is plain `timestamp` like every other
  field. Real schema has 22 CSV columns plus 4 unlabelled trailing ones (`a, b, c, d` after
  normalisation) whose meaning is not yet confirmed — likely AIS antenna/base-station diagnostic
  fields; revisit if they turn out to matter.
- **Verified 2026-09-16 (real download, 2024-06-05):** format is `.zip`, 610 MB compressed,
  17,239,519 rows, 4,878 distinct MMSI. The question of `.zip` vs `.csv` is closed.
- **Why this source:** dense coverage of the Danish straits, the chokepoint through which all
  Baltic oil traffic must pass.

### AISStream.io — live
- **URL:** `wss://stream.aisstream.io/v0/stream`
- **Contents:** real-time global AIS, filtered by arbitrary bounding boxes.
- **Access:** free API key. Store it in `.env`, never in the repository.
- **Limits:** 3 connections per account and per IP. One subscription message within 3 seconds of
  connecting. Up to 200 MMSI filters per subscription.
- **Important:** since September 2026, uncompressed connections are subject to per-user bandwidth
  limits and excess messages are dropped. **Enable compression from the start.**

### Kystverket (Norway) — optional future extension
- **URL:** `https://ais-public.kystverket.no/`
- **Licence:** NLOD. No registration required.

## Sanctions lists (the labels)

The designation **date** matters as much as the vessel identity — forward-looking validation depends
on it. Always capture it. All three confirmed live 2026-09-21; ingested by `ingest/sanctions.py`
(P3-1), landed combined at `data/reference/sanctions.parquet` (`source` column distinguishes
origin). Real run 2026-09-21: OFAC 1,540 vessels (1,528 with IMO, 37 with no flag), EU 2 vessels
(both with IMO and flag), UK 663 vessels (662 with IMO, 143 with no flag) — 2,205 total. Every
`designation_date_precision` observed live was `"day"`; OFAC's partial-date handling (month/year
only) is implemented and unit-tested but never exercised by the current live snapshot.

### OFAC (US) — `fetch_ofac`
- **URL:** `https://www.treasury.gov/ofac/downloads/sanctions/1.0/sdn_advanced.xml` — 302-redirects
  to `sanctionslistservice.ofac.treas.gov`, then to a signed S3 URL; no auth; ~127MB. Confirmed live
  2026-09-21.
- **Format:** the "Advanced XML" SDN export. Parsed with a single streaming
  `xml.etree.ElementTree.iterparse` pass (never a full in-memory DOM) — see `ingest/sanctions.py`'s
  module docstring for the full structural detail (vessel profiles are `PartySubTypeID=1`, resolved
  dynamically from the file's own `<ReferenceValueSets>` rather than hardcoded; IMO and designation
  date each live in a separate top-level container, joined by `IdentityID`/`ProfileID`).
- **Quirk:** a vessel's primary `Alias` can carry both a Latin-script and a transliterated (e.g.
  Cyrillic) `DocumentedName`; the Latin one is preferred. A profile can carry more than one
  `SanctionsEntry`/"Created" `EntryEvent` (93 of 19,579 real profiles did on 2026-09-21) — the
  earliest is taken as `designation_date`.
- **Real run 2026-09-21:** 1,540 vessels, 1,528 with a valid IMO, designation dates spanning
  1989-01-05 to 2026-08-24, all `"day"` precision.

### EU consolidated list (Financial Sanctions Files) — `fetch_eu`
- **URL used:** `https://data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv` — an
  OpenSanctions mirror of the official EU FSF feed, NOT the official `webgate.ec.europa.eu`
  endpoint (see quirk below). Confirmed reachable 2026-09-21, 307-redirects to a dated CDN URL each
  time (the stable `data.opensanctions.org` URL is always hit fresh).
- **Quirk (why the mirror):** the official endpoint
  (`https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content`) returns
  HTTP 403 without a registered access token — confirmed by direct curl and a cookie/session
  handshake attempt on 2026-09-21, both failed. Registering for official access is out of scope for
  this project; see `docs/DECISIONS.md` for the substitution reasoning.
- **Format:** CSV, columns `id,schema,name,aliases,birth_date,countries,addresses,identifiers,
  sanctions,phones,emails,program_ids,dataset,first_seen,last_seen,last_change`. Vessel rows are
  `schema == "Vessel"`. IMO comes from the `identifiers` column (`IMO1234567`, no space, possibly
  mixed with other identifier types, `;`-separated). Flag is the (lowercase ISO2) `countries`
  column.
- **Quirk:** no dedicated designation-date column — extracted by regex (`YYYY-MM-DD`, earliest if
  several) from the free-text `sanctions` column (e.g. `"PRK - 2022/2429 (OJ L318I) - 2022-12-12"`).
  `first_seen` is deliberately never used for this: it is OpenSanctions' own ingestion timestamp,
  not a designation date, and using it would be a temporal leak.
- **Real run 2026-09-21:** only 2 vessel rows in the entire feed (both North Korea-flagged, both
  with IMO).

### UK Sanctions List — `fetch_uk`
- **URL:** `https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv` — confirmed reachable
  2026-09-21, no auth, ~50MB, no redirect. The UK moved to this single consolidated list on
  2026-01-28; the former OFSI Consolidated List is closed and no longer updated — do not use it.
- **Format:** CSV, but the first line is `Report Date: DD-Mon-YYYY`, not the header — skipped before
  parsing. A vessel row is `Designation Type == "Ship"` (NOT `Type of entity`, which is empty for
  every ship row — a real trap). IMO comes from `IMO number` (usually `IMO1234567`, no space, but
  observed WITHOUT the prefix on at least one real row).
- **Quirk:** a single vessel (`Unique ID`) spans multiple rows — one per address line, and the name
  itself varies row-to-row too (`Name 6`, `Name type` == `"Primary name"` for exactly one row per
  group, `"Alias"` for the rest). Grouped by `Unique ID`; the earliest `Date Designated` across the
  group is taken as `designation_date`, with `program` (`Regime Name`) tied to that specific row.
- **Real run 2026-09-21:** 663 vessels, 662 with a valid IMO, designation dates spanning
  2017-10-03 to 2026-08-06.

## Cross-checking

### Global Fishing Watch Events API
- **URL:** `https://globalfishingwatch.org/our-apis/`
- **Contents:** encounters, loitering and AIS-off events computed by a third party.
- **Access:** free token, requested at `https://globalfishingwatch.org/our-apis/tokens`. Their own
  page describes the process as: register a GFW account, request an API key, agree to the terms of
  use (attribute GFW in any publication) and to participate in follow-up surveys. Nothing on that
  page requires a registered legal organisation — GFW states its APIs are used by "government
  institutions and academia to nonprofits and small technology firms", which reads as inclusive of
  individuals/independent projects; any "organisation" field in the signup form is expected to be
  free text, not a gate. Not yet confirmed by actually submitting the form (blocked on the
  session that needed it not having a token — see `docs/DECISIONS.md`).
- **Role:** independent cross-check for our own detectors, **not** a primary source. Measuring
  agreement with their published definitions is itself a reportable result (P2-7,
  `detect/sts_agreement.py`).
- **Their definitions:** an encounter is two vessels within 500 m for at least 2 hours at a median
  speed under 2 knots, at least 10 km from a coastal anchorage. Loitering is an average speed under
  2 knots at least 20 nautical miles from shore.
- **Scope quirk:** the encounters dataset only covers vessel-type pairs GFW classifies as
  fishing-economy activity (fishing-fishing, fishing-carrier, fishing-support, fishing-bunker,
  tanker-fishing, carrier-bunker, support-bunker) — not every vessel encounter GFW's own AIS
  pipeline could in principle see. `detect.sts` has no such restriction. See
  `detect/sts_agreement.py`'s module docstring for why this matters when reading P2-7's numbers.
- **API contract** (endpoint/auth/pagination), confirmed against live documentation on
  2026-09-21 — see `ingest/gfw.py`'s module docstring for the full detail:
  - `GET https://gateway.api.globalfishingwatch.org/v3/events`, `Authorization: Bearer <token>`.
  - `datasets[0]=public-global-encounters-events:latest`, `types[0]=ENCOUNTER`,
    `start-date`/`end-date` (`YYYY-MM-DD`), `limit`/`offset` pagination (200/page observed max).
  - No bounding-box/geometry parameter at the raw HTTP level (GFW's SDKs support one; this
    project calls the endpoint directly and filters client-side, see `ingest/gfw.filter_bbox`).
  - Documented caps: 50,000 requests/day, 1,500,000/month; exceeding either returns HTTP 429.
  - **Response JSON shape, confirmed 2026-09-21 against a live call (see `docs/DECISIONS.md`).**
    One page: `{"entries": [...], "limit", "offset", "nextOffset", "total", "metadata"}`. Every
    encounter is reported TWICE, mirrored, as two entries sharing an id up to a trailing
    `.1`/`.2` suffix — but each entry already carries BOTH vessels (`vessel.ssvid` for the
    reporting side, `encounter.vessel.ssvid` for the other), so the two are duplicates, not
    complementary halves. The vessel-id field is `ssvid` (a string), not `mmsi`; position is
    nested at `position.lat`/`position.lon`, not top-level. `encounter.type` (e.g.
    `"fishing-fishing"`, `"tanker-fishing"`) carries GFW's own vessel-type-pair classification —
    the ground truth for the scope quirk noted above, more precise than any proxy computed from
    our own AIS ship-type field. `datasets[0]=...:latest` resolved server-side to
    `public-global-encounters-events:v4.0` on this call.

### DTU Data — academic benchmark
- *AIS Trajectories from Danish Waters for Abnormal Behavior Detection*
- `https://data.dtu.dk/collections/AIS_Trajectories_from_Danish_Waters_for_Abnormal_Behavior_Detection/6287841`

## Reference geometry (not AIS, not a label)

### Natural Earth — land polygons
- **URL:** `https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip` (primary), mirrored at
  `https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip` (fallback). Both verified live
  2026-09-17, identical content (3,269,070 bytes, `application/zip`).
- **Contents:** a shapefile (`.shp`/`.shx`/`.dbf`/`.prj`), 11 features, at 1:10,000,000 scale.
- **Access:** direct download, no registration, public domain.
- **Role:** `ingest/landmask.py` lands this once as `data/reference/land.parquet` (DuckDB spatial's
  `ST_Read`, native `GEOMETRY` column round-trips through Parquet directly). `detect/spoofing.py`'s
  "position on land" check is the only consumer.
- **Quirk (verified 2026-09-17):** this is a generalized coastline, not a precise boundary. A
  genuinely on-land point in central Copenhagen (55.6761, 12.5683) sits ~205m *outside* the polygon.
  `detect/spoofing.py` compensates with a ~1.1km inward erosion buffer before testing containment —
  see `docs/DECISIONS.md`. Do not treat this dataset as ground truth right at a coastline.
