"""Downloader for Danish Maritime Authority (DMA) historical AIS data.

Source: the DMA AIS archive S3 bucket — one file per calendar day, covering
every AIS message received by the Danish coastal network that day. See
``docs/DATA_SOURCES.md`` for licence and reachability notes.

Two quirks of this source drive the design below:

1. **Path-style S3 URLs over HTTPS.** The archive has moved to an S3 bucket
   (``aisdata.ais.dk``, region ``eu-central-1``); the legacy host
   ``web.ais.dk`` no longer answers on port 80 and its HTTPS listener serves
   a mismatched certificate and then resets the connection. The bucket name
   contains dots, so the virtual-hosted URL
   ``aisdata.ais.dk.s3.eu-central-1.amazonaws.com`` does not match Amazon's
   ``*.s3.eu-central-1.amazonaws.com`` wildcard certificate. The path-style
   form used in ``BASE_URL`` puts the bucket in the path instead, so the
   hostname matches and TLS verification passes normally. Never "fix" a
   certificate error here by passing ``verify=False``.
2. **Keys are namespaced by year, and the format has varied.** A daily key
   looks like ``2024/aisdk-2024-06-05.zip``. Confirmed live on 2026-09-16:
   that day is a 610 MB zip. Older years ship monthly rather than daily
   archives, and bare CSV has been used in some periods, so ``_fetch_day``
   still tries both known patterns and ``_extract_csv`` sniffs the downloaded
   bytes (zip magic number) rather than trusting the key's extension.
3. **The endpoint routinely cuts a transfer mid-stream.** Confirmed twice
   independently: the A0.4 re-download drill (P3-4/A5, 2026-09-22) needed up
   to 4 attempts for one file even at a 300s client timeout, and building a
   second real window (2026-09-23) hit the same thing repeatedly across a
   60-day range (``httpx.ReadTimeout``, occasionally ``httpx.ConnectError``
   from a DNS lookup failure). ``_fetch_day`` retries a transient
   ``httpx.TransportError`` in place, per URL, with capped exponential backoff
   (:data:`MAX_FETCH_ATTEMPTS`, :data:`RETRY_BACKOFF_BASE_SECONDS`,
   :data:`MAX_BACKOFF_SECONDS`) before moving on to the next filename pattern
   or giving up -- a real 404 is not retried, only a network-level failure is.
   A retry resumes the partial file with an HTTP ``Range`` request (the bucket
   answers 206) instead of restarting ~500 MB from byte 0, and an attempt that
   received bytes resets the failure count.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb
import httpx

logger = logging.getLogger(__name__)

# Path-style S3 URL, not the legacy host: see module docstring point 1.
BASE_URL = "https://s3.eu-central-1.amazonaws.com/aisdata.ais.dk"

# Candidate keys for a given day, tried in order until one is found. Keys are
# namespaced by year. Zip is tried first: confirmed live for 2024-06-05 (see
# module docstring point 2); bare CSV is the documented fallback for periods
# where the archive shipped uncompressed.
FILENAME_PATTERNS = ("{year}/aisdk-{day}.zip", "{year}/aisdk-{day}.csv")

RAW_ROOT = Path("data/raw/ais_dk")

ZIP_MAGIC = b"PK\x03\x04"

# See module docstring point 3. Attempts count only consecutive failures with no bytes
# received; doubling from 2s, capped at 60s, 10 attempts wait ~6 min before giving up -- a
# 2026-09-25 outage of a few minutes (DNS failing) exhausted the old ~30s budget and failed every
# window of a multi-month build in cascade.
MAX_FETCH_ATTEMPTS = 10
RETRY_BACKOFF_BASE_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0


def _daterange(start: date, end: date) -> Iterator[date]:
    """Yield each date from start to end, inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _partition_path(day: date, out_root: Path) -> Path:
    """Hive-style partition path for one day, e.g. .../date=2024-06-05/part-0.parquet."""
    return out_root / f"date={day.isoformat()}" / "part-0.parquet"


