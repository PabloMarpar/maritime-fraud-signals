# Project state

_Last updated: 2026-09-16_

## Done

- Plan approved. Full plan lives at
  `C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`.
- Repository scaffolded: directory layout, continuity files, subagent definitions, `/retomar` and
  `/cerrar` commands, dependency list, licence, README.
- Data sources verified as open and reachable on 2026-09-16 (see `docs/DATA_SOURCES.md`).
- **Phase 0 complete.** `ingest/dma.py` downloads a day/date range of Danish AIS from the DMA's S3
  archive (the legacy `web.ais.dk` host is dead; see `docs/DECISIONS.md`) and lands Parquet
  partitioned by `date=YYYY-MM-DD`. Real day landed and verified: **2024-06-05**, 17,239,519 rows,
  4,878 distinct MMSI, format confirmed `.zip`. `report/quicklook_map.py` rendered it —
  `outputs/quicklook_2024-06-05.png` — dense cluster over Danish/Baltic waters as expected, plus a
  few far-flung outlier points left untouched (Phase 2 spoofing-detector material, not a bug).
  `Dockerfile` + `.dockerignore` done: reproducible environment, `data/`/`outputs/` as volumes.
- **Phase 1 complete.** All three processing modules built and validated against the real
  2024-06-05 day (not just synthetic fixtures), DuckDB-only, no pandas, matching `ingest/dma.py`'s
  conventions:
  - `process/clean.py` (P1-1): drops invalid MMSI, impossible coordinates, duplicate messages. Real
    day: 17,239,519 → 10,429,200 rows (mostly duplicate broadcasts, expected for AIS).
  - `process/identity.py` (P1-2): MMSI↔IMO resolution table (`data/identity/mmsi_imo.parquet`),
    flags orphaned and reused MMSI. Real day: 4,519 MMSI, 1,304 with a valid IMO, 3,215 orphaned, 0
    reused (single day — reuse only shows up across a longer range).
  - `process/tracks.py` (P1-3): per-vessel track reconstruction and voyage segmentation on a
    6-hour gap threshold (`data/tracks/points.parquet`, `data/tracks/voyages.parquet`). Real day:
    4,519 vessels, 4,751 voyages, ~2,195 points/voyage average.
  - P1-4 (synthetic unit tests per pathology) satisfied by the above: 54 tests total, all passing,
    `ruff` clean across the repo.

## In progress

- Nothing running right now.

## Next up

**Phase 2 (the five detectors) starts.** First task: **P2-1**, the empirical coverage map
(receive-probability per sea grid cell and vessel class) — the core technical piece the other
detectors lean on, especially P2-2 (deliberate gaps), which needs to tell "AIS off" apart from
"outside receiver coverage."

Before widening the date range for Phase 2 pathology variety (a few weeks, per
`docs/DECISIONS.md`), note the disk constraint below — plan the download/process/discard cycle
rather than accumulating raw days.

## Blocked

- Nothing blocked.

## Open questions

- ~~Which date to use as the first sample day.~~ Decided: **2024-06-05**. See `docs/DECISIONS.md`.
- ~~Whether the Danish HTTPS certificate issue requires a documented `verify=False` or plain
  HTTP.~~ Superseded — the archive moved to S3, reached over verified HTTPS. See
  `docs/DECISIONS.md`.
- ~~Whether the real DMA file is `.zip` or `.csv`.~~ Decided: `.zip`, confirmed live.
- **Meaning of the real schema's trailing `a, b, c, d` columns** (unlabelled in the source CSV,
  passed through untouched by every module so far) — likely AIS antenna/base-station diagnostic
  fields, not confirmed. Only worth resolving if a future detector needs them.
- **Voyage gap threshold (6h default in `process/tracks.py`)** is a defensible but unvalidated
  judgement call, not tuned against this project's own data. Revisit if Phase 2's gap detector
  (P2-2) needs a specific value — it's a `gap_hours` parameter, not a hardcoded constant.

## Disk budget — read before downloading more days

One raw day ≈ 507 MB (Parquet); one cleaned day ≈ 444 MB more. This machine had **~79 GB free**
(of 931 GB) as of 2026-09-16 — continuous multi-year download does not fit (3 years ≈ 542 GB for
raw alone). Phase 3-4's "years of depth" requirement (several validation cutoffs `T`, each needing
data before and after) should be met by **sampling short windows around each cutoff**, not
downloading every day — and by discarding raw/cleaned intermediates once aggregated into the
(much smaller) vessel-month panel, rather than keeping years of raw positions on disk at once.
