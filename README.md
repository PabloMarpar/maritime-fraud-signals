# maritime-fraud-signals

Detecting evasive vessel behaviour from open AIS data, validated against official sanctions lists.

> **Status:** scaffolding. No analysis has been run yet. See [`docs/STATE.md`](docs/STATE.md).

## The problem

Merchant vessels broadcast their position continuously over AIS (Automatic Identification System),
a public, mandatory radio protocol. Vessels engaged in sanctions evasion routinely interfere with
that signal: they switch the transponder off in specific waters, broadcast falsified positions,
change identity, and transfer cargo ship-to-ship on the open sea to obscure its origin.

Over 2,100 vessels are currently designated under US, EU, UK and UN sanctions regimes for this
behaviour, and new tranches are published every month.

## The approach

Five rule-based detectors run over reconstructed vessel tracks. Their output feeds a risk score
that ranks vessels for analyst review. Every alert carries the specific evidence that triggered it.

The detectors are deliberately **not** machine learning. They encode physics and maritime practice,
so they work without training data and their reasoning is inspectable.

## How it is evaluated

Strict forward-looking validation: the model is fitted only on information available before a cutoff
date `T`, then scored against sanctions designations published **after** `T`. This measures whether
the system anticipates official designations rather than memorising them.

Calibration is reported explicitly — a score of 0.8 must be right about 80% of the time.

## Limitations

**Label bias.** Sanctioned vessels are the ones that were *caught*, not all offenders. A model
trained on designations learns the sanctioning bodies' own targeting priorities — flag, trade
route, ownership nationality, political salience — at least as much as it learns evasive
manoeuvring. A high score may mean "this vessel resembles vessels regulators choose to
designate", not "this vessel is evading". Forward-looking validation (above) tests whether the
system anticipates designations; it does not, on its own, correct for the fact that the target
being anticipated is itself a selection process.

**What a corroboration verdict does and does not mean.** The AIS-gap detector judges a silence by
asking whether *other* vessels were heard nearby at the time (`detect/liveness.py`). All three
possible verdicts are conditioned on there having been some vessel in the area to be heard at all:
"the receiver was working" is inferred from "traffic happened to be there", and "a receiver outage
is plausible" is inferred from "the area is normally busy but nobody happened by". Neither is
ground truth for what the receiver could actually hear at the moment in question.

## Data sources

All open, free, and publicly documented. See [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

No personal data enters the pipeline at any stage. The only identifiers used are vessel identifiers
(MMSI, IMO), which are public by international convention.

## Reproducing the environment

A `Dockerfile` pins Python and every dependency so the pipeline runs identically on any machine.
`data/` and `outputs/` are never baked into the image — they are gitignored and mounted as volumes
so results land back on the host:

```bash
docker build -t maritime-fraud-signals .

# Run the test suite (the default command):
docker run --rm -v "$(pwd)/data:/app/data" -v "$(pwd)/outputs:/app/outputs" maritime-fraud-signals

# Drop into a shell to run an ingestion module instead:
docker run --rm -it \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/outputs:/app/outputs" \
  -v "$(pwd)/.env:/app/.env:ro" \
  maritime-fraud-signals bash
```

Secrets (e.g. the AISStream key, once `ingest/aisstream.py` exists) live in `.env` on the host and
are mounted read-only at run time — never copied into the image.

## Licence

MIT. Each upstream data source retains its own terms; see `docs/DATA_SOURCES.md` for attribution.