def _fetch_day(day: date, client: httpx.Client, dest_dir: Path) -> Path:
    """Download the raw file for one day into dest_dir, streaming to disk.

    Tries each entry in FILENAME_PATTERNS in turn and returns the local path
    to whichever one exists. Streams the response body in chunks rather than
    buffering it in memory, since a day of AIS data can run into the
    hundreds of megabytes. A transient network failure (see module docstring
    point 3) is retried in place, per URL, before moving on to the next
    pattern -- a real 404 is not retried, it just means "try the next
    pattern" like before.
    """
    last_error: Exception | None = None
    for pattern in FILENAME_PATTERNS:
        key = pattern.format(year=day.year, day=day.isoformat())
        filename = Path(key).name
        url = f"{BASE_URL}/{key}"
        local_path = dest_dir / filename
        local_path.unlink(missing_ok=True)
        attempt = 1
        while True:
            offset = local_path.stat().st_size if local_path.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else None
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 404:
                        break  # not found under this pattern -- try the next one, no retry
                    if response.status_code == 416 and offset:
                        # Range starts at/after the end: the previous attempt had in fact
                        # received the whole body before the connection dropped.
                        logger.info("Downloaded %s (completed on resume)", url)
                        return local_path
                    response.raise_for_status()
                    # 206 honours the Range and appends; a 200 means the server ignored it,
                    # so start over rather than appending a second full copy.
                    mode = "ab" if response.status_code == 206 else "wb"
                    with open(local_path, mode) as fh:
                        fh.writelines(response.iter_bytes())
                logger.info("Downloaded %s", url)
                return local_path
            except httpx.HTTPStatusError as exc:
                last_error = exc
                break  # a real HTTP error (e.g. 403/500) -- try the next pattern, not a retry
            except httpx.TransportError as exc:
                last_error = exc
                if local_path.exists() and local_path.stat().st_size > offset:
                    attempt = 1  # bytes arrived before the drop: progress, not a dead host
                elif attempt == MAX_FETCH_ATTEMPTS:
                    logger.warning(
                        "Giving up on %s after %d attempts without progress (%s)",
                        url,
                        MAX_FETCH_ATTEMPTS,
                        exc,
                    )
                    break  # exhausted retries for this pattern -- try the next one, if any
                else:
                    attempt += 1
                backoff = min(
                    RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS
                )
                logger.warning(
                    "Transient error fetching %s (attempt %d/%d, %d bytes kept): %s -- "
                    "retrying in %.0fs",
                    url,
                    attempt,
                    MAX_FETCH_ATTEMPTS,
                    local_path.stat().st_size if local_path.exists() else 0,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
    raise FileNotFoundError(
        f"No file found for {day.isoformat()} under any of "
        f"{FILENAME_PATTERNS} at {BASE_URL}"
    ) from last_error


def _extract_csv(raw_path: Path, work_dir: Path) -> Path:
    """Return a path to a plain CSV file, extracting one from a zip if needed.

    Only the first four bytes are read to decide the branch, so a bare CSV
    day never has its (potentially large) content touched here. Extraction
    streams member bytes straight to disk, so a zipped day is never held in
    memory whole either.
    """
    with open(raw_path, "rb") as fh:
        magic = fh.read(4)

    if magic != ZIP_MAGIC:
        return raw_path

    with zipfile.ZipFile(raw_path) as zf:
        members = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not members:
            raise ValueError(f"{raw_path} is a zip archive with no CSV member")
        if len(members) > 1:
            logger.warning(
                "%s contains %d CSV members, using the first: %s",
                raw_path,
                len(members),
                members[0],
            )
        member = members[0]
        extracted_path = work_dir / Path(member).name
        with zf.open(member) as src, open(extracted_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return extracted_path


def _normalise(column: str) -> str:
    """DMA column headers are 'Title Case With Spaces'; make them SQL/parquet friendly.

    Confirmed live on 2026-09-16: the real file's first header is
    ``# Timestamp`` — a stray leading ``#`` (a comment-marker artifact from
    whatever tool the DMA uses to export the file), not a distinct field.
    Stripped here so the column lands as plain ``timestamp`` like every
    other field, rather than the meaningless ``#_timestamp``.
    """
    return column.strip().lstrip("#").strip().lower().replace(" ", "_").replace("-", "_")


def _csv_to_parquet(csv_path: Path, parquet_path: Path) -> int:
    """Load csv_path with DuckDB and write it to parquet_path. Returns the row count.

    DuckDB streams the CSV itself; nothing here loads the file into a Python
    object. ``ignore_errors=true`` drops structurally malformed lines (wrong
    field count for the header) instead of failing the whole day. Column
    types are auto-detected rather than hardcoded, since the exact schema was
    not confirmed against a live file (see module docstring point 2).
    """
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    # CREATE VIEW cannot be a prepared statement in DuckDB, so the path is
    # embedded as a literal below rather than bound as a parameter. It comes
    # from our own tempfile.TemporaryDirectory, never from user input.
    csv_source = csv_path.as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        con.execute(
            f"CREATE OR REPLACE VIEW raw AS SELECT * FROM read_csv('{csv_source}', "
            "header=true, auto_detect=true, ignore_errors=true)"
        )
        columns = con.execute("DESCRIBE raw").fetchall()
        select_list = ", ".join(
            f'"{col[0]}" AS "{_normalise(col[0])}"' for col in columns
        )
        # Written to a temporary sibling and renamed into place, so a process killed mid-write
        # never leaves a truncated part-0.parquet that download_day would then skip as done.
        tmp_path = parquet_path.with_name(parquet_path.name + ".tmp")
        con.execute(
            f"COPY (SELECT {select_list} FROM raw) TO '{tmp_path.as_posix()}' (FORMAT PARQUET)"
        )
        (row_count,) = con.execute(
            f"SELECT count(*) FROM read_parquet('{tmp_path.as_posix()}')"
        ).fetchone()
        os.replace(tmp_path, parquet_path)
        return row_count
    finally:
        con.close()


def download_day(
    day: date,
    out_root: Path = RAW_ROOT,
    force: bool = False,
    client: httpx.Client | None = None,
    tmp_dir: Path | None = None,
) -> Path:
    """Download and land one day of DMA AIS data as Parquet.

    Idempotent: if the day's partition already exists, this is a no-op
    unless force=True. Returns the path to the partition's Parquet file
    either way. tmp_dir, if given, is the parent directory for the working
    TemporaryDirectory (zip + extracted CSV); by default (None) that lands
    under the platform temp dir, which on Windows is the C: drive rather
    than wherever data/ lives -- pipeline.backfill passes tmp_dir=data/tmp
    explicitly so its disk-space guard measures the same volume the download
    actually writes to.
    """
    parquet_path = _partition_path(day, out_root)
    if parquet_path.exists() and not force:
        logger.info(
            "%s already downloaded, skipping (pass force=True / --force to re-fetch)",
            day.isoformat(),
        )
        return parquet_path

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        with tempfile.TemporaryDirectory(prefix=f"dma-{day.isoformat()}-", dir=tmp_dir) as tmp:
            work_dir = Path(tmp)
            raw_path = _fetch_day(day, client, work_dir)
            csv_path = _extract_csv(raw_path, work_dir)
            row_count = _csv_to_parquet(csv_path, parquet_path)
            logger.info("Landed %d rows for %s at %s", row_count, day.isoformat(), parquet_path)
    finally:
        if owns_client:
            client.close()
    return parquet_path


def download_range(
    start: date,
    end: date,
    out_root: Path = RAW_ROOT,
    force: bool = False,
    client: httpx.Client | None = None,
    tmp_dir: Path | None = None,
) -> list[Path]:
    """Download DMA AIS data for every day in [start, end], inclusive.

    A single client is reused across the whole range (connection pooling);
    pass one in for testing, otherwise a default one is created and closed
    automatically. tmp_dir is forwarded to download_day, see its docstring.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        results = [
            download_day(day, out_root=out_root, force=force, client=client, tmp_dir=tmp_dir)
            for day in _daterange(start, end)
        ]
    finally:
        if owns_client:
            client.close()
    return results


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download DMA historical AIS data as Parquet.")
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if the partition already exists"
    )
    parser.add_argument(
        "--out-dir", default=str(RAW_ROOT), help="Root of the Parquet partitions"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    download_range(start, end, out_root=Path(args.out_dir), force=args.force)


if __name__ == "__main__":
    main()
