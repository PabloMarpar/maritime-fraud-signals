"""Downloader for Danish Maritime Authority (DMA) historical AIS data.

Source: ``http://web.ais.dk/aisdata/`` — one file per calendar day, covering
every AIS message received by the Danish coastal network that day. See
``docs/DATA_SOURCES.md`` for licence and reachability notes.

Two quirks of this source drive the design below:

1. **Plain HTTP, not HTTPS.** The archive's certificate (``*.govcloud.dk``)
   is expired. This is documented and deliberate, not a bug: we use the
   ``http://`` URL directly rather than silently passing ``verify=False``
   to a client that thinks it is doing HTTPS. Do not "fix" this by switching
   to ``https://`` with verification disabled.
2. **The on-disk format is not fully pinned down.** The archive is known to
   have shipped daily files as bare CSV in some periods and as a zip archive
   containing one CSV in others, and the true format for a given day was not
   confirmed against a live request while this module was written — outbound
   port 80 was unreachable from that environment (see the task notes in
   ``docs/STATE.md``). ``_fetch_day`` therefore tries both known filename
   patterns, and ``_extract_csv`` sniffs the downloaded bytes (zip magic
   number) rather than trusting the URL extension.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb
import httpx

logger = logging.getLogger(__name__)

# Plain HTTP, not HTTPS: see module docstring point 1.
BASE_URL = "http://web.ais.dk/aisdata"

# Candidate filenames for a given day, tried in order until one is found.
# Zip is tried first: it is what the archive is believed to serve today
# (see module docstring point 2); bare CSV is the documented fallback.
FILENAME_PATTERNS = ("aisdk-{day}.zip", "aisdk-{day}.csv")

RAW_ROOT = Path("data/raw/ais_dk")

ZIP_MAGIC = b"PK\x03\x04"


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
    hundreds of megabytes.
    """
    last_error: Exception | None = None
    for pattern in FILENAME_PATTERNS:
        filename = pattern.format(day=day.isoformat())
        url = f"{BASE_URL}/{filename}"
        local_path = dest_dir / filename
        try:
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                with open(local_path, "wb") as fh:
                    fh.writelines(response.iter_bytes())
            logger.info("Downloaded %s", url)
            return local_path
        except httpx.HTTPStatusError as exc:
            last_error = exc
            continue
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
    """DMA column headers are 'Title Case With Spaces'; make them SQL/parquet friendly."""
    return column.strip().lower().replace(" ", "_").replace("-", "_")


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
            f'"{col[0]}" AS {_normalise(col[0])}' for col in columns
        )
        parquet_target = parquet_path.as_posix()
        con.execute(
            f"COPY (SELECT {select_list} FROM raw) TO '{parquet_target}' (FORMAT PARQUET)"
        )
        (row_count,) = con.execute(
            f"SELECT count(*) FROM '{parquet_target}'"
        ).fetchone()
        return row_count
    finally:
        con.close()


def download_day(
    day: date,
    out_root: Path = RAW_ROOT,
    force: bool = False,
    client: httpx.Client | None = None,
) -> Path:
    """Download and land one day of DMA AIS data as Parquet.

    Idempotent: if the day's partition already exists, this is a no-op
    unless force=True. Returns the path to the partition's Parquet file
    either way.
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
        with tempfile.TemporaryDirectory(prefix=f"dma-{day.isoformat()}-") as tmp:
            tmp_dir = Path(tmp)
            raw_path = _fetch_day(day, client, tmp_dir)
            csv_path = _extract_csv(raw_path, tmp_dir)
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
) -> list[Path]:
    """Download DMA AIS data for every day in [start, end], inclusive.

    A single client is reused across the whole range (connection pooling);
    pass one in for testing, otherwise a default one is created and closed
    automatically.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        results = [
            download_day(day, out_root=out_root, force=force, client=client)
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
