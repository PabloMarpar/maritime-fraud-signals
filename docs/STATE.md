# Project state

_Last updated: 2026-09-16_

## Done

- Plan approved. Full plan lives at
  `C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`.
- Repository scaffolded: directory layout, continuity files, subagent definitions, `/retomar` and
  `/cerrar` commands, dependency list, licence, README.
- Data sources verified as open and reachable on 2026-09-16 (see `docs/DATA_SOURCES.md`).

## In progress

- Nothing. Next session starts Phase 0 proper.

## Next up

Phase 0, remaining items — see `tasks.json` for the granular list:

1. Download a **single day** of Danish AIS data and land it as Parquet.
2. Open it with DuckDB, confirm the column schema, count rows and distinct vessels.
3. Render those vessels on a static map.

The goal of Phase 0 is to see ships on screen on day one. A pipeline that takes two weeks before it
shows a single ship gets abandoned.

## Blocked

- Nothing.

## Open questions

- Which date to use as the first sample day. Pick one with normal traffic, not a holiday.
- Whether the Danish HTTPS certificate issue requires a documented `verify=False` or plain HTTP.
