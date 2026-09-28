# Project state

_Last updated: 2026-09-28_

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
- **P2-5 done**: `detect/identity_anomalies.py` (8 kinds: `no_valid_imo`, `name_change`/
  `name_flapping`, `callsign_change`/`callsign_flapping`, `reused_mmsi`, `shared_imo` with
  `cross_mid`, `shared_identity`) + `detect/identity_anomalies.build_vessel_links`
  (`data/identity/vessel_links.parquet`) + new `process/mid.py` (MID→flag lookup). Reframed per
  P1-2: gates on "ever validly Tanker/Cargo without a valid IMO", not the global 71%
  orphaned-MMSI rate. Real run: 265 events (`no_valid_imo` 91, `name_change` 47, `name_flapping`
  60, `callsign_change` 12, `callsign_flapping` 22, `reused_mmsi` 1 — matching `process.identity`'s
  own checksum-validated count exactly — `shared_imo` 18 with 14 `cross_mid`, `shared_identity`
  14), 28 MMSI linked into 14 vessel groups, ~4.5 min wall-clock. A dedicated review pass found and
  fixed four real temporal-leakage bugs before close (whole-window judgements need
  `knowable_at = window_end`, a group-size gate must be evaluated as-of each pair's own
  `knowable_at`, `build_vessel_links` needed a timestamp at all) — full detail in
  `docs/DECISIONS.md`. 32 new tests, 242 total, `ruff` clean.

- **P2-6 done**: `detect/behaviour.py` — detector 5, declared-behaviour contradictions.
  `destination_course_mismatch` (declared destination matched to a port by name; flags a voyage
  whose median COG-to-bearing deviation exceeds 90° over ≥10 qualifying points, ≥20km from the
  port, ≥2.0kn SOG — COG used instead of heading, 91% vs 76% coverage in this window) and
  `draught_change_unexplained` (≥2m median-draught change between consecutive voyages with no
  port/anchorage or ship-to-ship-transfer evidence in the gap). Both scoped to MMSI with a valid
  IMO, reusing P2-5's reframing. Real 30-day run: 916 events (152 `destination_course_mismatch`
  across 117 MMSI, 764 `draught_change_unexplained` across 586 MMSI), ~55s wall-clock. A dedicated
  review pass found and fixed three real bugs before close — an impossible `knowable_at` for the
  draught check (now `window_end`, a whole-window judgement, not gap-bounded as first drafted), an
  anchorage-evidence self-exoneration circularity (now leave-one-out, mirroring `detect.sts`'s own
  fix from P2-1b), and ~24-30% of the course check's events being stationary-vessel COG noise (now
  gated on SOG ≥2.0kn). 13 tests, `ruff` clean. Full detail in `docs/DECISIONS.md`.

- **P2-7 done**: `ingest/gfw.py` (fetch/normalise/bbox-filter GFW Events API encounters) +
  `detect/sts_agreement.py` (matching + agreement measurement against `detect.sts`), 26 tests,
  `ruff` clean. Real run over the 30-day window: 116,966 raw global entries -> 58,359 unique GFW
  encounters worldwide, only 6 inside the Danish/Baltic bbox for the whole month. Agreement: 0/1,689
  detect.sts events matched (0/177 in the fishing-ship-type subset), 0/6 GFW encounters matched.
  Investigated the one shared-vessel-pair case by hand: DMA's own AIS shows the pair drifting from
  374m to 2,174m apart across GFW's reported ~2h20m encounter window, never sustaining GFW's own
  <=500m criterion — a cross-provider position discrepancy, not a code or threshold bug. Headline
  reads as "scope mismatch dominates" (GFW's fishing-economy dataset has almost no data in this
  region/vessel-type combination to agree or disagree with), not as validation or invalidation of
  detect.sts. Full detail in `docs/DECISIONS.md`.

