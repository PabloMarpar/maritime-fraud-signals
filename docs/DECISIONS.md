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

_2026-09-20_

- **P2-6 done: detector 5, declared-behaviour contradictions (`detect/behaviour.py`,
  `data/detect/behaviour.parquet`).** Two kinds. `destination_course_mismatch`: resolves each
  candidate voyage's modal declared destination, matches it by exact normalized name against
  `ingest.ports`'s Natural Earth port list (bboxed to the data's own extent + a 10-degree margin),
  and flags a voyage whose median COG-to-bearing deviation exceeds 90 degrees over >=10 qualifying
  points, each at least 20km from the matched port and at least 2.0kn SOG. `draught_change_unexplained`:
  flags a >=2m median-draught change between an MMSI's consecutive voyages with no corroborating
  port/anchorage or ship-to-ship-transfer evidence in the gap. Both scoped to MMSI that have ever
  broadcast a valid IMO, reusing P2-5's reframing rather than re-deriving it. 13 tests, `ruff`
  clean.
- **COG used instead of heading, though the task named "destination vs heading"**: `heading` is
  valid (!= 511) for only 76% of rows in this window vs. 91% for `cog`, and COG is the more
  meaningful signal for "is this vessel moving toward its declared destination" regardless of
  coverage. A deliberate substitution, not an oversight -- see the module docstring.
- **LOCODE-style destinations (`NLRTM`, `PLGDN`, `DK SKA`, ...) are not resolved** -- ~30% of all
  destination text in this window is literally `Unknown`, and the remainder mixes legible port
  names, LOCODE-style codes, and free-text activity descriptors (`FISHING`, `FISHING GROUNDS`).
  Only exact-normalized-name matches against the Natural Earth port list participate; no
  abbreviation resolver was built. Recall loss, not a bug -- most real destination broadcasts
  simply produce no check-1 event.
- **Real 30-day run (post-review-fixes, see below for the pre-fix numbers this superseded): 916
  events total -- 152 `destination_course_mismatch` across 117 MMSI, 764 `draught_change_unexplained`
  across 586 MMSI.** `destination_course_mismatch` evidence (median course deviation): 90.0-179.8
  degrees, median 139.1. `draught_change_unexplained` evidence (draught change): -7.8m to +13.5m,
  median +2.3m. Wall-clock ~55s, far faster than P2-3/P2-4/P2-5's multi-minute real runs, because
  both checks are pre-scoped to the ~29% of MMSI with a valid IMO before any expensive per-point
  work runs.
