"""Orchestrate the download -> clean -> discard-raw cycle, one day at a time.

A day of Danish AIS history is large enough (hundreds of MB raw, several GB
transiently while unzipping) that simply looping ``ingest.dma.download_range``
over a multi-month window would fill the disk before Phase 2's detectors ever
run. This module is the explicit cycle that keeps disk usage bounded: for
each day, download the raw file, clean it, verify the clean result looks
sane, then delete the raw file -- since it can always be re-fetched from the
public DMA archive -- and record what happened in
:mod:`pipeline.manifest` before moving to the next day.

**Resumability is the whole point.** Each day is independent and is recorded
in the manifest as soon as its state changes (after download, after clean,
after raw is discarded), not batched up and written once at the end of a
range. If the process is killed mid-day -- or mid-*range* -- rerunning
``backfill_range`` over the same ``start``/``end`` skips every day already
marked ``"clean"`` (or ``"reduced"``) in the manifest and picks up exactly
where it left off, without re-downloading anything that already finished.

**Never silently over-delete.** Two guards sit between "cleaned" and "raw
discarded": a disk-space check before the day even starts (so a
misconfigured range aborts loudly instead of filling the disk one day at a
time), and a retention check after cleaning (if the clean partition kept
suspiciously few rows relative to the raw one -- under 10% -- that smells
like a cleaning bug, not real data loss, so the raw file for that day is left
in place for manual inspection rather than deleted). Both are warnings-and-
skip for the retention case, or a hard ``RuntimeError`` for the disk guard --
never a silent delete of the only remaining copy of a day's raw data.

**Why raw discard, not clean discard, in this module.** The five fraud
detectors (Phase 2) do not exist yet, so there is no way to know today what
must be kept from a cleaned day once a detector runs over it. Discarding
*clean* data is therefore explicitly out of scope here (see
``docs/DECISIONS.md``); this module only ever throws away raw data, which is
safely reproducible from the DMA archive and never a load-bearing artifact
once it has been cleaned.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from ingest.dma import download_day
from pipeline import manifest
from process.clean import clean_day

logger = logging.getLogger(__name__)

DATA_ROOT = Path("data")

# Below this fraction of raw_rows surviving cleaning, something looks wrong
# with the cleaning step itself rather than the data -- see module docstring.
MIN_RETENTION_RATIO = 0.10

DEFAULT_MIN_FREE_GB = 20.0


def _daterange(start: date, end: date) -> Iterator[date]:
    """Yield each date from start to end, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_count(path: Path) -> int:
    con = duckdb.connect()
    try:
        (count,) = con.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchone()
        return count
    finally:
        con.close()


