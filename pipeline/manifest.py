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

**Several processes may use it at once** (e.g. a background backfill of later months while
``pipeline.window`` runs detectors on earlier ones). ``record`` is a whole-file read-modify-write,
so without a lock two writers silently drop each other's updates, and on Windows ``os.replace``
fails if another process has the file open. Every read and write therefore holds a lock file
(``manifest.json.lock``, created with ``O_EXCL``); the critical section takes milliseconds, so a
lock older than :data:`LOCK_STALE_SECONDS` is from a killed process and is broken.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


LOCK_STALE_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.02


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold `path`'s lock file for the duration of the block -- see module docstring."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            # PermissionError: on Windows, a lock file being deleted by its holder.
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    logger.warning("Breaking stale manifest lock %s", lock)
                    lock.unlink(missing_ok=True)
                    continue
            except (FileNotFoundError, PermissionError):
                pass
            time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _load_unlocked(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load(path: Path = MANIFEST_PATH) -> dict:
    """Load the manifest, or {} if it does not exist yet (nothing downloaded so far)."""
    with _locked(path):
        return _load_unlocked(path)


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
    The read-modify-write runs under the manifest lock, so concurrent
    processes recording different days never lose each other's updates.
    """
    with _locked(path):
        manifest = _load_unlocked(path)
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
