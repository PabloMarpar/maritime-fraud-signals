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
  **Superseded by P2-1b as the coverage-vs-evasion signal** — `coverage_probability` had no
  discriminative range (62.5% of estimates exactly 1.0, none below 0.5). `grid.parquet` and the
  score itself are retired; `detect/coverage.py`'s code is untouched pending a future session that
  repurposes it to emit reporting-cadence percentiles instead (Tramo B, not yet started).
- **P2-1b done: cross-vessel corroboration (`detect/liveness.py`, `data/coverage/liveness.parquet`).**
  A tri-state verdict (`receiver_alive` / `area_dark` / `no_evidence`) for a silence: was some
  *other* vessel heard nearby, leave-one-out applied to both the concurrent count and the
  historical baseline (so a vessel can't exonerate itself with its own record). An `analyst-review`
  pass on the first draft caught three real bugs before anything was built on it — wrong baseline
  denominator, a vessel-hours/distinct-vessels unit mismatch letting one recurring vessel justify
  `area_dark`, and a returned field leaking post-window data — all fixed; see `docs/DECISIONS.md`
  for the full list and the fixes. Validated on the full real 30-day window, all 11,556 scoreable
  gaps (not a sample): 85.0/2.5/12.5% split; a duration-matched placebo control clears the
  pre-registered falsification bar for short-to-medium gaps (+19.0/+16.6 points) but only weakly
  for 12h+ gaps (+6.4 points) — **the signal is validated for short-to-medium AIS gaps, not for
  very long ones**, P2-2 must not lean on it alone there.
- **P2-2 done: detector 1, deliberate AIS gaps (`detect/gaps.py`, `data/detect/gaps.parquet`).**
  Every consecutive pair of voyages for the same MMSI in `voyages.parquet` is already a candidate
  gap (voyage segmentation only draws the boundary; classifying it was this module's job per
  `docs/DECISIONS.md`) — scored by calling `detect.liveness.liveness_verdict` once per gap on grid
  cells from both endpoints (where the vessel went dark, where it reappeared). A fixed table maps
  the verdict to a probability (`receiver_alive`=0.9, `area_dark`=0.15, `no_evidence`=0.5); output
  is a probability, not a boolean. Gaps of 12h or more get that probability clamped into an
  inconclusive `[0.4, 0.6]` band instead of trusted at face value, per P2-1b's finding that
  corroboration saturates past that duration. All thresholds are unvalidated defaults, same
  posture as `detect/liveness.py`'s own. Run over the real 30-day window this session — see the
  real-run finding below (most gaps are long enough to fall in the clamped, inconclusive band).
- **P2-3 done: detector 2, position spoofing (`detect/spoofing.py`, `data/detect/spoofing.parquet`).**
  Four independent rule-based checks over clean AIS, merged into one flat table (one row per
  detected event, not per vessel, mirroring P2-2's own output shape): **impossible speed**
  (consecutive same-mmsi pairs implying >50kn via great-circle distance/time), **on land**
  (point-in-polygon against a new land mask, eroded ~1.1km inward to absorb the mask's own
  coastline-generalization error), **synthetic circles** (a Kasa algebraic circle fit per voyage —
  candidates pre-filtered cheaply by point count/duration before any raw points are pulled — flagged
  on tight residual/radius plus a wide angular sweep), and **simultaneous positions** (same mmsi and
  timestamp, positions too far apart for one transponder). New supporting module
  `ingest/landmask.py` downloads and lands Natural Earth's land polygons once
  (`data/reference/land.parquet`) — see `docs/DATA_SOURCES.md`. 23 new tests (163 total), `ruff`
  clean. Every threshold in this detector (speed ceiling, erosion buffer, circle-fit gates,
  simultaneous-position distance) is an unvalidated default, same posture as P2-2's. **Not yet run
  against real data**, and the on-land spatial join's performance at the full 30-day scale has not
  been benchmarked — both outstanding, see Next up.
- 163 tests passing, `ruff` clean across the repo (as of this session's close).

- **`detect.gaps` run over the real 30-day window (2024-06-01..2024-07-01), sanity-checked.**
  74,546 candidate gaps scored (`data/detect/gaps.parquet`) — exactly `95,692 voyages - 21,146
  distinct mmsi`, confirming `candidate_gaps` captured every voyage-to-voyage boundary correctly.
  Took ~55 minutes wall-clock (heavily CPU-parallel via DuckDB) — much slower than expected for
  11k-ish calls to `liveness_verdict`; not yet worth optimizing since it only needs to run once per
  window, but note it if the working window ever grows. **The key finding**: gap duration is
  strongly right-skewed — median 22h, only ~20% of gaps are under the 12h threshold where P2-1b
  validated the corroboration signal as reliable. The other ~80% (mean duration in the 48-77h
  range depending on verdict, tail out to 695h, 9.3% exceed a full week) get their probability
  correctly clamped into the inconclusive `[0.4, 0.6]` band rather than trusted at face value — this
  is the clamp working as designed, but it means **for most real gaps, this detector alone is only
  weakly informative**; a very long silence needs a second signal (or accepting "inconclusive" as
  the honest answer) before P2-2 can say much about it. The near-window-length tail (898 gaps
  >500h, close to the full 720h window) likely reflects vessels with very sparse reporting overall
  (few voyages total) rather than one genuine multi-day evasion each — a data-quality nuance to
  keep in mind before reading those specific rows as strong fraud signal.
- 163 tests passing, `ruff` clean across the repo (as of this session's close).

## In progress

- Nothing running right now.

## Next up

1. **Run `detect.spoofing.build_spoofing_events` over the same real window** and sanity-check its
   output — first checking whether the on-land join is actually fast enough at real scale (millions
   of points) before assuming it is; `detect.gaps`'s real run above took much longer than expected,
   so budget time for this one too.
2. After that's sanity-checked, **P2-4, detector 3: ship-to-ship transfers** (GFW definition) is
   next in `tasks.json`.

## Blocked

- Nothing blocked.

## Open questions

- **`detect/gaps.py`'s verdict→probability table (0.9/0.15/0.5) and the 12h clamp band ([0.4, 0.6])
  are unvalidated guesses**, never checked against any labelled case (the real 30-day run only
  confirmed the code runs correctly and the clamp fires where expected, not that the numbers are
  right). Revisit once sanctions-list join (Phase 3) gives a handful of known-evasive vessels to
  sanity check against.
- **~80% of real candidate gaps (59,297 of 74,546) are ≥12h and land in the clamped, inconclusive
  band** — P2-2 alone says little about most real silences, only the shorter ~20%. Worth deciding,
  before leaning on this detector's output in `features/`, whether that's acceptable (report
  "inconclusive" honestly) or whether a second signal for long gaps is worth building first (e.g.
  destination/ETA consistency, or a same-route-history prior) — not decided yet, just surfaced by
  the real run.
- **`MIN_EXPECTED_CORROBORATORS` (3.0) and `MIN_BASELINE_VESSELS` (3) in `detect/liveness.py` are
  unvalidated defaults**, the same posture `detect/coverage.py`'s retired threshold had. Revisit if
  P2-2's output looks miscalibrated at the boundary between `area_dark` and `no_evidence`.
- **`exclude_mmsi` in `liveness_verdict` accepts a sequence but nothing yet resolves *which* MMSIs
  are the same vessel.** A vessel that reuses or spoofs a second MMSI could still build its own
  `area_dark` baseline under the other identity. Relevant once P2-5 (identity anomalies) exists;
  not a P2-2 blocker today given only 1 reused MMSI has been found in this window.
- Tramo B (`detect/coverage.py` → `cadence.parquet`, reporting-interval percentiles instead of the
  retired coverage score) is planned but not started.
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
