# Project state

_Last updated: 2026-09-22_

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

## In progress

**P3-4, Part A: per-window storage pipeline -- A1 and A2 done, A3-A5 not started.** Full spec:
`docs/PLAN_P4-0_P3-4.md`. **A1 done 2026-09-22**: `detect/liveness.py`'s `build_liveness` now
writes one day-partition at a time (`data/coverage/liveness/date=.../part-0.parquet`, atomic,
skips days already built) instead of one whole-range file the next window would overwrite;
`liveness_verdict` accepts that directory (new default) or a legacy whole-range file (still
supported). Includes A1.1's denominator fix (exact day-count, not span, under disjoint coverage --
the old arithmetic silently biased verdicts toward `no_evidence`). Verified on real data: migrated
2024-06-01..2024-06-30 (3,988,982 rows, matches the legacy file exactly), and `detect.gaps`
rebuilt against it reproduces the existing real `gaps.parquet` row for row (74,546 gaps, zero
diffs). A real perf bug (naive per-day stat loop) found and fixed along the way -- directory mode
is now ~3x faster than the legacy path, not just equivalent. 365 tests, `ruff` clean.

**A2 done 2026-09-22**: every detector (`anchorages`, `spoofing`, `sts`, `identity_anomalies`,
`behaviour`) plus `process/tracks.py`/`process/identity.py` now write
`data/<kind>/window=<start>_<end>/part-0.parquet` atomically instead of one whole-range file a
later window silently overwrote. `detect/gaps.py` deliberately NOT converted -- the plan's own A2
section lists it as a consumer, not a producer. New `process/ship_type.py` fixes the plan's named
hard blocker: `features/panel.py`'s `_build_ship_type` and `detect/identity_anomalies.py`'s
`_build_ship_types` both used to scan clean partitions directly for this one static fact; both now
read a shared raw `(mmsi, ship_type, type_of_mobile, n_messages)` reference instead, each
re-applying its own prior resolution logic on top (provably equivalent, verified against real
data). New shared helpers in `process/partitions.py`
(`window_partition_path`/`atomic_write_parquet`/`partition_exists`/`git_sha`). Consumer defaults
(`mmsi_imo_path`/`voyages_path`/`liveness_path`/`spoofing_path`/`sts_path`/
`identity_anomalies_path`/`behaviour_path`/`anchorages_path`) point at `window=*` globs; the one
exception is `ship_type_reference_path`, resolved to the caller's own exact window rather than a
glob, so this one feature doesn't silently widen the already-documented "whole window, no month
bound" limitation (see the open question below) beyond what P3-3 already flagged.
**Real-window equivalence check (delegated to a background agent) PASSED for all 9 real builds**
against the existing 2024-06-01..2024-06-30 legacy artifacts: exact row-count match on 8/9 (the
9th, `spoofing`, 10,098,758 vs the legacy run's 10,098,760, traced to a pre-existing
non-deterministic tie-break in `check_impossible_speed`'s unmodified SQL -- not a regression, see
open questions); every substantive column (excluding provenance) matched exactly except a handful
of floating-point last-bit differences (`voyages`: 254/95,692 rows; `anchorages`: 273/900 rows)
and one `sts` row's `mode()`-tie-break `nav_status_b` (doesn't change that row's confidence). 381
tests, `ruff` clean, both re-confirmed after the real run. Full detail:
`docs/DECISIONS.md`'s 2026-09-22 "P3-4/A2" entry.

## Next up

**P3-4/A3**: `process/thin.py`, downsampled tracks (~5min buckets) for the Phase 5 map and manual
review only -- never for re-detection. Measure real per-day size before committing to the interval.
After A3: A4 (`pipeline/window.py`, creates only) and A5 (`pipeline/prune.py`, deletes with
quarantine -- gated on the A0.4 re-download drill, not yet run).

**P4-1 (naive baseline)** stays blocked on the vessel-age open question below regardless of P3-4.

## Blocked

- Nothing blocked.

## Open questions

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
- **No vessel-age (build-year) data exists anywhere in this project's ingested data.** Confirmed
  against the real clean-partition schema while building `features/panel.py` (P3-3): the DMA AIS
  feed carries no build-year field, and nothing else ingested so far (sanctions lists, GFW) carries
  one either. This directly blocks P4-1's naive baseline exactly as specified ("tanker over 15 years
  old under a flag of convenience") -- P4-1 must either find a ship-registry data source for build
  year (e.g. an IMO-keyed registry lookup) or redefine the baseline without an age term before it
  can be implemented. Not solved here; `features/panel.py` provides `flag_country` (raw MID-derived
  flag name) but deliberately no age column and no "flag of convenience" classification (both
  explicitly P4-1's job, not P3-3's).
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

**~80 GB free** (of 931 GB) as of 2026-09-19 (not re-measured this session; last real check
2026-09-18). `data/clean/` for the 30-day window is ~14 GB.
`data/identity/` and `data/tracks/` together are a few MB; `data/coverage/` is ~123 MB (the
legacy `liveness.parquet`, ~65 MB, plus the new partitioned `liveness/`, ~58 MB, both present at
once post-P3-4/A1 migration — the legacy file is not deleted until P3-4/A5's `prune.py` exists and
is run); `data/detect/` (gaps, spoofing, sts) is well under 200 MB total — the whole point of
reducing to aggregates. Post-P3-4/A2, `data/coverage/anchorages/`, `data/detect/{spoofing,sts,
identity_anomalies,behaviour}/`, `data/tracks/voyages/`, `data/identity/mmsi_imo/` and the new
`data/reference/ship_type/` each hold one `window=2024-06-01_2024-06-30/` partition ALONGSIDE
their legacy single-file counterpart (same "both present until A5 prunes" posture as A1's
liveness migration) — measured real total ~115 MB added (dominated by `spoofing/`'s 109 MB,
matching its on_land-heavy legacy file), well within budget. One raw day
≈ 507 MB, discarded immediately after cleaning by `pipeline/backfill.py`. Phase 3-4's "years of
depth" requirement (several validation cutoffs `T`, each needing data before and after) should
still be met by **sampling short windows around each cutoff**, not downloading every day, and
eventually by discarding cleaned days too once Phase 2's detectors exist to define what's safe to
reduce them to. **That deferral is now lifted: the detectors all exist, and P3-4 is the task that
implements the discard.** Measured reduction ratios that make it work: 30 days of clean data are
14.2 GB, while `liveness.parquet` (all that `detect.gaps` needs) is 64 MB, all five detector outputs
are 114 MB, and `anchorages.parquet` is 0.10 MB. `pipeline/manifest.py` already has the unused
`clean_discarded_at` / "reduced" state stubbed and tested for exactly this.