- **P3-1 done**: `ingest/sanctions.py` — three fetchers (`fetch_ofac`, `fetch_eu`, `fetch_uk`),
  normalised to a shared `SanctionedVessel` dataclass, landed combined at `data/reference/
  sanctions.parquet` (`source` column distinguishes origin). OFAC parsed with a single streaming
  `iterparse` pass (never a full DOM), the four type-IDs it depends on resolved dynamically from
  the file's own `<ReferenceValueSets>` rather than hardcoded. EU sourced from an OpenSanctions
  mirror of the official FSF feed (the official endpoint 403s without a registered token — see
  `docs/DECISIONS.md`). UK's designation date/program are grouped by `Unique ID`, earliest `Date
  Designated` wins. Real run 2026-09-21: **2,205 vessels total** — OFAC 1,540 (1,528 with IMO,
  dates 1989-01-05..2026-08-24), EU 2 (both with IMO, both 2022-12-12), UK 663 (662 with IMO,
  dates 2017-10-03..2026-08-06); every real `designation_date_precision` is `"day"` (OFAC's
  partial-date path is implemented/tested but not exercised by the live snapshot). Found and fixed
  a real iterparse memory-management bug (clearing a row's own descendants before the row itself
  was processed) and two real quirks not in the task spec (OFAC dual-script names, UK's per-row
  alias/IMO-prefix variance) — full detail in `docs/DECISIONS.md`. 34 new tests, 315 total, `ruff`
  clean.

- **P3-2 done**: `process/sanctions_match.py` — joins `sanctions.parquet` to `mmsi_imo.parquet` by
  checksum-valid IMO (reusing `process.identity.VALID_IMO_SQL` verbatim, not reimplemented), lands
  `data/identity/sanctions_matches.parquet` (one row per sanctioned-vessel-record x matching-mmsi
  pair). Sanctions lists carry no MMSI of their own — MMSI surfaces in the output as the AIS-side
  key that comes along once IMO matches, documented explicitly so "join by IMO and MMSI" isn't
  misread as a direct MMSI field on the sanctions side. Real run 2026-09-21, **not** near-zero as
  initially expected by analogy with P2-7's GFW comparison: of 2,191 checksum-valid sanctioned
  records, **205 matched (9.4%), 164 distinct mmsi** — UK 130/662 (19.6%), OFAC 75/1,527 (4.9%), EU
  0/2 (too small to read anything into). 41 of the 164 matched IMO are corroborated by both OFAC
  and UK. Neither `mmsi_is_reused` matches nor the double-distinct-imo-same-mmsi ambiguity case
  occurred in this window (0 each) — both are implemented and unit-tested against synthetic
  fixtures, not exercised for real here. 1 of 2,192 real sanctions IMO values failed the checksum
  (excluded, counted). A real bug found before trusting the result: computing the checksum-invalid
  set as `WHERE ... AND NOT (VALID_IMO_SQL)` crashed with a `CAST` error on real OFAC/UK data —
  wrapping the checksum arithmetic in `NOT(...)` defeats DuckDB's filter short-circuit that the
  plain `WHERE ... AND VALID_IMO_SQL` form relies on, so `CAST` ran on non-numeric substrings.
  Fixed with an anti-join against the already-validated view instead. 15 new tests, 330 total,
  `ruff` clean. See `docs/DECISIONS.md` for the full write-up and open questions below for why the
  headline number departs from the pre-registered near-zero expectation.

- **P3-3 done.** `features/panel.py`: one row per `(mmsi, year_month)`, DuckDB-only, left-joins all
  five detector tables plus the sanctions match table onto the `mmsi_imo.parquet` roster. 346 tests
  pass (16 new), `ruff` clean, real run over 2024-06 landed `data/processed/vessel_month_panel.parquet`
  (21,146 rows, 4,881 with a valid imo, 16,265 orphaned, 1 reused). Labels: 164 ever matched a
  sanctions record, 17 already sanctioned by window_end, 147 sanctioned only after (the
  forward-looking population). A first pass was flagged NOT safe for Phase 4 by `analyst-review`;
  the fix pass (interrupted mid-session once, resumed and finished this session) covered all 8
  items:
  1. **Population restriction documented as a hard blocker**, not just a caveat: the module
     docstring states plainly that 77.5% of panel rows (`is_orphaned=true`) can structurally never
     be labelled positive (the sanctions join is by imo only) and that Phase 4's modelling
     population MUST be restricted to `imo IS NOT NULL` rows, or a model trivially "solves" the
     panel by re-deriving `is_orphaned`/`ship_type='Tanker'` instead of predicting anything.
  2. **All five sanctions-derived columns renamed to a `label_` prefix**
     (`label_is_sanctioned_ever`, `label_earliest_designation_date`,
     `label_is_sanctioned_as_of_window_end`, `label_is_sanctioned_after_window_end`,
     `label_n_sanctions_sources`), with a new guard test
     (`test_label_columns_are_the_only_ones_naming_sanctions`) asserting the real panel's output
     columns match that exact set and no other column name contains "sanction"/"designat".
  3. **The false "sts reproduces exactly" claim** is corrected in `docs/DECISIONS.md`'s
     `_2026-09-22_` entry (append-only, per that file's own convention -- the original wrong
     paragraph is left in place with the correction appended after it, not rewritten).
  4. **The P4-3 rolling-cutoff blocker list is now complete and accurate**, written directly into
     `features/panel.py`'s own docstring (not just this file): `gaps` confirmed SAFE (0/74,546 real
     gaps violate `gap_end <= window_end`); `sts` confirmed WORSE than originally stated (its
     `_repetition` self-join has no temporal ordering -- 819/1,689 real episodes, 48.5%, have a
     later episode inflating their own repetition score -- and `_context` reads up to 6h past
     `end_time`, so filtering alone cannot fix it, the detector itself would need re-running per
     cutoff); `spoofing`'s `synthetic_circle` is a CONFIRMED backdating (`event_time = voyage_start`,
     93 real events), not merely plausible; and the single largest, previously-missing prerequisite
     is now documented -- every STATIC identity feature (`total_message_count`, `voyage_count`,
     `ship_type`, `is_reused`, and critically the representative `imo` used for the sanctions join)
     is aggregated over the ENTIRE window with no month bound, which would leak a later month's
     identity into an earlier month's row/label in a future multi-month panel.

  Real panel numbers are unchanged by the fix pass (same 21,146/4,881/164/17/147 as the original
  build) -- only column names and documentation changed. **P3-1 and P3-2 are unaffected, already
  committed, and were never in question.**

- **P4-0 done: the discriminative check ran. Verdict GO, narrowly, on one detector family only.**
  `features/panel.py` gained `n_observed_hours`/`n_observed_days` (from `detect.liveness`,
  survives discarding clean data) and 21 `rate_*` columns (`RATE_BASE_COLUMNS` x
  per-voyage/per-observed-day/per-1000-messages), added alongside every existing raw count --
  `nullif`-guarded so zero exposure gives NULL ("undefined"), never a divide-by-zero infinity. New
  `model/discriminative_check.py`: 147 positives (sanctioned after window_end) vs 4,717 negatives
  (never sanctioned) within `imo IS NOT NULL`, excluding the 17 already-sanctioned-as-of-window_end
  rows from both classes; AUC + 95% stratified-bootstrap CI + Mann-Whitney p per feature, in both
  an unmatched and a `(ship_type, n_observed_days quintile)`-matched scope (10 groups contain both
  classes, 3,011/4,864 rows -- effectively Tanker-and-thinly-Cargo, matching the ad-hoc check's own
  population). **Real result: only `n_draught_change_unexplained` (raw and all 3 rate variants)
  discriminates in the expected direction, in both scopes** (AUC 0.60-0.65, CI excluding 0.5).
  Matching/normalization did NOT rescue `gaps`/`spoofing` (`discriminates_opposite`, AUC 0.26-0.33),
  `sts` (`discriminates_opposite`, tiny effect, AUC ~0.47-0.49) or
  `identity_anomalies`/`destination_course_mismatch` (`no_discrimination`) -- no feature's verdict
  category changed between raw and normalized. Full detail, including two real bugs found and
  fixed before trusting this (a DuckDB same-name-alias resolution bug in the rate SQL, and an
  ignored `n_bootstrap` argument), in `docs/DECISIONS.md`'s 2026-09-22 "P4-0 executed" entry.
  **P3-4 may proceed per the plan's literal rule, but Phase 4 must not report this as "the
  detectors work" -- four of five families show no rescuable signal here; the README's limitations
  section must say so plainly.** 360 tests pass (13 new), `ruff` clean.

- **P3-4 done: the full per-window storage pipeline (A1-A5), 2026-09-22.** Windows can now be
  built and safely pruned without keeping a whole month's clean data on disk: detectors +
  `process/tracks.py`/`identity.py` write window-partitioned artifacts, `detect/liveness.py`/
  `process/thin.py` write day-partitioned ones, `pipeline/window.py` orchestrates
  build→verify→fingerprint (creates only), `pipeline/prune.py` is the only place deletion happens
  (quarantine-first `.trash`, dry-run by default, `--yes-delete` opt-in). The mandatory A0.4
  re-download drill PASSED first (fingerprint reproduced exactly), confirming the DMA archive is
  stable enough to trust deletion against. **Never actually run with `--yes-delete`** -- the 30-day
  window's clean data and every legacy single-file artifact are still all on disk. 429 tests,
  `ruff` clean. Full detail: `docs/DECISIONS.md`'s 2026-09-22 "P3-4/A1".."A5" entries.

- **P4-1 done: the naive baseline, 2026-09-23. Unblocked, not worked around.** `model/baseline.py`:
  R1 tanker (893 flagged, 135 tp, 15.1% precision, 5.0x lift) and R2 tanker+flag-of-convenience
  (572 flagged, 125 tp, 21.9% precision, 7.2x lift) -- the shipped baseline every Phase 4 model
  must beat, on P4-0's exact 4,864-row/147-positive population. New `process/foc.py`: the ITF's 48
  flag-of-convenience registries, hardcoded and dated (no feed to scrape), 43/48 matching
  `process.mid.MID_COUNTRY` verbatim, 3 name variants resolved, 2 (France/Germany's international
  second registers) unmapped by construction, guard-tested. New `model/evaluation.py` extracted
  from P4-0's `discriminative_check.py` (shared population + bootstrap, verified byte-identical
  after the refactor). **R3 (age >15y) fully implemented but correctly `blocked_by_gate`**: new
  `ingest/wikidata_ships.py` (CC0 SPARQL source; GFW has no build-year field, Equasis/GISIS
  prohibit bulk extraction by licence; real run 95,503 checksum-valid IMO, 92,112 with a usable
  build_year) + new `model/build_year_gate.py` (3 gates against P4-0's population: G1 coverage
  PASSED 85.26%; **G2 differential coverage FAILED** -- 94.56% vs 84.97% Wikidata-coverage rate
  between sanctioned-after and never-sanctioned vessels, AUC 0.548 CI [0.527, 0.565] excluding
  0.5, real measured label contamination; G3 plausibility failed in isolation on one real vessel,
  verified as source noise not a join bug). **Overall verdict NO-GO** -- this resolves the
  standing vessel-age open question below, it does not defer it. `CLAUDE.md`/`tasks.json` updated
  to the shipped rule. A dedicated `analyst-review` pass before close found and fixed 6 real issues
  (misleading "happened to" precision@20 framing -- it's a deterministic mmsi/flag sort artifact,
  not a random draw; the gate's window/freshness weren't checked before honouring a GO verdict; G2
  tested only the whole population, missing a worse gap inside the tanker+FOC subpopulation where
  R3 actually operates, now tested both ways; the AUC branch was mathematically redundant with the
  Fisher branch and had no effect-size floor; a real DuckDB DECIMAL-vs-DOUBLE type bug in
  `build_year_gate.parquet`; and real flag-concentration evidence for the label-bias limitation,
  Gabon 34/34 positive within R2) -- full detail in `docs/DECISIONS.md`'s 2026-09-23 entry, which
  also lists what was deliberately left as documented limitation rather than fixed. 45 new tests
  (474 total), `ruff` clean.

- **Second real window, started 2026-09-23: DMA archive range corrected, `ingest/dma.py` hardened,
  60/60 days banked -- build itself not yet finished (see "In progress").**
  `docs/DATA_SOURCES.md`'s "2006 onwards" claim for the DMA archive was wrong: per-day HEAD probing
  (zip and csv) confirmed the bucket currently only serves **2024-03-01..2025-02-26**; corrected,
  full detail in `docs/DECISIONS.md`'s 2026-09-23 entry. Chose **2024-11-01..2024-11-30** as the
  second window (same size as June, ~5 months separated for a real temporal split + seasonal
  variety, safely inside the confirmed range incl. its 30-day lead-in). Found and fixed a real bug
  blocking progress: `ingest/dma.py` had no retry on a transient network failure
  (`httpx.ReadTimeout`/`ConnectError`) -- now retries in place per URL with exponential backoff
  (`MAX_FETCH_ATTEMPTS=5`), a real 404 still falls through immediately, no retry. 2 new tests (478
  total), `ruff` clean. This was this file's own previously-documented open question about
  `download_day` lacking retry logic -- now resolved. All 60 days (30-day lead-in
  2024-10-02..2024-10-31 + the window itself) downloaded, cleaned and validated; thin tracks,
  ship_type reference, voyages, identity resolution and the anchorage mask all built for the
  window. Two real operational incidents along the way, not code bugs: an earlier background retry
  loop was left running unsupervised and raced a later attempt against the same `data/` tree for
  30+ minutes, corrupting exactly one clean partition (`2024-10-23`, caught via a real DuckDB
  read+row-count validation pass, deleted, rebuildable) -- lesson: never launch a new background run
  over the same `data/` tree without confirming the previous one actually exited, not just that its
  wrapper reported done; and Windows Defender's real-time protection has no exclusion for this
  project's `data/` folder, plausibly contributing to that lock contention and to `check_on_land`
  running >2x slower than the real June baseline (94.3 min documented) -- the user asked for a
  Defender exclusion but the auto-mode classifier blocked `Add-MpPreference` as a system security
  change, so it needed the user to run it directly. **Resolved 2026-09-25: see the P4-1b entry
  below** -- the user applied the exclusion, and `on_land` was also parallelized.

- **P4-1b done: second real window (2024-11-01..2024-11-30) finished and verified, 2026-09-25.**
  Resumed with `python -m pipeline.window --start 2024-11-01 --end 2024-11-30` (idempotent, skipped
  everything already banked). Two real fixes landed first: the user applied the Windows Defender
  real-time-protection exclusion for `data/` directly (elevated PowerShell -- Claude Code's
  auto-mode classifier blocks `Add-MpPreference` itself as a security-weakening action, confirmed
  again this session); and `detect/spoofing.py`'s `check_on_land` was parallelized -- its per-day
  point-in-polygon join ran in a single Python `for` loop over one shared DuckDB connection (~18%
  CPU use measured last session, 3 of 16 logical threads). Now runs on an 8-worker
  `ThreadPoolExecutor` (`ON_LAND_MAX_WORKERS`), each worker on its own `con.cursor()`. Required
  changing `_land_pieces` from a `TEMP TABLE` to a regular `TABLE`: verified empirically first that
  `cursor()`-derived connections cannot see the parent connection's TEMP tables, only the shared
  catalog (regular tables, and the loaded spatial extension, are visible fine). 476 tests pass,
  `ruff` clean. **Combined effect, real run:** `on_land` finished in 58 min -- faster than even the
  healthy June baseline (94.3 min), not just the >2x-degraded November one. Full window (30 days)
  completed and verified end to end in 2h25min wall-clock (09:31-11:57): spoofing checks 61.5 min
  (8,237,660 events: `impossible_speed` 2,398, `on_land` 8,234,803, `synthetic_circle` 36,
  `simultaneous_position` 423), `sts` 76 min (502 candidates survive the hard gates, mean confidence
  0.356 -- see Open questions below, this stage was NOT sped up and is now the slowest one),
  `behaviour` 1.6 min (864 events: 140 `destination_course_mismatch`, 724
  `draught_change_unexplained`), `identity_anomalies` 5.2 min (199 events: 66 `no_valid_imo`, 35
  `name_flapping`, 19 `name_change`, 20 `callsign_flapping`, 9 `callsign_change`, 38 `shared_imo`,
  12 `shared_identity`). `Window 2024-11-01..2024-11-30 verified and recorded (30 day(s))` --
  `_verify_window` passed for every day.

- **P4-2 done: Isolation Forest loses to the baseline, decisively, out-of-time, 2026-09-25.**
  Prereqs fixed first: `features.panel.build_panel` read detector tables via `window=*` globs with
  only an upper time bound, so a November panel would have counted June events -- now every input
  resolves to the exact window partition (June rebuild matches the legacy panel except the known
  `impossible_speed` tie-break). November gaps computed (26,203), `sanctions_matches` rebuilt over
  both windows (282 matched mmsi). Per-window panels live at
  `data/processed/panel/window=<start>_<end>/part-0.parquet`. November panel: population 4,449,
  139 forward positives; **R2 replicates out-of-time: precision 0.204, lift 6.5x**
  (`outputs/baseline_summary_2024-11.txt`). New `model/isolation_forest.py`: fit on June (no
  labels, train-only preprocessing), scored on November, matched alert budget k=574 with a paired
  bootstrap: precision 0.016-0.017 vs R2's 0.204, lift 0.50x (worse than random), AUC 0.364
  (detectors) / 0.529 (+ is_tanker/is_foc), both CIs entirely below R2. Top "anomalies" are
  dockside-dwelling, heavily-observed local traffic -- P4-0's finding again: future-sanctioned
  vessels produce *less* detector signal in Danish coverage. No GPU needed (~50s on CPU).
  `analyst-review`: no blockers; its should-fixes applied (see `docs/DECISIONS.md`). 491 tests,
  `ruff` clean.

- **P4-3 done: LightGBM ties R2, does not beat it, 2026-09-25.** `model/lightgbm_risk.py`: one
  cutoff (train June, test November), as-of-cutoff training label -- positive only if designated
  in (2024-06-30, 2024-11-01) -- which leaves **16 training positives** (all tankers). At k = 574:
  `context` (is_tanker, is_foc) reproduces R2 exactly (0.204); `detectors` alone 0.143, loses;
  `detectors_context` 0.214, CI [-0.003, +0.025], **no significant difference** (ceiling 0.242,
  i.e. at most +0.038 was possible). Post hoc only: detectors improve ranking over `context` (AUC
  +0.033 [0.023, 0.042]). Budgets k = 50/100/200 and a no-exposure variant are pre-registered for
  the next window in `docs/DECISIONS.md`. `analyst-review`: no blockers. `lightgbm` 4.7.0
  installed into the environment (was in `requirements.txt`, not installed).

- **P4-3c done: static and declared-destination features beat R2 at small budgets, 2026-09-27.**
  New `features/static.py`: per mmsi per window, `length_m`/`width_m`/`min|max_draught_m`/
  `draught_range_m`, `n_destinations`, and three frozen destination regexes (`dest_russia`,
  `dest_south_route` = Suez/India/Turkey/STS hubs, `dest_for_orders`). Plus `imo_serial` (the IMO
  number as an age proxy: Spearman 0.977 vs Wikidata build year, 100% coverage, so no P4-1-style
  contamination). Exploratory out-of-time check (train June with P4-3's 16 as-of positives, test
  November): logistic regression **P@50 0.64, P@100 0.55, P@200 0.45 vs R2's 0.204**, all paired
  CIs excluding 0; P@574 0.223 (ceiling 0.242). Unfitted rule `tanker AND (dest_russia OR
  dest_south_route)`: 144 flagged, 55.6% precision, 17.8x lift. The gain is mostly the
  destination; already-sanctioned vessels as extra positives HURT. **Caveats:** regexes were
  written after seeing June's destinations (so only the new archive months are a clean test --
  regexes and feature set are now FROZEN, see `docs/DECISIONS.md` 2026-09-27); a Russian-port
  destination is close to the designation reason itself (README must say so). Not yet wired into
  `pipeline.window`/`features.panel` (the running build re-imports them); run
  `python -m features.static --start ... --end ...` per window after the build. Only verified into
  a scratch dir; **nothing under `data/processed/static/` exists yet.** 49 new tests, `ruff` clean.

- **P4-3e done: the pooled walk-forward evaluation protocol, frozen and implemented, 2026-09-27.**
  Pre-registered in `docs/DECISIONS.md` before any new window is scored:
  - 7 primary cutoffs (2024-08..2025-02); primary budget matched to R2's count, plus k=50/100/200.
  - Expected precision under random tie-breaking; micro-averaged pooling, reported next to its
    ceiling.
  - Paired bootstrap clustered by IMO; secondary clustering by designation package.
  - Label snapshot frozen at `sanctions.parquet` 2026-09-21.

  Implemented in `model/pooled_evaluation.py` (15 tests); it reproduces the June->November P4-3c
  numbers exactly.
- **P4-3f done: the OpenSanctions owner/manager coverage gate is NO-GO, 2026-09-27.** 0/893
  (June) and 0/898 (November) tankers have any vessel-organisation link dated before the cutoff,
  from any source: OpenSanctions ingested these links from 2025 onwards (median `first_seen`
  2025-07-13). Network features are dropped for the archive period.
- A deep literature/model search (5 researchers) produced
  `reports/Modelos para predecir la flota fantasma.md` (Spanish; not committed, pending the
  author's call). Its top-ranked next steps are now tasks P4-3g (discrete-time hazard model)
  and P4-3h (implied Russian loading from draught).

- **P5 web front end started, 2026-09-27: ahead of order at the author's request, with
  prediction frozen on the site (author's call).** `report/export_viz.py` (read-only on `data/`)
  exports every built window to `viz/public/data/` (gitignored). June + November give 24,133
  indexed vessels and 9,145 dossiers, ~5 MB per window plus 28 MB of dossiers, ~3 min at 4
  threads. `viz/` is an Astro 7 + Svelte 5 + deck.gl + MapLibre 5 site, bilingual (`/en/`,
  `/es/`), with six pages: front-page story (the NS LOTUS case), explorer (animated month, event
  layers, timeline, vessel panel), vessel search, dossiers, live, and about.
  `ingest/aisstream.py` relays AISStream to the live page but has **not been run against the
  real service yet (no key)**. 24 new Python tests (export + relay), `ruff` clean, `astro check`
  0 errors. Node 24 is portable in `%LOCALAPPDATA%\Programs\nodejs`. Run with
  `cd viz && npm run dev`, then open http://127.0.0.1:4321/. Full detail in
  `docs/DECISIONS.md`, 2026-09-27 "P5".

## In progress

**P4-3b: building every month the DMA archive serves (2024-04..2025-02), author's call 2026-09-25.**
`scripts/build_archive_windows.sh`, **relaunched 2026-09-28 11:36 via WMI (PID 5416, parent
`WmiPrvSE`)**. It builds one window at a time, then `detect.gaps` per window, then
`process.sanctions_match --force`, then one panel per window at
`data/processed/panel/window=<start>_<end>/`. Progress: `outputs/logs/build_archive_windows.status`
(START/OK/FAIL per step, `DONE` at the end); per-step logs sit next to it (appended across runs,
so an old `Traceback` in a log does not mean the current run failed).
- **Why it died on 2026-09-28 (~09:00-11:00) with no error:** the machine did NOT reboot. It had
  been started with `Start-Process` from a Claude Code session, and Windows kills a session's child
  processes when the session exits (job object). **Always launch it via WMI**, which parents it to
  `WmiPrvSE`, outside the session: PowerShell `Invoke-CimMethod -ClassName Win32_Process
  -MethodName Create -Arguments @{CommandLine = '"<Git>/bin/bash.exe" scripts/build_archive_windows.sh';
  CurrentDirectory = '<repo root>'}` (Git bash is at `C:/Program Files/Git/bin/bash.exe`).
- **Why May's `check_on_land` ran >6 h: the 8-worker thread pool was counterproductive.** Fixed
  (`ON_LAND_MAX_WORKERS = 1`, ~30 min per window expected) -- see `docs/DECISIONS.md` 2026-09-28.
- **Clean on disk at relaunch:** 2024-03 (19 d), 04 (30), 05 (31), 06, 10, 11 -- ~81 of ~272 new
  days (~30%). This run starts with April (its earlier FAIL is retried naturally), then May
  (spoofing restarts from scratch: it was killed before writing).
- The other agent's live relay (`python -m ingest.aisstream`, P5-6) may run at the same time; it
  writes nothing to disk, so it does not conflict with the build.
- ~190 days remain at ~3-4 min/day, plus ~1.5-2 h of detectors per window (`sts` ~76 min is now
  the slowest step). That is **~1-1.5 days if the machine stays on**.
- **If the machine sleeps or shuts down, the build stops.** Validate the last written day with
  DuckDB (a shutdown mid-write can leave a truncated partition), then just re-run the script:
  every step is idempotent.
- **Never start a second build over `data/` while it runs.** Parallel work may read `data/`
  through DuckDB (keep it to ~4 threads) but must not write there.
- A stale partial download `data/tmp/dma-2024-04-02-*` is harmless; delete it after `DONE`.
- Disk: ~128 GB of new clean data against ~239 GB free; no pruning needed. **Do not prune clean
  data**: `features.static` and P4-3h need message-level draught, destination and positions.

## Next up

1. **When P4-3b ends:** re-run the script for April, then `python -m features.static --start
   <s> --end <e>` for every window (it writes `data/processed/static/window=.../`).
2. **Walk-forward (rest of P4-3b):** extend `model.lightgbm_risk` to every monthly cutoff,
   scored through `model.pooled_evaluation` (the frozen P4-3e protocol). Variants: R2, P4-3's
   pre-registered ones, and the frozen P4-3c `static` variant (logistic regression, plus LightGBM
   as secondary). Report results per cutoff and pooled; run `analyst-review` afterwards.
3. **P4-3g, discrete-time hazard model.** Every vessel designated inside the archive contributes
   its pre-designation months, giving ~100 positives instead of 16. This also explains why adding
   post-designation rows hurt. Pre-register it before running.
4. **P4-3h, implied Russian loading from draught** (eastbound in ballast, westbound laden).
   Thresholds are fixed on March 2024 only; validate against GFW port visits.
5. **Web (P5), in parallel:**
   - **Live (P5-6 done 2026-09-28):** the key is in `.env`, and the relay works against real
     AISStream. After ~10 min: 3,210 vessels (3,121 Danish straits, 89 Gibraltar, which is sparse
     in AISStream), ~25 msg/s, 713 with a destination, 449 with an IMO (static reports arrive
     every ~6 min). The page handled 3,000+ vessels with no errors. It already showed a listed
     tanker live: CELT (ex-CALLISTO, IMO 9299692, UK 2024-10-17, OFAC 2025-01-10). Locally it
     works only while `python -m ingest.aisstream` runs. A public site needs the relay on an
     always-on machine with `wss://` (e.g. `live.checkgraph.dev`); not chosen yet. The dev server
     can time out while the archive build loads the machine: use `npx astro build` +
     `npx astro preview` instead.
   - **Publishing (P5-3):** the domain is **checkgraph.dev**, retired from the author's
     `fake-review-detector` project. Its DNS is on Cloudflare (norman/jocelyn.ns.cloudflare.com).
     Today the apex A record (75.2.60.5) and `www` (CNAME to `fake-review-detector-s.netlify.app`)
     serve that old Netlify site. Recommended host: Cloudflare Pages. The DNS is already there, it
     has unlimited bandwidth, and `wrangler pages deploy dist` uploads the prebuilt site (the data
     is not in git). Limits: 20,000 files and 25 MiB per file. Today's build is 68 MB and 9,352
     files (9,145 are dossiers), so more windows may need the dossiers bundled into shards. Waiting
     for the author's go-ahead: publishing replaces the old site. Check the DMA's AIS licence
     first. `gh` is not installed.
   - After the archive build, run `python -m report.export_viz`: it picks up every built window
     automatically.
