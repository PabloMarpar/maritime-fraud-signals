# Decisions

One line per decision, with the reason. Read this before re-opening a settled question.

_2026-09-16_

- **Scope: Danish straits (historical) + Gibraltar/Ceuta (live).** Denmark gives years of free
  history over a chokepoint all Baltic oil must transit; Gibraltar/Ceuta is a documented
  ship-to-ship transfer node. One method, two independent theatres — proves it generalises.
- **Detectors are rules, not models.** They work from day one without labels and their reasoning is
  inspectable. Models only rank what the detectors find.
- **Isolation Forest + LightGBM only.** Both have a long track record on this shape of problem.
  Trajectory autoencoders and similar are explicitly out of the main path: high risk of burning
  weeks for nothing. Optional extension once everything else is finished.
- **Forward-looking validation is the headline result.** Train before cutoff `T`, evaluate only on
  designations published after `T`. Anything else is circular.
- **Storage: Parquet + DuckDB.** Handles tens of millions of positions with no database server, and
  keeps raw data out of the context window.
- **Flat package layout (`ingest/`, `process/`, …) and `requirements.txt`.** Matches the convention
  already used across the author's other portfolio repositories.
- **Label bias gets its own README section.** Sanctioned vessels are the ones that were *caught*,
  not all offenders. Training on that label partly teaches the sanctioner's criteria. Saying so
  precisely is worth more than a decimal point of precision.

_2026-09-16_

- **Historical download range grows phase by phase, there is no single "download everything"
  step.** Phase 0 pulls one day only, to prove the pipeline end to end fast. Phase 1-2 widens to a
  few weeks for pathology variety. Phase 3-4 needs years of depth — the temporal-cutoff experiment
  requires several cutoffs `T` each with enough "before" and "after". Gibraltar/Ceuta is never a
  bulk download: it is continuous live capture via AISStream, starting in Phase 6.
- **Phase 0 sample day: 2024-06-05.** An ordinary Wednesday, no Danish or EU public holiday, so
  traffic should be representative rather than a quiet/busy outlier.
- **`ingest/dma.py` sniffs the downloaded bytes (zip magic number) instead of trusting the URL
  extension, and tries `.zip` before `.csv`.** The real on-disk format could not be confirmed
  against a live request while writing the module (see next decision), so the code is built to be
  correct either way rather than guessing once and hardcoding it.
- **Did not use `dangerouslyDisableSandbox` to reach `web.ais.dk` from the work computer.** This
  session's sandbox blocks all outbound port 80, so the live download and the real-format check are
  postponed to a session run from home, rather than bypassing the sandbox on a work machine for
  convenience.

_2026-09-16_

- **Used `dangerouslyDisableSandbox` from the author's personal machine, on explicit request, to
  complete the real download.** Confirmed the sandbox was the only blocker (Windows Firewall had no
  outbound block rules; the same request timed out with the sandbox off, ruling out router/ISP too,
  once a plain-HTTP control site also failed and then a bucket host succeeded). Read-only HTTP/S
  requests carry no risk to the host. Not a precedent for the work machine.
- **`web.ais.dk` is dead; the DMA archive now lives at S3 bucket `aisdata.ais.dk`
  (`eu-central-1`), reached over path-style HTTPS.** Discovered via web search after the legacy host
  timed out even with the sandbox disabled. `ingest/dma.py` and `docs/DATA_SOURCES.md` updated
  accordingly. This also closes the open question of `.zip` vs `.csv`: confirmed `.zip`.
- **P0-2 and P0-3 done: real 2024-06-05 data landed and verified.** 17,239,519 rows, 4,878 distinct
  MMSI. Quicklook PNG shows the expected dense cluster over Danish/Baltic waters, plus a handful of
  far-flung outlier points (South America, mid-Atlantic, near-Antarctic, Indian Ocean) — left as-is
  rather than filtered, since implausible-but-in-range positions are exactly what the Phase 2
  spoofing detector exists to catch, not a quicklook bug.

_2026-09-16_

- **Phase 1's three modules (`process/clean.py`, `process/identity.py`, `process/tracks.py`) each
  validated with a real-data smoke check against the 2024-06-05 day, not just synthetic tests.**
  Confirms the DuckDB-only, no-pandas approach holds up at real scale (~17M rows) before trusting it
  for Phase 2's detectors.
- **`identity.py` and `tracks.py` output flat cross-date tables (`data/identity/`, `data/tracks/`),
  not Hive-partitioned by day like `clean.py`.** An MMSI↔IMO pairing and a voyage are inherently
  cross-date facts (a voyage can span a day boundary); a per-day partition would arbitrarily split
  them. Only `clean.py`'s output is naturally day-scoped.
- **Voyage segmentation uses a single time-gap signal (default 6h, parameterized), not
  `navigational_status`.** Kept deliberately simple for Phase 1; classifying a gap as suspicious vs.
  ordinary is Phase 2 Detector 1's job, not this module's.
