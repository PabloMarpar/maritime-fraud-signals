"""Unit tests for ingest.landmask. All HTTP is mocked; nothing here touches the network."""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path

import duckdb
import httpx
import pytest

from ingest import landmask


def _make_land_zip() -> bytes:
    """Build a tiny, valid shapefile zip: one square polygon, same shape as the real download.

    Verified recipe: DuckDB spatial's GDAL COPY writes .shp/.shx/.dbf siblings, and zipping all
    three (matching how the real ne_10m_land.zip bundles its shapefile members) round-trips
    through ST_Read exactly like the real download would.
    """
    tmp = tempfile.mkdtemp()
    shp_path = os.path.join(tmp, "ne_10m_land.shp")
    con = duckdb.connect()
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    con.execute(
        "CREATE TABLE t AS SELECT 1 AS id, "
        "ST_GeomFromText('POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))') AS geom"
    )
    con.execute(f"COPY t TO '{shp_path}' WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')")
    con.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in os.listdir(tmp):
            zf.write(os.path.join(tmp, name), arcname=name)
    return buf.getvalue()


LAND_ZIP_BYTES = _make_land_zip()


def _client_primary_ok(calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(200, content=LAND_ZIP_BYTES, headers={"content-type": "application/zip"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_primary_404_mirror_ok(calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if str(request.url) == landmask.BASE_URLS[0]:
            return httpx.Response(404)
        return httpx.Response(200, content=LAND_ZIP_BYTES)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_primary_connection_error_mirror_ok(calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if str(request.url) == landmask.BASE_URLS[0]:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, content=LAND_ZIP_BYTES)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_both_fail() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _assert_valid_land_parquet(out_path: Path) -> None:
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        (inside,) = con.execute(
            f"SELECT ST_Contains(geom, ST_Point(0.5, 0.5)) FROM read_parquet('{out_path.as_posix()}')"
        ).fetchone()
        (outside,) = con.execute(
            f"SELECT ST_Contains(geom, ST_Point(5, 5)) FROM read_parquet('{out_path.as_posix()}')"
        ).fetchone()
        assert inside is True
        assert outside is False
    finally:
        con.close()


def test_build_land_mask_from_primary(tmp_path):
    out_path = tmp_path / "reference" / "land.parquet"
    client = _client_primary_ok()

    result = landmask.build_land_mask(out_path=out_path, client=client)

    assert result == out_path
    assert out_path.exists()
    _assert_valid_land_parquet(out_path)


def test_build_land_mask_falls_back_on_404(tmp_path):
    out_path = tmp_path / "reference" / "land.parquet"
    calls: list[str] = []
    client = _client_primary_404_mirror_ok(calls=calls)

    result = landmask.build_land_mask(out_path=out_path, client=client)

    assert result == out_path
    assert out_path.exists()
    assert landmask.BASE_URLS[0] in calls
    assert landmask.BASE_URLS[1] in calls
    _assert_valid_land_parquet(out_path)


def test_build_land_mask_falls_back_on_connection_error(tmp_path):
    out_path = tmp_path / "reference" / "land.parquet"
    calls: list[str] = []
    client = _client_primary_connection_error_mirror_ok(calls=calls)

    result = landmask.build_land_mask(out_path=out_path, client=client)

    assert result == out_path
    assert out_path.exists()
    assert landmask.BASE_URLS[0] in calls
    assert landmask.BASE_URLS[1] in calls
    _assert_valid_land_parquet(out_path)


def test_build_land_mask_raises_when_both_mirrors_fail(tmp_path):
    out_path = tmp_path / "reference" / "land.parquet"
    client = _client_both_fail()

    with pytest.raises(RuntimeError, match="naciscdn|amazonaws"):
        landmask.build_land_mask(out_path=out_path, client=client)

    assert not out_path.exists()


def test_build_land_mask_is_idempotent_by_default(tmp_path):
    out_path = tmp_path / "reference" / "land.parquet"
    calls: list[str] = []
    client = _client_primary_ok(calls=calls)

    first = landmask.build_land_mask(out_path=out_path, client=client)
    calls_after_first = len(calls)
    assert calls_after_first > 0

    second = landmask.build_land_mask(out_path=out_path, client=client)

    assert second == first
    assert len(calls) == calls_after_first, "re-running without --force must not re-download"


def test_build_land_mask_force_rebuilds(tmp_path):
    out_path = tmp_path / "reference" / "land.parquet"
    calls: list[str] = []
    client = _client_primary_ok(calls=calls)

    landmask.build_land_mask(out_path=out_path, client=client)
    calls_after_first = len(calls)

    landmask.build_land_mask(out_path=out_path, client=client, force=True)

    assert len(calls) > calls_after_first, "force=True must re-download even if the output exists"
