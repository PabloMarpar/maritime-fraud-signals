"""JSON registry of what AIS data is on disk, per day, and in what state.

``pipeline.backfill`` downloads a day, cleans it, then deletes the raw file to
keep disk usage bounded (see the module docstring of ``pipeline.backfill`` for
the full cycle). Once the raw file is gone, nothing on disk records that it
ever existed, how many rows it had, or when it was fetched -- and a crash
mid-cycle must not leave that history ambiguous either. This module is that
record: it survives the deletion of the data it describes, so a rerun can
tell "never downloaded" apart from "downloaded, cleaned, raw discarded" apart
from "downloaded but never finished cleaning" without re-touching the
filesystem beyond stat-ing this one small file.

**Why JSON, not Parquet.** A backfill run covers dozens to a few hundred days,
one record each -- small enough to read at a glance and diff meaningfully in
git (see the ``!data/manifest.json`` exception in ``.gitignore``: unlike the
rest of ``data/``, this file is deliberately tracked). Parquet would need a
DuckDB round-trip to inspect a single day's state; a text editor is enough
for JSON.

**Why fields are absent, not null.** A day's record only carries the keys
that are actually known (e.g. a freshly-downloaded, not-yet-cleaned day has
no ``clean_rows`` key at all, rather than ``"clean_rows": null``). This keeps
``day_state`` a simple presence check and keeps the on-disk JSON honest about
what has actually happened, not what is merely expected to happen next.

**Atomic writes.** ``record`` writes to a ``.tmp`` sibling and ``os.replace``s
it over the real file. ``os.replace`` is atomic on both POSIX and Windows, so
a crash or power loss mid-write leaves either the old manifest or the new one
intact, never a half-written, unparseable file -- important here specifically
because the manifest is the *only* record of what has already been deleted.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path("data/manifest.json")

# Field names that mark a day as having reached each state, in day_state's
# escalating order. "reduced" is for Tramo 2 (clean-data discard, once the
# five detectors exist to justify what to keep): nothing writes it yet, but
# day_state already recognizes it so backfill.py's skip logic is future-proof.
_RAW_MARKER = "downloaded_at"
_CLEAN_MARKER = "cleaned_at"
_REDUCED_MARKER = "clean_discarded_at"


def load(path: Path = MANIFEST_PATH) -> dict:
    """Load the manifest, or {} if it does not exist yet (nothing downloaded so far)."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def record(path: Path, day: date, **fields) -> dict:
    """Merge fields into day's entry and save. Returns the updated full manifest.

    Existing fields for the day are kept; only keys passed in fields are
    added or overwritten. This lets backfill.py call record() once per step
    (download, clean, discard) without clobbering what earlier steps wrote.
    """
    manifest = load(path)
    key = day.isoformat()
    entry = dict(manifest.get(key, {}))
    entry.update(fields)
    manifest[key] = entry
    _write_atomic(path, manifest)
    return manifest


def day_state(path: Path, day: date) -> str:
    """Classify one day as "absent" | "raw_only" | "clean" | "reduced".

    Escalating: a day only reaches "clean" once cleaning markers are present
    (it may still also have raw fields -- discarding raw is a separate,
    later step), and only reaches "reduced" once the clean data itself has
    been discarded (Tramo 2, not produced by anything in this repo yet).
    """
    manifest = load(path)
    entry = manifest.get(day.isoformat())
    if entry is None:
        return "absent"
    if _REDUCED_MARKER in entry:
        return "reduced"
    if _CLEAN_MARKER in entry:
        return "clean"
    if _RAW_MARKER in entry:
        return "raw_only"
    return "absent"


def summary(path: Path = MANIFEST_PATH) -> dict:
    """Aggregate day counts by state, e.g. {"total_days": 30, "raw_only": 0, "clean": 30, "reduced": 0}."""
    manifest = load(path)
    counts = {"raw_only": 0, "clean": 0, "reduced": 0}
    for key in manifest:
        state = day_state(path, date.fromisoformat(key))
        if state in counts:
            counts[state] += 1
    return {"total_days": len(manifest), **counts}