- **Disk budget is the binding constraint for widening the date range, not download time.** One raw
  day is ~507 MB; continuous multi-year download (~542 GB for 3 years) does not fit in the ~79 GB
  free on this machine. Phase 3-4 will sample short windows around each validation cutoff `T` and
  discard raw/cleaned intermediates after aggregating to the vessel-month panel, rather than
  accumulating years of raw positions.

_2026-09-16_

- **The high orphaned-MMSI rate from P1-2 (71%) is not a data quality problem — checked by
  breaking it down by `ship_type`.** Small craft with no IMO requirement (Sailing, Pleasure) account
  for almost all of it; Tanker (97%) and Cargo (93%) — the classes that matter for sanctions
  evasion — are well covered. Reframes P2-5 (identity anomalies): a Tanker/Cargo vessel *without* a
  valid IMO is the interesting case, not the orphaned rate in general.

_2026-09-17_

- **P2-1 coverage map built over 2024-06-01..2024-06-30: 62.5% of (cell, ship_type) estimates
  are exactly 1.0 and none below 0.5.** Consistent with — not despite — the selection-bias/
  circularity limitation documented in `detect/coverage.py` (a pair only forms between two
  received messages, so true coverage holes vanish rather than scoring low). The map must not be
  treated as ground-truth receiver coverage until that's addressed (e.g. cross-vessel
  corroboration); P2-2 must not naively read a low `coverage_probability` as "just bad coverage,
  not evasion" until then.
- **Phase 2's working window is 30 contiguous days, 2024-06-01..2024-06-30, not a longer or
  split range.** Big enough to develop and unit-test all five detectors and to surface phenomena a
  single day can't (1 reused MMSI appeared, vs. 0 on the single day). If a detector later needs
  more variety (noisy P2-1 estimates, too few ship-to-ship candidates, no reused MMSI beyond the
  one found), extend by adding separate weeks spread across the year rather than more contiguous
  June days — same disk cost, far more seasonal/behavioural variety, and the code already tolerates
  gaps in the requested range without changes.
- **`data/tracks/points.parquet` removed; voyage membership is now attached on demand via
  `process.tracks.attach_voyage_ids` (an ASOF JOIN against `voyages.parquet`).** The old file was a
  full copy of the clean range plus two columns — 13 GB of pure duplication over the 30-day window.
  Voyages partition each MMSI's track without overlap, so the ASOF join (nearest voyage start at or
  before a point's timestamp) is exact and unique; no information was lost.
- **The download/process/discard cycle (`pipeline/backfill.py`, `pipeline/manifest.json`) only
  discards raw data, not cleaned data, for now.** Phase 2's detectors don't exist yet, so it isn't
  known what a safe reduction of a cleaned day would need to keep. Discarding cleaned days once
  detectors exist and are validated (turning `identity`/`tracks`-style whole-range rebuilds into
  mergeable per-window partials) is deferred to Phase 3. `data/manifest.json` is tracked in git
  (explicit `.gitignore` exception) so this state survives even though `data/` itself is not
  committed.
- **`pipeline/backfill.py`'s disk guard defaults to 20 GB free, and `ingest.dma.download_day` now
  accepts a `tmp_dir` pointed at `data/tmp` instead of the OS default temp directory.** The OS
  default lands on the same drive being guarded but outside where the guard measures unless told
  otherwise; without this the disk check could pass while the actual download (a transient
  multi-GB zip+CSV) fills the same volume elsewhere.

_2026-09-17_

- **P2-1b done: `detect/liveness.py` replaces `grid.parquet`'s gap-ratio `coverage_probability`
  as the primary signal for judging AIS silences.** Cross-vessel corroboration, not a vessel's own
  gap ratio: for a set of cells and a time window, was some *other* vessel heard there (leave one
  out on the vessel under scoring)? A tri-state verdict (`receiver_alive` / `area_dark` /
  `no_evidence`) replaces a single score, so "no data" is never confused with "low coverage". The
  historical baseline is also leave-one-out — not just the concurrent corroboration count — or a
  vessel that is the sole historical occupant of a quiet corridor would exonerate itself using its
  own record, reproducing one level up the exact circularity this module exists to escape.
  `coverage_probability` and `grid.parquet` are retired outright (not kept as a secondary signal):
  it estimates the wrong quantity, and empirically has no discriminative range (62.5% of estimates
  exactly 1.0, none below 0.5, over the 30-day window) — a constant with noise, not fit to survive
  into a future model's feature vector. `detect/coverage.py` itself is untouched pending a Tramo B
  session that repurposes it to emit reporting-cadence percentiles instead (still useful, unaffected
  by the selection-bias critique since it conditions on "heard at all", which is the honest reading).
