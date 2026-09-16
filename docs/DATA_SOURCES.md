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
on it. Always capture it.

| Source | Format | URL |
|---|---|---|
| OFAC (US) | `sdn_enhanced.xml` | `https://ofac.treasury.gov/sanctions-list-service` |
| EU consolidated list | XML | EU sanctions portal |
| UK Sanctions List | XML / CSV | `https://search-uk-sanctions-list.service.gov.uk/` |

Note: the UK moved to a single consolidated list on 2026-01-28. The former OFSI Consolidated List is
closed and no longer updated — do not use it.

## Cross-checking

### Global Fishing Watch Events API
- **URL:** `https://globalfishingwatch.org/our-apis/`
- **Contents:** encounters, loitering and AIS-off events computed by a third party.
- **Access:** free token.
- **Role:** independent cross-check for our own detectors, **not** a primary source. Measuring
  agreement with their published definitions is itself a reportable result.
- **Their definitions:** an encounter is two vessels within 500 m for at least 2 hours at a median
  speed under 2 knots, at least 10 km from a coastal anchorage. Loitering is an average speed under
  2 knots at least 20 nautical miles from shore.

### DTU Data — academic benchmark
- *AIS Trajectories from Danish Waters for Abnormal Behavior Detection*
- `https://data.dtu.dk/collections/AIS_Trajectories_from_Danish_Waters_for_Abnormal_Behavior_Detection/6287841`
