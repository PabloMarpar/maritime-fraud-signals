"""Downloader for the Natural Earth 1:10m land polygon dataset, used as an offline land mask.

**Why this exists.** ``detect.spoofing``'s "position on land" check needs a fast, offline
point-in-land test -- it cannot call out to a geocoding service for every AIS message. Natural
Earth's 1:10,000,000-scale physical land layer is small (a handful of megabytes, 11 polygons for
the whole planet) and stable enough to fetch once and keep as a local Parquet file rather than
re-downloading it per run.

**Two live mirrors.** Confirmed reachable on 2026-09-17, both HTTP 200, both
``content-type: application/zip``, both exactly 3,269,070 bytes (i.e. the same file):
``https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip`` (primary, the project's
canonical distribution point) and ``https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip``
(fallback). :func:`build_land_mask` tries the primary first and falls back to the mirror on any
failure (404, connection error, or other HTTP error), mirroring ``ingest.dma``'s ``_fetch_day``
retry idiom -- try each candidate in turn, ``continue`` past a failure, raise a single clear error
naming every URL tried only once all of them are exhausted.

**A shapefile is a multi-file format.** The zip contains ``ne_10m_land.shp``/``.shx``/``.dbf``/
``.prj``/``.cpg`` plus a README/VERSION file, not a single self-contained geometry file. DuckDB
spatial's ``ST_Read`` needs the ``.shp``'s sibling files present alongside it, so the whole zip is
extracted to a temporary directory (never just the ``.shp`` member) before ``ST_Read`` is pointed
at the extracted path.

**Known, empirically observed limitation.** This is a 1:10,000,000-scale generalized coastline,
not a precise boundary. Checked on 2026-09-17: a real-world "Copenhagen city centre" coordinate
(55.6761, 12.5683), genuinely on land, sits roughly 205m *outside* this polygon (``ST_Distance``
to the nearest land ring is about 0.00185 degrees there). Interior points (e.g. 55.64, 12.08 in
Zealand) and open-sea points (e.g. 57.3, 11.3 in the Kattegat) classify correctly -- only positions
right at the coastline are unreliable. Fixing that at query time (an inward erosion buffer so only
positions solidly inland get flagged "on land") is ``detect.spoofing``'s job, not this module's --
this module only lands the raw polygons.

DuckDB's native ``GEOMETRY`` type round-trips through Parquet cleanly (``COPY ... TO ...
(FORMAT PARQUET)`` / ``read_parquet(...)``, no WKB conversion needed), confirmed on 2026-09-17.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import zipfile
from pathlib import Path

import duckdb
import httpx

logger = logging.getLogger(__name__)

BASE_URLS = (
    "https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip",
    "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_land.zip",
)

LAND_PATH = Path("data/reference/land.parquet")


def _download_zip(client: httpx.Client, dest_path: Path) -> None:
    """Download the land-polygon zip to dest_path, trying each of BASE_URLS in turn.

    Streams the response body to disk rather than buffering it in memory. A 404 or any other
    HTTP/connection error moves on to the next URL; only once every URL has failed is a single
    clear error raised, naming all of them.
    """
    last_error: Exception | None = None
    for url in BASE_URLS:
        try:
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    last_error = httpx.HTTPStatusError(
                        f"404 for {url}", request=response.request, response=response
                    )
                    continue
                response.raise_for_status()
                with open(dest_path, "wb") as fh:
                    fh.writelines(response.iter_bytes())
            logger.info("Downloaded %s", url)
            return
        except httpx.HTTPError as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"Could not download Natural Earth land polygons from any of {BASE_URLS}"
    ) from last_error


def _build_parquet(shp_path: Path, out_path: Path) -> int:
    """Load shp_path's polygons with DuckDB spatial and write out_path. Returns the row count."""
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        con.execute(
            f"CREATE OR REPLACE TABLE land AS SELECT * FROM ST_Read('{shp_path.as_posix()}')"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY land TO '{out_path.as_posix()}' (FORMAT PARQUET)")
        (row_count,) = con.execute("SELECT count(*) FROM land").fetchone()
        return row_count
    finally:
        con.close()


def build_land_mask(
    out_path: Path = LAND_PATH,
    client: httpx.Client | None = None,
    tmp_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Download and land the Natural Earth land polygons as Parquet.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. tmp_dir, if given, is the parent directory for the working TemporaryDirectory
    (zip + extracted shapefile); by default (None) that lands under the platform temp dir, same
    convention as ingest.dma.download_day.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        with tempfile.TemporaryDirectory(prefix="landmask-", dir=tmp_dir) as tmp:
            work_dir = Path(tmp)
            zip_path = work_dir / "ne_10m_land.zip"
            _download_zip(client, zip_path)

            extract_dir = work_dir / "extracted"
            extract_dir.mkdir()
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            shp_candidates = sorted(extract_dir.rglob("*.shp"))
            if not shp_candidates:
                raise ValueError(f"Downloaded zip has no .shp member: {zip_path}")
            if len(shp_candidates) > 1:
                logger.warning(
                    "Zip contains %d .shp members, using the first: %s",
                    len(shp_candidates),
                    shp_candidates[0],
                )
            shp_path = shp_candidates[0]

            row_count = _build_parquet(shp_path, out_path)
            logger.info("Built land mask: %d polygon(s) at %s", row_count, out_path)
    finally:
        if owns_client:
            client.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the Natural Earth 1:10m land polygons and land them as Parquet."
    )
    parser.add_argument("--out-path", default=str(LAND_PATH), help="Output path for the land mask")
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_land_mask(out_path=Path(args.out_path), force=args.force)


if __name__ == "__main__":
    main()
