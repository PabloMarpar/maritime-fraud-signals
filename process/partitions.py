"""Shared Hive-style day-partition and window-partition helpers.

Extracted from ``process.identity``, ``process.tracks`` and ``detect.coverage``, which had
carried byte-identical copies of these three functions since each was written independently
against the same ``data/clean/ais_dk/date=YYYY-MM-DD/part-0.parquet`` layout. Kept here as one
inspectable module rather than a fourth copy for ``detect.liveness``.

``window_partition_path`` and ``atomic_write_parquet`` (P3-4/A2) extend the same idea to the
whole-window artifacts every detector and ``process.tracks``/``process.identity`` produce: one
file per ``[start, end]`` window under ``data/<kind>/window=<start>_<end>/part-0.parquet``,
instead of the single ``data/<kind>/<kind>.parquet`` a later window used to silently overwrite --
see ``docs/PLAN_P4-0_P3-4.md``'s A2.
"""

from __future__ import annotations

import glob as glob_module
import logging
import os
import subprocess
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)


def daterange(start: date, end: date) -> Iterator[date]:
    """Yield each date from start to end, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def partition_path(day: date, root: Path) -> Path:
    """Hive-style partition path for one day, e.g. .../date=2024-06-05/part-0.parquet."""
    return root / f"date={day.isoformat()}" / "part-0.parquet"


def existing_partitions(start: date, end: date, in_root: Path) -> list[tuple[date, Path]]:
    """Return (day, path) for every day in [start, end] whose clean partition exists.

    A missing day is logged and skipped, not fatal: building an aggregate over a range is
    meant to work with whatever has been cleaned so far, not require every day be present.
    """
    found = []
    for day in daterange(start, end):
        path = partition_path(day, in_root)
        if path.exists():
            found.append((day, path))
        else:
            logger.warning("No clean partition for %s at %s, skipping", day.isoformat(), path)
    return found


def window_partition_path(start: date, end: date, root: Path) -> Path:
    """Hive-style partition path for a whole [start, end] window, e.g.

    .../window=2024-06-01_2024-06-30/part-0.parquet

    Used by every detector and by process.tracks/process.identity (P3-4/A2) so that a later
    window's build lands next to an earlier one's instead of overwriting it -- the single
    ``<kind>.parquet`` layout every one of them used before could not be accumulated across
    windows sampled from different years.
    """
    return root / f"window={start.isoformat()}_{end.isoformat()}" / "part-0.parquet"


# Reading back a `window=...` path or glob: DuckDB auto-detects the Hive-style directory name
# and injects an extra `window` string column (e.g. "2024-06-01_2024-06-30") into `SELECT *`,
# even for a single explicit file path, not only a glob over several windows -- verified in
# test_partitions.py. This does not collide with the explicit `window_start`/`window_end` DATE
# columns every builder already writes, but a caller doing `SELECT *` across a
# `window=*/part-0.parquet` glob will see it and should not be surprised by it.


def partition_exists(path: Path) -> bool:
    """True if ``path`` is a literal file/dir that exists, or -- when it contains a glob
    wildcard, as a consumer's ``window=*/part-0.parquet`` default now does post-P3-4/A2 -- at
    least one file matches it. ``Path.exists()`` alone always returns False for a pattern
    containing ``*``, since no file is literally named that; every consumer whose default input
    path changed from a single file to a window glob must use this instead, or its "does the
    input exist" precondition check would always fail even when data is present.
    """
    pattern = path.as_posix()
    if not any(c in pattern for c in "*?["):
        return path.exists()
    return bool(glob_module.glob(pattern))


def git_sha() -> str:
    """Short git commit SHA of the working tree, or "unknown" if it can't be determined.

    Provenance metadata only, never correctness-critical, so any failure (not a git repo, git
    not on PATH, etc.) falls back to a literal string rather than raising. Was a byte-identical
    ``_git_sha()`` copy in every one of the modules this is now shared by; consolidated here
    while touching all of them for P3-4/A2's window-partitioned output.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def atomic_write_parquet(con: duckdb.DuckDBPyConnection, select_sql: str, out_path: Path) -> None:
    """Write the result of ``select_sql`` to ``out_path`` atomically: temp file + os.replace.

    A partial, mid-write Parquet file would otherwise exist and be non-empty, which a naive
    idempotency check ("does the output path exist?") could mistake for a complete build -- see
    ``docs/PLAN_P4-0_P3-4.md``'s A0.8. ``select_sql`` must be a complete ``SELECT`` statement
    (typically ``SELECT *, ... AS window_start, ... AS built_at, ... AS git_sha FROM <view>``);
    this function only owns the destination side of the write, not the query.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tmp")
    con.execute(f"COPY ({select_sql}) TO '{tmp_path.as_posix()}' (FORMAT PARQUET)")
    os.replace(tmp_path, out_path)
