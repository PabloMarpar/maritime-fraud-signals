# Project state

_Last updated: 2026-09-17_

## Done

- Plan approved. Full plan lives at
  `C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`.
- Repository scaffolded: directory layout, continuity files, subagent definitions, `/retomar` and
  `/cerrar` commands, dependency list, licence, README.
- Data sources verified as open and reachable on 2026-09-16 (see `docs/DATA_SOURCES.md`).
- **Phase 0 complete.** `ingest/dma.py` downloads a day/date range of Danish AIS from the DMA's S3
  archive (the legacy `web.ais.dk` host is dead; see `docs/DECISIONS.md`) and lands Parquet
  partitioned by `date=YYYY-MM-DD`. `report/quicklook_map.py` renders a day's positions.
  `Dockerfile` + `.dockerignore` done: reproducible environment, `data/`/`outputs/` as volumes.
- **Phase 1 complete.** `process/clean.py`, `process/identity.py`, `process/tracks.py` — cleaning,
  MMSI↔IMO identity resolution, and voyage segmentation (6h gap threshold, parameterized).
  DuckDB-only, no pandas. `process/tracks.py` no longer writes `data/tracks/points.parquet` (see
  decision log); voyage membership is attached on demand via `attach_voyage_ids` (ASOF JOIN against
  `voyages.parquet`).
- **Download/process/discard pipeline built (`pipeline/manifest.py`, `pipeline/backfill.py`).**
  One day at a time: download → clean → verify retention ratio → discard the raw partition →
  record state in `data/manifest.json` (tracked in git, atomic writes). Resumable, with a disk
  guard (default 20 GB free) before each day. Only raw is discarded for now — cleaned days are kept
  deliberately; see `docs/DECISIONS.md`.
- **Phase 2's 30-day working window landed: 2024-06-01..2024-06-30, all 30 days cleaned, all 30
  raw partitions discarded, ~14 GB on disk.** `process/identity` and `process/tracks` rebuilt over
  the full range: 21,146 distinct MMSI, 4,881 with a valid IMO, 16,265 orphaned, **1 reused MMSI**
  (0 on the single day — reuse only shows up across a longer range, as expected), 95,692 voyages.
- **P2-1 done: the empirical coverage map (`detect/coverage.py`, `data/coverage/grid.parquet`).**
  Receive probability per 0.1° grid cell and `ship_type`, from the ratio of short (≤30min) to long
  gaps between a vessel's consecutive messages, each row carrying its build window and git SHA.
  Built over the full 30-day range: 9,265 cells, 42,968 (cell, ship_type) estimates with ≥20 pairs.
  **Flagged, not fully trustworthy yet** — see Open questions and `docs/DECISIONS.md`: the method
  can't see true coverage holes (no message ⇒ no row) and a deliberate AIS shutdown depresses its
  own cell's score, so 62.5% of estimates came back at exactly 1.0 and none below 0.5. Needs
  cross-vessel corroboration (or similar) before P2-2 can trust a low score as "just bad coverage."
- 97 tests passing, `ruff` clean across the repo (as of this session's close).

## In progress

- Nothing running right now.

## Next up

**P2-2, detector 1: deliberate AIS gaps.** Before leaning on `detect/coverage.py`'s
`coverage_probability` naively, address (or explicitly work around) the selection-bias/circularity
limitation logged in `docs/DECISIONS.md` — a low score there does not yet safely mean "just bad
coverage." One option worth trying first: cross-vessel corroboration (do *other* vessels report
normally from a cell while this one is dark?) instead of a single vessel's own short/long ratio.

## Blocked

- Nothing hard-blocked. Soft-blocked: P2-2 depends on either fixing P2-1's coverage methodology or
  deliberately designing around its documented bias — see Next up.

## Open questions

- **Is P2-1's coverage-map methodology (self-referential short/long gap ratio) good enough to ship,
  or does it need the cross-vessel-corroboration redesign before P2-2 is built on top of it?** Not
  yet decided — flagged this session, see `docs/DECISIONS.md`.
- **Meaning of the real schema's trailing `a, b, c, d` columns** (unlabelled in the source CSV,
  passed through untouched by every module so far) — likely AIS antenna/base-station diagnostic
  fields, not confirmed. Only worth resolving if a future detector needs them.
- **Voyage gap threshold (6h default in `process/tracks.py`)** is a defensible but unvalidated
  judgement call, not tuned against this project's own data. Revisit if P2-2 needs a specific value
  — it's a `gap_hours` parameter, not a hardcoded constant.
- **Whether the 30-day window needs extending for detector variety** (noisy P2-1 estimates, too few
  ship-to-ship candidates for P2-4, only 1 reused MMSI for P2-5) — no signal either way yet since
  no detector past P2-1 has been built. If so, extend with separate weeks spread across 2024 rather
  than more contiguous June days (see `docs/DECISIONS.md`).

## Disk budget — read before downloading more days

**~73 GB free** (of 931 GB) as of 2026-09-17. `data/clean/` for the 30-day window is ~14 GB.
`data/identity/`, `data/tracks/`, `data/coverage/` together are a few MB — the whole point of
reducing to aggregates. One raw day ≈ 507 MB, discarded immediately after cleaning by
`pipeline/backfill.py`. Phase 3-4's "years of depth" requirement (several validation cutoffs `T`,
each needing data before and after) should still be met by **sampling short windows around each
cutoff**, not downloading every day, and eventually by discarding cleaned days too once Phase 2's
detectors exist to define what's safe to reduce them to (deferred, see `docs/DECISIONS.md`).
