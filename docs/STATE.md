# Project state

_Last updated: 2026-09-16_

## Done

- Plan approved. Full plan lives at
  `C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`.
- Repository scaffolded: directory layout, continuity files, subagent definitions, `/retomar` and
  `/cerrar` commands, dependency list, licence, README.
- Data sources verified as open and reachable on 2026-09-16 (see `docs/DATA_SOURCES.md`).
- `ingest/dma.py`: downloader for a day/date range of Danish AIS, lands Parquet partitioned by
  `date=YYYY-MM-DD`. Handles the unconfirmed `.zip` vs `.csv` format by trying both and sniffing
  actual bytes. 9 unit tests, all mocked HTTP, all passing; `ruff` clean.
- `Dockerfile` + `.dockerignore`: reproducible environment, `CMD ["pytest"]` by default, `data/` and
  `outputs/` mounted as volumes, secrets stay a runtime `.env` mount.

## In progress

- Nothing running right now. See "Blocked" below for what's paused and why.

## Next up

**Do this from home, on a network that isn't restricted:**

1. `python -m ingest.dma --start 2024-06-05 --end 2024-06-05` — the real live download. This working
   session's sandbox could not reach `web.ais.dk:80` at all (confirmed independently, not just the
   subagent's finding), so the real file format (`.zip` vs `.csv`) is still unconfirmed and no real
   day has actually been landed yet.
2. Open the landed Parquet with DuckDB: confirm the column schema, row count, distinct MMSI count.
   If the real format turns out to differ from what the code assumes, no code change should be
   needed — `ingest/dma.py` sniffs bytes rather than trusting the extension — but double check.
3. Point `report/quicklook_map.py` at that real partition and look at the actual PNG.

Then Phase 0 is complete and Phase 1 (cleaning, identity resolution, track reconstruction) starts.

## Blocked

- **Live download of real Danish AIS data.** This environment (a work computer, current session)
  cannot reach `http://web.ais.dk` on port 80 — outbound port 80 appears blocked at the sandbox
  level for *any* host, not something specific to this source. Deliberately not working around it
  with `dangerouslyDisableSandbox` while on the work machine. Resume from a home network instead.
- Consequently P0-3 (open the real Parquet with DuckDB) and the "real day" half of P0-4 (see below)
  are blocked on the same thing.

## Open questions

- ~~Which date to use as the first sample day.~~ Decided: **2024-06-05** (Wednesday, no EU/DK
  holiday). See `docs/DECISIONS.md`.
- ~~Whether the Danish HTTPS certificate issue requires a documented `verify=False` or plain
  HTTP.~~ Decided: plain HTTP, documented in `ingest/dma.py`'s module docstring.
- Whether the real DMA file is `.zip` or `.csv` — genuinely unconfirmed, not just undecided. Will be
  answered by the first successful live download.
