# maritime-fraud-signals

Detecting evasive vessel behaviour from open AIS data, validated against sanctions lists.

## Start every session here

Run `/retomar`. It reads `docs/STATE.md`, `tasks.json` and recent commits — about 1.5k tokens, and
enough to know exactly where the work stands. Do not reconstruct state any other way.

Run `/cerrar` **before** context runs low, not after. It updates state and commits.

## Where things live

| Path | Contains |
|---|---|
| `ingest/` | Downloaders: Danish AIS history, AISStream live feed, sanctions lists |
| `process/` | Cleaning, identity resolution, track reconstruction, voyage segmentation |
| `detect/` | The five detectors. Pure functions over clean tracks |
| `features/` | Aggregation to the vessel-month panel |
| `model/` | Anomaly scoring and supervised risk scoring |
| `report/` | Vessel dossiers, GeoJSON export for the map |
| `viz/` | deck.gl map, built to GitHub Pages |
| `docs/STATE.md` | Done / in progress / blocked. The live state of the project |
| `docs/DECISIONS.md` | One line per decision, with the reason. Read before re-opening a settled question |
| `docs/DATA_SOURCES.md` | Every source, its access method, quirks and licence |
| `tasks.json` | Granular task list with `done` flags |

Full project plan: `C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`

## Token discipline

This project handles datasets far larger than any context window. These rules are not optional.

1. **Raw data never enters context.** Never `cat`, `head` or `Read` a file under `data/`. Query it
   with DuckDB and return an aggregate or at most 10 rows.
2. **Delegate heavy exploration to subagents.** They work in their own context and return a short
   summary. See `.claude/agents/`.
3. **One session, one objective.** Finish it, run `/cerrar`, start fresh.
4. **Commit small and often.** Git history is free memory; the conversation is not.

## Working agreements

- Code and documentation in English. Conversation with the author in Spanish, in plain language:
  define each technical term the first time it appears.
- Detectors are rule-based, not learned. Keep them inspectable and unit-tested against synthetic
  cases.
- Models: Isolation Forest (unsupervised) and LightGBM (supervised). Nothing exotic until the
  baseline is beaten. Every model must beat the naive baseline — "tanker over 15 years old under a
  flag of convenience" — or the problem is in the features, not the model.
- **Never let future information reach a model trained on the past.** Every feature must be
  computable from data available strictly before the cutoff date. Run `analyst-review` to check this
  after any modelling change; a temporal leak invalidates the entire result and is easy to miss.
- State limitations explicitly in the README rather than burying them.

## Commands

```bash
pytest                 # run tests
ruff check .           # lint
python -m ingest.dma   # (not yet implemented) download Danish AIS for a date range
```

## Attribution

Never add `Co-Authored-By` trailers, "Generated with Claude Code" lines, or any other AI-attribution
footer to commits, pull requests or documentation in this repository. The author's name and email are
the only attribution. This applies to every commit without exception.
