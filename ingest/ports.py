"""Downloader for the Natural Earth 1:10m ports point layer, used only as an external sanity
check on detect.anchorages' empirically derived mask -- never as an input to building it.

**Why this exists.** detect.anchorages derives its coastal-anchorage mask from our own AIS, by
design (see that module's docstring on why: it captures small Danish harbours no global port list
holds). But a mask derived entirely from the data it will filter has no outside check on it. This
module lands a small, independent public port list -- the same one detect.anchorages'
compare_to_ports function reads -- purely as a plausibility check at the validation checkpoint,
not as a dependency of the mask itself.

**Two live mirrors**, same dual-mirror/zip/shapefile shape as ingest.landmask, confirmed reachable
2026-09-18, both HTTP 200, both content-type application/zip, 51,824 bytes:
``https://naciscdn.org/naturalearth/10m/cultural/ne_10m_ports.zip`` (primary) and
``https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_ports.zip`` (fallback).
:func:`build_ports` tries the primary first and falls back to the mirror on any failure, mirroring
ingest.landmask.build_land_mask's own retry idiom exactly.

**Points, not polygons.** ``ST_Read`` returns one POINT geometry per port in the ``geom`` column,
same round-trip-through-Parquet convention as ingest.landmask (no WKB conversion needed).
Consumers read a port's coordinates via ``ST_X(geom)``/``ST_Y(geom)``.

**Known limitation.** This 1:10,000,000-scale layer holds only major world ports -- it is not
expected to contain small Danish harbours. detect.anchorages.compare_to_ports reads a low
"anchorages near a NE port" rate as this project's mask adding coverage, not as imprecision -- see
that function's docstring.
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
    "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_ports.zip",
    "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_ports.zip",
)

PORTS_PATH = Path("data/reference/ports.parquet")


def _download_zip(client: httpx.Client, dest_path: Path) -> None:
    """Download the ports zip to dest_path, trying each of BASE_URLS in turn.

    Streams the response body to disk rather than buffering it in memory. A 404 or any other
    HTTP/connection error moves on to the next URL; only once every URL has failed is a single
    clear error raised, naming all of them -- same idiom as ingest.landmask._download_zip.
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
        f"Could not download Natural Earth ports from any of {BASE_URLS}"
    ) from last_error


def _build_parquet(shp_path: Path, out_path: Path) -> int:
    """Load shp_path's points with DuckDB spatial and write out_path. Returns the row count."""
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        con.execute(
            f"CREATE OR REPLACE TABLE ports AS SELECT * FROM ST_Read('{shp_path.as_posix()}')"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ports TO '{out_path.as_posix()}' (FORMAT PARQUET)")
        (row_count,) = con.execute("SELECT count(*) FROM ports").fetchone()
        return row_count
    finally:
        con.close()


def build_ports(
    out_path: Path = PORTS_PATH,
    client: httpx.Client | None = None,
    tmp_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Download and land the Natural Earth ports layer as Parquet.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. tmp_dir, if given, is the parent directory for the working TemporaryDirectory
    (zip + extracted shapefile); by default (None) that lands under the platform temp dir, same
    convention as ingest.landmask.build_land_mask.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        with tempfile.TemporaryDirectory(prefix="ports-", dir=tmp_dir) as tmp:
            work_dir = Path(tmp)
            zip_path = work_dir / "ne_10m_ports.zip"
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
            logger.info("Built ports table: %d port(s) at %s", row_count, out_path)
    finally:
        if owns_client:
            client.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the Natural Earth 1:10m ports layer and land it as Parquet."
    )
    parser.add_argument("--out-path", default=str(PORTS_PATH), help="Output path for the ports table")
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_ports(out_path=Path(args.out_path), force=args.force)


if __name__ == "__main__":
    main()