- **`_daterange`/`_partition_path`/`_existing_partitions`, byte-identical across
  `process/identity.py`, `process/tracks.py` and `detect/coverage.py`, extracted to
  `process/partitions.py`** before `detect/liveness.py` became a fourth copy.
- **An `analyst-review` pass on the first working version of `detect/liveness.py` found three real
  correctness bugs, not just polish, before anything was built on top of it — logged here in full
  because the fixes changed the module's numbers materially:**
  1. *Wrong baseline denominator.* The expected-corroborators rate divided by a hardcoded
     `baseline_days * 24` (720 hours) regardless of how much of that period the built
     `liveness.parquet` actually covered. A gap early in a 30-day build window (e.g. June 3) has a
     nominal 30-day lookback of which only 1-2 days exist, so real evidence was diluted by up to
     30x. Fixed by reading the built table's own `window_start`/`window_end` provenance and using
     the actual overlap as the denominator (`LivenessVerdict.baseline_hours_available`). Measured
     on the real data: this quantity ranges from 24 hours (a gap on June 2) to 696 hours (a gap
     near the end of June), never the hardcoded 720 — confirming the bug was live, not theoretical.
  2. *Units mismatch enabling a new self-exoneration path.* The baseline counted vessel-*hours*
     (rows), while the corroboration count is distinct *vessels* — so a single vessel reporting
     continuously for the whole baseline accumulated "evidence" just as fast as several different
     vessels passing through, and could alone justify `area_dark` for a *different* vessel's
     silence. Fixed with an added gate, `min_baseline_vessels` (default 3, a separate parameter
     from `min_expected`): `area_dark` now requires breadth (multiple distinct historical
     occupants), not just volume from one recurring source. Regression test:
     `test_single_recurring_vessel_does_not_trigger_area_dark`.
  3. *`n_cell_hours_scanned` leaked post-window data.* The field counted distinct hours across the
     vessel-cell join with no time bound at all, so it silently included hours after the window
     being scored — a temporal-leakage bug in a returned field, on a module whose whole purpose is
     leakage-free infrastructure. Fixed by bounding the query to `< window_end + 1h`. Also
     documented explicitly (module docstring): a `LivenessVerdict` reflects evidence available at
     `window_end`, not `as_of` — a caller attaching it to a temporally-cut-off panel must stamp it
     with `max(window_end, as_of)`, or it will credit an earlier period with information that only
     existed once the gap closed.
  - Also fixed: `test_area_dark_baseline_also_excludes_self`'s original fixture used a 2-hour
    window in which `area_dark` was mathematically unreachable regardless of leave-one-out (max
    possible `expected_corroborators` from one vessel over 2h was always < the 3.0 threshold), so
    the test passed without ever exercising the claim in its name. Rewritten with a fixture proven
    (by a second assertion in the same test) capable of `area_dark`, so the sole-occupant case's
    `no_evidence` result is evidence of the guard working, not of the mechanism being unreachable.
  - `exclude_mmsi` widened to accept a sequence, not just one mmsi: an MMSI is a radio identity,
    not a hull, and P2-5 will eventually need to exclude a set of MMSIs linked to one vessel. Not
    implemented here (this module does no identity resolution), only made possible for the caller.
  - The Poisson-derived "~5% false-negative rate" claim for `min_expected=3.0` was removed from the
    docstring: it doesn't survive the units-mismatch finding above, and was never independently
    validated. `min_expected` and `min_baseline_vessels` are documented as tunable defaults pending
    calibration, the same posture the project already takes with `detect/coverage.py`'s thresholds.
- **Validation run against the full, real 2024-06-01..06-30 window — all 11,556 scoreable
  Tanker/Cargo AIS gaps >=2h (533 of 12,089 skipped for lacking any baseline day), not a subsample.**
  (An earlier sampled run is superseded and not reported here: `random.sample` over an unordered
  DuckDB fetch does not produce a reproducible sample, and scoring the full population removed the
  need for sampling at all — it completed in ~11 minutes.)
  - Headline split: **receiver_alive 85.0%, area_dark 2.5%, no_evidence 12.5%.** Within the
    pre-registered non-damning range (not >95% receiver_alive, not <40%, not >30% no_evidence).
  - By duration: receiver_alive rises monotonically with gap length (75.5% at 2-4h, 81.3% at
    4-12h, 92.2% at 12h+). Reported for a sanity check only, **not as evidence the method works**:
    for any positive traffic rate, P(at least one corroborator) rises with window length by
    construction, so this shape is mechanically guaranteed and would appear even for a detector
    doing nothing useful.
  - **Duration-matched placebo control** (a same-vessel-population continuous-reporting instance,
    scored as if it were a silence of the matching length, per bucket): delta (placebo minus real)
    receiver_alive is **+19.0 points at 2-4h, +16.6 at 4-12h, but only +6.4 at 12h+** — below the
    10-point bar set in advance for "the method is doing real work". Read plainly: the
    corroboration signal discriminates a genuine silence from routine reporting well for short
    gaps, but far more weakly for 12h+ gaps, where both real and placebo verdicts are pushed close
    to saturation (>90% `receiver_alive`) simply because the window is long. **P2-2 should not
    treat `receiver_alive`/`area_dark` on very long gaps as strong evidence**; the corroboration
    signal's usable range is short-to-medium gaps, and this needs to be revisited if P2-2 leans on
    long gaps specifically (e.g. multi-day AIS-off stretches).
  - Gate correctness: every `area_dark` verdict had `n_baseline_vessels >= 3` (0 violations) —
    confirms the breadth gate from fix #2 above is wired correctly, not just present in the code.