6. Then P4-4 (calibration). Challengers from the research report:
   - TabPFN v2 / TabICL (licence-clean);
   - bagging PU (averaging models trained on resampled vessels whose label is unknown);
   - a trajectory encoder without coordinates, as a probable null.
   All need PyTorch >= 2.7 with cu128 wheels for the RTX 5060 Ti (sm_120).
7. Remaining signal ideas are task P4-3d: pilotage refusal, Skagen anchoring, GFW port visits.
   Flag/name changes mostly happen AFTER designation (CREA), so they leak unless restricted to
   well before the cutoff. Owner/manager networks are out (P4-3f NO-GO).

**Research report.** The deep search (2026-09-27) lives in
`reports/Modelos para predecir la flota fantasma.md` and `research_notes/`. Both are in Spanish
and kept out of git at the author's request (listed in `.git/info/exclude`). Headline: nobody has
published a forward-in-time sanctions predictor, and at this label count, gains come from data
design, not model architecture.

## Blocked

- Nothing blocked.

## Open questions

- **`detect/gaps.py`, `detect/spoofing.py` and `detect/behaviour.py` still default their voyages
  input to the `window=*` glob.** `voyage_seq` restarts at 1 per window, so `lead() OVER (PARTITION
  BY mmsi ORDER BY voyage_seq)` over the glob would pair voyages across windows. No current output
  is affected (`pipeline.window` and this session's gaps run pass exact partitions; verified by
  `analyst-review`), but the default is a trap -- switch it to the exact window partition as
  `features.panel` now does. The stray `data/tracks/voyages/window=2024-06-10_2024-06-11` (P3-4/A4
  validation window) sits in every window-partitioned tree; harmless to exact-partition readers.

