"""Static-map sanity check for one day of landed AIS positions.

This is deliberately not the polished map: that is ``viz/`` (deck.gl + MapLibre,
Phase 5). The point here is "ships on screen" as fast as possible, with no
network access and no basemap tiles — a plain lon/lat scatter of whatever
``ingest/dma.py`` landed, so a bad ingest run is obvious at a glance.

Reads a single day's Hive-partitioned Parquet (as produced by
``ingest.dma.download_day``, schema: ``timestamp, type_of_mobile, mmsi,
latitude, longitude, navigational_status, sog, cog, ship_type, date``) via
DuckDB, drops rows with impossible coordinates, and renders a PNG scatter
plot.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import duckdb
import matplotlib
import pandas

matplotlib.use("Agg")  # no display available; write straight to file
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

MAX_COLOURED_CATEGORIES = 12


def _glob_for(input_path: str) -> str:
    """Turn a partition directory into a Parquet glob; pass a glob through untouched."""
    path = Path(input_path)
    if path.is_dir():
        return (path / "*.parquet").as_posix()
    return path.as_posix()


def load_positions(input_path: str) -> tuple[pandas.DataFrame, int]:
    """Read the partition with DuckDB, keeping only rows with valid coordinates.

    Returns the filtered dataframe plus the number of rows dropped. DuckDB
    does the filtering itself; only the (small, one-day) filtered result is
    ever pulled into a pandas dataframe, which is expected here — see the
    module docstring for why this differs from ``ingest/dma.py``'s
    streaming discipline.
    """
    glob = _glob_for(input_path)
    source = glob.replace("'", "''")
    con = duckdb.connect()
    try:
        total = con.execute(f"SELECT count(*) FROM read_parquet('{source}')").fetchone()[0]
        df = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{source}')
            WHERE latitude BETWEEN -90 AND 90
              AND longitude BETWEEN -180 AND 180
            """
        ).df()
    finally:
        con.close()
    dropped = total - len(df)
    return df, dropped


def render(input_path: str, output_path: str) -> Path:
    """Render one day's positions to a static PNG scatter plot.

    Colours points by ``ship_type`` when there aren't too many distinct
    values to keep a legend readable (an uncapped category count would make
    the plot noise rather than signal); falls back to a plain single-colour
    scatter otherwise. Raises ``ValueError`` if no valid rows remain after
    dropping impossible coordinates — a blank PNG would look like a bug that
    quietly failed, not a rendering script telling you the truth.
    """
    df, dropped = load_positions(input_path)
    if dropped:
        logger.info("Dropped %d row(s) with impossible coordinates", dropped)

    if df.empty:
        raise ValueError(
            f"No valid positions to plot for {input_path!r} "
            f"(dropped {dropped} row(s) with impossible coordinates, 0 remain)"
        )

    fig, ax = plt.subplots(figsize=(10, 8))
    categories = df["ship_type"].nunique(dropna=True) if "ship_type" in df.columns else 0
    if "ship_type" in df.columns and 0 < categories <= MAX_COLOURED_CATEGORIES:
        for ship_type, group in df.groupby("ship_type", dropna=False):
            ax.scatter(
                group["longitude"], group["latitude"], s=6, alpha=0.6, label=str(ship_type)
            )
        ax.legend(title="ship_type", loc="best", fontsize="small", markerscale=2)
    else:
        ax.scatter(df["longitude"], df["latitude"], s=6, alpha=0.6, color="tab:blue")

    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"AIS positions — {input_path} ({len(df)} points)")
    ax.set_aspect("equal", adjustable="datalim")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("Wrote %d point(s) to %s", len(df), out_path)
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render one day of vessel positions as a static PNG scatter plot."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Partition directory (date=YYYY-MM-DD) or a Parquet glob",
    )
    parser.add_argument("--output", required=True, help="Path to write the PNG to")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    render(args.input, args.output)


if __name__ == "__main__":
    main()