- **A dedicated review pass (run before closing the task, per `CLAUDE.md`), on the FIRST working
  version and its FIRST real run (964 events: 200/764), found three real correctness bugs, none
  caught by the 10 unit tests at the time.** All three are fixed, covered by new regression tests
  (13 total), and re-validated against a second real run (916 events: 152/764, reported above):
  1. **`draught_change_unexplained`'s `knowable_at = next_voyage.start_time` was impossible.** The
     event's own headline number -- the NEW draught -- is a median over the vessel's ENTIRE next
     voyage, not resolved until that voyage's `end_time`; measured on the first real run, all 764
     events had `next_voyage.end_time` a median 42h (max 505h) after the stamped `knowable_at`. Two
     further problems compounded this: the anchorage-evidence check reads `detect.anchorages`'s
     mask, whose cells exist only as a WHOLE-30-day-window fact (14% of real coastal cells qualify
     only because a 5th vessel showed up elsewhere in the window); and the STS-evidence check
     borrows `detect.sts`'s `start_time`/`end_time` as if they were knowability stamps, but
     `detect.sts` emits no `knowable_at` at all -- its own hard gates are whole-episode judgements.
     Fixed by setting `knowable_at = window_end` for every `draught_change_unexplained` event, the
     same posture `detect.identity_anomalies` already uses for its own whole-window judgements, for
     the same reason: "no evidence found in this window" cannot be confirmed before the window ends.
  2. **Anchorage evidence had a self-exoneration circularity bug, the same shape P2-1b already
     found and fixed in `detect.sts`.** Without excluding the candidate MMSI from the anchorage
     cell's member count, a vessel that is itself one of the required 5 qualifying members could
     use its own mooring to exonerate its own draught change. Fixed with a leave-one-out check
     (`len(member_mmsis) - list_contains(member_mmsis, mmsi)::INT >= MIN_DISTINCT_VESSELS`),
     mirroring `detect.sts`'s own `_ANCHORAGE_MIN_OTHER_VESSELS` idiom exactly. Did not change the
     real run's `draught_change_unexplained` count (764 before and after) -- no cell that provided
     evidence in this window happened to be a marginal, self-qualifying one -- but is a correctness
     fix independent of whether this window happens to exercise it.
  3. **`destination_course_mismatch` had no speed floor, and ~24-30% of its events were stationary-
     vessel COG noise, not a real contradiction.** COG is meaningless for a vessel making no way;
     uniform-random COG jitter against a fixed bearing has an EXPECTED deviation of exactly 90
     degrees, which was this check's own flagging threshold, so the rule was structurally selecting
     for the artefact it should have excluded. The first real run's own 200 flagged voyages: 36.5%
     had median SOG under 0.5kn (`detect.anchorages`'s own "stationary" definition). Fixed with a
     2.0kn SOG floor on qualifying points; the real count dropped from 200 to 152 (-24%).
  Also fixed, lower severity: the scope gate (`_valid_imo_mmsi`) used each MMSI's WHOLE-window
  valid-IMO history with no time bound, which is monotone-accruing (not an absence claim) and so
  does not need `window_end` -- but did need its own per-MMSI `first_valid_imo_at` folded into
  `knowable_at` via `GREATEST`, mirroring `detect.identity_anomalies.check_reused_mmsi`'s reasoning
  for the same shape of fact, not `check_no_valid_imo`'s. Empirically inert on the real run (zero
  of 964 first-draft events had a first-valid-IMO timestamp later than their own `knowable_at`) but
  fixed for correctness rather than left true only by luck in this one window. Check 1's
  `knowable_at` also gained a `+ DEFAULT_GAP_HOURS` offset (a voyage boundary is only confirmed
  once that much silence has elapsed, per `process.tracks`'s own definition) -- a minor, bounded
  optimism fix, unlike the three bugs above.
- **Known limitation, left as-is, not a bug**: `draught_change_unexplained`'s evidence search
  checks only the two voyage-boundary positions, not the interior of the gap, and has no upper
  bound on gap length -- the real run's flagged gaps have a median of ~5 days (117h) and a 90th
  percentile of ~13 days (307h), with the flagged position a median 27.6km from the nearest land.
  The modal flagged event is plausibly an ordinary round trip to a port this project's Danish-only
  data cannot see (e.g. Rotterdam), not evasion. Declared draught is also a hand-keyed Message 5
  field with a large benign base rate for "unexplained" change. Neither factor is fixable without
  data this project does not have (a broader AIS feed, or a port-depth reference); both are stated
  plainly in the module docstring rather than left implicit, per `CLAUDE.md`'s instruction to state
  limitations explicitly.

_2026-09-21_

- **P2-7 (GFW agreement) started without a GFW API token; code built to the point of "ready to
  run", real run deferred.** GFW requires registering a free account and requesting a token
  (`https://globalfishingwatch.org/our-apis/tokens`); no token existed this session. Rather than
  wait idle, `ingest/gfw.py` (fetch + normalise + bbox-filter) and `detect/sts_agreement.py`
  (matching + agreement measurement) were written and fully unit-tested against a documented,
  assumed API/response shape, with the real API call left for once a token exists.
- **The GFW response JSON shape assumed while writing `ingest/gfw.py` (no token available) was
  wrong in two concrete ways, found by the first real call once a token arrived.** The
  documentation-research guess used `vessel.mmsi` (top-level, one side per entry, requiring the
  two mirrored `.1`/`.2` entries to be paired) and top-level `lat`/`lon`. The real API instead
  uses `vessel.ssvid` (a string) for the reporting side and `encounter.vessel.ssvid` for the other
  side — both already present on a single entry, so the `.1`/`.2` entries are mirrored duplicates
  of the same fact, not complementary halves — and nests position under `position.lat`/
  `position.lon`. Fixed in `ingest/gfw._normalise_entry`/`normalise_entries`: de-duplicate by base
  id, read both sides off one entry, raise `GFWSchemaError` (payload attached) if the confirmed
  shape ever stops holding. `encounter.type` (GFW's own vessel-type-pair label, e.g.
  `"fishing-fishing"`) is also captured now — more precise than any proxy this project could
  compute from its own AIS ship-type field, see `docs/DATA_SOURCES.md`.
- **Matching rule for P2-7: same unordered MMSI pair + time-window overlap (30min tolerance),
  position NOT checked.** Two independent detectors segment episode boundaries differently
  (`detect.sts` from 10-minute slots; GFW's own undocumented method), so requiring exact position
  agreement would test boundary-drawing convention, not whether the two methods agree an encounter
  happened. The tolerance is an unvalidated default, same posture as every other threshold in this
  project.
- **P2-7's agreement numbers must be reported split by GFW-scope, never as one headline number.**
  GFW's encounters dataset only covers vessel-type pairs it classifies as fishing-economy activity
  (fishing-fishing, fishing-carrier, fishing-support, fishing-bunker, tanker-fishing,
  carrier-bunker, support-bunker); `detect.sts` has no such restriction, and P2-4's real run
  skewed away from that population ("no Belts, few tankers"). `detect/sts_agreement.py` uses
  `ship_type == 'Fishing'` on either side as a simplified in-scope proxy (it cannot recover GFW's
  carrier-bunker/support-bunker subtypes from `detect.sts`'s own ship-type pairing) and reports
  the full-set and in-scope-subset rates side by side. Low overall agreement is expected to partly
  reflect this scope mismatch, not detector failure — the two numbers must travel together in any
  write-up.
- **Registering for a GFW token does not appear to require a legal organisation**, despite an
  earlier (uncorroborated) research finding to that effect. GFW's own documentation states only:
  register an account, request a key, agree to terms of use and attribution, participate in
  surveys — and describes its user base as spanning "government institutions and academia to
  nonprofits and small technology firms", with no stated exclusion of individuals. Confirmed: the
  author registered as an individual/independent project and received a token the same session.
- **P2-7 real run result: 0/1,689 detect.sts events agree with GFW, 0/6 GFW encounters agree with
  detect.sts.** Full pipeline run 2026-09-21 over the real 30-day window (2024-06-01..2024-06-30):
  - Fetched 116,966 raw global entries (GFW has no bbox filter, see `ingest/gfw.py`), normalised
    to 58,359 unique encounters worldwide. Only **6** fall inside the project's Danish/Baltic bbox
    (52.15-60.09N, 2.42-17.88E, a 1st/99th-percentile-plus-2-degree-margin box derived from the
    real clean window's own lat/lon distribution) for the *entire month* — GFW's fishing-economy
    scope (see earlier decision) barely touches this region at all, before any matching even runs.
  - `detect/sts_agreement.build_agreement`: 0 of detect.sts's 1,689 gated events matched a GFW
    encounter, including the 177-event subset where one side's `ship_type` is `'Fishing'` (the
    in-scope proxy). 0 of the 6 in-bbox GFW encounters matched a detect.sts event.
  - Of the 6 GFW encounters, 3 involve vessels never received by the DMA network at all (MIDs
    109/258, positions near 59.6-59.9N — offshore North Sea, outside dense Danish coastal
    coverage): those can never match by construction, not a detector failure on either side.
  - The remaining 3 include exactly one pair BOTH sides of which DMA did receive throughout June
    (219007313, 219004002 — both Danish-flagged, `ship_type='Fishing'`), the only case where a
    real agreement was even possible. Investigated by hand: GFW reports this pair encountering
    2024-06-25 05:50-08:10 (2h20m) at <=500m. DMA's own AIS for the same two MMSI over that exact
    window shows separation starting at 374m, crossing 500m within ~15-20 minutes, and reaching
    2,174m by the window's end — never sustaining GFW's own <=500m criterion for anywhere near
    2 hours. detect.sts, built on DMA's data, is correct not to have gated this pair; the
    discrepancy is between what DMA's terrestrial feed and GFW's own (blended, likely
    multi-source/satellite-supplemented) AIS pipeline each computed as these vessels' positions
    for the same real-world window, not a bug in the matching code or detect.sts's thresholds.
  - **Interpretation.** The headline 0% figure is real but should not be read as "detect.sts
    disagrees with GFW" — GFW's own dataset essentially has no opinion on this region for this
    vessel-type scope (6 candidates all month, half not even in DMA's coverage), so there was
    almost nothing to agree or disagree WITH. A plausible compounding factor, not verified here:
    the EU's AIS carriage mandate exempts many smaller fishing vessels, which would suppress GFW's
    fishing-encounter counts in Danish waters independent of any detector's behaviour. P2-7 is
    closed as "measured, reported honestly, scope mismatch dominates the result" rather than
    "detect.sts validated" or "detect.sts invalidated" — neither is what this comparison could
    have shown, given GFW's own scope. `data/reference/gfw_encounters.parquet` and
    `data/detect/sts_gfw_agreement.parquet` hold the full output for a future re-check (e.g. once
    the live window advances past 2026, a global rather than Danish-only comparison, or a
    same-provider replication, could all still be worthwhile).

_2026-09-21_ (P3-1 session: sanctions list ingestion)

- **EU sanctions ingested from an OpenSanctions mirror, not the official `webgate.ec.europa.eu`
  endpoint.** The official endpoint returns HTTP 403 without a registered access token — confirmed
  by both a direct curl and a cookie/session handshake attempt, both failed the same session.
  Registering for official access is out of scope for this project. The OpenSanctions mirror
  (`data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv`) republishes the same
  official EU FSF feed, a standard practice for this kind of compliance data, and was confirmed
  reachable with the expected schema. Documented in `ingest/sanctions.py`'s module docstring and
  `docs/DATA_SOURCES.md`, not a silent swap.
- **OFAC's designation date is the earliest "Created" (`EntryEventTypeID`) `EntryEvent` across all
  of a profile's `SanctionsEntry` elements**, not e.g. the most recent one or a specific
  programme's own date. Reason: this project's forward-looking validation needs the earliest point
  a vessel became knowable as sanctioned; a later re-designation under an additional programme
  should not push the label's date forward. 93 of 19,579 real profiles (confirmed 2026-09-21) carry
  more than one `SanctionsEntry` and/or more than one "Created" event, so this was a real choice,
  not a hypothetical one.
- **Partial OFAC dates (year+month only, or year only) are represented as a full `date` with the
  missing day/month defaulted to 1**, with `designation_date_precision` (`"day"`/`"month"`/
  `"year"`) carrying the actual precision separately. Reason: `SanctionedVessel.designation_date`
  is typed as a plain `date` (matching every other date field in this project), so a placeholder is
  unavoidable if a day is genuinely unknown; day=1 is the least presumptive choice (does not imply
  a specific day within the month/year actually occurred). Not exercised in the live 2026-09-21
  OFAC snapshot (every real designation date resolved to full day precision) but implemented and
  unit-tested, since OFAC's own XSD allows it and a future snapshot could differ.
- **EU's designation date is extracted by regex (`YYYY-MM-DD$`-style, earliest if multiple) from
  the free-text `sanctions` column, not read from any dedicated column** — none exists in the
  OpenSanctions mirror's simplified export. `first_seen` was explicitly rejected as a substitute:
  it is OpenSanctions' own ingestion timestamp, a different quantity, and using it would be exactly
  the kind of temporal leak `CLAUDE.md` warns against (a vessel's OpenSanctions `first_seen` can
  postdate its true EU designation by years). A row with no parseable date is logged and skipped,
  not guessed — real run: 0 of 2 real EU vessel rows needed this fallback, but the path is
  unit-tested.
- **Two real, undocumented-in-the-task-spec quirks found while verifying against live data, both
  handled defensively rather than assumed away:**
  1. *OFAC*: a vessel's primary `Alias` can carry two `<DocumentedName>` siblings under the SAME
     primary alias -- one Latin-script, one transliterated (confirmed on real profile 34940,
     "Baltic Leader" / its Cyrillic transliteration, `ScriptID` 215=Latin vs 220=Cyrillic). The
     task spec's own name-extraction path assumed exactly one `NamePartValue`. Fixed by preferring
     the Latin-script part when more than one is present, falling back to whichever is found if
     none is Latin -- see `ingest/sanctions._extract_ofac_vessel`.
  2. *UK*: the vessel NAME, not just address fields, varies row-to-row within one `Unique ID`
     group -- each row is one alias (`Name 6`), with `Name type == "Primary name"` marking exactly
     one row per group as the vessel's real name. Fixed by using that row, falling back to the
     group's first row if none is marked primary. Also: `IMO number` was observed WITHOUT the
     `IMO` prefix on at least one real row (Unique ID DPR0076, `8628597`) despite the task spec
     describing the format as always `IMO1234567` -- the prefix is stripped if present, not
     required, mirroring how OFAC's and EU's IMO fields are already handled defensively.
- **A real iterparse memory-management bug found and fixed before the first successful test run,
  not caught until then:** an early version called `elem.clear()` on every non-target "end" event
  while streaming a section (meant to bound memory), but `iterparse` fires "end" events bottom-up
  -- so a row's own descendants (e.g. `<Identity>`, `<Alias>`) each fired their own "end" event and
  got cleared BEFORE the enclosing row (`<Profile>`) was itself processed, wiping the very data the
  row-level extraction function still needed to read. Fixed by only calling `elem.clear()` on the
  row-level element itself (after extracting from it, which recursively frees its whole subtree)
  and on the section container when it closes -- never on an intermediate descendant. Documented
  in `ingest/sanctions.py` at each of the four affected functions so the same mistake is not
  repeated elsewhere in this project's other streaming parsers.
- **Real run 2026-09-21: 2,205 sanctioned vessel records landed at `data/reference/
  sanctions.parquet`** -- OFAC 1,540 (1,528 with IMO, 37 with no flag, dates 1989-01-05 to
  2026-08-24), EU 2 (both with IMO and flag, both 2022-12-12), UK 663 (662 with IMO, 143 with no
  flag, dates 2017-10-03 to 2026-08-06). Every real `designation_date_precision` resolved to
  `"day"` -- OFAC's partial-date path is implemented and unit-tested but not exercised by the
  current live snapshot. 34 new tests, `ruff` clean.

_2026-09-21_ (P3-2 session: sanctions-to-AIS identity join)

- **The join is IMO-to-IMO, not "by IMO and MMSI" in the literal sense of the task title.**
  Sanctions lists (OFAC/EU/UK) carry no MMSI field at all -- MMSI is a radio-transponder identity,
  not something a vessel registry or sanctions authority tracks. `process/sanctions_match.py`
  joins on the one identifier both sides share (checksum-valid IMO) and lets MMSI surface in the
  *output* as the AIS-side key that comes along once an IMO matches. Stated explicitly in the
  module docstring so a future reader of the task title doesn't expect a direct MMSI column on
  the sanctions side.
- **`process.identity.VALID_IMO_SQL` is imported and reused verbatim against
  `sanctions.parquet`'s own `imo` column, not reimplemented.** `mmsi_imo.parquet`'s `imo` is
  already checksum-valid by construction; `sanctions.parquet`'s `imo` is bare digits typed by a
  human at OFAC/EU/UK and had never been checked. Reusing the exact same SQL fragment both sides
  means a sanctions-list data-entry typo that isn't even structurally a real IMO cannot coincide
  with, or worse silently mis-join to, an unrelated real vessel.
- **A real `CAST` error found and fixed before the real run could be trusted.** The first working
  version computed the checksum-invalid sanctions set as `WHERE imo IS NOT NULL AND imo != '' AND
  NOT (VALID_IMO_SQL)` -- this crashed (`Conversion Error: Could not convert string '' to INT32`)
  against the real OFAC/UK data, but never crashed in the unit tests, whose only invalid fixture
  (`"1234568"`) happens to be a well-formed 7-digit string with a wrong check digit, not one of
  the malformed real values (wrong digit count / non-numeric) that actually triggered it. Root
  cause: `process.identity`'s own `WHERE ... AND VALID_IMO_SQL` form works safely on malformed
  strings because DuckDB short-circuits a top-level `WHERE` conjunction -- rows failing
  `regexp_full_match` never reach the later `CAST(substr(...) AS INTEGER)` conjuncts. Wrapping the
  identical expression in `NOT(...)` turns it into one nested boolean expression rather than a
  top-level conjunction, which loses that short-circuit and evaluates the `CAST` on non-numeric
  substrings of real malformed sanctions `imo` values. Fixed by computing the checksum-invalid set
  as an anti-join against the already-validated `sanctions_valid` view instead of re-evaluating
  `VALID_IMO_SQL`'s negation. Recorded here, not just fixed silently, because the exact same
  `NOT(VALID_IMO_SQL)` pattern would fail identically anywhere else in this project that ever
  wraps it.
- **Real run 2026-09-21 over the 30-day window: 205/2,191 checksum-valid sanctioned records
  matched (9.4%), 164 distinct mmsi -- NOT the near-zero result initially expected by analogy
  with P2-7's GFW-encounters comparison.** Before running, the working assumption was that a
  narrow 30-day Danish-only AIS window would barely intersect a global sanctions list, the same
  shape of scope mismatch P2-7 found against GFW. That analogy does not hold here: the Danish
  straits are not an arbitrary shipping lane but *the* chokepoint essentially all Baltic-origin
  (heavily Russian) crude and product oil transits (this project's own founding scope decision,
  see above) -- precisely the corridor a sanctioned-tanker "shadow fleet" would be expected to
  use, unlike GFW's fishing-economy encounter dataset, which has no reason to concentrate here.
  By source: UK 130/662 (19.6%), OFAC 75/1,527 (4.9%), EU 0/2 (too small a source, 2 vessels
  total, to read anything into). Sanity-checked before trusting the number: the denominators
  (2,191 checksum-valid + 1 checksum-invalid + 13 with no imo at all = 2,192 with-imo + 13 = 2,205
  total) reproduce P3-1's own real counts exactly; a random sample of matched rows (vessel name,
  imo, mmsi, AIS first/last-seen) was inspected by hand and is unremarkable -- plausible tanker
  names and MIDs, message counts and date ranges consistent with real June 2024 AIS traffic, no
  sign of a join fanning out or matching by accident. 41 of the 164 matched imo are corroborated
  by both OFAC and UK (both sanctioned the same vessel, expected for high-profile Russia-linked
  tankers after 2022); the remaining 123 are single-source. Neither of the other two ambiguity
  kinds occurred for real in this window: 0 matched rows have `mmsi_is_reused=true`, and 0 mmsi
  resolved to more than one distinct sanctioned imo -- both are implemented and unit-tested
  against synthetic fixtures (a real population this small and this window's near-total absence
  of MMSI reuse, see P2-5's own finding of exactly 1 reused mmsi project-wide, made both
  genuinely unlikely to occur here, not untested).
- **What this result does and does not mean, stated so P3-3 doesn't over-read it (see
  `docs/STATE.md`'s open questions).** A match means the sanctioned vessel's IMO was observed
  under some mmsi in this AIS window at all -- it is a presence signal, not a behavioural one, and
  says nothing about whether that presence overlaps the vessel's evasive activity or precedes/
  follows its `designation_date`. Turning "matched, designated on date D" into a temporally sound
  per-vessel-month label (not using a designation before it was actually knowable) is explicitly
  left to P3-3, per `CLAUDE.md`'s leakage rule -- not decided or pre-empted here.

_2026-09-21_ (P3-3 session: labelled vessel-month panel)

- **Three-way sanctions label split (`is_sanctioned_ever` / `is_sanctioned_as_of_window_end` /
  `is_sanctioned_after_window_end`), all three kept, none collapsed into a single label column.**
  P3-2 left "how to turn a sanctions match into a temporally sound label" as an open question
  (`docs/STATE.md`). A single boolean would either look backward (already-sanctioned vessels still
  transiting -- interesting, but not a forward prediction) or forward (designated after the
  observation window -- the genuinely predictive population), and conflating the two would let a
  model "predict" a designation that had already happened before the observation window, which
  isn't prediction at all. Keeping all three, plus `earliest_designation_date` and
  `n_sanctions_sources` (corroboration strength), lets Phase 4 make the actual modelling choice
  (most likely `is_sanctioned_after_window_end` as the positive class) with full information rather
  than having `features/panel.py` pre-empt it. Real run: 164 ever-matched, 17 already sanctioned as
  of window_end, 147 only after -- the 147 is the real, non-synthetic forward-looking population
  Phase 4 will most likely train on.
- **`gaps`/`spoofing`/`sts` are filtered into each vessel-month using each event's own timestamp as
  a `knowable_at` proxy (`gap_end`/`event_time`/`end_time`), NOT a verified temporal-leakage
  guarantee.** Unlike `identity_anomalies`/`behaviour` (which carry a real `knowable_at`, the
  product of a dedicated P2-5/P2-6 review pass), these three detector tables have no such column at
  all. All three are batch, whole-window detectors whose scores lean on whole-window computation
  (`detect.liveness`'s leave-one-out corroboration for `gaps`, repetition/pattern scores for `sts`)
  -- plausibly the same "only truly knowable at window_end" shape P2-5/P2-6 found and fixed for the
  other two tables, but nobody has checked this specifically. Using the event's own timestamp is
  harmless for the current single-month panel (both quantities land in the same month regardless)
  but is a documented, real blocker for P4-3's multi-month rolling-cutoff design -- recorded as its
  own open-question entry in `docs/STATE.md`, not left as a code comment.
- **Real run confirms the `knowable_at` filter is not always a no-op, even within a single month.**
  P2-5/P2-6 found every real `identity_anomalies`/`behaviour` event's `knowable_at` equalled
  `window_end` exactly in the 2024-06 build, which could have been (mis)read as "the filter never
  actually does anything here." Checked directly against the real panel build, not assumed: 6 of
  152 real `destination_course_mismatch` events have a voyage ending in the final minutes of
  2024-06-30, so `knowable_at` (`end_time + DEFAULT_GAP_HOURS`, per `detect.behaviour`) spills into
  2024-07-01 -- after `window_end` -- and the panel's filter correctly drops exactly those 6. Worth
  recording because it demonstrates the filter earning its keep on real data, not just passing a
  synthetic unit test.
- **No vessel-age data exists anywhere in this project.** Confirmed against the real clean-partition
  schema while building this module (the task spec already suspected this; verified, not assumed).
  Blocks P4-1's naive baseline exactly as specified; see `docs/STATE.md`'s open questions.
- **Real run 2026-09-21: `data/processed/vessel_month_panel.parquet`, 21,146 rows (one per distinct
  mmsi in the 2024-06 window, all in a single `year_month`), 4,881 with a valid imo (16,265
  orphaned, 1 reused).** `flag_country` resolved for 21,106/21,146 (99.8%); `ship_type` resolved for
  all 21,146. Detector aggregate totals reproduce every prior phase's own real-run numbers exactly:
  gaps 74,546 (72,631 >=0.6 probability), spoofing 10,098,760, sts 3,350 episode-sides (a vessel
  counted once per episode it participates in, on either side), identity_anomalies 265, behaviour
  152 `destination_course_mismatch` + 764 `draught_change_unexplained` before the `knowable_at`
  filter (146 after, see above). 15 new tests (345 total), `ruff` clean.

_2026-09-22_ (P3-3 review-and-pause session)

- **The "sts 3,350 episode-sides ... exactly" claim two paragraphs above is WRONG, caught by
  `analyst-review`, not by the original build.** Real raw count is 1,689 episodes = 3,378
  episode-sides; 3,350 is 3,378 minus 14 episodes (28 sides) whose `end_time` is exactly
  `2024-07-01 00:00:00`, correctly right-censored by the panel's `<= month_end_ts` cutoff filter.
  The filter's behaviour is correct and conservative; the word "exactly" above is simply false.
  Left the original paragraph unedited above (append-only) rather than rewritten, per this file's
  own convention -- this entry is the correction of record.
- **P3-3 was reverted from `done: true` to `done: false` in `tasks.json`, despite `features/panel.py`
  passing all 345 tests and landing a real, working panel.** Reason: `analyst-review`, run
  specifically because this panel is the direct input to Phase 4's modelling (per `CLAUDE.md`'s
  instruction to run it after any modelling-adjacent change), found four real defects that would
  silently corrupt Phase 4's results if left as-is -- an undocumented population restriction
  (orphaned MMSI can never be positively labelled, and `is_orphaned` is itself a feature, so a naive
  model would trivially "solve" the panel by re-deriving the sanctioner's own selection criterion),
  label columns indistinguishable from features by name, the sts miscount above, and an incomplete
  P4-3 blocker list. None of these is a live leak in the current single-month build, but the project
  standard here is that "tests pass" and "methodologically sound enough to build a career-relevant
  result on" are different bars, and `CLAUDE.md` is explicit that this project treats the second bar
  as the one that counts. A fix pass covering all four (plus four cheaper documentation-only
  corrections analyst-review also found) was fully specified and dispatched but was interrupted by a
  session rate limit before finishing -- see `docs/STATE.md`'s "In progress" entry for the complete,
  ready-to-resume fix list. Nothing from the fix pass is committed; `features/panel.py`'s docstring
  carries an explicit warning block (added this session) flagging that its prose describes the
  target end-state, not yet the real code, so a future reader isn't misled by the mismatch.
- **P3-3's fix pass finished and the panel rebuilt.** All 8 items resumed and completed: the
  `label_` prefix was actually applied to the five sanctions-derived SQL columns in
  `features/panel.py` and `tests/test_panel.py` (previously only the docstring described this), a
  new guard test (`test_label_columns_are_the_only_ones_naming_sanctions`) asserts the real panel's
  output column set matches exactly, and the docstring's stale warning block describing the
  interrupted state was removed. `data/processed/vessel_month_panel.parquet` was rebuilt with
  `--force`; the real numbers are unchanged (21,146 rows, 4,881 with a valid imo, 16,265 orphaned, 1
  reused, 164/17/147 sanctions split) -- confirming the fix pass only touched column names and
  documentation, not the underlying computation. 346 tests pass (345 + the new guard test), `ruff`
  clean. `tasks.json` marks P3-3 `done: true`.

_2026-09-22_ (planning session: where the project goes after P3-3)

- **Modelling is gated behind a discriminative check (new task P4-0), which must pass before P4-1.**
  Reason: an ad-hoc version run this session against the real panel says the detectors do not
  separate later-sanctioned vessels from the rest -- among tankers with a valid imo, positives show
  0.43 vs 0.73 gaps per voyage, 0.0% vs 0.5% any-STS, 5.9% vs 32.0% any-spoofing, with near-identical
  median message counts (23,245 vs 22,459), so it is not a raw-exposure artifact. Building models on
  features that demonstrably encode local operating volume rather than evasion would produce a
  result that looks fine and means nothing, which is exactly the failure mode `CLAUDE.md` exists to
  prevent.
- **Exposure is measured from `liveness.parquet`, not from the clean partitions.** Its
  `(cell_lat, cell_lon, cell_hour, mmsi)` grain yields observed-hours and observed-days per mmsi by
  distinct-count. Reason: it is the only exposure measure that survives discarding clean data, so
  the normalized features stay computable under P3-4's storage regime rather than silently becoming
  unbuildable the moment positions are deleted.
- **P4-0 (the check) runs BEFORE P3-4 (the storage work), reversing the order originally requested.**
  Reason: P3-4 is days of work whose entire purpose is enabling more data; if normalized features
  still fail to discriminate, more data of the same kind does not fix it and the effort would be
  wasted. The check is roughly half a day.
- **`detect.liveness` was investigated as a suspected blocker for discarding clean data and cleared.**
  `liveness_verdict` reads only `liveness.parquet` (2.1 MB/day), never clean positions, and
  `detect.gaps` reads only that plus `voyages.parquet`. The real obstacle is that it -- like every
  detector -- writes one whole-range file, so window N+1 overwrites N. Recorded because the opposite
  was assumed out loud earlier in the same session and the assumption drove the initial plan.
- **`liveness` will be partitioned by day rather than kept as a union view or appended-and-rewritten.**
  Reason: a DuckDB view is invisible to a fresh connection, and rewriting a multi-GB file once per
  window is quadratic I/O. Day partitions also make provenance exact for disjoint sampled ranges and
  give partition pruning, which `score_gap`'s per-gap call pattern needs to stay tractable.
- **`liveness_verdict`'s `baseline_hours_available` must be computed from actually-covered days, not
  a span.** Reason: with disjoint sampled windows the naive span at `liveness.py:407-414` over-counts
  the denominator, understating `expected_corroborators` and biasing every verdict toward
  `no_evidence` -- a silent, systematic miscalibration rather than a visible failure.
- **Artifact creation and data deletion are split into two commands that never run together**
  (`pipeline/window.py` creates and never deletes; `pipeline/prune.py` deletes, opt-in behind
  `--yes-delete`, quarantine-first via `data/.trash`, with a `.no-prune` kill switch, a per-run day
  cap, per-day fingerprints recorded before deletion, and all gates re-evaluated at deletion time).
  Reason: discarding clean data is the only irreversible operation in the project, and a bug in the
  creation path must not be able to reach it. The fingerprints plus a mandatory re-download drill
  convert "irreversible" into "verifiably reproducible from source".

_2026-09-22_ (P4-0 executed: the discriminative check)

- **P4-0 ran to completion. Verdict: GO, but narrowly, and only one detector family carries the
  signal.** `features/panel.py` gained `n_observed_hours`/`n_observed_days` (from
  `detect.liveness`, survives discarding clean data) and 21 `rate_*` columns (per-voyage,
  per-observed-day, per-1,000-messages, for each of `n_gaps`, `n_gaps_high_probability`,
  `n_spoofing_events_total`, `n_sts_episodes`, `n_identity_anomalies_total`,
  `n_destination_course_mismatch`, `n_draught_change_unexplained` -- `RATE_BASE_COLUMNS`), added
  alongside every existing raw count. `model/discriminative_check.py` compares
  `label_is_sanctioned_after_window_end` (147 positives) against never-sanctioned vessels (4,717
  negatives) within `imo IS NOT NULL`, excluding the 17 already-sanctioned-as-of-window_end rows
  from both classes, with AUC + 95% stratified-bootstrap CI + Mann-Whitney p per feature, in both
  an unmatched and a `(ship_type, n_observed_days quintile)`-matched scope (10 groups contain both
  classes; 3,011/4,864 rows fall in one -- effectively a Tanker-and-thinly-Cargo comparison, since
  135/147 positives are Tanker and 9 are Cargo, matching the ad-hoc check's own population).
  **Real result: `n_draught_change_unexplained` (raw AND all 3 of its rate variants) is the ONLY
  feature that discriminates in the expected direction, in BOTH scopes** (AUC 0.60-0.65 unmatched,
  0.60-0.62 matched, CI excluding 0.5 in every case). **Matching and normalization did not rescue
  any of the other four detector families** -- `gaps` and `spoofing` remain `discriminates_opposite`
  in both scopes (AUC 0.26-0.33), `sts` is `discriminates_opposite` with a tiny, barely-excluding-
  0.5 effect (AUC ~0.47-0.49), and `identity_anomalies`/`destination_course_mismatch` are
  `no_discrimination` in both scopes. Normalizing by exposure did not change any feature's verdict
  category from its raw count's own verdict -- the confound the plan set out to correct turned out
  not to be what was suppressing signal in the other four families; `n_draught_change_unexplained`
  was already discriminative even in raw form, exactly as the original ad-hoc check's own
  observation flagged (see the prior 2026-09-22 planning entry above).
- **Reading of the GO decision: real, but narrow.** Per the plan's literal rule (any matched,
  normalized feature discriminating with a CI excluding no-effect is a GO), P4-0 passes and P3-4
  may proceed. This must not be reported as "the detectors work" -- four of five detector families
  show no rescuable signal against this label in this window, with or without normalization. Phase
  4 should expect `draught_change_unexplained`-derived features to dominate any model's importance
  ranking, and the README's limitations section (`CLAUDE.md`'s working agreement) must say this
  plainly rather than let a headline AUC imply all five detectors contribute.
- **A real DuckDB correctness bug found and fixed while building the rate columns, before trusting
  any of the above.** A rate expression written as a bare `n_gaps / nullif(voyage_count, 0)`
  silently divided by NULL, not the intended 0, for every vessel-month with zero events that
  month: several joined tables (`_gaps_agg`, `_spoofing_agg`, `_sts_agg`, `_identity_agg`,
  `_behaviour_agg`) each define a real column with the SAME NAME as the panel's own
  `COALESCE(...)` output alias (e.g. both `g.n_gaps` and the SELECT's own `n_gaps`), and DuckDB
  resolves a later bare reference to the JOINed table's column over the SELECT's own alias. Caught
  by `tests/test_panel.py`'s existing zero-detector-events test, not assumed safe. Fixed by fully
  qualifying every rate numerator/denominator to its source table (`_RATE_NUMERATOR_SQL` in
  `features/panel.py`) rather than relying on same-SELECT alias back-reference at all.
- **A second real bug found while writing `model/discriminative_check.py`'s own tests: the
  `n_bootstrap` argument was accepted but silently ignored**, with the bootstrap loop always using
  the module-level default (2000) regardless of what was passed in. Invisible against real data
  (the CLI default already equals the hardcoded value), but made every test using a smaller
  `n_bootstrap` for speed hang for ~2 seconds per feature-scope evaluation instead of the
  milliseconds intended -- caught by the test suite unexpectedly timing out, not by inspection.
  Fixed by threading `n_bootstrap` through `_evaluate_feature` explicitly. The real 2026-09-22 GO
  result above was computed before AND after this fix with identical numbers (both runs used the
  same effective bootstrap count), so it is not affected.

_2026-09-22_

- **P3-4/A1 done: `detect.liveness` is now accumulable by day.** `build_liveness` writes one
  partition per day (`data/coverage/liveness/date=YYYY-MM-DD/part-0.parquet`, atomic temp-file +
  `os.replace`), skipping days already built unless `force=True`, so a later window's build never
  overwrites an earlier one. `liveness_verdict` now accepts either that directory (new default) or
  a single legacy whole-range file (still fully supported, `liveness_path.is_file()` branch) --
  see `_liveness_sources`. **A1.1's denominator fix**: directory mode computes
  `baseline_hours_available` as `24 * (exact number of baseline days present on disk)`, never the
  old span-based arithmetic (`max(window_end) - min(window_start)` from a file's own provenance),
  which silently overcounts under disjoint coverage and biases verdicts toward `no_evidence` --
  legacy file mode keeps the old arithmetic unchanged, since it is exact for a genuinely
  contiguous single build. Real migration: rebuilt `data/coverage/liveness/` partitioned over the
  full 2024-06-01..2024-06-30 window while the legacy `liveness.parquet` still existed side by
  side -- both hold exactly 3,988,982 rows. **Equivalence check passed exactly, row for row**:
  re-ran `detect.gaps.build_gap_scores` over the identical real window (2024-06-01..2024-07-01,
  the same exclusive end the original real run used) against the new partitioned liveness --
  74,546 candidate gaps, identical verdict breakdown (`receiver_alive` 72,631 / `no_evidence`
  1,311 / `area_dark` 604), and a row-level `EXCEPT` diff against the existing real `gaps.parquet`
  on `(mmsi, gap_start, gap_end, verdict, probability, n_corroborators, expected_corroborators,
  n_baseline_vessels)` found zero rows different in either direction. Expected, since this window
  is contiguous (A1.1's fix only changes behaviour under disjoint coverage) -- confirms the
  rewrite is a correctness-preserving refactor on real data, not just on synthetic tests.
- **A real performance bug found and fixed before the equivalence check could finish: a naive
  per-call directory scan made directory-mode `liveness_verdict` far too slow to use.** The first
  implementation resolved `_liveness_sources` with one `Path.exists()` stat per candidate day in
  range, called fresh on every `liveness_verdict` invocation. `detect.gaps` calls this once per
  candidate gap -- 74,546 times on the real window, each pulling in a ~30-40 day baseline range --
  so this was 2-3 million individual filesystem stats. Measured: did not finish rebuilding the
  real 30-day gap-score table in over 10 minutes (twice, including one run with `--out-path`
  isolated to rule out a write-contention explanation) before being killed. Fixed by
  `_list_liveness_days`, an `lru_cache`d one-time `glob("date=*")` per directory, replacing the
  per-day stat loop with a single directory listing reused for the rest of the process. Benchmarked
  on a real 200-gap subset before trusting it at full scale: directory mode with the cache is
  0.017s/gap (~21 min projected for all 74,546), actually ~3x FASTER than the legacy single-file
  mode's 0.056s/gap (~69 min projected) -- not merely "fixed", better than before. Not caught by
  the unit tests (all synthetic fixtures are far too small, a handful of days, to expose an O(days
  x calls) cost) -- a reminder that a real-scale timing run is a load-bearing verification step
  here, not optional, same lesson `detect.spoofing`/`detect.sts`/`detect.identity_anomalies` each
  already learned the hard way (see their own entries above).
- **Scope note: A1 only.** P3-4's remaining parts (A2 per-window artifacts for every detector plus
  a ship_type reference table, A3 thinned tracks, A4 `pipeline/window.py` orchestration, A5
  `pipeline/prune.py` deletion with quarantine, and the A0.4 re-download drill) are not started.
  `docs/PLAN_P4-0_P3-4.md`'s Part A spec still governs; nothing here reopens A1's design.

_2026-09-22_ (P3-4/A2: per-window storage for the five detectors, process.tracks/identity, and a
new ship_type reference)

- **Shared window-partition helpers landed in `process/partitions.py`** (`window_partition_path`,
  `atomic_write_parquet`, `partition_exists`, `git_sha` -- the last deduplicated from what had been
  a byte-identical `_git_sha()` copy in every one of the seven modules touched this session).
  `atomic_write_parquet` is temp-file-plus-`os.replace`, same pattern A1 established for
  `detect.liveness`. `partition_exists` exists because `Path.exists()` on a `window=*/...` glob
  string always returns `False` (no file is literally named that) -- every consumer whose default
  input changed from a single file to a glob needs this instead of a bare `.exists()` check, or
  its "does the input exist" precondition would always fail even with real data present.
- **`window=<start>_<end>/part-0.parquet` is the new layout for `detect.anchorages`/`spoofing`/
  `sts`/`identity_anomalies`/`behaviour` and `process.tracks`/`identity`.** Each `build_*`
  function's idempotency check is now simply "does this exact window's path exist" -- the old
  single-file layout's `ValueError` on a mismatched stored window is gone outright, since a
  different `[start, end]` now lands at a different path by construction; there is nothing left to
  mismatch. `detect.gaps` is deliberately NOT converted -- the plan's own A2 section lists it as a
  consumer (of `voyages_path`/`liveness_path`), not a producer, and its own `gaps.parquet` stays a
  single whole-range file.
- **DuckDB auto-detects the `window=...` Hive-style directory name and injects an extra `window`
  string column into `SELECT *`, even reading a single explicit file path, not only a glob.**
  Verified in `tests/test_partitions.py`. Never collides with the explicit `window_start`/
  `window_end` DATE columns every builder already writes, but a caller doing `SELECT *` over one of
  these paths should not be surprised by it.
- **A glob string works as a bound `read_parquet(?)` parameter, not just a literal.** A1's own
  docstring flagged this as unverified for the list-based liveness case; confirmed directly this
  session (`tests/test_partitions.py`) before relying on it everywhere a consumer's default now
  reads `data/<kind>/window=*/part-0.parquet`.
- **New `process/ship_type.py`: a raw `(mmsi, ship_type, type_of_mobile, n_messages)` reference,
  per window.** This is the plan's named "hard blocker" -- `features.panel._build_ship_type` and
  `detect.identity_anomalies._build_ship_types` both scanned clean partitions directly for this one
  static fact, which P3-4's whole point is to stop doing. Deliberately NOT the already-resolved
  modal ship_type: the two callers apply different resolution logic on top (the panel does no text
  normalization and no `type_of_mobile` filter; identity_anomalies normalizes/validates text and
  filters to `detect.liveness.MOBILE_TYPES`), and unifying those into one resolution here would
  have been a silent behaviour change disguised as a storage refactor. Both callers now read this
  reference and re-run their own prior logic over it via `sum(n_messages)` in place of the original
  `count(*)` over raw messages -- provably the same regrouping, verified byte-for-byte against the
  real window (see below).
- **`ship_type_reference_path`/`ship_type_reference_root` is resolved to the caller's own EXACT
  window, never a glob**, unlike every other consumer default changed this session. Both
  `detect.identity_anomalies.build_identity_events` and `features.panel.build_panel` already had
  the "computed over the WHOLE window, no month bound" limitation documented (P3-3); a glob
  default would have silently widened that from "this one window" to "every window ever built".
  Kept scoped to exactly what was already documented, not wider.
- **`features.panel.build_panel` lost `clean_root`/`CLEAN_ROOT` and the now-dead
  `existing_partitions`/`_partitions_union_sql` entirely.** Ship_type was their only remaining use
  in this module; fixing the hard blocker also removes panel.py's last direct dependency on raw
  clean data staying on disk, which matters once P3-4/A5 starts discarding it.
- **`mmsi_imo_path`/`voyages_path`/`liveness_path` (in `detect.gaps`, `detect.spoofing`,
  `detect.behaviour`, `features.panel`) default to a glob over every window built so far.** Safe
  for `voyages_path`/`liveness_path` because their own aggregate queries already filter to the
  relevant window/month (`_build_voyage_counts`'s explicit date bounds; `_exposure_agg`'s join on
  `year_month`) regardless of what else the glob contains. NOT safe in the same way for
  `mmsi_imo_path` in `features.panel` -- `_build_roster` has no date filter at all, so this
  genuinely widens the already-documented "whole window, no month bound" limitation from "this
  one window" to "every window ever built", noted explicitly in both the module and function
  docstrings rather than silently. Deferred to P4-3 exactly as P3-3 already promised, not solved
  here.
- **`process.sanctions_match.identity_valid` gained a `GROUP BY (mmsi, imo)`** the plain `SELECT`
  didn't have before. `identity_path`'s default glob can legitimately repeat a pair across windows
  for a vessel active in more than one; without the aggregation the later JOIN would emit one row
  per (sanctions record x window occurrence) instead of per (sanctions record x mmsi), silently
  multiplying match counts once a second window exists. Not observable against today's
  single-window real data (verified: the aggregation is a no-op there, `sum`/`min`/`max`/`bool_or`
  over exactly one row each), but a real bug the glob default would otherwise introduce silently
  the moment a second window is built.
- **`detect.identity_anomalies.build_vessel_links`'s own output stays one flat file, not
  window-partitioned** -- union-find over presumed-same-hull groups is inherently a whole-history
  recomputation, not a per-window append, so there was no overwrite problem to fix. Its INPUT
  default (`events_path`) did change, to a glob over every window's identity-anomaly events, so a
  link spanning two windows is not missed; the CLI's own call to it was wired to only the
  just-built window's events before this session and is fixed to use that default.
- **Real-window equivalence check, delegated to a background agent, PASSED for all nine real
  builds against the existing 2024-06-01..2024-06-30 legacy artifacts** (`process.tracks`,
  `process.identity`, `process.ship_type` [new, no legacy comparator], `detect.anchorages`,
  `detect.spoofing`, `detect.sts`, `detect.identity_anomalies`, `build_vessel_links`,
  `detect.behaviour`) -- row counts matched exactly on 8/9 (spoofing: 10,098,758 vs 10,098,760, see
  next entry), and every substantive column (excluding `built_at`/`git_sha`) matched exactly except
  a handful of floating-point last-bit differences in `voyages` (254/95,692 rows,
  lat/lon `arg_min`/`arg_max` under DuckDB's parallel execution), `anchorages`
  (273/900 rows, a summed `total_stationary_hours` float), and one `sts` row's `nav_status_b`
  (a `mode()` tie-break that happens not to change that row's confidence score). Wall-clock: tracks
  13m54s, identity 1m12s, ship_type 3s, anchorages 1m9s, spoofing 128m48s (concurrent with
  sts/identity_anomalies; dominated by a 10M-row `executemany` insert -- an existing cost, not
  something this session's changes made slower), sts 23m35s, identity_anomalies 3m51s, behaviour
  42s. 381 tests, `ruff` clean, both re-confirmed after the real run.
- **`detect.spoofing.check_impossible_speed`'s two-row discrepancy (5,497 vs the legacy run's
  5,499) is a pre-existing non-determinism, not a P3-4/A2 regression.** Root-caused: its
  `lag() OVER (PARTITION BY mmsi ORDER BY timestamp)` has no tie-break secondary sort key, so when
  an mmsi has duplicate `(mmsi, timestamp)` rows (confirmed real: mmsi 219018851 alone has 14 such
  duplicates on 2024-06-20 -- exactly the population `simultaneous_position` exists to catch),
  which row DuckDB treats as "previous" is non-deterministic across runs/parallelism, changing the
  implied speed for a handful of pairs. Present in the unmodified SQL before this session touched
  the file; this session's rebuild happened to surface it because it re-ran the check against the
  same real data a second time. Not fixed here -- would need an explicit tie-break (e.g. a stable
  row-id) added to the `lag()` window, out of scope for a storage-layout change. Revisit if
  reproducibility of exact spoofing counts across reruns ever matters downstream; `detect.sts`'s
  `nav_status` `mode()` tie-break (see above) is the same class of issue.

_2026-09-22_ (P3-4/A3 session)

- **P3-4/A3 done: `process/thin.py`, day-partitioned downsampled tracks
  (`data/tracks/thin/date=YYYY-MM-DD/part-0.parquet`) for the Phase 5 map and manual review only,
  never for re-detection.** Bucketing: `time_bucket(INTERVAL 'N minutes', timestamp)` +
  `row_number() OVER (PARTITION BY mmsi, bucket ORDER BY timestamp) = 1`, keeping
  `mmsi, timestamp, latitude, longitude, sog, cog, nav_status, ship_type`. Day-partitioned like
  `detect.liveness.build_liveness` (P3-4/A1), not window-partitioned like P3-4/A2's detectors, since
  a thinned day never changes once a later window is built -- idempotent per day, same contract.
  A real-schema mismatch found before the first real run: the clean partition's navigational-status
  column is named `navigational_status`, not `nav_status` (the plan's own shorthand, already used by
  `detect.sts`'s output) -- fixed by aliasing on read (`navigational_status AS nav_status`) so the
  output matches the plan spec and `detect.sts`'s naming without renaming the source column. 9 new
  tests, 390 total, `ruff` clean.
- **Real run over the full 2024-06-01..2024-06-30 window at the default 5-minute interval: 452 MB
  total, 26,333,818 rows, 21,146 distinct mmsi across 30 day-partitions.** One real day
  (2024-06-01) measured first: 18 MB, 985,725 rows, 7,232 distinct mmsi -- within the plan's
  10-25 MB/day estimate, so the default interval is kept rather than widened to 10-15 minutes. The
  full window's distinct-mmsi count (21,146) matches `features.panel`'s own whole-window roster
  count exactly, a real-data sanity check that thinning lost no vessel. Reduction ratio: 14.2 GB of
  clean data for the window down to 452 MB (~3.2%), the same order of magnitude as every other
  P3-4 storage reduction (see `docs/STATE.md`'s disk-budget section).

_2026-09-22_ (P3-4/A4 session)

- **P3-4/A4 done: `pipeline/window.py`, the per-window orchestrator -- creates only, never
  deletes.** `process_window(start, end, data_root, lead_in_days=30, min_free_gb, thin_minutes,
  force, dry_run)` runs, in order: `pipeline.backfill.backfill_range` and
  `detect.liveness.build_liveness` over `[start - lead_in_days, end]` (the lead-in is needed
  because liveness's own baseline looks backward `BASELINE_DAYS`, default 30, matched by this
  module's own default); then, over `[start, end]` only, `process.thin`, `process.ship_type`,
  `process.tracks`, `process.identity`, and the five detectors in their real dependency order
  (`anchorages` -> `spoofing`/`sts` -> `behaviour` -> `identity_anomalies`); then
  `_verify_window()` reads every artifact back with DuckDB (never just checks a file exists --
  A0.9) and returns a list of failures; only if that list is empty does it record an A0.3
  fingerprint (message count, distinct mmsi, timestamp range, bbox) and `verified_at` per day in
  `[start, end]` via `pipeline.manifest` -- the field `pipeline.prune` (A5, not yet built) will
  select on. A verification failure raises and writes nothing to the manifest; there is no
  partial-credit path.
- **Every downstream path is derived from `data_root`, not from each module's own hardcoded
  default constant.** Every producer module already accepts its root paths as overridable
  parameters (for its own tests' sake); `pipeline.window` passes an explicit override for every
  one of them, computed from `data_root` to mirror what each module's own default already is
  under `data_root=Path("data")`. This is what makes the whole orchestrator testable against
  `tmp_path` (9 unit tests, monkeypatching every downstream builder with a fake, the same pattern
  `tests/test_backfill.py` already uses) without needing to monkeypatch a dozen module-level
  constants, and it is also the only way a future non-default `data_root` could work at all.
- **A real-data bug found and fixed before trusting the fingerprint: raw `min`/`max` lat/lon
  produces a near-useless bbox, swamped by `process.clean`'s already-documented handful of
  corrupted-coordinate outliers (up to 89 deg latitude, see this file's P2-3 entry).** A real run
  over 2024-06-10..11 first produced `[-55.48, 64.13, -157.23, 123.7]` for a single Danish day --
  nearly the whole globe, useless as a re-download sanity check. Fixed by reusing
  `detect.spoofing`'s own already-proven fix for the identical problem: tail quantiles
  (`quantile_cont` at 0.1%/99.9%) instead of raw min/max. Re-run after the fix:
  `[54.14, 58.9, 3.46, 16.18]` and `[54.13, 58.53, 3.46, 16.11]` for the same two days --
  correctly the Danish/Baltic region, not the globe.
- **Real end-to-end validation run, 2024-06-10..2024-06-11 (`--lead-in-days 5`, so no new
  downloads -- the lead-in and window both fall entirely inside the already-backfilled
  2024-06-01..2024-06-30 month), with all real builders, not fakes.** Exercised the full real
  chain including the DuckDB spatial extension (`detect.anchorages`/`detect.sts`/
  `detect.behaviour`): 199 anchorage cells (all coastal), 509,726 spoofing events (dominated by
  `on_land`, 509,390, consistent with the dwell-time explanation already established for P2-3),
  55 STS candidates (mean confidence 0.384), 13 behaviour-contradiction events, 17
  identity-anomaly events -- all in the same real-data proportions as the full 30-day run,
  scaled down. `_verify_window` passed, both days got `verified_at` + fingerprint. A second run
  over the same window (no `--force`) confirmed idempotency end to end: every producer logged
  "already exists, skipping" and the window still re-verified and re-recorded cleanly. Wall-clock
  ~11 minutes, dominated by `check_on_land` (328.9s, consistent with its known per-day cost) and
  a large gap before `detect.sts`'s first logged stage (likely first-use `INSTALL`/`LOAD spatial`
  overhead, not investigated further -- out of scope for an orchestration task). 9 new tests, 399
  total, `ruff` clean.

_2026-09-22_ (P3-4/A5: the A0.4 re-download drill, then `pipeline/prune.py`)

- **A0.4 re-download drill PASSED: real fingerprint match, byte-for-byte, on 2024-06-15.** Chosen
  as an ordinary mid-month Wednesday already `clean` in the manifest, not touching the two edge
  days (06-01, 06-30) or the days already used by A4's own real validation window (06-10/06-11).
  Quarantined `data/clean/ais_dk/date=2024-06-15/` to `data/.trash/date=2024-06-15/` (same-
  filesystem rename), re-downloaded via `ingest.dma.download_day` and re-cleaned via
  `process.clean.clean_day` into a fresh partition at the same path, then compared A0.3
  fingerprints (`pipeline.window._day_fingerprint`, reused verbatim rather than reinventing the
  query) on both copies. **Exact match on every field**: `fingerprint_message_count` 10,839,896,
  `fingerprint_n_distinct_mmsi` 5,085, timestamp range `2024-06-15T00:00:00`..`23:59:58`, bbox
  `[53.94, 58.28, 4.49, 17.68]` -- identical to 12 decimal-rounded places on both sides, and
  `clean_rows`/`raw_rows` also matched the original manifest entry exactly (10,839,896 /
  19,279,718). **Conclusion: the DMA S3 archive is stable for re-download; the whole
  quarantine-then-delete strategy is verifiable in practice, not just in principle.** Per the
  plan's own A0.3 note, `sum(message_count)` collapses to the same `count(*)` already aliased
  `message_count` in `_day_fingerprint` -- the clean schema has no separate per-row message-count
  field to sum, so reusing the existing fingerprint function satisfies the spirit of the check
  without inventing a second, redundant query.
- **The download itself was NOT reliable this session -- three of four attempts timed out
  mid-transfer** (`httpx.ReadTimeout`, once at ~60s default client timeout, twice more even at a
  300s read timeout, after 89-181 MB of a ~189 MB zip had already streamed). Not reproduced as a
  server-side outage (each retry from byte zero eventually succeeded, including the one that
  passed), so read as this session's network being flaky against the S3 endpoint, not a DMA-side
  problem -- but real, not hypothetical: a production `pipeline.window`/`pipeline.prune` run over
  many days should expect to retry a download at least once. `ingest.dma.download_day` has no
  built-in retry logic today (a single `httpx.Client(timeout=60.0)` call, no backoff) -- not fixed
  here (out of scope for A5), flagged in `docs/STATE.md`'s open questions.
- **`pipeline/prune.py` done.** `prune(data_root, max_days=15, yes_delete=False)`, dry-run by
  default. Selects candidate days (`verified_at` present, `clean_discarded_at` absent) from the
  manifest, capped at `max_days`, then groups them into maximal CONTIGUOUS runs -- the manifest
  records `verified_at`/the A0.3 fingerprint per DAY with no window boundary alongside it, so a
  run's own `(min day, max day)` is used to locate the exact `window=<start>_<end>` artifact
  `pipeline.window` would have written for that call, since a single `process_window` call's days
  are always contiguous. Verified against the real manifest: only 2024-06-10/06-11 currently carry
  `verified_at` (from A4's own validation run), and they form exactly one contiguous run matching
  the real `window=2024-06-10_2024-06-11` artifacts on disk -- confirms the grouping strategy
  works on real data, not just synthetic fixtures.
- **Every A5 gate re-implemented to re-read the artifacts, never trust the manifest's earlier
  `verified_at`**: window-partitioned artifacts (ship_type, anchorages, spoofing, sts, behaviour,
  identity_anomalies) opened by exact path plus a `window_start`/`window_end` column cross-check
  where the artifact carries those columns (voyages/identity don't -- see `process/tracks.py`/
  `process/identity.py`, neither writes them, so provenance for those two rests on the path alone,
  which already encodes the window); day-partitioned artifacts (liveness, thin) checked directly,
  independent of any window's boundaries, since neither is ever discarded by this module; fresh
  `count(DISTINCT mmsi)` comparison (thin >= 95% of clean) and day-count comparison (thin days ==
  clean days) computed from the files, not from any prior run's numbers; free disk, `.no-prune`,
  and `--yes-delete` checked at deletion time, not build time. A gate failing for one contiguous
  run blocks only that run's days, not the whole invocation -- an independent run (e.g. a
  different sampled window elsewhere in the manifest) is unaffected, covered by
  `test_one_group_failing_does_not_block_an_independent_group`.
- **Statistical sanity gate compares each window-partitioned event table's per-day rate against
  the real June 2024 totals already in `docs/STATE.md` (impossible_speed, on_land, synthetic_
  circle, simultaneous_position, sts, identity_anomalies, behaviour -- 7 of the plan's 8 named
  checks), flagging a >10x deviation in either direction.** `detect.gaps` is the one named check
  NOT covered: it is still a single legacy whole-range file (`data/detect/gaps.parquet`, never
  window-partitioned -- P3-4/A2 deliberately left it as a `pipeline.window` consumer, not a
  producer, see this file's A2 entry), so there is no per-window gap count for `pipeline.prune` to
  compare against a per-day rate. Logged once per run rather than silently absent; not fixed here
  since converting `detect.gaps` to window-partitioned output is outside A5's scope.
- **Quarantine-then-empty (A0.2) implemented as: `_empty_trash` always runs before any new day is
  quarantined in the same call, and only removes a `.trash` entry once `pipeline.manifest.
  day_state` reports `"reduced"` for that day** (i.e. `clean_discarded_at` is actually on record,
  not merely attempted) -- so a day this very invocation quarantines can never also be the one
  permanently emptied by it; real emptying happens only on the invocation after, exactly as A0.2
  specifies. An entry whose manifest state is anything else (a crash between the rename and the
  `manifest.record` call) is left in `.trash` untouched and logged loudly, not guessed about.
- **Manifest backup (`data/manifest.json.bak`) happens unconditionally, before the `.no-prune`
  check's `return` is the only thing that can skip it** -- i.e. `.no-prune` skips EVERYTHING
  including the backup (nothing is touched at all, per the plan's literal wording), but a dry run
  (`yes_delete=False`) still backs up the manifest, matching the plan's own step ordering (abort
  check, then backup, then everything else, all before the yes_delete branch).
- **30 new tests** (`tests/test_prune.py`, all synthetic `tmp_path` fixtures, most disabling the
  statistical sanity gate via `monkeypatch.setattr(prune, "REFERENCE_EVENT_RATES_PER_DAY", {})` so
  other gates' fixtures don't also need real-scale event counts -- the sanity gate itself gets
  dedicated tests with a small explicit reference dict) cover: happy path (prune with
  `--yes-delete`), dry run touches nothing, `.no-prune` aborts with nothing touched (not even the
  manifest backup), `--max-days` caps candidates to the earliest N, each of the 11 gates failing
  independently blocks only its own day/run (missing artifact, unreadable artifact, empty required
  artifact, zero-row event detectors NOT blocking, missing/mismatched thin day count, thin mmsi
  ratio below/at the 95% threshold, incomplete/complete liveness lead-in, missing fingerprint
  field, provenance mismatch, low/sufficient free disk, statistical sanity deviation below/above/
  within 10x in both directions), one group failing does not block an independent group, `.trash`
  emptying (confirmed-reduced entries removed, dry-run leaves them, unconfirmed entries always left
  alone, this invocation's own quarantine survives until the next), and selection edge cases
  (already-reduced day is not a candidate, a day without `verified_at` is not a candidate, no
  candidates is a clean no-op). 429 total, `ruff` clean.

_2026-09-23_ (P4-1: the naive baseline, unblocked)

- **P4-1 shipped as three nested rules (R1 tanker, R2 tanker+FOC, R3 tanker+FOC+age), not one,
  because the age term's viability could not be known in advance.** `CLAUDE.md`'s original literal
  definition ("tanker over 15 years old under a flag of convenience") was blocked since P3-3: no
  build-year data exists in any source this project ingests. Rather than block the whole baseline
  on resolving that, R1/R2 were shipped as a standalone, immediately publishable floor, with R3
  wired to activate only if a later-built acceptance gate (`model.build_year_gate`) says GO — see
  below for why it didn't. `model/evaluation.py` was extracted from `model/discriminative_check.py`
  (P4-0) first, so both P4-0 and P4-1 share one definition of the evaluation population and one
  bootstrap implementation — verified byte-identical (real `discriminative_check.parquet` rebuilt
  with `--force` and diffed against a pre-refactor backup; every column matched except
  `built_at`/`git_sha`, which are expected to change).
- **`process/foc.py`: the ITF's 48-registry flags-of-convenience list, hardcoded and dated
  2026-09-23**, not fetched — the ITF's page (`itfseafarers.org/.../current-registries-listed-focs`)
  carries no last-updated date and no machine-readable feed, so a live scrape would be a
  silent-drift dependency, not reproducibility. 43 of the 48 names matched `process.mid.
  MID_COUNTRY`'s values verbatim; 3 were name variants resolved by hand (`St Kitts and Nevis` →
  `Saint Kitts and Nevis`, `St Vincent` → `Saint Vincent and the Grenadines`, `Tanzania (Zanzibar)`
  → `Tanzania`); 2 (`French International Ship Registry (FIS)`, `German International Ship
  Registry (GIS)`) have no MID-level counterpart at all — second/international registers invisible
  at MID granularity — and are left unmapped, with a guard test asserting every ITF name is either
  mapped or explicitly accounted for.
- **Real R1/R2 numbers on P4-0's own population** (4,864 rows, 147 positive, 3.02% prevalence):
  R1 (tanker) flags 893, 135 true positives, 15.1% precision, 91.8% recall, 5.0x lift. R2
  (tanker+FOC) flags 572, 125 true positives, 21.9% precision, 85.0% recall, 7.2x lift. **This is a
  demanding baseline even before any age term** — these are the numbers P4-2/P4-3 must beat.
  **precision@20 came out 0.000 for both, and this is NOT a random draw that "happened to" miss —
  an `analyst-review` pass caught the original write-up of this entry making exactly that false
  claim.** `model.baseline`'s tie-break for "top 20" is (rule value DESC, mmsi ASC), and an MMSI's
  first three digits are its MID, i.e. its flag state — sorting by mmsi ascending sorts by flag,
  not neutrally. R2's real top-20-by-mmsi is 100% Cyprus (the lowest-numbered flag-of-convenience
  MID), which contributes 0 of R2's 125 true positives; the real positives cluster in
  higher-numbered MIDs (Panama, Gabon, Cook Islands, Liberia, Marshall Islands — see the flag
  concentration finding below). precision@20 = 0.000 is therefore a **deterministic property of
  the sort key**, and would read 0.000 on every future run of this window regardless of how good
  R1/R2 are, unless a low-MID flag is designated. Fixed: `model.baseline`'s module docstring and
  summary text now say this explicitly, and `n_tied_at_cutoff` (893/572, i.e. every flagged row
  ties the boundary) is documented as evidence the top-20 slice is arbitrary among flagged rows,
  not proof of what drove the number. **The number to compare P4-2/P4-3 against is precision at
  n_flagged (plain `precision`, already reported per rule), never precision@20 from this module.**
- **Build-year sourcing: GFW and Equasis/GISIS ruled out, Wikidata chosen, confirmed live
  2026-09-23.** GFW's `/v3/vessels/search` `registryInfo` has no build-year field (confirmed
  against live API documentation). Equasis and IMO GISIS both explicitly prohibit bulk/automated
  extraction in their terms of use — ruled out on licence grounds, not difficulty. Wikidata's
  public SPARQL endpoint (`query.wikidata.org/sparql`) is CC0 and needs no token; live count
  2026-09-23: 96,571 ship items with an IMO number, most also carrying a build/service date.
  **Real quirk: Wikimedia's User-Agent policy is enforced, not just documented** — an unlabelled
  request gets HTTP 403; `ingest/wikidata_ships.py` sends a descriptive User-Agent. **Real quirk: a
  P729/P571/P458 statement marked "unknown value" surfaces as a blank-node genid URI, not a
  literal** (real example: Q12329788) — this was not anticipated from documentation alone and was
  only found by running the real fetch, which crashed on the first attempt (`WikidataSchemaError`
  raised at `datetime.fromisoformat` on a genid URL); fixed by checking each binding's `type` field
  before treating it as a date literal, and now covered by a dedicated test. Reconciliation (per
  IMO, agree within ±1 year → keep the earliest; disagree → NULL + `is_ambiguous`) run for real:
  95,503 distinct checksum-valid IMO (17 raw literals were not 7 digits, dropped), 92,112 with a
  usable `build_year`, 18 ambiguous.
- **`model.build_year_gate`'s real verdict: NO-GO — R3 stays blocked, and this is the correct,
  informative answer, not a failure to resolve the question.** Three gates run against P4-0's exact
  population: **G1 coverage PASSED** (85.26%, well above the 50% bar — Wikidata is a strong source
  by volume). **G2 differential coverage FAILED, in both scopes tested**: vessels sanctioned after
  window_end have a Wikidata-coverage rate of 94.56% vs 84.97% for never-sanctioned ones over the
  whole population (9.59pp gap, Fisher p=0.00054), and — after an `analyst-review` pass found the
  whole-population test alone could pass even if the gap were worse inside the specific vessels R3
  scores — a SECOND Fisher test restricted to the tanker+FOC (R1 AND R2) subpopulation was added
  (`model.evaluation.add_tanker_foc_columns`, shared with `model.baseline` so both modules use one
  rule definition): real result 95.20% vs 83.89% (11.31pp gap, Fisher p=0.00061), WORSE than the
  global test, confirming the risk was real, not hypothetical, even though in this particular run
  it didn't flip the overall verdict (both scopes already failed independently). Both must pass;
  either failing fails G2. The bare `has_build_year` indicator's AUC against the label (0.548, 95%
  CI [0.527, 0.565], excluding 0.5) is reported for context but no longer independently drives the
  verdict — `analyst-review` showed it is algebraically the same statistic as the coverage gap for
  a binary predictor (AUC = 0.5 + gap/2 exactly: 0.5 + (0.9456-0.8497)/2 = 0.5480, matching the
  real recorded value), so letting its own CI fail G2 made `MAX_COVERAGE_GAP_PP`'s 5pp
  effect-size floor dead weight at any population size large enough for a small, practically
  irrelevant gap to still exclude 0.5. This is real, measured evidence of exactly the contamination
  mechanism the plan worried about before any data was fetched: a sanctioned vessel is more likely
  to have a Wikidata page, independent of its actual age. **G3 plausibility FAILED in isolation** (1
  of 4,147 covered rows — mmsi 211401960, imo 9832767 — has a `build_year` of 2025, after the
  window it was observed transmitting AIS in; verified by hand this is isolated Wikidata source
  noise, not a join bug, since exactly one Wikidata item claims that IMO). G3's original design (a
  hard `ValueError` on ANY future build_year, per the approved plan) was revised after this real
  finding: an isolated case (below `MAX_IMPLAUSIBLE_FRACTION_BEFORE_RAISE`, 1%) now fails G3 but
  lets the gate complete and report — a `ValueError` crash would have hidden G1/G2's already-
  decisive verdict behind a traceback; a rate above that threshold still raises, since that pattern
  would look systemic (a real join bug) rather than isolated noise. Overall verdict: NO-GO.
  `model.baseline` correctly reports R3 as `blocked_by_gate` with the specific reason
  (`model.build_year_gate verdict is 'NO-GO', not GO`), never silently.
- **`model.baseline._resolve_r3_availability` now checks the gate's window and freshness, not just
  its verdict — an `analyst-review` finding that was real but not yet exercised.** Originally it
  only read `overall_verdict`. Per the module's own design note ("the gate, not a hardcoded flag,
  is the switch"), a future GO computed for one window would otherwise have silently authorised R3
  the moment the panel was rebuilt for a DIFFERENT window, or the moment `ingest.wikidata_ships`
  was re-fetched after the gate last ran — a real temporal-safety gap by construction, even though
  it never fired in this session (only one window and one Wikidata snapshot exist so far). Fixed:
  R3 is now blocked, with the specific reason, if the gate's own `(window_start, window_end)`
  doesn't match the panel being scored, or if `build_year_gate.parquet`'s file mtime predates
  `ship_build_year.parquet`'s.
- **`CLAUDE.md` and `tasks.json` updated to match the shipped rule (tanker + flag of convenience),
  not the original age-inclusive definition** — the age term was investigated in good faith with a
  real, viable, legally-usable source, and rejected on real measured evidence of label
  contamination, not abandoned for lack of trying. If `ingest.wikidata_ships` is ever re-run against
  a future window and `model.build_year_gate` returns GO, R3 activates automatically in
  `model.baseline` with no code change — the gate, not a hardcoded flag, is the switch.
- **The positive class R2 flags is heavily flag-concentrated, confirmed on the real data**: within
  R2's 572 tanker+FOC rows, Gabon is 34/34 (100%) positive, Cook Islands 22/26 (85%), Panama 44/80
  (55%), against Liberia 7/93 (8%) and Marshall Islands 5/93 (5%). A meaningful share of R2's 7.2x
  lift is the baseline re-deriving OFAC/UK's own shadow-fleet targeting by registry, not detecting
  vessel behaviour — consistent with, and now direct evidence for, the label-bias limitation
  already stated in the README. Worth citing next to the 7.2x number wherever it's quoted, not only
  in the README's general statement.
- **Real DuckDB syntax bug found in three new modules (`model.baseline`, `model.build_year_gate`):
  `by` is a reserved keyword and cannot be used as a table alias** (`LEFT JOIN ... by ON by.imo =
  ...` raised `ParserException: syntax error at or near "by"`). Renamed to `byr` in both. A small,
  easy-to-hit trap worth remembering before reaching for `by` as a join alias in this codebase
  again.
- **Real DuckDB type bug found by `analyst-review`'s window/freshness fix exposing a test edge
  case: embedding a Python float literal directly in a `SELECT ... AS col` (rather than binding it
  through a typed `CREATE TABLE` + `executemany`) makes DuckDB infer `DECIMAL(17,16)`, not
  `DOUBLE`.** Confirmed on the real, already-built `build_year_gate.parquet`: every float column
  (`g1_coverage`, `g2_fisher_p`, `g2_auc`, ...) was `DECIMAL`, not `DOUBLE` — silently, since
  DuckDB never raises for this, and a test only caught it because `pytest.approx` can't mix
  `float`/`Decimal` arithmetic. `model.baseline` and `model.discriminative_check` were already
  unaffected (both build their results via a typed `CREATE TABLE` DDL + `executemany`, the correct
  pattern) — `model.build_year_gate` was the one module that embedded literals directly, now fixed
  to match the other two. Worth remembering as a general rule for any future module: never embed a
  Python float in SQL text, always bind it through a typed column.
- **An `analyst-review` pass on the finished P4-1 work found 6 real, confirmed issues (the ones
  above — misleading precision@20 framing, the gate's window/freshness gap, G2's whole-population
  blind spot, the redundant AUC/Fisher branches, the DECIMAL type bug, and the flag-concentration
  evidence) plus several smaller/lower-impact ones left as documented limitations rather than
  fixed**: `has_date_conflict`/`date_source` are recorded by `ingest.wikidata_ships` but not yet
  consumed by the gate or baseline (P571-vs-P729 heterogeneity near the 15-year cut, and the
  ±1-year reconciliation rule's `min()` choice, both bias R3 slightly toward firing more often —
  moot while R3 is blocked); every module's idempotent-skip (`out_path.exists() and not force`)
  doesn't compare against the input panel's own freshness, a pattern this project already uses
  everywhere (`model.discriminative_check`, `process.sanctions_match`, ...), not something unique
  to introduce a special case for here; and the project's single real window (2024-06 only) means
  P4-2/P4-3 cannot yet do a real temporal train/test split — R1/R2's in-sample reporting is honest
  for unfitted rules, but this is the largest real constraint facing P4-2, and belongs in its own
  task's scoping, not fixed retroactively in P4-1.

_2026-09-23_ (second real window: the DMA archive is not "2006 onwards" as documented)

- **`docs/DATA_SOURCES.md` claimed daily DMA files "from 2006 onwards"; the bucket does not
  actually serve that range right now.** Discovered while scoping P4-2/P4-3's documented
  prerequisite (a second real window, for an honest temporal train/test split — see P4-1's close
  above). Before picking dates, probed `https://s3.eu-central-1.amazonaws.com/aisdata.ais.dk/
  {year}/aisdk-{day}.{zip,csv}` with per-day HEAD requests spanning 2020-01-01..2026-09-20.
  Result: 200 for every date from **2024-03-01 through 2025-02-26 inclusive**, 404 everywhere
  else tested — all of 2023, January/February 2024, and every date from 2025-03-01 onward,
  including recent 2026 dates. The already-built 2024-06 window sits inside this range, so
  nothing already shipped is affected, but the "2006 onwards" claim (from the module docstring
  and `docs/DATA_SOURCES.md`, based on the archive's documented naming scheme, not verified
  end-to-end) is wrong for what is actually reachable today. Corrected in
  `docs/DATA_SOURCES.md`.
- **Read as a fixed ~12-month snapshot currently exposed by the bucket, not a rolling
  most-recent-N-months window** — the available range ends 2025-02-26, over a year and a half
  before today (2026-09-23), which rules out "keeps the trailing 12 months relative to now" as
  the mechanism. Not investigated further (out of scope — DMA gives no public retention policy
  to check this against), but worth remembering: a future session extending the range further
  must re-probe rather than trust either the old "2006 onwards" claim or this snapshot's own
  boundaries, since both could be wrong by the time anyone reads this.
- **Consequence for `pipeline.prune`'s A0.4 re-download drill (P3-4/A5, 2026-09-22):** that drill
  confirmed re-download stability for 2024-06-15, which is still inside today's reachable range,
  so the drill's PASS verdict stands. But the drill's implicit assumption — that any previously
  verified day can always be re-downloaded later — does not hold in general if this bucket
  rotates older content out over time. Not a regression of anything already built; a caveat for
  whoever next relies on re-download as a safety net for a day this session or a future one
  eventually finds has aged out of range.
- **Second window chosen: 2024-11-01..2024-11-30**, same 30-day size as the existing June window
  for direct comparability. Picked to sit comfortably inside the confirmed range including its
  30-day lead-in (`pipeline.window`'s liveness baseline reaches back to 2024-10-02, also
  confirmed live), ~5 months from June 2024 for both a genuine temporal separation and seasonal
  variety (autumn Baltic vs. summer), and far enough from both range edges (2024-03-01,
  2025-02-26) to leave margin if the boundary turns out to be less exact than the single day-by-
  day probe suggests. Built via the existing `pipeline.window` orchestrator (P3-4/A4) with no
  code changes — see `docs/STATE.md` for the real run's numbers.
- **`ingest/dma.py` retries a transient `httpx.TransportError` in place, per URL, up to 5 times
  with exponential backoff (2s, 4s, 8s, 16s), before moving to the next filename pattern.** Reason:
  the second window's download stalled repeatedly against this session's flaky connection
  (`ReadTimeout`, occasionally `ConnectError`) — confirmed blocking, not cosmetic: a naive
  whole-pipeline retry loop only advanced ~1 real day per attempt. Scoped deliberately narrow: a
  real 404 (`httpx.HTTPStatusError`) is NOT retried, it still falls straight through to the next
  pattern as before — only a network-level failure gets the backoff treatment, so a genuinely dead
  host still fails in bounded time (~30s/pattern) rather than hanging. This was the open question
  already on record in `docs/STATE.md` about `download_day` lacking retry logic; now resolved by
  code, not just noted.
- **The second window's build was stopped mid-run (during `detect.spoofing`'s `on_land` check) for
  a planned machine shutdown, and confirmed safe to interrupt at any point before
  `pipeline.window._verify_window` runs.** Reason: `build_spoofing_events` only writes its output
  file once, after all four checks finish — killing the process mid-check leaves no partial file on
  disk, and no `verified_at`/fingerprint is recorded until every artifact for the window passes
  verification (P3-4/A4's own all-or-nothing design). Confirmed empirically: no stray temp files,
  no partial `data/detect/spoofing` output for the window, no manifest entries with `verified_at`
  for any November day. Resume with the same `pipeline.window` invocation; every artifact already
  built (60 days clean, tracks, identity, anchorages, ship_type, `impossible_speed`) is skipped as
  already-present.
- **A stray background process from an earlier, abandoned retry attempt was left running
  unsupervised and raced a later attempt against the same `data/` tree, corrupting one clean
  partition (`2024-10-23`, truncated parquet).** Caught by a real DuckDB read + row-count
  validation pass over every clean partition (not just the one that errored) before trusting
  anything — every other day (2024-10-02..2024-10-22, all 30 of November) validated intact.
  Corrupted partition deleted; harmless to rebuild (raw data is always re-derivable). Operational
  lesson, not a code defect: confirm a previous background invocation has actually exited (not just
  that its own wrapper/log reported completion) before launching another one against the same data
  directory — Windows background process tracking in this environment does not guarantee a killed
  or superseded shell also kills its python child.

_2026-09-25_ (P4-1b close: on_land parallelized, second window finished)

- **`detect/spoofing.py`'s `check_on_land` was parallelized (8-worker `ThreadPoolExecutor` over
  day partitions instead of a serial Python `for` loop), to use more of this machine's CPU
  headroom (see the [[feedback-hardware-parallelism]] memory note).** Reason: last session's real
  run measured only ~18% CPU use (3 of 16 logical threads) during this check, because the per-day
  point-in-polygon join ran one at a time over a single shared DuckDB connection. Real result: 58
  min for the November 30-day run, faster than even the healthy June baseline (94.3 min), not just
  the >2x-degraded November-before-the-fix one.
- **`_land_pieces` changed from a `TEMP TABLE` to a regular `TABLE` as a required part of the
  parallelization above, not a style choice.** Reason: verified empirically before trusting the
  change that DuckDB connections created via `con.cursor()` (one per worker thread) cannot see the
  parent connection's TEMP tables — only the shared catalog. Regular tables, and the already-loaded
  spatial extension, are visible fine from every cursor with no extra `LOAD spatial` call needed.
  Without this fix, every worker thread would have failed with a real Catalog Error.
- **The Windows Defender real-time-protection exclusion for `data/`, requested last session, is
  now applied** — the user ran `Add-MpPreference` directly in an elevated PowerShell, after Claude
  Code's own auto-mode classifier again refused to run it (flagged `[Security Weaken]`, consistent
  with last session). Not independently re-measured in isolation from the `on_land` parallelization
  above, since both landed in the same run — see the combined real timing in `docs/STATE.md`'s
  P4-1b entry.
- **`detect/sts.py` was NOT parallelized this session and is now the slowest stage (76 min for
  November, vs. 17.6 min for June) — left as an open question, not chased down.** Reason: its own
  ~62-minute unlogged gap before named stages begin was only noticed after `on_land`'s fix had
  already made it the new bottleneck; diagnosing it (unlogged setup cost vs. a similar serial-loop
  pattern) is separate work, not blocking P4-2. See `docs/STATE.md`'s Open questions.

_2026-09-25_ (P4-2: Isolation Forest, first out-of-time evaluation)

- **`features.panel.build_panel` now reads only its own window's partitions, never a `window=*`
  glob.** Found before building the first November panel: every detector join bounds events only
  from above (`<= month_end_ts`), so with June and November both on disk (plus the small
  `2024-06-10_2024-06-11` validation window) a November panel read through the globs would have
  counted every June event too, and the roster/representative imo would have mixed windows.
  Identity, voyages, ship_type and all five detector tables (gaps now included, at
  `data/detect/gaps/window=<start>_<end>/`) resolve to the exact window partition. Verified: the
  June panel rebuilt this way matches the legacy `vessel_month_panel.parquet` on every row and
  column except `n_impossible_speed` on 14 rows (5,499 vs 5,497 events), the already-documented
  `lag()` tie-break non-determinism. One panel per window, at
  `data/processed/panel/window=<start>_<end>/part-0.parquet`; the legacy `PANEL_PATH` is untouched
  so P4-0/P4-1's own outputs stay reproducible.
- **November gaps computed separately** (`detect.gaps` is not part of `pipeline.window`):
  26,203 candidates (June: 74,546), end-exclusive `--end 2024-12-01`, matching June's
  `--end 2024-07-01` convention; ~10 min serial, measured before deciding not to parallelize it.
  June's legacy `gaps.parquet` was copied (not moved) into its own window partition.
- **`sanctions_matches.parquet` rebuilt over both windows' identity** (164 -> 282 matched mmsi).
  Without it, vessels seen only in November would have been labelled negative. June's labels are
  unaffected (the panel joins by its own window's imo). November: 26 already sanctioned by
  window_end, 139 forward positives, population 4,449.
- **The baseline replicates out-of-time.** R2 on November: 574 flagged, 117 tp, precision 0.204,
  lift 6.5x (June: 0.219, 7.2x). R3 correctly stays blocked (the build-year gate was computed for
  June's window).
- **Isolation Forest loses to the baseline, decisively, and this is the finding, not a bug.** Fit
  on June (no labels), scored on November, compared at a matched alert budget (k = R2's own flag
  count per split) with a paired row-bootstrap CI on the precision difference. Test: `detectors`
  AUC 0.364, precision@574 0.016; `detectors_context` (+ is_tanker, is_foc) AUC 0.529, precision
  0.017; both CIs entirely below 0 vs R2's 0.204. Stable across 10 seeds. The top-ranked
  "anomalies" are dockside-dwelling, heavily-observed local traffic (median 67 `on_land` events vs
  0; 12.5% tankers vs 21%) — the same P4-0 finding restated as a model: sanctioned vessels generate
  *less* detector signal inside Danish coverage, so "most unusual" points away from them. Even
  handing the forest R2's own two inputs doesn't help, because an anomaly score has no reason to
  weight "tanker" as risky. Per `CLAUDE.md`, the problem is in the features, not the model; P4-3's
  supervised model is the right tool to test whether any feature adds to R2.
- **No GPU for P4-2.** ~5k rows x ~60 features per window; the whole run (2 variants x 11 forests
  x 1,000 trees + 2,000-resample bootstraps) takes ~50s on CPU with `n_jobs=-1`. cuML has no
  native Windows build. Revisit for P4-3 only if the panel grows by orders of magnitude.
- **`analyst-review` pass before close: no blockers.** Independently confirmed: no November data
  reaches the fit or the preprocessing; the rebuilt `sanctions_matches` changes 0 of 21,146 June
  rows (imo, both labels, designation date); every November detector partition has 0 events before
  2024-11-01; the 26,203 November gaps reproduce from the November voyages alone. Acted on:
  the regression test now covers all five detector tables plus the default roster; the docstring
  no longer claims the population filter is exactly knowable at window end (2026 snapshot,
  delistings/re-designations) and lists every feature; the summary now reports lift (0.50x on
  test: *worse than random*), AUC separately for vessels seen vs unseen in June (0.305 / 0.430 —
  the inversion holds for both, so it isn't vessel novelty), and the seasonal-drift and label-bias
  caveats. **Recorded for P4-3, not fixed here: the two windows' label periods overlap** — 121 of
  June's 147 forward positives were designated after 2024-11-30, so a supervised model trained on
  June's labels would be learning November-period outcomes; P4-3 must cap the training label at
  designations in (train window_end, test window_start). Design was fixed before looking at
  November; no sign flip applied after seeing AUC < 0.5.

_2026-09-25_ (P4-3: LightGBM, one out-of-time cutoff)

- **Training label is as-of-cutoff, not `label_is_sanctioned_after_window_end`.** Positive iff
  designated in (train window_end, test window_start) = (2024-06-30, 2024-11-01); every other
  training row is negative, including the 121 June vessels designated after 2024-11-30 (excluding
  them would itself use future information; labelling them negative is the conservative choice
  and can only hurt the test result -- 42 of them are November positives). **This leaves 16
  training positives, all tankers** -- the binding constraint on everything below.
- **Result (test = November, k = 574 = R2's flags):** `context` (is_tanker, is_foc) reproduces R2's
  flagged set exactly (0.204, verdict `reproduces_r2`, a sanity check that the pipeline learns the
  rule when given only its inputs); `detectors` alone 0.143, loses (CI [-0.088, -0.036]);
  `detectors_context` 0.214 vs 0.204, CI [-0.003, +0.025] -> **no significant difference**.
  Refitting on resampled training rows moves it 0.192-0.218. The primary test can barely move by
  construction: with 139 test positives the best possible precision@574 is 0.242 (+0.038 over R2).
- **Secondary, post hoc (added after the first run, not evidence):** `detectors_context` ranks
  better than `context` overall (AUC +0.033, paired CI [+0.023, +0.042]) -- detectors add some
  ranking signal on top of R2's two inputs. AUC against R2 itself (0.953 vs 0.868) is NOT reported
  as a finding: R2 is binary, and `is_tanker` alone already scores 0.879. The detectors-only
  signal is partly real (per `analyst-review`: detectors-only AUC 0.796 within tankers, 0.754
  within R2) but its top feature is exposure (`n_observed_hours`, fewer hours -> riskier), which
  may encode route/transit through Danish waters rather than evasion.
- **Pre-registered now, for the next window's evaluation (frozen by this commit):** (1) alert
  budgets k = 50, 100, 200 alongside the matched k, since k = 574 has almost no headroom --
  post hoc these look strong (0.37-0.38 at k = 50-200) but cannot be claimed from this window;
  (2) a `detectors_context` variant without exposure columns (`n_observed_hours`,
  `n_observed_days`, `total_message_count`, `voyage_count`); (3) AUC difference vs `context` as a
  secondary test. Variants and PARAMS unchanged.
- **`analyst-review`: no blockers, no temporal leak.** Confirmed designation_date never enters X
  (now also a test), no identity memorization (the higher "seen in June" AUC comes from the 42
  trained-as-negative positives, i.e. it works against the model). Acted on: `reproduces_r2`
  verdict, precision ceiling, training-row refit bootstrap (test-row CIs are conditional on one
  fitted model), post-hoc AUC-diff vs context, docstring now states one cutoff (not rolling),
  the 4- vs ~22-month label-horizon mismatch, scores-not-probabilities, and that "not tuned on
  test" is an author statement frozen by the commit, not independently verifiable.
- **No GPU.** Full run incl. 3x200 refits ~70s on CPU.

_2026-09-27_ (P4-3c: static and declared-destination features -- `features/static.py`, pre-registered)

- **Why.** P4-0..P4-3 showed the behavioural detectors carry ~no signal: future-sanctioned vessels
  transit Danish waters normally. The literature agrees -- every shadow-fleet definition (KSE, CREA,
  S&P, Lloyd's List) is structural (age >15y, Aframax class, Russian cargo, no IG P&I, new/opaque
  owner), and a Sentinel-1 SAR study of Danish waters 2020-25 found no association between dark
  ships and the shadow fleet. So the next signal is what a vessel *is* and where it *says* it goes,
  both already in our own clean AIS (length, width, draught, destination) and never used.
- **Exploratory result (one cutoff; train 2024-06 with P4-3's as-of labels = 16 positives, test
  2024-11 = 4,449 rows / 139 positives; logistic regression C=0.3, class_weight=balanced,
  median-imputed, standardized; paired bootstrap 1,000 over test rows vs R2 with random tie-break):**
  P@50 0.640 [+0.290, +0.595], P@100 0.550 [+0.270, +0.448], P@200 0.450 [+0.196, +0.307],
  P@574 0.223 [+0.010, +0.038] (ceiling 0.242), AUC 0.969 -- vs R2's flat 0.204. Unfitted rule
  `tanker AND (dest_russia OR dest_south_route)`: 144 flagged, precision 0.556, lift 17.8x.
  Ablation (same protocol): almost all the gain is the destination group (context+dest P@50 0.65);
  size alone helps mainly at k=200 (0.39). Reproduced exactly from `features.static`'s own output.
- **Honesty caveats.** (1) The destination regexes were written after looking at June 2024's
  most common tanker destinations with labels running to 2026 -- mild data snooping; November was
  then examined once. **Every other archive month is a clean test, hence the freeze below.**
  (2) Declaring a Russian port is close to the designation criterion itself; not a leak (public
  before designation) but the README must say the model finds "tankers in the Russian trade".
  (3) Coverage is balanced (length non-null 99.2% vs 100%, any destination 100% vs 99.9% among
  positive vs negative test tankers) -- no repeat of Wikidata's differential-coverage problem.
  (4) Adding already-sanctioned vessels as extra training positives (16 -> 33) HURT (P@574 0.159
  LightGBM / 0.167 logistic, CIs below R2): already-designated vessels behave differently, so the
  literature's "borrow sanctioned vessels as positives" advice does not transfer here.
- **Age proxy from the IMO number (`imo_serial = TRY_CAST(imo AS BIGINT)`).** IMO numbers are
  issued roughly in build order: Spearman 0.977 vs Wikidata build year (n=3,784; tankers 0.973,
  median error of a linear fit 0.9 years). Future-sanctioned test tankers median ~9.31M vs 9.47M
  (older). Coverage is 100% of the modelling population by construction (it IS the population
  key), so this recovers the age signal WITHOUT P4-1's Wikidata label contamination. Adds little
  on top of destination (P@50 0.60-0.64) but helps without it. R3 in `model.baseline` is untouched
  (still gated on Wikidata); this is a model feature, not a baseline rule.
- **Frozen for the walk-forward evaluation (P4-3b), by this commit:** `features.static`'s three
  regexes and column definitions; the feature set `is_tanker, is_foc, length_m, width_m,
  max_draught_m, draught_range_m, imo_serial, dest_russia, dest_south_route, dest_for_orders,
  n_destinations`; the model above (logistic C=0.3 balanced) as the primary "static" variant, with
  LightGBM on the same columns (P4-3's PARAMS) as secondary; budgets k=50/100/200 plus R2's own
  count; comparison vs R2 with the paired bootstrap; per-cutoff and pooled reporting. Any later
  regex change must be a new, separately named column.
- **Not integrated into `pipeline.window` or `features.panel` yet, on purpose:** the archive build
  (`scripts/build_archive_windows.sh`) is running and re-imports both per window; changing them
  mid-run would give different windows different code. `python -m features.static --start ...
  --end ...` is run per window after the build (clean data is kept, nothing is pruned). Verified on
  June/November into a scratch directory only (21,146 / 9,477 mmsi, ~50 s per window at 4 threads);
  nothing written under `data/` while the build runs. 49 new tests.

_2026-09-27_ (P0: the pooled walk-forward evaluation protocol, frozen before any new window is scored)

- **Why now.** With 16-140 positives per cutoff the binomial half-width of precision@50 is ~±14
  points: no single cutoff can separate two good models. Every result from P4-3b onwards is read
  through this protocol, so it is fixed before the archive build produces anything to look at.
  Source: the 2026-09-27 deep-research report (`reports/Modelos para predecir la flota
  fantasma.md`, P0) plus this project's own P4-0..P4-3c conventions.
- **Cutoffs.** One per built-and-verified monthly window W_m; the cutoff is W_m's `window_start`.
  Training may use only windows that ended before the cutoff and only designations dated strictly
  before it. **Primary pooled set: test months 2024-08..2025-02 (7 cutoffs, >= 3 training months
  each).** 2024-05..2024-07 are reported per cutoff only (too little history to train on). A month
  missing from the build is dropped and reported as missing, never replaced.
- **Labels and population.** `model.evaluation.POPULATION_WHERE_SQL` per window (valid IMO, not
  sanctioned as of `window_end`). Primary test label: eventual designation after `window_end`
  (as in P4-0..P4-3). Secondary: designation within 12 months of `window_end`, which gives every
  cutoff the same horizon. **Label snapshot frozen:** `data/reference/sanctions.parquet` built
  2026-09-21 19:22 UTC (2,205 records, last designation 2026-08-24); a refetch is a new evaluation.
- **Budgets.** Primary, per `CLAUDE.md`: R2's own flagged count in each cutoff. Secondary:
  k = 50/100/200 (pre-registered in P4-3). A model that wins only at the secondary budgets is
  reported as "better at small alert budgets", not as "beats R2".
- **Ties.** Every score's precision@k is the EXPECTED precision under uniformly random
  tie-breaking at the cutoff boundary, so a binary rule such as R2 scores its flagged-set
  precision at any k <= its flag count. This replaces `model.baseline`'s mmsi-order tie-break
  for this evaluation only (that artifact is documented in its docstring).
- **Pooling.** Micro-average: total expected hits over all pooled cutoffs divided by total
  budget. Per-cutoff figures are always reported next to the pooled one, and so is the pooled
  ceiling sum(min(n_pos, k)) / sum(k).
- **Uncertainty.** Paired cluster bootstrap, 2,000 resamples, 95% percentile interval of the
  pooled difference (model minus reference). **Clusters = IMO**: a vessel's rows in every cutoff
  move together, because the same tanker appears in many months. Secondary clustering: the
  designation package (sanctioning source + date of the vessel's earliest designation) for
  positives, IMO for negatives. One OFAC action listed 183 vessels, so co-designated vessels are
  not independent evidence. The matched budget is recomputed inside every resample.
- **Decision rule.** "A beats B" iff the IMO-clustered interval of the pooled difference at the
  primary budget excludes 0. A win that disappears under package clustering is reported with
  that caveat. Hyperparameters may be chosen only on cutoffs earlier than the one scored; the
  variants frozen so far are R2, P4-3's `context`/`detectors`/`detectors_context` (+ its
  no-exposure variant), and P4-3c's `static` (logistic, plus LightGBM secondary).

_2026-09-27_ (P4 gate: OpenSanctions owner/manager coverage, criteria fixed before measuring)

- **Question.** Can owner/manager links for the vessels in our panel come from a legal, free,
  dated source without contaminating the label? The literature ranks "shares a manager/owner with
  an already-sanctioned vessel" as the strongest missing signal (Port State Control's "company
  performance"; KSE/C4ADS network findings). It is the same shape of gate as P4-1's Wikidata
  build-year gate.
- **Source.** OpenSanctions bulk data (CC BY-NC 4.0, non-commercial use). Downloaded to a scratch
  directory, NOT under `data/`, while the archive build runs. The dataset version/date is
  recorded.
- **Population.** P4-0's evaluation population in the June and November panels. The gate is
  judged on tankers (where R2 and the static model operate) and also reported for the whole
  population.
- **"Covered" vessel.** Its IMO matches an OpenSanctions vessel with at least one link to an
  owner/operator/manager organisation, where (a) the link's `first_seen` is before the panel's
  cutoff (the first day of the next month) and (b) at least one of its source datasets is NOT a
  sanctions list. The Ukrainian GUR `ua_war_sanctions` dataset counts as label-like and is
  measured separately, not as independent coverage. Links derived from sanction status
  (`sanction.linked`/`sanction.control` topics) never count.
- **Checks.** G1 coverage: at least 50% of tankers covered. G2 differential coverage: the
  covered rate among future-designated vs never-designated tankers, Fisher's exact test, and the
  AUC of "covered" as a predictor with a stratified bootstrap 95% interval. G2 passes iff that
  interval includes 0.5. **GO iff G1 and G2 both pass, in both panels.** If per-link `first_seen`
  is not available in the free bulk data, the verdict is NO-GO by construction (links cannot be
  gated in time). Also reported: the share of links whose only sources are sanctions lists.
- **Naming note.** "P0"/"P4" in the two entries above are the research report's plan steps, not
  project phases: P0 = task P4-3e (implemented in `model/pooled_evaluation.py`, which reproduces
  the June->November P4-3c numbers exactly), and the P4 gate = task P4-3f.

_2026-09-27_ (P4-3f result: the OpenSanctions coverage gate is NO-GO, decisively)

- **Source measured.** OpenSanctions `entities.ftm.json`, version 20260927185432-jyu, 2.6 GB,
  downloaded to the session scratchpad (never under `data/`). Vessel IMO values are stored as
  `IMO1234567` and were normalized to the panel's 7 digits. Vessel-organisation links are the
  `Ownership` (asset) and `UnknownLink` (subject/object) schemas. FtM does not distinguish owner,
  manager, operator or ISM company.
- **Result, verified independently of the data-scout run with a DuckDB upper bound that ignores
  the source filter.** No tanker in either panel has any link with `first_seen` before its cutoff:
  0/893 in June (cutoff 2024-07-01) and 0/898 in November (cutoff 2024-12-01), from ANY source.
  The earliest vessel-link `first_seen` in the whole file is 2023-04-20, the 10th percentile is
  2025-07-03, and the median is 2025-07-13. Even ignoring time, only 218/893 and 211/898
  tankers have any link at all (~24%). **G1 fails twice over, so G2 is moot. Verdict: NO-GO.**
  (The data-scout's own report said "60 tankers" in one place and 0 in its table; the
  independent recount confirms 0.)
- **Consequence.** No owner/manager network features for any cutoff in the archive period
  (<= 2025-02). OpenSanctions' coverage of vessel links is recent: most links were ingested
  from 2025 onwards, largely after the designations we are trying to predict, which is also the
  circularity the literature warned about. Research-report steps P6 (network features) and the
  "new to the network" subset are therefore dropped for this archive, not deferred.
- **What could revive it (not pursued):** OpenSanctions' historical archives (these need a
  paid data-delivery token), or a dated registry such as GFW's `registryOwners` (often empty for
  non-fishing vessels, per the research notes). Equasis is ruled out by its terms of use.

_2026-09-27_ (session close: decisions taken from the deep-research report)

- **LLMs never score vessels.** A 2025-26 model may remember post-cutoff designations (hindsight
  leakage; the report cites an audit of 2026 models), so LLMs are allowed only for
  preprocessing, e.g. normalizing destination strings, never as a risk scorer or feature oracle.
- **No SMOTE, synthetic positives or self-training.** They miscalibrate without improving
  ranking in the cited evidence, and they hurt TabPFN in its only controlled imbalance study.
- **Already-sanctioned vessels are not borrowed as positives** (measured to hurt in P4-3c).
  Their signal enters instead through P4-3g's hazard design, which uses only their
  pre-designation months.
- **Tree models stay on CPU.** GPU LightGBM/XGBoost gains nothing at ~5k-60k rows, and RAPIDS
  and DGL lack Windows/Blackwell support. The GPU is reserved for the TabPFN/TabICL challenger
  and the trajectory-encoder experiment.
- **Research report and notes stay out of git** (author's call). They are in Spanish, while
  repo documentation is English; they are listed in `.git/info/exclude`, and STATE.md points to
  them.

_2026-09-27_ (P5: the web front end, `viz/`, started ahead of order at the author's request)

- **Why now, out of order.** The author asked to start the interactive map while P4-3b's archive
  build runs, to iterate on it early. It only reads `data/` (DuckDB, 4 threads) and writes to
  `viz/public/data/`, so it cannot interfere with the build.
- **Prediction is frozen on the site (author's call).** No model beats R2 under the frozen
  walk-forward protocol yet, so the site shows measured facts only: tracks, detector events,
  identity, sanctions designations. Vessel panels are laid out so a score can be added once a
  model is validated.
- **Stack: Astro 7 + Svelte 5 + deck.gl 9.4 + MapLibre 5**, static output (hostable anywhere,
  including the author's own domain), bilingual routes `/en/` and `/es/`, interactive islands
  only where needed. MapLibre is pinned to v5: v6 moved the controls and popups out of the core
  package. Plain CDN pages without a build step were considered and dropped once the author
  asked for a polished multi-page site. Node 24 LTS is installed portable in
  `%LOCALAPPDATA%\Programs\nodejs` (user PATH, no admin rights).
- **Export format (`report/export_viz.py`), not GeoJSON/Arrow as P5-1 was worded.** Tracks are
  three uint16 arrays (lon/lat quantized over `MAP_BBOX`, ~12 m; minutes since window start):
  654k June points = 3.9 MB, against ~25 MB as JSON. Only the focus fleet is animated (tankers,
  sanctioned vessels, both parties to STS episodes with confidence >= 0.5); all traffic is a Web
  Mercator density raster (distinct vessels per pixel). Trips split at silences > 60 min and
  implied jumps > 40 kn, so no straight line is ever drawn across a gap. `on_land` spoofing
  (10M dockside rows) is left off the map. Dossiers exist for every vessel with a valid IMO,
  every tanker and every vessel with a detector event (9,145 over June and November).
- **Generated data is never committed** (`viz/public/data/` is gitignored). It is rebuilt by
  `python -m report.export_viz` after each window and will be uploaded with the site (P5-3).
- **Colour: three categorical hues, validated all-pairs** with the dataviz validator on the
  dark surface `#0a1422`: blue = vessel, orange = on a sanctions list, aqua = detector event
  (worst CVD dE 9.4, normal-vision 20.9). Five hues failed (magenta vs aqua, deutan dE 1.6),
  so event kinds are told apart by glyph shape (dash, ring, diamond, triangle, dot), never by
  colour. P5-2's "gaps in red" was dropped: red collides with the sanctions orange under
  colour-blindness. Status colours appear only on the live connection pill, always with a label.
- **The live page goes through a local relay (`ingest/aisstream.py`).** An AISStream key cannot
  ship in a static page. The relay keeps it in `.env`, negotiates permessage-deflate, folds
  position and static reports into one record per vessel, and broadcasts 1 Hz diffs on
  `ws://127.0.0.1:8765` for the Danish straits and Gibraltar. **Not yet run against AISStream**
  (no key yet); verified by 12 unit tests and a local mock of the browser protocol. Serving the
  live page publicly needs an always-on relay (a small VPS or a Cloudflare Worker): undecided.
- **Front-page case: NS LOTUS (IMO 9339337)**, chosen because every step is in this project's
  own June 2024 data: in ballast (8.2 m) declaring EGSUZ>RUPRI on 17 Jun; last heard east of
  Bornholm 19 Jun 18:31; heard again 26 Jun 08:46 at 14.0 m declaring RUULU>EGPSD (flagged by
  `draught_change_unexplained`); designated by the UK on 31 Jul 2024 and by OFAC on 10 Jan 2025
  as "Legacy", flag Barbados. The story's closing chart is computed in the browser from the
  exported events and matches P4-0: June tankers sanctioned later vs other tankers with at
  least one AIS gap 55% vs 79%, position anomaly 6% vs 8%, STS 0% vs 0.9%, draught change with
  no port call 38% vs 24%.
- **Open before publishing (P5-3).** The DMA's AIS licence and attribution terms are not
  recorded in `docs/DATA_SOURCES.md`; the EU list comes via OpenSanctions (CC BY-NC 4.0: fine
  for a non-commercial site with attribution); the author's domain and DNS; where the live relay
  runs.

_2026-09-28_ (P5-3: the domain)
- **The site will be published on checkgraph.dev**, the author's domain, which the author is
  retiring from their `fake-review-detector` project. Its DNS is on Cloudflare and today serves
  that old site from Netlify. The host is not decided yet. Cloudflare Pages is recommended because
  the DNS is already there, bandwidth is unlimited, and it takes a direct upload of the prebuilt
  site (the exported data is not in git). The switch waits for the author's go-ahead and the DMA
  licence check.

_2026-09-28_ (P4-3b archive build: two operational fixes)

- **`detect.spoofing.check_on_land` runs days one at a time again (`ON_LAND_MAX_WORKERS = 1`).**
  The 8-worker pool added in P4-1b was counterproductive: each day's point-in-polygon join already
  keeps ~12 of 16 threads busy inside DuckDB, and concurrent cursors only contend. Measured on 4
  May-2024 days with identical events: 1 worker 248 s (12.4 cores avg), 4 workers 531 s (7.7
  cores); May's 31-day window with 8 workers ran >6 h at ~20-30% CPU. Ruled out first: the land
  geometry (one May day costs 54-55 s against May's or November's land pieces alike). The P4-1b
  claim that the pool made November faster than June was confounded with the Windows Defender
  exclusion applied in the same run.
- **Long builds are launched through WMI, not `Start-Process`.** A process started from a Claude
  Code session is killed when the session exits (Windows job object); that, not a reboot, is what
  stopped the build on 2026-09-28. `Win32_Process.Create` parents it to `WmiPrvSE`, outside the job.

_2026-09-28_ (P5-7: the public live relay is a free Cloudflare Worker, not a server or the browser)

- **No direct browser connections to AISStream.** The author asked whether each visitor's browser
  could call AISStream itself, to avoid paying for a server. AISStream's documentation forbids it
  ("Direct browser connections are not permitted. Connect from your own server and proxy only the
  information each client needs"). It also caps an account at 3 subscribed connections, so a
  fourth visitor would break it, and the key would be public in the page.
- **No paid server (author's call).** The relay for the public site is `relay/`: a Cloudflare
  Worker plus one Durable Object that holds a single upstream connection shared by every viewer,
  on the Workers Free plan. No card is needed; past a limit it stops for the day and never
  bills. The domain's DNS is already on Cloudflare.
- **Staying inside the free limits.** The free plan gives 100,000 requests and 13,000 GB-s of
  Durable Object duration per day. One object awake all day is ~10,800 GB-s. The docs do not say
  whether messages arriving on an outbound WebSocket count as requests; if they count at the
  inbound 20:1 ratio, a 24 h connection is ~108,000. So the upstream opens with the first viewer
  and closes 5 min after the last, and only `ALLOWED_ORIGINS` (the site's own domain) may connect.
- **One protocol, two relays.** The Worker is a TypeScript port of `ingest/aisstream.py` with the
  same browser protocol. `relay/test/ais.test.ts` mirrors `tests/test_aisstream.py`, and
  `tests/test_relay_tables.py` checks its MID-to-flag JSON (regenerate with
  `python -m report.countries`). `new WebSocket()` in a Worker negotiates permessage-deflate by
  itself, which AISStream needs for full rate. Frames arrive binary, so `binaryType` is set to
  `arraybuffer`.
- **Visitor-facing offline card.** The page shows the developer instructions (key, Python
  command) only when its relay URL is on localhost. The public build shows "unavailable right
  now, reconnecting" with a link to the explorer.
- **Basemap land #0d1624 → #1c2839.** The author could not tell land from sea. The sea is
  unchanged, so tracks and density read as before.
- **Later months are downloaded in the background while earlier windows run their detectors.**
  Measured: one S3 stream 9.4 MB/s, four streams 12.5 MB/s in total -- the line is saturated, so
  parallel downloads alone barely help; overlapping the bandwidth-bound downloads with the
  CPU-bound detectors hides them (~20 h -> ~15 h). Made safe by (1) a lock file around every
  `pipeline.manifest` read/write (a whole-file read-modify-write; without the lock a 6-writer test
  kept 10 of 60 entries and Windows raised `PermissionError` on `os.replace`), and (2) the build
  script waiting for the backfill that covers a window's days before starting that window.

_2026-09-28_ (P5-3: the site is published on checkgraph.dev)

- **The site is an assets-only Cloudflare Worker, not Pages.** `viz/wrangler.jsonc` serves the
  prebuilt `viz/dist` (the data is not in git, so a git-connected build cannot produce it).
  - A Worker `custom_domain` route creates its own DNS record and certificate with the same
    wrangler login the relay uses.
  - Pages needs dashboard steps or a DNS-scoped token to attach an apex domain.
  - Static-asset requests are free and unmetered on the Workers Free plan. The per-deployment
    limit is 20,000 files, so the dossiers need sharding before re-exports reach it (P5-8).
- **Switching the domain needed the author's hands once.** Two things got in the way:
  - A new account needs a `workers.dev` subdomain before any Worker deploys. It was created
    through the API as `checkgraph.workers.dev`.
  - Cloudflare refuses to attach a custom domain over existing DNS records. The API's
    `override_existing_dns_record` did not help, and wrangler's OAuth login has no DNS scope.
    So the author deleted the two Netlify records (apex A 75.2.60.5 and `www` CNAME
    `fake-review-detector-s.netlify.app`) in the dashboard. Recreating them rolls the switch back.
- **Absolute URLs.** `site: 'https://checkgraph.dev'` is set. `Base.astro` now emits a canonical
  link, `og:url` and absolute hreflang alternates. There is a bilingual `404.astro`, because the
  language of a broken link is unknown.
- **DMA terms.** They were checked before publishing and recorded in `docs/DATA_SOURCES.md`. No
  licence is published for the archive. The policy's one restriction concerns identifying
  *persons*, and the site shows vessels only, credited to the DMA.

_2026-09-28_ (P5-9: the shadow-fleet page and share cards, author's pick)

- **One vessel = one IMO.** Its mmsi are merged, since a new flag means a new mmsi. "Seen"
  counts days with any observed hour. `lead_days` is the first designation minus the first
  sighting in the exported months. The page states that this is not the real first passage,
  and that passing before a sanction is not predictability (prediction stays frozen).
- **Regimes, not "Russian".** Each vessel's first designation is classed russia / iran / other
  from its programme text (RUSSIA, UKRAINE, PEESA → russia). The page shows 28 Iran-programme
  vessels instead of calling all 260 the Russian shadow fleet.
- **EU absence stated, not hidden.** The EU's vessel listings (port-access bans, Annex XLII of
  Regulation 833/2014) are not in the EU financial-sanctions file this project uses, which
  holds 2 vessels. The "who sanctioned" chart covers the UK and the US, and says why.
- **Colour.** Orange means sanctioned after passing, grey means already sanctioned. The
  validator passes CVD (13.7 protan) and normal vision. It flags the grey as reading grey, which
  is intended: it is the context category, and it carries a legend and tooltips.
- **Table signals.** Only AIS gaps, ship-to-ship encounters and unexplained draught changes are
  shown. The course/destination check shared the draught check's glyph and does not
  discriminate (P4-0).
- **Share cards come from the site itself.** They are rendered from the heroes at 1200x630 and
  are git-ignored like the data they draw.
- **Visitor stats wait for the author.** The wrangler login cannot create a Web Analytics site
  (API auth error). The zone's own traffic analytics already work without it.
- **`process.clean` dedups once, by `file_row_number`, and writes atomically.** It ran the Rule 3
  window function twice (a `count(*)` over the view, then the `COPY`), and numbered rows with
  `row_number() OVER ()`, a serial pass over the whole day. Now the raw view reads
  `file_row_number` (the row's position in the file -- the "file order" Rule 3 always meant) and the
  count comes from the written file. Real 2024-07-09 (26.5M raw rows): 101 s -> 5 s, identical rows
  both ways and identical schema. `process.clean` and `ingest.dma._csv_to_parquet` now write a
  `.tmp` sibling and `os.replace` it: shutdowns and kills had left truncated partitions (2024-03-21
  raw, 2024-07-09 clean, 2025-01-15 raw) that the "already done" checks would skip for good.
- **`process.tracks` and `process.identity` materialize their result once.** Each ran its
  window-function / aggregation view chain over every point in the window (~300M rows) once per
  summary count and again for the write -- four and five full passes. Now one `CREATE TEMP TABLE`
  feeds the counts and the write. Voyages on November: ~19 min -> 171 s, same 35,680 voyages;
  44 differ only in start/end lat/lon, the known equal-timestamp tie of `arg_min`/`arg_max` (same
  class as `impossible_speed`'s `lag()` tie), not a change in logic.
- **`check_on_land` splits the eroded land into a 0.1-degree grid before the join.** The
  `SPATIAL_JOIN` prunes only by bounding box, and a big piece (Jutland, southern Sweden) has a box
  covering most of the nearby sea, so nearly every point paid a full `ST_Contains` against
  thousands of vertices. Split into cells (73 pieces -> 3,538, at most 32 vertices, cells
  overlapping by 1e-6 degrees so a point on a grid line stays inside one cell): one day 44-63 s ->
  1.1-1.6 s, identical rows (0 missing, 0 extra) on five real days in April, June and November
  2024. Real May window: 127 s for 31 days, where April's run had not finished after 3.5 h.
- **`on_land` rows stay in DuckDB.** `build_spoofing_events` fetched ~12M on_land rows into
  `SpoofingEvent`s and inserted them back with `executemany`, which runs at ~3,200 rows/s: about an
  hour per window in a fresh process, and May's run spent 17:48-19:40 in that one call. Now
  `_build_on_land_table` writes `_on_land` and the output reads it with SQL; `check_on_land` still
  returns `SpoofingEvent`s. 2024-04-03..04: 9.9 s vs 191 s, identical on_land rows (the only
  differences were the known `impossible_speed` tie-break and last-digit float noise in one
  `synthetic_circle` row, neither touched by this change).
- **`python -m pipeline.window` runs each producer in a fresh process.** On Windows DuckDB
  allocates from the process heap (no jemalloc there), and after hours of heavy queries in one
  process its allocations serialize on the heap lock: py-spy showed April's on_land worker waiting
  in `RtlAllocateHeap`, at ~1.7 of 16 cores for 3.5 h; November's `sts` took 76 min where a fresh
  process needs ~5 (3 April days: `slots` 6.5 s/day). DuckDB threads are not the main cause, but
  more is not better either: `sts` slots on 3 days took 19.5 s at 4 threads, 25.0 s at 8, 29.8 s
  at 16. `process_window(isolate_steps=False)` stays the default so tests can monkeypatch; the CLI
  isolates unless `--in-process`.

_2026-09-29_ (P4-3b/P4-3i/P4-3j: walk-forward details, two new challengers -- pre-registered before any
archive-month score exists)

- **State at registration.** All 11 windows (2024-04..2025-02) are built, with panels and
  `features.static` partitions. No model has scored any window other than June/November (P4-2,
  P4-3, P4-3c). Nothing below has been run against any label.
- **Walk-forward mechanics (P4-3b), filling in what P0 left open.** Expanding window: for cutoff
  C = test window_start, the training set is every built window whose window_end < C, each with
  its own P4-0 population and P4-3's as-of-cutoff label (designated in (own window_end, C)),
  pooled row-wise (a vessel seen in several months contributes several rows). Test rows: the
  test window's P4-0 population; primary label `label_is_sanctioned_after_window_end`,
  secondary designated within 12 months of window_end (P0). Static columns are joined by mmsi
  from `data/processed/static/window=<same window>/`; `imo_serial = TRY_CAST(imo AS BIGINT)`
  from the panel. Missing values: medians of the training rows only. Cutoffs with 0 training
  positives are reported as "not scorable" for learned models, never dropped silently.
- **Sensitivity pre-registered:** the pooled result is also reported without the 2024-11 cutoff,
  the only test month already examined (P4-3, P4-3c).
- **P4-3i: TabICLv2 as a pretrained challenger.** `tabicl==2.2.0`, checkpoint
  `tabicl-classifier-v2-20260212.ckpt` (BSD-3, publishable; TabPFN >= 2.5 is excluded because its
  licence forbids using outputs in commercial decisions and this project publishes its scores).
  Inputs: exactly the frozen P4-3c static column set, training-median imputed, no scaling (the
  model normalises internally). Every package default (n_estimators=8, softmax temperature,
  feature shuffling). Contexts: all training positives + 5,000 training negatives drawn
  uniformly without replacement (all of them if fewer), 10 contexts with seeds 0..9; score = mean
  P(positive) over contexts; ranked by that raw score, no oversampling, no threshold. GPU. No
  hyperparameter is chosen after seeing any score. Comparisons, P0 decision rule: vs R2 (the
  project bar) and vs the static logistic (does a pretrained transformer add anything to the
  linear model?). **Expected: small or no gain** (research report, 2026-09-27: no published test
  at ~16 positives or with precision@k).
- **P4-3j: our own vessel encoder, pre-registered as a design, built later.** Goal: complexity
  where there is data (unlabelled AIS, ~20k vessels/month), simplicity where there is none
  (16-150 labels).
  - Architecture: per vessel-month, (a) a transformer over the thinned track (`data/tracks/thin`,
    5-min) WITHOUT absolute latitude/longitude -- per-step dt, SOG, COG change, draught,
    nav_status, distance to land, in-anchorage flag, silence markers; (b) a transformer over the
    detector event sequence (gaps, spoofing, sts, behaviour, identity kinds, with times);
    (c) an MLP over the static columns; fused by cross-attention into one ~64-d embedding. No
    mmsi/imo/name/flag input (no identity memorisation).
  - Self-supervised pretraining ONLY on 2024-04-01..2024-07-31 (before the first primary cutoff),
    frozen for every cutoff: masked-span reconstruction, same-vessel contrastive pairs across
    months, and next-month region/laden-state prediction with targets restricted to data up to
    2024-07-31 (July rows have no next-month target). Normalisation statistics also from that
    period only. Architecture sizes and pretraining settings are chosen on self-supervised
    losses within that period, never on a label.
  - Supervised head: the P4-3c logistic (C=0.3, balanced) on [static columns + embedding].
    Primary comparison: vs the static logistic alone (does the embedding add anything?), P0
    rule; also vs R2. **Expected: probably null** (research report's "trajectory encoder without
    coordinates"); a null is reported, not buried.

_2026-09-29_ (P4-3b + P4-3i result: the walk-forward, 10 cutoffs, pooled 2024-08..2025-02)

- **Run.** `python -m model.walk_forward` (commit 43c5a58), ~4 min incl. TabICLv2 on the GPU.
  Before scoring any archive month it reproduced P4-3c's June -> November logistic exactly
  (P@50/100/200/574 = 0.640/0.550/0.450/0.223). Training positives per primary cutoff grow
  23 -> 428 (a later-designated vessel is positive in every earlier window's row). 2024-05 has no
  training positive (n/s); 2024-06/07 have 2 each, both non-tanker, so learned models rank
  non-tankers first there (below random; expected, outside the primary pool).
- **Primary budget (R2's own count), pooled, ceiling 0.216, IMO-clustered 95% CI of the
  difference vs R2 (0.180):** static_logistic 0.189 (+0.009 [+0.001, +0.018]); static_lightgbm
  0.196 (+0.016 [+0.008, +0.025]); **tabicl 0.198 (+0.018 [+0.009, +0.028])**; lgbm_context
  reproduces R2 (0.180); lgbm_detectors 0.145 (**loses**, -0.035 [-0.048, -0.022]);
  lgbm_detectors_context 0.193 (+0.013 [+0.006, +0.021]); no-exposure variant 0.190
  (+0.010 [+0.003, +0.018]). tabicl - static_logistic +0.009 [+0.005, +0.015].
- **Robustness (primary budget).** tabicl, static_lightgbm and both detectors_context variants
  keep their win under package clustering, the 12-month label and without 2024-11.
  **static_logistic's win is fragile:** its interval includes 0 under package clustering
  ([-0.003, +0.023]) and without 2024-11 ([-0.002, +0.015]); per P0 it is reported with that
  caveat.
- **Small budgets (pooled, vs R2 0.178):** k=50: static_logistic 0.566, static_lightgbm 0.709,
  tabicl 0.680, detectors_context 0.476, detectors 0.230; k=100: 0.483 / 0.587 / 0.577 / 0.452 /
  0.221; k=200: 0.350 / 0.413 / 0.425 / 0.378 / 0.199. Every static-column model's interval vs R2
  excludes 0 at every budget.
- **Verdict.** Per `CLAUDE.md`'s bar, the static models (and TabICLv2) beat R2 at the matched
  budget, by little because the ceiling leaves little room (+0.036 at most), and by a lot at
  small alert budgets. The behavioural detectors alone still lose to R2 (P4-0's finding again);
  added to R2's inputs they help a little. **P4-3i:** TabICLv2 beats the linear model, as
  pre-registered; but it is level with static_lightgbm (0.198 vs 0.196, not a pre-registered
  comparison), so the honest reading is "nonlinear beats linear on these columns", not "a
  pretrained transformer adds something a tree model does not".
- **`analyst-review` (same day).** No temporal leak: 0 training-positive IMO/MMSI in any test
  population; test positives appear in training only as negatives (works against the models);
  pooled precisions recomputed independently in SQL; LightGBM seed spread 0.1957-0.1977.
  - **Blocker, fixed:** the first run (97f0661) fed the static columns into P4-3's detector
    variants (`load_window` added them to `split.features` before `variant_columns` listed its
    columns), inflating lgbm_detectors to 0.70 at k=50. Fixed in 43c5a58 with a regression test;
    the numbers above are the rerun. Static and TabICL rows were unaffected.
  - **Should-fix, OPEN:** `pipeline/window.py` passes a `window=*` anchorage-mask glob to `sts`
    and `behaviour`, read without a time filter, so archive windows built after later masks
    existed used future AIS (not labels) in the anchorage mask. Affects only detector columns
    (so only the lgbm_detectors* rows); fix = restrict to masks with window_end <= the window's
    own, then rebuild sts/behaviour/panels.
  - **Vessel-level snooping:** 71% (608/856) of the primary pool's test-positive rows are June or
    November forward-positives, the vessels seen while designing the regexes and freezing the
    column set, so "without 2024-11" does not remove it. Excluding those IMOs (post hoc,
    exploratory): at R2's budget the ceiling drops to 0.071 and nothing is distinguishable; at
    k=50 R2 0.063 vs 0.20 / 0.31 / 0.30 (logistic / lightgbm / tabicl), so the small-budget gain
    survives. Aug-Feb are not "clean" tests of the column design.
  - **Notes:** 9 comparisons x 4 budgets with no multiplicity correction; CIs are conditional on
    one fit per cutoff (no training-row refit bootstrap); labels are effectively OFAC + UK (EU
    contributes 2 records), so EU-only vessels count as negatives.

_2026-09-29_ (anchorage-mask fix, rebuild, walk-forward rerun -- closes the review's open should-fix)

- **Fix (8363cd5).** `detect.sts.apply_structural_gates` and `detect.behaviour`'s anchorage
  evidence keep only mask rows with `window_end <=` the window's own end, so the `window=*` mask
  glob (kept on purpose: "every window built so far") can never lend a later window's AIS.
  `pipeline.window` now passes this window's own voyages and sts partitions instead of globs:
  the date filter alone did not stop the 2-day validation window inside June from duplicating
  voyages (voyage_seq restarts per window). 3 new regression tests with controls; 611 pass.
- **Rebuilt sts -> behaviour -> panel for all 11 windows.** A first parallel attempt (3 windows
  at 5 threads / 6 GB `memory_limit` each) failed 4 sts windows: 2 out-of-memory, 2 with a DuckDB
  "Date out of range in timestamp conversion" inside `segment_episodes` (before the changed
  code) while spilling. Rerun one at a time without a memory cap: all OK. **Do not cap sts at
  6 GB for a monthly window.**
- **What changed (sts events / draught_change_unexplained):** April 283 -> 1,616 / 696 -> 802 and
  May 828 -> 1,845 / 812 -> 863 (built when every later mask existed: future masks had been
  excluding encounters); June 1,689 -> 1,091 / 764 -> 736 and November 502 -> 203 / 724 -> 676
  (built before the earlier months' masks existed; now they use them, which is legitimate past
  data); July-October and December-February unchanged. **Earlier documented June/November sts
  counts (e.g. P2-4's 1,689) describe the old build, not the current partitions.**
- **Design property, now explicit:** each window uses every earlier mask, so the mask history
  grows along the archive (April sees only its own; February sees eleven). No future data, but
  the exclusion strength is not stationary across windows; relevant only to detector columns.
- **Walk-forward rerun:** static_logistic, static_lightgbm, tabicl, R2 and lgbm_context are
  identical to the previous run (they use no detector columns). Detector variants barely move:
  lgbm_detectors 0.145 (-0.035 [-0.048, -0.023], still loses), lgbm_detectors_context 0.192
  (+0.012 [+0.005, +0.021]), no-exposure 0.191 (+0.011 [+0.004, +0.019]); k=50: 0.229 / 0.481 /
  0.424. All three keep their verdicts under package clustering, the 12-month label and without
  2024-11.

_2026-09-29_ (P4-3j: our own vessel encoder -- detailed design, amending the 2026-09-29 sketch
BEFORE any embedding is scored against a label)

The governing constraint: 23-428 training positives per cutoff, but ~20k unlabelled vessel-months
in the pretraining period. So all capacity goes into label-free learning, and the part that sees
labels stays as small as the static models it must beat.

- **Amendment 1 -- no static branch in the encoder.** The sketch had an MLP over the static
  columns. Removed: with the same-vessel contrastive objective, length/width/imo_serial identify a
  vessel exactly, so the encoder would solve its main task from them and ignore the track (an
  identity shortcut, decided on self-supervised grounds, not on any label). Static columns enter
  only the head, as in every other model.
- **Amendment 2 -- a 16-d embedding** (the sketch said ~64): with 23 positives at the first
  primary cutoff, the head gets 11 static + 16 embedding columns. Fixed a priori, not tuned.
- **Input tokens: one per OBSERVED hour** of a vessel-month (not per 5-min point; up to 744),
  built from clean AIS of that window only. Channels: log1p messages, SOG mean/max/std, COG
  dispersion within the hour (1 - mean resultant length, SOG > 0.5 kn), |COG change| vs the
  previous token, mean |heading - COG|, max draught (+ missing flag) and its change vs the previous
  token, nav-status fractions (underway / at anchor / moored / restricted-or-constrained), log1p
  distance to land (0.05-degree grid from `data/reference/land.parquet`, capped at 50 km),
  inside-a-coastal-anchorage-cell flag (the window's OWN mask only), share of the hour's messages
  flagged `on_land`, log1p hours since the previous observed token. Position enters only through
  the hour index (sinusoidal). **No latitude, longitude, raw COG, mmsi, imo, name or flag is ever
  an input**; the token table does not even store lat/lon (a guard test enforces the input list).
- **Event tokens** (the window's own detector partitions): gaps (duration, probability), sts
  (duration, confidence), behaviour and identity kinds, spoofing kinds except on_land (which is a
  track channel); type embedding + hour index + log1p duration + score; at most 64 per
  vessel-month, plus an always-present "no event" token.
- **Architecture.** Track transformer (d=64, 2 layers, 4 heads, ff 128, dropout 0.1), event
  transformer (1 layer), fusion = 4 learned latent queries cross-attending over both token sets,
  flattened -> linear -> 16-d LayerNorm embedding. ~150k parameters.
- **Population and period.** Every panel vessel-month with a valid IMO in windows
  2024-04..2024-07, NOT filtered by sanctions status (filtering on the label would itself use
  it). 10% of IMOs held out (hash of IMO) for early stopping.
- **Self-supervised losses (equal weights, fixed a priori).** (1) Masked-span reconstruction:
  ~15% of track tokens in spans of 3-8, predict their standardized channels (not the derivable
  dt/draught-change ones). (2) Same-vessel contrastive (InfoNCE, temperature 0.1, one pair per IMO
  per batch): two different months of the same IMO when available, otherwise two random crops of
  one month; every view is a random contiguous crop (60-100%) with 10% token dropout. (3)
  Next-month prediction from the embedding, only for source months 2024-04..06 (targets up to
  2024-07-31): observed next month (BCE), share of next month's observed hours in each of 12
  coarse regions (3 latitude x 4 longitude bands; soft cross-entropy), next month's median draught
  / the vessel's max draught over 2024-04..07 (MSE). AdamW lr 3e-4, wd 0.01, batch 128, <= 40
  epochs, early stopping on the held-out total loss (patience 3). Standardization statistics from
  the pretraining period only.
- **Frozen encoder -> embeddings for all 11 windows.** Scored only at cutoffs >= 2024-08-01 (the
  encoder saw 2024-04..07, so earlier cutoffs are n/s).
- **Heads and comparisons (P0 rule, primary budget = R2's count, IMO-clustered).**
  Primary: `enc_logistic` (P4-3c logistic on static + embedding) vs `static_logistic`.
  Secondary: `enc_lightgbm` vs `static_lightgbm`, `enc_tabicl` vs `tabicl` (same recipes as
  their static versions), all three vs R2; `emb_logistic` (embedding only) vs R2, descriptive.
  Encoder seed 0 is primary; seeds 1 and 2 are retrained and reported for stability only.
- **Expected: probably null.** A null is reported as the result, not tuned away.

_2026-09-29_ (P4-3j result: our own vessel encoder -- it learns real structure, and adds nothing
to the sanctions prediction. A null, reported as the result.)

- **Built as pre-registered** (28a6abe; code b11c90a, 5f5b569, f8bb3f8, edf94fe). Tokens:
  8,672,945 hourly track tokens over 11 windows (~14 s per window) plus event tokens. A NULL bug
  was caught on the real June window before any use: DuckDB's `least()` skips NULLs, so a missing
  draught had become 30 m and a missing SOG 40 kn; all clamps now go through `_clip()`, with a
  regression test. Encoder: 147,054 parameters; pretraining on 19,418 vessel-months / 8,429 IMOs
  (2024-04..07); ~15-26 s per epoch on the RTX 5060 Ti; early stopping at epochs 30-34 (best held-out
  total 4.74-4.79 from 6.28-6.40; contrastive 1.83-1.90 against ~4.85 for chance).
- **Label-free diagnostics (`model/encoder_diagnostics.py`, all three seeds):** no collapse
  (effective rank 8.2-9.1 of 16); ship type, never an input, is linearly recoverable (tanker AUC
  0.81-0.83, passenger 0.91-0.93), equally in June (in the pretraining period) and November (not);
  same-IMO retrieval between two unseen months (2024-08 -> 09, 3,804 vessels): top-1 0.18-0.19,
  top-10 0.40-0.42, median rank 18-21 against ~1,902 for chance. The encoder works.
- **Against the label (primary pool 2024-08..2025-02, R2's budget, ceiling 0.216; each comparison
  now has its own fixed bootstrap stream, f8bb3f8):**
  - **Primary, pre-registered: enc_logistic vs static_logistic: -0.005 [-0.010, -0.001] (seed 0),
    -0.004 [-0.008, +0.001] (seed 1), -0.005 [-0.010, -0.000] (seed 2).** Borderline negative:
    adding the embedding to the linear model does not help and may slightly hurt.
  - enc_lightgbm vs static_lightgbm: -0.002 / -0.000 / -0.001, all n.s.; enc_tabicl vs tabicl
    (seed 0 only): +0.004 [-0.001, +0.008], n.s. enc_tabicl's 0.202 is the highest pooled figure
    of any model, but it is not distinguishable from tabicl's 0.198.
  - Every enc_* model still beats R2 where its static twin does; the embedding alone
    (`emb_logistic`, descriptive) loses to R2 at the matched budget (0.165-0.172 vs 0.180) and
    beats it at small budgets (k=50: 0.24-0.30 vs 0.178), far below the static models (0.57-0.71).
  - **k=200, exploratory only:** enc_logistic vs static_logistic +0.016 n.s. / +0.024 / +0.041
    (seeds 0/1/2), while enc_lightgbm vs static_lightgbm is +0.001 / +0.004 / +0.004. A secondary
    budget, 2 significant of 36 encoder comparisons (and 2 significantly negative), not replicated
    for the nonlinear head, and still below static_lightgbm (0.366-0.391 vs 0.413): best read as
    the embedding giving the linear head nonlinear information the static columns already hold.
- **Verdict (worded per `analyst-review`):** a month-invariant track fingerprint, learned without
  labels from Danish AIS, adds nothing to static columns plus a nonlinear head. This does NOT show
  that tracks carry no signal: the contrastive objective by design rewards what stays constant for
  a vessel across months, which overlaps with its static identity, and the behaviours that would
  matter are rare here (the 141/139 test positives of July/November have 0 sts events between
  them). Consistent with P4-0 (detectors) and the research report's expectation.
- **`analyst-review`: no blocker, no leak.** Verified: every token reads only its window's
  partitions; pretraining never reads past 2024-07-31 (window guard, standardizer, next-month
  targets, draught scale); `region_id` never enters `collate`; the pretraining population is
  label-free (imo IS NOT NULL, incl. already-sanctioned vessels); 0 missing/NaN embeddings over
  52,337 rows x 3 seeds; stored GPU embeddings match a fresh CPU run to 2e-4. In-sample embeddings
  for training rows from 2024-04..07 are not a material handicap (a classifier separating July
  from August embeddings reaches AUC 0.56-0.59, the same as between two in-period months; the
  tanker probe transfers within 0.003). Acted on: per-comparison bootstrap streams (the seed
  runs with/without TabICL had drawn different resamples), `emb_logistic` verdicts flagged
  descriptive, the narrower verdict wording above, k=200 read as exploratory. Noted, not changed:
  24-27% of tokens sit at land distance 0 and 7-18% at the 50 km cap (0.05-degree grid; positions
  outside its box silently get 50 km); the anchor view carrying the reconstruction mask also feeds
  the contrastive/next-month losses (standard, not in the design text); the LayerNorm output has a
  near-constant norm (~4.42, one degree of freedom).
- **Not done, on purpose:** no retuning of the encoder after seeing these results -- any change
  now would be selected on the test months. A behaviour-focused objective (month-specific rather
  than month-invariant) or re-testing the embedding inside P4-3g's hazard design (many more
  positives) would each need a fresh pre-registration.

_2026-09-29_ (session close)

- **README update is now task P4-8 and first in the queue** (P4-7's label-bias section folded in):
  this session produced the project's main modelling results and their caveats, and `CLAUDE.md`
  requires limitations in the README, not only here.
- **P5-4's dossier confidence levels are unblocked:** they waited for a model that beats R2, and
  static LightGBM and TabICLv2 now do (at R2's budget and, by a wide margin, at small budgets).

_2026-09-29_ (P4-10: the sealed IMO split -- pre-registered before any new label source or
feature is scored)

- **Why.** The author's goal is now the best achievable prediction, through five lines of work:
  more label sources (P4-9), GFW port visits to Russian terminals (P4-11), implied Russian
  loading from draught (P4-3h), cross-month vessel history (P4-12), and the model side (P4-13:
  ensemble, PU learning, P4-3g's hazard framing). Picking the best of many attempts on the same
  2024-08..2025-02 pool selects on that pool, and 71% of its positive rows are vessels examined
  while designing P4-3c. A prospective test (score today, wait for new designations) was offered
  and declined by the author: its result takes months.
- **Groups** (`model/sealed_split.py`, by IMO, the same in every month): `examined` = the 260
  IMOs frozen in `model/examined_imos.txt` (ever-sanctioned on the 2026-09-21 snapshot and in the
  June or November population; identical to the website's `shadow.json`); `sealed` = every other
  IMO with `crc32("p4-10|<imo>")` odd; `dev` = the rest. Primary pool (2024-08..2025-02 P4-0
  populations, v1 labels), distinct IMOs / forward-positive IMOs / tankers: dev 4,966 / 58 /
  1,000; examined 193 / 193 / 181; sealed 4,828 / 63 / 988.
- **Rules.**
  1. From now on a run reports test rows of scope `dev` (examined + dev) or `dev_clean` (dev
     only). `model.walk_forward` defaults to `dev`; `sealed` and `all` refuse to run without
     `--unseal`. Training rows are never filtered (training labels are as-of-cutoff public
     designations; learning from a sealed vessel's past reveals nothing about performance on it).
  2. Budgets inside a scope: R2's own count within the scope's rows, and k = 25/50/100 per cutoff
     (half the vessels, so half the earlier 50/100/200).
  3. Decision metric: pooled precision at k=50 in scope `dev`, with R2's budget next to it. A
     change is adopted if it raises dev P@50 without lowering dev precision at R2's budget;
     `dev_clean` is reported alongside. Intervals are reported but not required: dev decisions
     are selections, not claims. Every configuration tried is logged in this file, and the count
     is reported with the final result.
  4. The sealed scoring runs ONCE (P4-14), after the five lines are finished, for candidates
     named here before it runs: R2, `static_lightgbm` (P4-3b's reference) and at most two
     finalists chosen on dev. Metrics: pooled precision at R2's budget and k = 25/50/100 within
     sealed rows; paired IMO-clustered bootstrap vs R2 and vs `static_lightgbm` (P0 rule).
     Primary label: P4-9's new version if adopted, v1 alongside.
  5. `data/processed/walk_forward_scores.parquet` (P4-3b, scope `all`) holds sealed vessels'
     scores and is never sliced by group. New runs write scope-suffixed files holding only their
     scope's rows.
- **Known limits of the seal.** (a) P4-3b's pooled numbers above mixed dev and sealed rows, so
  the choice of the static columns, LightGBM and TabICLv2 was informed by aggregates that
  included sealed vessels: the seal is clean for every decision from here on, not for those.
  (b) Positives added by new label sources (P4-9) never counted as positives in any metric, so on
  them the seal is fully clean; they get their own line in the final scoring. (c) 63 sealed
  forward-positive IMOs (v1) is a small set, so the final intervals will be wide.

_2026-09-29_ (P4-10 amendment, before any new attempt was scored: decide on `dev_clean`)

- **Flaw found in rule 3.** `examined` IMOs are ever-sanctioned by construction, so in the
  primary pool every examined row is a forward positive. Scope `dev` (examined + dev) therefore
  has about twice the population's positive rate (R2 at its own budget: 0.278 in `dev` vs 0.180 on
  all rows in P4-3b) and 193 of its 251 positive IMOs are the examined vessels -- the bias the
  split exists to remove. Seen on the first `dev` run (the v1 reference, P4-3b's models only;
  `outputs/walk_forward_summary_dev.txt`), before any new label, feature or model was scored.
- **Amended rule 3.** The decision metric is pooled precision at k=50 in scope `dev_clean` (dev
  IMOs only), with R2's budget next to it. `dev_clean` and `sealed` are built by the same hash
  over the same non-examined IMOs, so `dev_clean` is an exchangeable preview of `sealed`; its cost
  is size (58 forward-positive IMOs under v1). `dev` is still reported, as secondary. Everything
  else in P4-10 stands.

_2026-09-29_ (P4-11: GFW port visits -- pre-registered before any port visit is fetched in bulk)

- **Source.** GFW API v3. IMO -> GFW vessel ids via `/v3/vessels/search` (`where=imo='<imo>'`,
  `datasets[0]=public-global-vessel-identity:latest`; every `selfReportedInfo` id, since a ship
  has several ids across MMSI/flag changes); port visits via `/v3/events`
  (`datasets[0]=public-global-port-visits-events:latest`, `vessels[i]=<id>`), 2023-06-01..
  2025-03-01. Probe: 5/5 sample IMOs resolved, one vessel's 72 visits in one 0.6 s call, each
  with `startAnchorage.flag` (ISO3), anchorage name/id, confidence 2-4. (A scout reported 0%
  resolution and no per-vessel filtering; both were wrong -- it omitted `datasets[0]`.)
  Licence: GFW API data is non-commercial with attribution; nothing derived goes on the website
  before the terms are checked.
- **Coverage gate (G2-style, P4-1's lesson), before any model uses the features.** Resolution
  rate (IMO found in GFW) and "any port visit found" rate, forward-positive vs never-sanctioned,
  on non-sealed IMOs only. NO-GO if the rates differ by more than 5 points with a 95% interval
  excluding 0 -- the same contamination that sank Wikidata build years. Result recorded here.
- **Features** (per row = vessel x window, from visits with `end < window_end` and `end >=
  window_end - 270 days`; 270 days is the longest lookback the first window allows, since data
  starts 2023-06-01; confidence >= 3 only):
  `gfw_resolved` (0/1), `pv_n_total`, `pv_n_rus`, `pv_any_rus`, `pv_n_rus_oil` (RUS anchorages
  whose name matches, case-insensitive, one of PRIMORSK, UST-LUGA/UST LUGA, VYSOTSK,
  NOVOROSSIYSK, SHESKHARIS, TAMAN, TUAPSE, KAVKAZ, KOZMINO, NAKHODKA, DE-KASTRI/DE KASTRI,
  MURMANSK, SABETTA, SAINT PETERSBURG/ST PETERSBURG -- the matched anchorage names are listed in
  the result entry), `pv_days_since_rus` (capped at 270; 270 when none), `pv_share_south`
  (share of visits in IND, TUR, CHN, ARE, EGY), `pv_n_sanctioned_states` (IRN, VEN, SYR, PRK).
  Unresolved vessels get NaN for every `pv_*` column (training-median imputation, as for the
  static columns) plus `gfw_resolved = 0`.
- **Variants** (new names; the frozen P4-3c set is untouched): `ports_logistic`,
  `ports_lightgbm`, `ports_tabicl` = the static recipes on STATIC_COLUMNS + the columns above.
  Decision per P4-10's amended rule 3 (dev_clean, k=50, R2's budget alongside), against the
  current best static model on dev_clean.
- **Known risks.** GFW recomputes history with its current algorithms, so a 2024 visit as served
  today is not exactly what was knowable in 2024 (the AIS underneath is). Shadow-fleet vessels
  that go dark in Russian ports produce fewer visits, which works against the feature, not for
  it. A Russian-terminal visit is close to the designation reason itself, like `dest_russia`
  (README must say so).

_2026-09-29_ (P4-10 reference on `dev_clean`, v1 labels -- the bar for P4-9..P4-13)

- **Run.** `python -m model.walk_forward --scope dev_clean --no-embeddings` (P4-3b's models,
  unchanged; `outputs/walk_forward_summary_dev_clean.txt`). Primary pool test positives: 24/21/
  21/0/16/17/17 rows (Aug..Feb; June and November have none left, all their positives are
  examined).
- **Pooled precision, dev_clean** (ceiling in brackets): R2's budget [0.070]: every model
  0.063-0.065, indistinguishable (the budget dwarfs the positives). k=25 [0.663]: R2 0.063,
  static_logistic 0.223, static_lightgbm 0.314, tabicl 0.326. **k=50 [0.331]: R2 0.063,
  static_logistic 0.180, static_lightgbm 0.226, tabicl 0.211**, lgbm_detectors_context 0.197,
  rule_tanker_dest 0.218. k=100 [0.166]: 0.063 / 0.116 / 0.133 / 0.133. Every static model beats
  R2 at k = 25/50/100 (IMO-clustered intervals exclude 0).
- **Bar for the decision metric: static_lightgbm, 0.226 at k=50.** Configurations tried on
  dev_clean so far: 0 new (this is the reference).
- **Reading.** On vessels nobody examined, the honest small-budget precision is ~0.2-0.3, not
  P4-3b's pooled 0.57-0.71 at k=50 (which the examined vessels inflated), and still 3-5x R2.

_2026-09-29_ (P4-13: model-side variants -- pre-registered before they are run)

Run first on the frozen static columns (v1 labels, dev_clean), and again at the end on the final
feature set. Each is compared with `static_lightgbm` on dev_clean (amended rule 3). Every other
setting is P4-3's PARAMS.
- **`ens_lgbm_tabicl`**: per cutoff, the mean of the test-row percentile ranks of
  `static_lightgbm` and `tabicl` (average ranks for ties). No labels involved.
- **`static_pu_lightgbm`** (bagging PU, Mordelet & Vert 2014): 50 bags; each bag = every training
  positive + 5x as many training non-positives drawn uniformly without replacement (all of them
  if fewer), LightGBM seed = bag index; score = mean P over bags. Treats non-positives as
  unlabelled rather than as clean negatives (EU-only and future designations sit among them).
- **`static_hazard_lightgbm`** (P4-3g's discrete-time framing, adapted): a training row of window
  w is positive iff designated in (w.end, w.end + 182 days] and before the cutoff; a non-positive
  row is kept only if w.end + 182 days < cutoff (its horizon is fully observed), else dropped as
  censored. 182 days rather than P4-3g's 1 or 3 months because the test label is long-horizon
  (designated any time after window_end); h = 1/3 answer a timing question this metric does not
  measure, and are not run.

_2026-09-29_ (P4-12: cross-month vessel history -- pre-registered before any history feature is
computed)

Per row (vessel IMO x window W), using only information dated strictly before W's `window_end`.
- **From GFW self-reported identity segments** (`data/reference/gfw/vessel_ids.parquet`, P4-11:
  ssvid, shipname, flag, `transmissionDateFrom`/`To`). Only segments with `transmissionDateFrom <
  window_end`, each clipped at `window_end` (a `transmissionDateTo` after it is never read).
  - `hist_n_flags_730d`, `hist_n_names_730d`, `hist_n_mmsi_730d`: distinct flags / names / ssvid
    among segments active at any time in [window_end - 730 d, window_end).
  - `hist_flag_age_days`: days from the start of the current flag run (the latest segment whose
    flag differs from the segment before it; the earliest segment if the flag never changed) to
    window_end, capped at 1,825.
  - `hist_ais_age_days`: window_end minus the earliest `transmissionDateFrom`, capped at 3,650.
  - `hist_to_foc_730d`: 1 if, inside the 730-day window, a segment under a non-FOC flag is
    followed by one under an FOC flag (`process.foc`'s registries, mapped to ISO3).
  - IMOs GFW does not resolve: NaN in all of these (training-median imputation).
- **From this archive's own panels** (Danish coverage), per IMO, windows strictly before W:
  - `hist_prior_windows_seen`: share of the previous (up to) 6 monthly windows in which the IMO
    appears; NaN when W is the first window.
  - `hist_prior_dest_russia`: 1 if any of those windows has `dest_russia` = 1 for the IMO; NaN
    when W is the first window.
- **Not features:** flag or name changes AFTER window_end (they mostly follow designation, CREA),
  GFW `registryInfo` (ownership/registry data is compiled with later knowledge).
- **Variants:** `hist_lightgbm`, `hist_tabicl` = the static recipes on STATIC_COLUMNS + the P4-11
  port columns (if P4-11 is adopted) + these columns. Decision per amended rule 3.

_2026-09-29_ (P4-3h: implied eastern-Baltic loading from draught -- pre-registered before any
crossing is computed)

- **Gate line.** Meridian 14.0 E between 54.3 N and 56.0 N (Arkona basin, east of the Sound and
  the Fehmarn Belt, west of Bornholm): traffic to and from the eastern Baltic crosses it inside
  Danish coverage. A crossing = two consecutive positions of one IMO (any of its mmsi) on
  opposite sides of 14.0 E, both inside the latitude band, at most 2 h apart. Tankers only
  (`ship_type = 'Tanker'` in that window's roster), valid IMO.
- **Round trip.** An eastbound crossing followed by that IMO's next westbound crossing, 2 to 30
  days later. Crossing draught = median reported draught in the +-12 h around the crossing (NaN if
  none; such trips are not counted). Relative change = (west - east) / the vessel's max draught
  seen before the westbound crossing.
- **Thresholds are fixed on March 2024 only** (clean days 2024-03-02..03-31, which precede every
  window's rows): the laden-return cut is the lowest point of a smoothed histogram of relative
  change between its two main modes; if no bimodality is visible, a fixed 0.20 (20% of max
  draught) is used. The chosen value is recorded here before any window's feature is built.
- **Validation gate (label-free), before any model uses it.** Over up to 100 laden-return trips
  (all months, 2024-04..2025-02), the share with a GFW port visit (P4-11, confidence >= 3)
  between the two crossings at a RUS anchorage. >= 80% -> continue; < 80% -> the feature is
  reported as "eastern-Baltic loading, not specifically Russian" and still tested, with that
  name. The share is recorded here either way.
- **Features** (per row = vessel x window W; trips whose westbound crossing is in
  [max(2024-03-02, window_end - 270 d), window_end)): `lr_n_trips`, `lr_n_laden_returns`,
  `lr_laden_share` (NaN when no trips), `lr_rate_per_30d` = laden returns per 30 observable days
  (the lookback length varies for early windows). knowable_at = the westbound crossing time.
- **Variants:** `draught_lightgbm` = static recipe on STATIC_COLUMNS + the adopted P4-11/P4-12
  columns + these. Decision per amended rule 3.

_2026-09-29_ (P4-13 first run: static columns, v1 labels, dev_clean)

- **Run.** `python -m model.walk_forward --scope dev_clean --no-embeddings` with the three
  pre-registered variants. Pooled dev_clean precision (ceiling), difference vs `static_lightgbm`
  with its IMO-clustered 95% interval:
  - k=50 [0.331]: static_lightgbm 0.226; **static_pu_lightgbm 0.246 (+0.020 [+0.000,
    +0.046])**; ens_lgbm_tabicl 0.223 (-0.003 [-0.029, +0.023]).
  - k=25 [0.663]: 0.314; PU 0.394 (+0.080 [+0.011, +0.097]); ens 0.343 (+0.029, n.s.).
  - k=100 [0.166]: 0.133; PU 0.143 (+0.010 [+0.001, +0.020]); ens 0.140 (+0.007 [+0.001,
    +0.016]).
  - R2's budget [0.070]: all 0.065-0.066, indistinguishable.
  PU's gain comes from the early cutoffs, where positives are fewest (Aug 0.36 vs 0.28, Sep 0.34
  vs 0.26 at k=50; equal from November on).
- **`static_hazard_lightgbm` is not evaluable as pre-registered:** a non-positive row needs its
  whole 182-day horizon before the cutoff, and the archive starts in April 2024, so no negatives
  exist before the 2024-11 cutoff and Aug-Oct are "not scorable". My design flaw, noticed only on
  running. Descriptive only, Nov-Feb at k=50: 0.000/0.260/0.280/0.240 vs static_lightgbm's
  0.000/0.260/0.260/0.240 -- no gain. Dropped; not rerun with a shorter horizon (that would be a
  new configuration chosen after seeing this one).
- **Decision (amended rule 3):** PU raises dev P@50 without lowering precision at R2's budget ->
  **adopted; the bar is now static_pu_lightgbm, 0.246 at k=50.** The ensemble is not adopted.
  Consequence for P4-11/P4-12/P4-3h: each new feature set is run with BOTH the LightGBM and the PU
  recipe (`<set>_lightgbm`, `<set>_pu_lightgbm`), and judged against the current bar. This adds
  one configuration per feature set, disclosed here.
- **Configurations tried on dev_clean so far: 3** (ens, PU, hazard).

_2026-09-29_ (P4-9: labels v2 landed and adopted -- before any model is scored with them)

- **Built.** `ingest/sanctions_v2.py` -> `data/reference/sanctions_v2.parquet` = the 2,205 v1 rows
  unchanged + `eu_vessels` 671 (Danish Maritime Authority's list of EU vessel designations; dates
  agree 671/671 with OpenSanctions' `eu_sanctions` mirror; package-date months 2024-06..2026-07),
  `ca` 731 (Global Affairs Canada SEMA XML, 5 excluded for non-numeric IMO), `nz` 210 (MFAT
  Russia Sanctions Register, "Ships" sheet). Dates capped at 2026-09-21 (same horizon as v1).
  Australia (DFAT blocks automated downloads) and Switzerland (no bulk export with per-vessel
  dates found) are not included.
- **Matches.** `process.sanctions_match` over `data/identity/mmsi_imo/window=*/part-0.parquet`
  (the same invocation reproduces the existing v1 table exactly: 728 rows, 0 differences) ->
  `data/identity/sanctions_matches_v2.parquet`: 478 matched IMOs vs v1's 459; **19 IMOs are new
  and 94 v1 IMOs get an earlier first designation** (an EU/CA/NZ listing before OFAC/UK's).
- **Adopted as the primary label, on validity grounds, before any model sees it:** these are
  real designations; under v1 an EU/CA/NZ-only vessel counted as a clean negative and a vessel
  listed by the EU months before OFAC/UK counted as "not yet sanctioned". From here on dev_clean
  decisions use v2, and the bar is re-measured under v2 (a re-run of existing models, not a new
  configuration). v1 is kept for comparison in the final report.
- **The P4-10 split is unchanged** (examined stays the frozen v1 list); the 19 new IMOs fall into
  dev or sealed by the hash.