- **P2-2 (`detect/gaps.py`) treats every consecutive pair of voyages for the same MMSI in
  `voyages.parquet` as the candidate-gap set, with no separate gap extraction from raw points.**
  Reason: `process.tracks`'s own docstring already assigns "classify this gap as suspicious or
  ordinary" to Phase 2 Detector 1, so the voyage boundary (gap > `gap_hours`, default 6h) already
  defines the candidate population; re-deriving it independently would just duplicate that logic
  with a second, possibly inconsistent threshold.
- **P2-2's verdict-to-probability mapping is a fixed table (`receiver_alive`=0.9, `area_dark`=0.15,
  `no_evidence`=0.5), not a formula, and gaps ≥12h get the result clamped into `[0.4, 0.6]`.**
  Reason: keeps the detector inspectable per the project's rule-based-detector convention, and the
  clamp directly encodes P2-1b's own falsification finding (corroboration saturates and stops
  discriminating past ~12h) instead of silently trusting a verdict known to be unreliable there.
  All five numbers (three probabilities, the 12h cutoff, the clamp band) are unvalidated defaults,
  not tuned constants — see `docs/STATE.md` open questions.
- **`detect.liveness.cells_within` expects already-floored grid cells, not raw lat/lon.** A caller
  (P2-2) passing a gap's raw endpoint positions straight into it would silently join the liveness
  table on almost nothing, since a real position almost never lands exactly on a 0.1° boundary.
  `detect/gaps.py` floors both endpoints with its own `_to_cell` (same epsilon-before-floor,
  round-after-multiply convention as `detect.liveness`/`detect.coverage`) before calling it. Worth
  remembering for any future caller of `cells_within` with real vessel positions.

- **P2-3's "positions on land" check uses Natural Earth's 10m-scale land polygons via DuckDB's
  `spatial` extension, not a new heavyweight geospatial dependency.** Reason: `duckdb spatial`
  (`INSTALL/LOAD spatial`) reads a shapefile directly (`ST_Read`), round-trips its native `GEOMETRY`
  column through Parquet with no WKB conversion needed, and runs `ST_Contains`/`ST_Buffer` in plain
  SQL — all verified working in this session — which keeps the project's DuckDB-only convention
  intact instead of adding `geopandas`/`cartopy`/a raster-mask package. `shapely`/`pyproj` were
  already dependencies but unused for this; they remain unused, spatial extension covers it.
- **The land polygons are eroded ~1.1km inward (`COASTAL_EROSION_DEG = 0.01`) before testing
  containment.** Reason: Natural Earth's 1:10,000,000-scale coastline is a generalization, not a
  precise boundary — verified empirically that a real Copenhagen on-land point sits ~205m outside
  the raw polygon. Without erosion, a position that is actually at sea near a jagged coastline
  could get flagged `on_land` just because the simplified polygon bulges out over real water; erosion
  trades some false negatives (real on-land positions very close to shore go unflagged) for avoiding
  that false-positive direction, which was judged the safer failure mode for a fraud-flagging
  detector. Unvalidated size, sized to the one measured offset, not a systematic study.
- **The synthetic-circle check uses a Kasa algebraic circle fit (linear least squares), not a full
  nonlinear geometric fit.** Reason: it reduces to a solvable 3x3 normal-equation system and is
  standard practice for this kind of flagging heuristic; it is known to be biased for partial arcs
  or noisy data relative to a nonlinear fit, which is why the detector also gates on a radius band
  and angular spread, not on residual ratio alone — a tight algebraic fit over a short,
  nearly-straight arc would otherwise still look deceptively "circular".
