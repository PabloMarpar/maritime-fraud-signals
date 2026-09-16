# Data sources

All sources are open, free and publicly documented. Verified reachable on 2026-09-16.

## AIS positions

### Danish Maritime Authority — historical
- **URL:** `http://web.ais.dk/aisdata/`
- **Contents:** daily files, `aisdk-YYYY-MM-DD.csv` (possibly `.zip` — see verification note below),
  from 2006 onwards.
- **Access:** direct download, no registration.
- **Quirk:** the HTTPS certificate (`*.govcloud.dk`) is expired (confirmed 2026-09-16 via
  `openssl s_client`, `notAfter=Jun 12 23:59:59 2025 GMT`) and the HTTPS listener resets the
  connection once a request is sent, even with certificate verification disabled. Use plain HTTP,
  and document the choice — do not silently disable certificate verification.
- **Verification note (2026-09-16):** could not confirm the real filename/extension against a live
  request — the sandbox `ingest/dma.py` was built in blocks all outbound port 80 (confirmed with a
  control request to an unrelated, definitely-live plain-HTTP site, which timed out identically).
  `ingest/dma.py` therefore tries `aisdk-YYYY-MM-DD.zip` first, falls back to `.csv` on a 404, and
  sniffs the downloaded bytes for the zip magic number rather than trusting the extension. Whoever
  runs the first live download from a network that actually reaches port 80 should update this
  entry with the confirmed format.
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
