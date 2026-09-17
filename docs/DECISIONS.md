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