- **`detect/sts.py`'s November run took 76 min, 4x the June baseline (17.6 min), with a ~62-minute
  gap where nothing is logged before its named stages (`slots`, `pair_slots`, `episodes`, `gated`,
  `scored`) begin.** Observed 2026-09-25 during P4-1b's real run, right after `on_land` was
  parallelized and ran faster than ever -- so this isn't the same class of fix. Not investigated:
  candidates are unlogged setup (loading tracks/anchorages, building candidate pairs) being slow,
  or the same kind of serial-per-partition loop `check_on_land` had. Worth a dedicated look if
  `sts` needs rerunning often; not urgent for P4-2.
- **`detect/spoofing.py`'s `check_impossible_speed` has no tie-break on its `lag() OVER (PARTITION
  BY mmsi ORDER BY timestamp)` window, so its event count is non-deterministic across reruns when
  an mmsi has duplicate `(mmsi, timestamp)` rows** -- confirmed real during P3-4/A2's equivalence
  check (2026-09-22): re-running the real 30-day window reproduced 5,497 `impossible_speed` events
  against the original run's 5,499, isolated entirely to that one check (`on_land`/
  `synthetic_circle`/`simultaneous_position` were exact). Root cause: mmsi 219018851 alone has 14
  duplicate-timestamp rows on 2024-06-20 -- exactly the population `simultaneous_position` exists
  to catch -- and which duplicate `lag()` treats as "previous" is order-dependent under DuckDB's
  parallel execution. Pre-existing in the unmodified SQL, not introduced by A2's storage-layout
  change; not fixed here. Would need an explicit secondary sort key (e.g. a stable row id) added
  to the `lag()` window if exact reproducibility of spoofing counts across reruns ever matters
  downstream. `detect/sts.py`'s `nav_status` `mode()` resolution has the same class of issue (one
  real row's `nav_status_b` flipped between reruns during the same check, though it happened not
  to change that row's confidence score) -- revisit both together if this becomes load-bearing.
- **THE BIG ONE, RESOLVED by P4-0 (2026-09-22): four of five detector families carry no rescuable
  signal against the sanctions label in this window, with or without exposure normalization.** The
  ad-hoc check that raised this question (below, kept for the record) hypothesized that raw counts
  were confounded by local operating volume and that normalizing by exposure would rescue signal.
  P4-0 formalized the test (`model/discriminative_check.py`: AUC + bootstrap CI + matched
  ship_type/exposure controls, see `docs/STATE.md`'s Done section and `docs/DECISIONS.md`) and
  found the hypothesis only half right: `n_draught_change_unexplained` was ALREADY discriminative
  in raw form (AUC ~0.63) and stays so, matched or not, normalized or not -- consistent with the
  original observation that it is the fingerprint of a transfer outside Danish coverage. But
  `gaps`/`spoofing` remain actively `discriminates_opposite` and `sts`/`identity_anomalies`/
  `destination_course_mismatch` remain non-discriminating in EVERY scope tested -- exposure was not
  the confound suppressing their signal, and no amount of normalization changed that. **Formal
  verdict: GO for P3-4** (the plan's literal rule only requires one normalized feature to clear the
  bar), but Phase 4 must treat this as "one detector family works," not "the detectors work," and
  say so in the README per `CLAUDE.md`'s limitations-disclosure agreement. Original ad-hoc numbers,
  for the record: among tankers with a valid imo, vessels sanctioned AFTER window_end showed *less*
  raw signal than those never sanctioned -- 0.43 vs 0.73 gaps per voyage, 0.0% vs 0.5% with any STS
  episode, 5.9% vs 32.0% with any spoofing event, median message count nearly identical (23,245 vs
  22,459, ruling out raw-exposure as a trivial explanation), `n_draught_change_unexplained` alone
  pointing the expected way (40.7% vs 24.6% of tankers).
- **RESOLVED by P4-1 (2026-09-23): vessel-age (build-year) data is now sourced from Wikidata
  (`ingest/wikidata_ships.py`), but `model/build_year_gate.py` found real label contamination and
  returned NO-GO, so the shipped baseline (`model/baseline.py`) does not use it.** This was
  originally raised here during P3-3: the DMA AIS feed, sanctions lists and GFW all carry no
  build-year field. GFW's vessel-registry endpoint was also confirmed to have none; Equasis/GISIS
  were ruled out on licence grounds (bulk extraction prohibited), not attempted. Wikidata (CC0)
  gave 85.26% coverage of P4-0's evaluation population -- comfortably enough data -- but
  differential coverage between the sanctioned-after and never-sanctioned classes (94.56% vs
  84.97%, AUC 0.548 CI [0.527, 0.565] excluding 0.5) means build-year *availability* itself mildly
  predicts the label, the exact contamination risk a crowd-sourced source raises. The age term (R3)
  is fully implemented and will activate automatically, no code change, if a future window's gate
  run returns GO. See `docs/DECISIONS.md`'s 2026-09-23 entry for the full real numbers.
- **The P4-3 rolling-cutoff blocker (`gaps`/`spoofing`/`sts` + the static whole-window features)
  is written up precisely under P3-3's "In progress" entry above -- read that, not this line,
  before starting P4-3.** (Superseded here to avoid keeping two versions of the same finding in
  sync; the earlier version of this bullet understated `sts`'s risk and wrongly implicated `gaps`,
  which `analyst-review` confirmed is actually safe -- see above.)
- **P2-7's near-zero GFW overlap is plausibly compounded by the EU AIS carriage mandate exempting
  many smaller fishing vessels, and by GFW's own AIS feed disagreeing with DMA's on at least one
  real vessel pair's positions** — neither is verified here (see `docs/DECISIONS.md`'s P2-7 entry
  for the one case investigated by hand). Not urgent to chase further unless a future task needs
  to lean on GFW agreement as evidence.
- **`detect/behaviour.py`'s `draught_change_unexplained` checks only the two voyage-boundary
  positions for corroborating evidence, never the interior of the gap, and has no upper bound on
  gap length** — the real run's flagged gaps run a median ~5 days (117h), p90 ~13 days (307h), with
  the flagged position a median 27.6km from the nearest land. The modal flagged event is plausibly
  an ordinary round trip to a port this project's Danish-only data cannot see (e.g. Rotterdam), not
  evasion — declared draught is also a hand-keyed field with a large benign base rate. Not fixable
  without broader AIS coverage or a port-depth reference; stated in the module docstring rather
  than hidden.
- **`detect/behaviour.py`'s thresholds (`COURSE_MISMATCH_ANGLE_DEG`, `MIN_PORT_APPROACH_DISTANCE_M`,
  `MIN_COURSE_CHECK_SOG_KNOTS`, `MIN_DRAUGHT_CHANGE_M`, `ANCHORAGE_EVIDENCE_RADIUS_M`,
  `PORT_MATCH_BBOX_MARGIN_DEG`) are unvalidated judgement calls**, same posture as every other
  detector's thresholds. Revisit once Phase 3's sanctions-list join gives known-evasive vessels to
  compare against.
- **`detect/behaviour.py` does not resolve LOCODE-style destination text** (`NLRTM`, `PLGDN`,
  `DK SKA`, ...) — only exact-normalized-name matches against the Natural Earth port list
  participate. ~30% of destination text in this window is literally `Unknown`; most of the
  remainder that isn't a legible port name simply produces no `destination_course_mismatch` event.
  A LOCODE→port gazetteer would recover this but does not exist in this project.
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
- **`exclude_mmsi` in `liveness_verdict` still doesn't resolve which MMSIs are the same vessel
  itself — `data/identity/vessel_links.parquet` now exists as the input to do so, but `gaps.py`/
  `liveness.py` haven't been wired up to consume it.** Only 28 MMSI linked in the current window,
  so still not a blocker; revisit if/when a caller actually needs the linkage. Note
  `vessel_links`'s `knowable_at` is a lower bound, not a precise cutoff, for multi-hop groups —
  see `detect/identity_anomalies.build_vessel_links`'s docstring before wiring it up under a
  temporal cutoff.
- **`detect/identity_anomalies.py`'s `KIND_BASE_CONFIDENCE` weights, `CROSS_MID_BONUS`, and
  `SHIP_TYPE_INSTABILITY_PENALTY` are unvalidated judgement calls**, same posture as every other
  detector's confidence score. Revisit once Phase 3's sanctions-list join gives known-evasive
  vessels to compare against.
- **`detect/identity_anomalies.py`'s `name_change`/`name_flapping`/`callsign_*`/`no_valid_imo`
  kinds are only knowable as of the WHOLE build window's `window_end`, not at any intra-window
  cutoff** — a deliberate, documented consequence of being a batch (not incremental) detector, not
  a bug; see `docs/DECISIONS.md`. `features/` consuming this table under a rolling temporal cutoff
  must treat the whole 30-day build as available only once `window_end` has passed, matching how
  `detect.liveness`'s verdicts already work.
- Tramo B (`detect/coverage.py` → `cadence.parquet`, reporting-cadence percentiles) is planned but
  not started.
- **Meaning of the real schema's trailing `a, b, c, d` columns** — likely AIS diagnostic fields,
  not confirmed. Only worth resolving if a future detector needs them.
- **Voyage gap threshold (6h default in `process/tracks.py`)** is defensible but unvalidated —
  it's a `gap_hours` parameter, not a hardcoded constant, if it needs revisiting.
- **Whether the 30-day window needs extending for detector variety** — P2-5 found real, if thin,
  signal in the current window (1 reused MMSI, 103 MMSI with a name change, 9 shared IMOs), so
  still no strong signal either way. If extended, use separate weeks spread across 2024 rather
  than more contiguous June days (see `docs/DECISIONS.md`).

## Disk budget — read before downloading more days

**~239 GB free** (of 931 GB) as of 2026-09-25 (re-measured this session). `data/clean/` now holds
both real windows (June + November's 60-day lead-in/window) at ~37 GB total.
`data/identity/` and `data/tracks/` together are a few MB; `data/coverage/` is ~123 MB (the
legacy `liveness.parquet`, ~65 MB, plus the new partitioned `liveness/`, ~58 MB, both present at
once post-P3-4/A1 migration — the legacy file is not deleted until P3-4/A5's `prune.py` exists and
is run); `data/detect/` (gaps, spoofing, sts) is well under 200 MB total — the whole point of
reducing to aggregates. Post-P3-4/A2, `data/coverage/anchorages/`, `data/detect/{spoofing,sts,
identity_anomalies,behaviour}/`, `data/tracks/voyages/`, `data/identity/mmsi_imo/` and the new
`data/reference/ship_type/` each hold one `window=2024-06-01_2024-06-30/` partition ALONGSIDE
their legacy single-file counterpart (same "both present until A5 prunes" posture as A1's
liveness migration) — measured real total ~115 MB added (dominated by `spoofing/`'s 109 MB,
matching its on_land-heavy legacy file), well within budget. Post-P3-4/A3, `data/tracks/thin/`
holds 30 real day-partitions at the default 5-minute interval, 452 MB total (~18 MB/day) — no
legacy counterpart, this is a wholly new artifact. Post-P3-4/A4, a second, smaller real window
(`window=2024-06-10_2024-06-11`) exists alongside the whole-month one in every window-partitioned
tree, from `pipeline.window`'s own end-to-end validation run — ~6.7 MB total, negligible. One raw day
≈ 507 MB, discarded immediately after cleaning by `pipeline/backfill.py`. Phase 3-4's "years of
depth" requirement (several validation cutoffs `T`, each needing data before and after) should
still be met by **sampling short windows around each cutoff**, not downloading every day, and
eventually by discarding cleaned days too once Phase 2's detectors exist to define what's safe to
reduce them to. **That deferral is now lifted: the detectors all exist, and P3-4 is the task that
implements the discard.** Measured reduction ratios that make it work: 30 days of clean data are
14.2 GB, while `liveness.parquet` (all that `detect.gaps` needs) is 64 MB, all five detector outputs
are 114 MB, and `anchorages.parquet` is 0.10 MB. Post-P3-4/A5, `pipeline/prune.py` exists and is
dry-run-safe against the real `data/` (confirmed this session), but has not yet been run with
`--yes-delete` against it -- every legacy single-file artifact listed above is therefore still
present alongside its window-partitioned counterpart, and the 30-day window's clean data is all
still on disk. `pipeline/manifest.py` already has the unused
`clean_discarded_at` / "reduced" state stubbed and tested for exactly this.