- **Detector output for both P2-2 and P2-3 is one row per detected event, not one row per vessel or
  vessel-month.** Reason: keeps each detector a pure, inspectable function of the evidence it found,
  with no premature aggregation decision baked in; rolling events up to the vessel-month panel is
  explicitly deferred to the not-yet-built `features/` module (Phase 2's own task list), which can
  choose how to combine multiple events/confidences without the detectors needing to agree on that
  now.
- **`detect.spoofing`'s `check_synthetic_circles` computes the Kasa fit's moment sums (and the
  residual/angular-spread stats) as bulk grouped SQL aggregates, not by pulling each voyage's raw
  points into Python one at a time.** Reason: the original per-voyage version assumed "most voyages
  are short and never reach this check" — false on real data (68,431 of 95,692 real voyages passed
  the pre-filter, 345M points total), so it never finished a real 30-day run. Only ~9 numbers per
  voyage (not the points) ever leave DuckDB now, in two passes (fit + radius gate first, then
  residual/spread only for survivors).
- **Angular coverage for the circle check is measured as mean resultant length (a standard
  circular-statistics dispersion measure), not a per-point unwrapped angular sweep.** Reason: the
  bulk-SQL rewrite above needs a measure computable as an aggregate (`sum(cos(theta))`,
  `sum(sin(theta))`) without per-point ordering/unwrapping. `MAX_CIRCLE_MEAN_RESULTANT_LENGTH = 0.6`
  is pinned close to the value for a uniform 180° arc (`2/pi ≈ 0.637`) so it's a like-for-like
  replacement of the old >=180° gate, not an independent guess — verified against a synthetic
  half-circle case that it correctly does not flag.
