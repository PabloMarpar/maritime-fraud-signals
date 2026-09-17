"""Shared Hive-style day-partition helpers.

Extracted from ``process.identity``, ``process.tracks`` and ``detect.coverage``, which had
carried byte-identical copies of these three functions since each was written independently
against the same ``data/clean/ais_dk/date=YYYY-MM-DD/part-0.parquet`` layout. Kept here as one
inspectable module rather than a fourth copy for ``detect.liveness``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

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