def backfill_day(
    day: date,
    data_root: Path = DATA_ROOT,
    manifest_path: Path | None = None,
    keep_raw: bool = False,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the download -> clean -> verify -> discard-raw cycle for one day.

    Idempotent and resumable: a day already "clean" (or "reduced") in the
    manifest is skipped unless force=True, and each state-changing step is
    recorded in the manifest immediately, not batched to the end.
    """
    manifest_path = manifest_path or data_root / "manifest.json"

    state = manifest.day_state(manifest_path, day)
    if state in ("clean", "reduced") and not force:
        logger.info(
            "%s already %s, skipping (pass force=True / --force to redo)", day.isoformat(), state
        )
        return

    # Disk guard: create data_root first so disk_usage measures the volume
    # this run actually writes to, not wherever the cwd happens to resolve.
    data_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(data_root).free
    min_free_bytes = min_free_gb * 1e9
    if free_bytes < min_free_bytes:
        raise RuntimeError(
            f"Refusing to backfill {day.isoformat()}: {free_bytes / 1e9:.1f} GB free under "
            f"{data_root}, need at least {min_free_gb:.1f} GB"
        )

    if dry_run:
        logger.info(
            "[dry-run] %s: would download -> clean -> verify -> %s raw",
            day.isoformat(),
            "keep" if keep_raw else "discard",
        )
        return

    tmp_dir = data_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw_root = data_root / "raw" / "ais_dk"
    clean_root = data_root / "clean" / "ais_dk"

    raw_path = download_day(day, out_root=raw_root, tmp_dir=tmp_dir, force=force)
    raw_rows = _row_count(raw_path)
    raw_bytes = raw_path.stat().st_size
    manifest.record(
        manifest_path, day, raw_rows=raw_rows, raw_bytes=raw_bytes, downloaded_at=_now_iso()
    )
    logger.info("%s: downloaded %d raw rows (%d bytes)", day.isoformat(), raw_rows, raw_bytes)

    clean_path = clean_day(day, in_root=raw_root, out_root=clean_root, force=force)
    clean_rows = _row_count(clean_path) if clean_path.exists() else 0
    clean_bytes = clean_path.stat().st_size if clean_path.exists() else 0
    manifest.record(
        manifest_path, day, clean_rows=clean_rows, clean_bytes=clean_bytes, cleaned_at=_now_iso()
    )
    logger.info("%s: cleaned %d rows (%d bytes)", day.isoformat(), clean_rows, clean_bytes)

    # Verification before delete: never discard the only copy of a day's raw
    # data on the strength of a suspicious clean result.
    if not clean_path.exists() or clean_rows <= 0:
        logger.warning(
            "%s: clean partition missing or empty, leaving raw in place for inspection",
            day.isoformat(),
        )
        return
    retention_ratio = clean_rows / raw_rows if raw_rows else 0.0
    if retention_ratio < MIN_RETENTION_RATIO:
        logger.warning(
            "%s: clean/raw retention ratio %.1f%% is suspiciously low (< %.0f%%), "
            "leaving raw in place for inspection",
            day.isoformat(),
            retention_ratio * 100,
            MIN_RETENTION_RATIO * 100,
        )
        return

    if keep_raw:
        logger.info("%s: keep_raw=True, leaving raw partition in place", day.isoformat())
        return

    raw_partition_dir = raw_path.parent
    raw_path.unlink()
    if raw_partition_dir.exists() and not any(raw_partition_dir.iterdir()):
        raw_partition_dir.rmdir()
    manifest.record(manifest_path, day, raw_discarded_at=_now_iso())
    logger.info("%s: raw partition discarded", day.isoformat())


def backfill_range(
    start: date,
    end: date,
    data_root: Path = DATA_ROOT,
    manifest_path: Path | None = None,
    keep_raw: bool = False,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Run backfill_day for every day in [start, end], inclusive, stopping on a real failure.

    Because each day is fully recorded in the manifest before moving to the
    next, rerunning this over the same range after a crash resumes exactly
    where it left off (see module docstring). A day raising (disk guard, a
    genuine download/clean error) is logged and re-raised -- this is
    different from the low-retention case inside backfill_day, which is a
    warning-and-continue by design, not a failure of the range.
    """
    for day in _daterange(start, end):
        try:
            backfill_day(
                day,
                data_root=data_root,
                manifest_path=manifest_path,
                keep_raw=keep_raw,
                min_free_gb=min_free_gb,
                force=force,
                dry_run=dry_run,
            )
        except Exception:
            logger.error("Backfill failed on %s, stopping range", day.isoformat())
            raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download, clean and discard raw AIS data one day at a time, resumably."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--keep-raw", action="store_true", help="Do not delete the raw partition after cleaning"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Log the plan for each day without touching disk"
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=f"Abort if free disk space under --data-root falls below this (default: {DEFAULT_MIN_FREE_GB})",
    )
    parser.add_argument(
        "--force", action="store_true", help="Redo a day even if already marked clean/reduced"
    )
    parser.add_argument(
        "--data-root", default=str(DATA_ROOT), help="Root directory for raw/clean/manifest data"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    backfill_range(
        start,
        end,
        data_root=Path(args.data_root),
        keep_raw=args.keep_raw,
        min_free_gb=args.min_free_gb,
        force=args.force,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
