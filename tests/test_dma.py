"""Unit tests for ingest.dma. All HTTP is mocked; nothing here touches the network."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import httpx
import pytest

from ingest import dma

DAY = date(2024, 6, 5)

# A tiny, synthetic stand-in for a DMA daily file: real headers ("Title Case
# With Spaces"), a handful of well-formed rows, one row with an extra field
# (structurally malformed: the header declares 9 columns, this row has 10).
SAMPLE_CSV = (
    "Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,SOG,COG,Ship type\n"
    "05/06/2024 00:00:01,Class A,219000001,55.6761,12.5683,Under way using engine,10.2,180.5,Cargo\n"
    "05/06/2024 00:00:02,Class A,219000002,55.7000,12.6000,Under way using engine,7.1,181.0,Tanker\n"
    "05/06/2024 00:00:03,Class A,219000003,55.8000,12.7000,Moored,0.0,0.0,Tanker,extra_field\n"
)

WELL_FORMED_ROW_COUNT = 2  # the third row above is dropped as malformed


def _zip_bytes(csv_text: str, member_name: str = "aisdk-2024-06-05.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_name, csv_text)
    return buf.getvalue()


def _client_always_csv(csv_text: str, calls: list[str] | None = None) -> httpx.Client:
    """A client whose transport 404s the .zip candidate and serves plain CSV for .csv."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.endswith(".zip"):
            return httpx.Response(404)
        return httpx.Response(200, content=csv_text.encode("utf-8"))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_always_zip(csv_text: str, calls: list[str] | None = None) -> httpx.Client:
    """A client that serves a zip archive for the first (.zip) candidate."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.endswith(".zip"):
            return httpx.Response(200, content=_zip_bytes(csv_text))
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _read_parquet(path: Path) -> list[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM '{path.as_posix()}' ORDER BY mmsi").fetchall()
    finally:
        con.close()


def test_download_day_parses_plain_csv(tmp_path):
    client = _client_always_csv(SAMPLE_CSV)
    out_root = tmp_path / "ais_dk"

    parquet_path = dma.download_day(DAY, out_root=out_root, client=client)

    assert parquet_path == out_root / "date=2024-06-05" / "part-0.parquet"
    assert parquet_path.exists()
    rows = _read_parquet(parquet_path)
    assert len(rows) == WELL_FORMED_ROW_COUNT


def test_download_day_parses_zipped_csv(tmp_path):
    """The archive is believed to ship zip today; confirm that branch works too."""
    client = _client_always_zip(SAMPLE_CSV)
    out_root = tmp_path / "ais_dk"

    parquet_path = dma.download_day(DAY, out_root=out_root, client=client)

    assert parquet_path.exists()
    rows = _read_parquet(parquet_path)
    assert len(rows) == WELL_FORMED_ROW_COUNT


def test_columns_are_normalised(tmp_path):
    client = _client_always_csv(SAMPLE_CSV)
    out_root = tmp_path / "ais_dk"

    parquet_path = dma.download_day(DAY, out_root=out_root, client=client)

    con = duckdb.connect()
    try:
        columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM '{parquet_path.as_posix()}'").fetchall()]
    finally:
        con.close()
    # "Type of mobile" -> "type_of_mobile", "MMSI" -> "mmsi", etc. No spaces, no
    # uppercase, so the columns are safe to reference unquoted in later SQL.
    # DuckDB also infers a "date" column from the Hive-style partition path
    # (date=2024-06-05/...) for free, which is why it appears here too.
    assert columns == [
        "timestamp",
        "type_of_mobile",
        "mmsi",
        "latitude",
        "longitude",
        "navigational_status",
        "sog",
        "cog",
        "ship_type",
        "date",
    ]


def test_malformed_row_is_dropped_not_crashed(tmp_path):
    """The negative case matters too: well-formed rows must survive untouched."""
    client = _client_always_csv(SAMPLE_CSV)
    out_root = tmp_path / "ais_dk"

    parquet_path = dma.download_day(DAY, out_root=out_root, client=client)

    rows = _read_parquet(parquet_path)
    mmsis = {row[2] for row in rows}
    # The two well-formed rows made it through...
    assert mmsis == {219000001, 219000002}
    # ...and the malformed one (MMSI 219000003) did not silently corrupt the load.
    assert 219000003 not in mmsis


def test_well_formed_file_loses_no_rows(tmp_path):
    """Negative case for the malformed-row handling: nothing is dropped when nothing is wrong."""
    clean_csv = (
        "Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,SOG,COG,Ship type\n"
        "05/06/2024 00:00:01,Class A,219000001,55.6761,12.5683,Under way using engine,10.2,180.5,Cargo\n"
        "05/06/2024 00:00:02,Class A,219000002,55.7000,12.6000,Under way using engine,7.1,181.0,Tanker\n"
        "05/06/2024 00:00:03,Class A,219000003,55.8000,12.7000,Moored,0.0,0.0,Tanker\n"
    )
    client = _client_always_csv(clean_csv)
    out_root = tmp_path / "ais_dk"

    parquet_path = dma.download_day(DAY, out_root=out_root, client=client)

    rows = _read_parquet(parquet_path)
    assert len(rows) == 3


def test_download_day_is_idempotent_by_default(tmp_path):
    calls: list[str] = []
    client = _client_always_csv(SAMPLE_CSV, calls=calls)
    out_root = tmp_path / "ais_dk"

    first = dma.download_day(DAY, out_root=out_root, client=client)
    calls_after_first = len(calls)
    assert calls_after_first > 0

    second = dma.download_day(DAY, out_root=out_root, client=client)

    assert second == first
    assert len(calls) == calls_after_first, "re-running without --force must not re-fetch"


def test_download_day_force_refetches(tmp_path):
    calls: list[str] = []
    client = _client_always_csv(SAMPLE_CSV, calls=calls)
    out_root = tmp_path / "ais_dk"

    dma.download_day(DAY, out_root=out_root, client=client)
    calls_after_first = len(calls)

    dma.download_day(DAY, out_root=out_root, client=client, force=True)

    assert len(calls) > calls_after_first, "force=True must re-fetch even if the partition exists"


def test_download_day_raises_when_nothing_found(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out_root = tmp_path / "ais_dk"

    with pytest.raises(FileNotFoundError):
        dma.download_day(DAY, out_root=out_root, client=client)


def test_download_range_covers_every_day(tmp_path):
    calls: list[str] = []
    out_root = tmp_path / "ais_dk"
    client = _client_always_csv(SAMPLE_CSV, calls=calls)

    results = dma.download_range(date(2024, 6, 5), date(2024, 6, 7), out_root=out_root, client=client)

    assert len(results) == 3
    for path in results:
        assert path.exists()
    assert {p.parent.name for p in results} == {
        "date=2024-06-05",
        "date=2024-06-06",
        "date=2024-06-07",
    }