- **`detect.spoofing`'s land-polygon bounding-box crop uses tail quantiles (0.1% each side) of the
  data's own lat/lon, not raw min/max.** Reason: a real clean day contained 963 of 10.4M points
  (0.0092%) with corrupted coordinates up to 89° latitude, which `process.clean`'s existing rules
  don't catch — a min/max bbox over that data covered most of the Northern Hemisphere and defeated
  the crop entirely (a 30-day run didn't finish in over 80 minutes). Quantiles ignore that handful
  of outliers while staying data-driven (works for a future non-Danish working region too); the
  tiny fraction of genuinely corrupted points outside the box simply never get an on-land verdict.
- **`check_impossible_speed` requires a minimum 60-second gap between a pair before evaluating
  implied speed (`MIN_SPEED_CHECK_INTERVAL_SECONDS`).** Reason: without it, the check flagged 31%
  of the entire real 30-day dataset — 98.2% of one real day's flagged pairs had a time gap under 10
  seconds, where ordinary GPS/positional jitter of a few dozen metres is amplified into an
  "impossible" speed by dividing by a near-zero interval. At 60s+ the same day's flags dropped from
  76,970 to 167, two orders of magnitude fewer and a plausible rate for a real anomaly signal.

_2026-09-18_

- **`check_on_land` decomposes the land mask with `ST_Dump` before cropping, and runs the
  point-in-polygon join per day partition instead of once over the full window.** Reason: a fourth
  real-data bug, found the same way as the first three — the single-day-validated version (68s/day)
  still did not finish a real 30-day run in over 3.5 hours. Root cause: the tail-quantile bbox is
  computed from the *whole* window, and the DMA's receivers see visibly more Baltic/Scandinavian
  coastline over a month than on any single day, so the crop pulled in more land complexity than the
  one-day benchmark assumed — compounded by one monolithic spatial join over the 312M-row union
  instead of a per-day checkpoint. Exploding each landmass/island into its own small-bbox row via
  `ST_Dump` (instead of eroding one or two sprawling multi-part geometries) measured ~2x faster per
  day on its own (68s -> 32s on 2024-06-05); chunking the join by day made total cost track the
  well-measured per-day rate instead of an unpredictable one-shot query. The real 30-day run
  completed in 94.3 minutes for this check (10,092,010 events) — slower than the ~25 min projected
  from a COUNT(*)-only benchmark, because the projection didn't account for `.fetchall()` materializing
  millions of Python row tuples; still bounded and non-pathological, unlike the pre-fix behaviour.
- **Decomposing the land geometry with `ST_Dump` before cropping changes `on_land` event counts by a
  small amount (+3.1%: 286,440 vs. the pre-fix 277,788 on 2024-06-05).** Reason: crop-then-erode
  processing order differs slightly from erode-each-decomposed-piece at shared boundaries between
  adjoining Natural Earth pieces. Not chased to exact parity — within the noise of an already
  unvalidated heuristic (see the erosion-buffer decision above), and the fix's purpose was
  performance, not a change in which points get flagged.
- **`on_land`'s high real-run volume (10,092,010 events, 3.23% of all positions) is not a
  miscalibration bug — sanity-checked and closed.** The volume is heavily concentrated: 6,164 of
  21,146 distinct MMSI in the window have at least one flagged position, but the top 10 MMSI alone
  account for over 2M events, each with a spatial stddev of 2e-05 to 0.001 degrees (2-100m) and
  timestamps spanning the full 2024-06-01..2024-06-30 window — i.e. vessels moored at one exact spot
  for the entire month, transmitting AIS continuously. This is exactly the behaviour the module
  docstring already warned about ("a vessel that spends real time at a berth... can produce many
  on_land rows for one stay -- expected, not deduplicated"), not a mask or erosion defect. Consequence
  for `features/` (not yet built): raw per-vessel on_land event count will be dominated by dwell time,
  not by anomalousness — a rate or distinct-dwell-episode feature will likely be more useful than a
  raw count.

_2026-09-18_ (P2-4 session: a fifth `detect.spoofing` bug found while building the anchorage mask,
fixed before continuing)

- **`ST_Distance_Sphere` in this DuckDB build takes each point as `ST_Point(latitude, longitude)`,
  not the standard `ST_Point(longitude, latitude)` every other spatial function here uses
  (`ST_Contains`, `ST_DWithin`, `ST_ClosestPoint`, and `ST_Point` construction itself — confirmed
  `ST_Point(x, y)` round-trips to WKT `POINT (x y)` exactly as given).** Verified against a
  real-world distance (Copenhagen to Malmö, true ~28.4km): the standard `(lon, lat)` order gave
  49.0km, `(lat, lon)` gave 28.45km. A pure east-west offset isolates it unambiguously — a pure
  north-south fixture cannot, since a shared longitude makes the two argument orders numerically
  close by coincidence, which is exactly why the original tests for `check_impossible_speed` and
  `check_simultaneous_positions` (both north-south fixtures) never caught this.
- **`check_impossible_speed` and `check_simultaneous_positions` both built their points with the
  standard, wrong-for-this-function order, so every distance and implied speed either check ever
  computed was systematically inflated** (worst for east-west separations, ~unchanged for
  north-south ones, up to ~1.8x at Danish latitudes). Found while building `detect/anchorages.py`
  for P2-4 (its own coastal-distance computation had the identical bug — fixed there first, then
  traced back here), not by review. `check_on_land` (`ST_Contains`) and `check_synthetic_circles`
  (a local planar projection with explicit trig, no `ST_Distance_Sphere` call) are unaffected.
- **Fixed by swapping the argument order at both call sites, with an east-west regression test
  added for each** (`test_impossible_speed_evidence_value_is_correctly_scaled_for_east_west_offset`,
  `test_simultaneous_positions_evidence_value_is_correctly_scaled_for_east_west_offset`) — a
  north-south-only fixture is no longer sufficient evidence that this convention holds. 180 tests
  pass, `ruff` clean.
- **The real 30-day run (2024-06-01..2024-06-30) was redone from scratch with the fix.** Both
  affected checks dropped, as expected of a fix to an over-inflating bug: `impossible_speed` 8,218
  → **5,499** (-33%), `simultaneous_position` 1,627 → **1,158** (-29%). `on_land` (10,092,010) and
  `synthetic_circle` (93) are unchanged, as expected since neither is affected. New total:
  10,098,760 events (was 10,101,948) in `data/detect/spoofing.parquet`. P2-3 remains closed; its
  recorded real-run numbers are now the corrected ones.

- **P2-4 done: detector 3, ship-to-ship transfers (`detect/sts.py`, `data/detect/sts.parquet`),
  GFW's structural definition as hard gates plus a rule-based confidence score.** Built on top of
  `detect/anchorages.py` (above). Episode segmentation (`segment_episodes`) is the direct fix for
  the 647-hour false-encounter artifact the session's opening proxy query found: co-location is
  cut into discrete episodes whenever consecutive 10-minute slots are more than
  `EPISODE_SEPARATION_MINUTES` (60) apart, run as ONE global pass (not per day), so an episode can
  never straddle a day boundary -- there is nothing to stitch. Hard gates are GFW's own four
  (duration >=2h, median speed <2kn, separation <=500m, >=10km from a coastal anchorage); the
  anchorage-distance leave-one-out reuses the arithmetic form from `detect.anchorages`'s
  `list_filter`+lambda finding (`len(member_mmsis) - list_contains(...)::INT -
  list_contains(...)::INT`), not `list_filter` itself. Confidence combines six discriminators
  (rendezvous signature, co-drift, ship-type pairing, `navigational_status` with a "moored
  offshore" inversion, pair repetition, duration) as a documented, unvalidated heuristic -- never
  a calibrated probability, mirroring `detect.gaps`'s own posture; P2-7's agreement with the GFW
  Events API must be measured on the hard-gated set, not a confidence cut. 22 new tests, 202
  total, `ruff` clean.
- **Real 30-day run: 1,255,460 candidate episodes -> 1,689 survive the hard gates** (a ~743x cut,
  stronger than the 7-day prototype's 180x -- a full month accumulates more repeat-moored-neighbour
  episodes for the anchorage exclusion to catch), mean confidence 0.382, in 17.6 minutes wall-clock
  (no per-point spatial join; the pairwise join runs on a 10-minute-slot aggregate, ~49.2M
  pair-slots for the month). Sanity-checked: all six score components land in their designed
  [0,1] ranges; top-15-by-confidence pairs are all short (2-9.3h) despite a 466h max duration in
  the full set (47 episodes >=24h) -- the duration-decay term is doing its job, not just present
  in the formula; survivor positions cluster in the Kattegat/Skagerrak/open Baltic, not the
  Belts, matching the pre-registered "this detector effectively cannot see the Danish Belts"
  limitation (10km coastal limit + 10km exclusion radius removes ~20km of shore, and the Great
  Belt is narrower than that). Ship-type mix matches the 7-day prototype's prediction: dominated
  by Passenger/Passenger (179), Other/Other (115), Passenger/SAR (87), Sailing/Sailing (69),
  Fishing/Fishing (65) -- only 28 of 1,689 rows involve a Tanker at all, none in the top 15.
- **Observed, not fixed: a service-vessel pair (e.g. Tug/Tug) can still score a high overall
  confidence (0.82 seen) despite `score_ship_type`'s 0.1 penalty**, if the other five
  discriminators are strong (genuine movement on both sides, real co-drift, a one-off encounter,
  short duration) -- `W_SHIP_TYPE` (0.20) is not large enough to override that combination on its
  own. This is the weighted-sum design working as specified, not a bug: two tugs that are
  genuinely under way together, drifting, meeting once, briefly, is exactly the shape of evidence
  the other five terms were built to reward. Left as observed behaviour, not retuned -- with no
  labels, adjusting a weight because one case "looks wrong" is the same mistake as loosening a
  gate until the count "looks right" (a trap this module's plan explicitly flagged in advance).
  Worth revisiting once P2-7 gives a labelled or cross-checked comparison point.
- **`data/coverage/` now means "derived spatial/temporal support tables consumed by a detector",
  not only receiver coverage** — `detect/anchorages.py`'s mask lands there (alongside
  `liveness.parquet`), not under `data/reference/`. Reason: `data/reference/` means geometry this
  project *downloaded* (`ingest/landmask.py`, `ingest/ports.py`); the anchorage mask is derived
  from the very AIS it will be used to filter, and keeping that provenance distinction visible in
  the directory layout is the point of the module's anti-circularity safeguards. A future derived
  support table belongs in `data/coverage/` too, not a new directory, unless it stops being a
  detector-support artifact.

- **P2-5 done: detector 4, identity anomalies (`detect/identity_anomalies.py`,
  `data/detect/identity_anomalies.parquet`), plus a derived MMSI-linkage table
  (`detect/identity_anomalies.build_vessel_links`, `data/identity/vessel_links.parquet`) and a new
  `process/mid.py` (MID→flag-state lookup, needed here for `cross_mid` and again later by P4-1's
  "flag of convenience" baseline).** Eight `kind`s: `no_valid_imo` (a Tanker/Cargo vessel that
  never broadcasts a valid IMO, per the P1-2 reframing below), `name_change`/`name_flapping` and
  `callsign_change`/`callsign_flapping` (a clean temporal switch between values vs. values
  overlapping in time), `reused_mmsi` (>1 valid IMO on one MMSI), `shared_imo` and
  `shared_identity` (one IMO or one (name, callsign) pair broadcast by >1 MMSI). `flag_change` is
  deliberately NOT its own kind: a cross-flag share is always a `shared_imo` row with
  `cross_mid=true`, since it is a strict subset of "same IMO, different MMSI" and a separate kind
  would double-count it in any downstream aggregation. Text fields are normalized (`@`-padding
  stripped, case/whitespace collapsed) before distinct-value comparison -- AIS Message 5 pads to a
  fixed width, so `'MAERSK'`/`'MAERSK@@'`/`'  maersk '` are one name, not three. 32 new tests, 242
  total, `ruff` clean.
- **Real 30-day run**: 265 events -- `no_valid_imo` 91, `name_change` 47, `name_flapping` 60,
  `callsign_change` 12, `callsign_flapping` 22, `reused_mmsi` 1, `shared_imo` 18 (9 IMOs × 2 MMSI,
  14 of the 18 rows `cross_mid`), `shared_identity` 14 (7 pairs). `reused_mmsi`'s count of 1 matches
  `process.identity`'s own checksum-validated finding exactly, the strongest available sanity check
  on the validity filter. `build_vessel_links` linked 28 MMSI into 14 groups. Wall-clock 4m14-30s
  (three runs), see the performance finding below.
- **`name`/`callsign` text normalization did NOT reduce the flapping population**: pre-registered
  expectations (from ad hoc profiling without `@`/case normalization) were 60 MMSI with two valid
  names overlapping in time; the real run, WITH normalization applied, found exactly 60 again.
  Padding/casing was not the explanation for the overlap after all -- these really are two
  genuinely distinct decoded values whose broadcast windows overlap, most likely AIS decoding
  noise (two receivers resolving the same message differently) rather than a real back-and-forth
  rename, especially since only 1 MMSI in the same window holds >1 checksum-valid IMO. Recorded as
  a finding, not treated as a bug: `name_flapping`/`callsign_flapping` keep low base confidence
  (0.15) precisely because of this.
- **Performance: an early version of this module funneled every check through one materialized
  table carrying all three normalized text columns for all 346M messages in the window; the real
  30-day run then took over 33 minutes and never finished logging its first check**, because
  several checks queried that table (or views chained on top of it) more than once, each re-paying
  for columns the check never used (`no_valid_imo`'s join alone cost 491.8s; `shared_identity`
  878.4s). Fixed by having each `_build_*` stage read only the 2-4 raw columns its own check
  needs, straight from the Parquet partitions -- the same per-purpose-scan pattern
  `process.identity`/`detect.spoofing` already use, not a shared wide table. Real run after the
  fix: ~254-270s total across six build stages (`value_intervals` ~51-53s and `identity_pairs`
  ~127-134s are the two heaviest), then all six checks combined in under 1s. This is slower than
  the ~60s this module's own plan assumed, but the same order of magnitude as this project's other
  detectors at this data volume (P2-4's real run took 17.6 minutes) -- not fixed further, see
  `docs/STATE.md`'s open questions.
- **A dedicated review pass (run before closing the task, per `CLAUDE.md`) found four real
  temporal-leakage bugs in the first working version, none caught by the unit tests at the time.**
  All four are fixed and now covered by tests; recorded here because each is a genuinely easy
  mistake to repeat elsewhere in this project:
  1. Whether an MMSI's name/callsign history is a "clean switch" or "flapping" is a judgement over
     the WHOLE window (a switch on day 5 that never reverts is indistinguishable, before
     `window_end`, from one that reverts on day 20 and turns out to be flapping). `knowable_at` for
     `name_change`/`name_flapping`/`callsign_change`/`callsign_flapping` is now `window_end`, not
     an intra-window timestamp -- the identical fix `detect.liveness` already made for its own
     verdicts, applied here independently before this session's reviewer pointed at the precedent.
  2. `no_valid_imo` is also a whole-window absence claim (a valid IMO arriving on day 25 would undo
     an "absence" staked on an earlier `last_seen`) -- `knowable_at` is now `window_end` here too,
     not `last_seen`. `reused_mmsi` is the one kind that does NOT need this: a second valid IMO
     makes the fact true immediately and no later evidence can undo it, so its `knowable_at` now
     equals its own `event_time` (previously it was wrongly stamped with the vessel's overall
     `last_seen`, always at or after the true moment).
  3. `MAX_IDENTITY_GROUP_SIZE`'s skip-oversized-groups guard was applied using each group's
     EVENTUAL, full-window member count, meaning whether an early, small `shared_imo`/
     `shared_identity` pair survived depended on how many MORE mmsi joined the same group later in
     the window -- a leak in the pair's very existence, not just its timestamp. `_expand_pairs` now
     evaluates the cap as of each pair's own `knowable_at` (how many members had broadcast the
     value by then), so early pairs formed before a group balloons past the cap still survive.
  4. `build_vessel_links`'s output carried no timestamp at all, despite its own docstring inviting
     a consumer to feed it into `detect.liveness.liveness_verdict`'s `exclude_mmsi` under a
     temporal cutoff -- a consumer had no way to avoid using a link that only became knowable after
     their cutoff. Fixed by carrying each mmsi's earliest edge `knowable_at` through, documented
     explicitly as a NECESSARY-BUT-NOT-SUFFICIENT lower bound (a multi-hop group's full
     connectivity can be knowable later than any single edge in it) rather than a precise cutoff --
     a fully rigorous point-in-time regrouping would need to re-run the union-find restricted to
     edges below the cutoff, which it does not do.
  None of these changed the real 30-day run's event counts (the fixes change *when* a row is
  knowable and, for point 3, *which* pairs inside an oversized group survive -- no group in the
  real data came close to `MAX_IDENTITY_GROUP_SIZE`), so the numbers above are the corrected,
  final ones.
- **Reframing from P1-2 applied as planned, not re-litigated**: `no_valid_imo` gates on "ever
  validly Tanker or Cargo" (a Tanker/Cargo vessel is required to carry an IMO; Sailing/Pleasure is
  not), not on the 71% global orphaned-MMSI rate, and requires a minimum of 20 static messages and
  3.0 observed days before treating an absence as evidence -- both unvalidated defaults, see
  `docs/STATE.md`. Gating on "ever" rather than a single resolved ship_type was a deliberate choice
  too: 98 of 21,146 MMSI report more than one valid ship_type, and gating on a resolved value would
  let that resolution rule silently change who counts.
