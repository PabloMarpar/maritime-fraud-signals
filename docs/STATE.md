# Project state

_Last updated: 2026-09-19_

## Done

- Plan approved; full plan at
  `C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`. Repository
  scaffolded (layout, continuity files, subagents, `/retomar`/`/cerrar`, Docker).
- **Phase 0**: `ingest/dma.py` downloads Danish AIS from the DMA's S3 archive (`web.ais.dk` is
  dead); `report/quicklook_map.py` renders a day.
- **Phase 1**: `process/clean.py`/`identity.py`/`tracks.py` — cleaning, MMSI↔IMO resolution,
  voyage segmentation (6h gap, parameterized). DuckDB-only, no pandas.
- **Pipeline**: `pipeline/manifest.py`/`backfill.py` — download → clean → discard-raw, resumable,
  disk-guarded (default 20GB free).
- **30-day working window landed**: 2024-06-01..2024-06-30, cleaned (~14GB on disk), 21,146
  distinct MMSI, 95,692 voyages, 1 reused MMSI.
- **P2-1 / P2-1b done**: the coverage map (`detect/coverage.py`) had no discriminative range and
  was superseded by cross-vessel corroboration (`detect/liveness.py` — tri-state verdict
  `receiver_alive`/`area_dark`/`no_evidence`, leave-one-out on both the concurrent count and the
  historical baseline). Validated on the real window: 85.0/2.5/12.5% split; reliable for
  short-to-medium gaps, only weakly for 12h+ ones. Three real bugs found by `analyst-review` before
  anything was built on it. See `docs/DECISIONS.md`.
- **P2-2 done**: `detect/gaps.py` — every voyage-to-voyage silence scored via
  `liveness_verdict`, a verdict→probability table, 12h+ gaps clamped to an inconclusive band.
  Real run: 74,546 candidate gaps, median duration 22h, ~80% land in the clamped band — this
  detector alone is only weakly informative for most real gaps.
- **P2-3 done**: `detect/spoofing.py` — four checks (impossible speed, on land, synthetic
  circles, simultaneous positions). Real run (numbers corrected during P2-4, see below):
  `impossible_speed` 5,499, `on_land` 10,092,010, `synthetic_circle` 93,
  `simultaneous_position` 1,158 — 10,098,760 events total. `on_land`'s volume is legitimate
  long-duration dockside dwell, not a bug. Several real-scale performance/correctness bugs found
  and fixed along the way (bbox quantiles, bulk-SQL circle fit, a speed-check time gate, land-mask
  chunking) — full list in `docs/DECISIONS.md`.
- **P2-4 done**: `detect/anchorages.py` (empirical coastal-anchorage mask, derived from our own
  AIS, never downloaded — two anti-circularity safeguards from P2-1b's lessons) +
  `detect/sts.py` (the ship-to-ship-transfer detector: episode segmentation fixing a 647-hour
  false-encounter artifact, GFW's structural hard gates, a six-term rule-based confidence
  heuristic, not a calibrated probability). Found and fixed a DuckDB `ST_Distance_Sphere`
  argument-order bug along the way that also affected P2-3 (re-ran its real numbers, see above).
  Real run: 1,255,460 candidate episodes → 1,689 survive the hard gates, mean confidence 0.382,
  sanity-checked against the pre-registered expectations (no Belts, few tankers, duration decay
  working). 202 tests total, `ruff` clean. Full detail in `docs/DECISIONS.md`.

## In progress

- Nothing in progress. P2-4 is fully closed.

## Next up

1. **P2-5, detector 4: identity anomalies** (flag, name, MMSI changes) is next in `tasks.json`.

## Blocked

- Nothing blocked.

## Open questions

- **`detect/sts.py`'s confidence weights (0.35/0.20/0.20/0.10/0.10/0.05) are unvalidated
  judgement calls.** The real run showed a service-vessel pair (Tug/Tug) can outscore its
  ship-type penalty when the other five discriminators are strong (0.82 seen) — observed and left
  as-is, not retuned without labels. Revisit once P2-7 (GFW Events API agreement) gives a
  comparison point.
- **~2.8% of `detect.sts`'s real survivors (47 of 1,689) run 24h or longer, tail out to 466h** —
  likely a small-harbour or two-vessel-mooring residue below `detect.anchorages`'s
  `MIN_DISTINCT_VESSELS` bar. The duration-decay confidence term suppresses these in practice, so
  not urgent, but worth a dedicated histogram before `features/` consumes this table.
- **`detect/anchorages.py`'s `distance_to_land_m` still has a smaller, un-fixed approximation**:
  `ST_ClosestPoint` is planar, not spherical — up to ~20% overestimate at these latitudes, biasing
  `is_coastal` toward excluding a cell, never toward wrongly protecting a real transfer site. Not
  fixed; would need a densified boundary or a locally-scaled coordinate space.
- **`detect/gaps.py`'s verdict→probability table and the 12h clamp band are unvalidated guesses**,
  never checked against a labelled case. Revisit once Phase 3's sanctions-list join gives known-
  evasive vessels to sanity check against.
- **`MIN_EXPECTED_CORROBORATORS`/`MIN_BASELINE_VESSELS` in `detect/liveness.py` are unvalidated
  defaults.** Revisit if P2-2's output looks miscalibrated at the `area_dark`/`no_evidence`
  boundary.
- **`exclude_mmsi` in `liveness_verdict` doesn't resolve which MMSIs are the same vessel.**
  Relevant once P2-5 (identity anomalies) exists; not a blocker today (only 1 reused MMSI found).
- Tramo B (`detect/coverage.py` → `cadence.parquet`, reporting-cadence percentiles) is planned but
  not started.
- **Meaning of the real schema's trailing `a, b, c, d` columns** — likely AIS diagnostic fields,
  not confirmed. Only worth resolving if a future detector needs them.
- **Voyage gap threshold (6h default in `process/tracks.py`)** is defensible but unvalidated —
  it's a `gap_hours` parameter, not a hardcoded constant, if it needs revisiting.
- **Whether the 30-day window needs extending for detector variety** (only 1 reused MMSI for
  P2-5) — no signal either way yet. If so, extend with separate weeks spread across 2024 rather
  than more contiguous June days (see `docs/DECISIONS.md`).

## Disk budget — read before downloading more days

**~80 GB free** (of 931 GB) as of 2026-09-19 (not re-measured this session; last real check
2026-09-18). `data/clean/` for the 30-day window is ~14 GB.
`data/identity/`, `data/tracks/`, `data/coverage/` together are a few MB; `data/detect/` (gaps,
spoofing, sts) is well under 200 MB total — the whole point of reducing to aggregates. One raw day
≈ 507 MB, discarded immediately after cleaning by `pipeline/backfill.py`. Phase 3-4's "years of
depth" requirement (several validation cutoffs `T`, each needing data before and after) should
still be met by **sampling short windows around each cutoff**, not downloading every day, and
eventually by discarding cleaned days too once Phase 2's detectors exist to define what's safe to
reduce them to (deferred, see `docs/DECISIONS.md`).
