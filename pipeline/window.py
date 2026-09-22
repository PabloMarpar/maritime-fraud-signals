"""Orchestrate one whole window's build: download -> liveness -> everything else -> verify.

**Why this exists (P3-4/A4).** Every producer this project has (``process.thin``,
``process.ship_type``, ``process.tracks``, ``process.identity``, and the five detectors) already
knows how to build its own window-scoped or day-partitioned artifact. What is missing is the glue
that runs them **in the right order**, over the right ranges, for one sampled window -- and,
critically, that only ever CREATES. Nothing in this module deletes anything; the separate,
opt-in ``pipeline.prune`` (P3-4/A5, not yet built) is the only place a delete can happen, per
``docs/PLAN_P4-0_P3-4.md``'s A0 safety principles.

**Order, and why it is fixed.**

1. ``pipeline.backfill.backfill_range`` over ``[lead_in_start, end]``, not just ``[start, end]`` --
   ``detect.liveness``'s historical baseline (:data:`detect.liveness.BASELINE_DAYS`, 30 days) needs
   clean data *before* the window starts, or every early gap in the window gets scored against a
   thin or missing baseline. Reused as-is: it already skips days already ``clean``/``reduced``.
2. ``detect.liveness.build_liveness`` over the same ``[lead_in_start, end]`` -- day-partitioned,
   idempotent per day (P3-4/A1), so re-running this over a range that overlaps a previous window's
   lead-in only fills in the new days.
3. Over ``[start, end]`` only (not the lead-in): downsampled tracks (``process.thin``), the
   ship_type reference (``process.ship_type``), voyages (``process.tracks``), identity
   (``process.identity``), then the five detectors in their own dependency order --
   ``detect.anchorages`` first (nothing else needs anything but clean data), then
   ``detect.spoofing`` (needs voyages), ``detect.sts`` (needs anchorages), ``detect.behaviour``
   (needs voyages + anchorages + sts), ``detect.identity_anomalies`` (needs the ship_type
   reference, for THIS exact window -- P3-4/A2's hard-blocker fix, see ``process.ship_type``).
4. :func:`_verify_window` opens every artifact just built with DuckDB (never just checks that a
   file exists and has nonzero bytes -- A0.9) and returns a list of failure strings, empty if
   everything checks out.
5. Only if that list is empty: for every day in ``[start, end]`` that has a clean partition, record
   a fingerprint (A0.3: message count, distinct mmsi, timestamp range, a coarse bbox) and
   ``verified_at`` in :mod:`pipeline.manifest`. ``pipeline.prune`` (A5, not yet built) is the only
   consumer of ``verified_at`` -- it will select days to delete by its presence, so a day that
   fails verification here is never eligible for deletion, by construction.

**Every output path is derived from ``data_root``, not from each module's own hardcoded default.**
Every producer module in this project owns its own ``<KIND>_ROOT = Path("data/...")`` constant and
accepts it as an overridable parameter -- this module passes an explicit override for every one of
them, built from ``data_root``, mirroring exactly what each module's own default would be under
``data_root=Path("data")``. This is what makes the whole cycle testable against ``tmp_path``
without monkeypatching a dozen module-level constants, and it is also what a future non-default
``data_root`` (e.g. a second disk) would need to work at all.

**A failed verification raises and writes nothing to the manifest.** There is no partial-credit
path: either every artifact for the window passed every gate, or none of the window's days become
eligible for :mod:`pipeline.prune` to consider.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from detect import anchorages, behaviour, identity_anomalies, liveness, spoofing, sts
from pipeline import backfill, manifest
from pipeline.backfill import DEFAULT_MIN_FREE_GB
from process import identity, ship_type, thin, tracks
from process.partitions import existing_partitions, partition_path

logger = logging.getLogger(__name__)

DATA_ROOT = Path("data")
DEFAULT_LEAD_IN_DAYS = liveness.BASELINE_DAYS  # 30 -- matches detect.liveness's own baseline window
DEFAULT_THIN_MINUTES = thin.DEFAULT_INTERVAL_MINUTES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_roots(data_root: Path) -> dict[str, Path]:
    """Every producer's root/reference path, derived from ``data_root`` -- mirrors each module's
    own default layout under ``data_root=Path("data")`` exactly, see module docstring."""
    return {
        "clean": data_root / "clean" / "ais_dk",
        "liveness": data_root / "coverage" / "liveness",
        "thin": data_root / "tracks" / "thin",
        "ship_type": data_root / "reference" / "ship_type",
        "voyages": data_root / "tracks" / "voyages",
        "identity": data_root / "identity" / "mmsi_imo",
        "anchorages": data_root / "coverage" / "anchorages",
        "land": data_root / "reference" / "land.parquet",
        "ports": data_root / "reference" / "ports.parquet",
        "spoofing": data_root / "detect" / "spoofing",
        "sts": data_root / "detect" / "sts",
        "behaviour": data_root / "detect" / "behaviour",
        "identity_anomalies": data_root / "detect" / "identity_anomalies",
    }


def _glob(root: Path) -> Path:
    """A window=*/part-0.parquet glob under root, matching every detector's own <KIND>_GLOB."""
    return root / "window=*" / "part-0.parquet"


def _check_parquet(label: str, path: Path, allow_zero: bool) -> list[str]:
    """Open ``path`` with DuckDB and confirm it is readable and, unless ``allow_zero``, nonempty --
    A0.9's "verify by reading, not looking". Returns a list of failure strings (empty if fine); a
    zero-row artifact where that's allowed is logged loudly, per A5's own posture on event tables.
    """
    if not path.exists():
        return [f"{label}: missing at {path}"]
    con = duckdb.connect()
    try:
        try:
            (n_rows,) = con.execute(
                f"SELECT count(*) FROM read_parquet('{path.as_posix()}')"
            ).fetchone()
        except duckdb.Error as exc:
            return [f"{label}: unreadable at {path} ({exc})"]
    finally:
        con.close()
    if n_rows == 0:
        if allow_zero:
            logger.warning("%s: 0 rows at %s (allowed -- nothing was detected)", label, path)
            return []
        return [f"{label}: 0 rows at {path}"]
    return []


def _check_day_partitions(label: str, paths: list[Path], expected_n_days: int) -> list[str]:
    """Verify every day-partition path is readable and nonempty, and that the count matches the
    number of clean days it should cover (see _verify_window)."""
    failures: list[str] = []
    for path in paths:
        failures.extend(_check_parquet(f"{label} {path.parent.name}", path, allow_zero=False))
    if len(paths) != expected_n_days:
        failures.append(
            f"{label}: {len(paths)} day partition(s) present, expected {expected_n_days} "
            "(one per day with a clean partition in range)"
        )
    return failures


def _verify_window(
    *,
    thin_paths: list[Path],
    liveness_paths: list[Path],
    n_clean_days_window: int,
    n_clean_days_lead_in: int,
    voyages_path: Path,
    identity_path: Path,
    ship_type_path: Path,
    anchorages_path: Path,
    spoofing_path: Path,
    sts_path: Path,
    behaviour_path: Path,
    identity_anomalies_path: Path,
) -> list[str]:
    """Read back every artifact this window just built and return a list of failure strings, empty
    if the window is fully verified. Never trusts that a build call returning without raising means
    the output is actually good -- see module docstring and A0.9."""
    failures: list[str] = []
    failures += _check_day_partitions("thin", thin_paths, n_clean_days_window)
    failures += _check_day_partitions("liveness", liveness_paths, n_clean_days_lead_in)
    failures += _check_parquet("voyages", voyages_path, allow_zero=False)
    failures += _check_parquet("identity", identity_path, allow_zero=False)
    failures += _check_parquet("ship_type reference", ship_type_path, allow_zero=False)
    failures += _check_parquet("anchorages", anchorages_path, allow_zero=False)
    failures += _check_parquet("spoofing", spoofing_path, allow_zero=True)
    failures += _check_parquet("sts", sts_path, allow_zero=True)
    failures += _check_parquet("behaviour", behaviour_path, allow_zero=True)
    failures += _check_parquet("identity_anomalies", identity_anomalies_path, allow_zero=True)
    return failures


FINGERPRINT_BBOX_OUTLIER_QUANTILE = 0.001


def _day_fingerprint(day: date, clean_root: Path) -> dict | None:
    """A0.3's fingerprint fields for one day's clean partition, or None if it has none.

    Deliberately re-derived from the clean partition directly (not reused from
    pipeline.backfill's own clean_rows/clean_bytes) so it stands alone as evidence a future
    re-download can be checked against, independent of whatever backfill happened to record.

    bbox uses tail quantiles (0.1% each side), not raw min/max, rounded to 2 decimal degrees
    (~1km at these latitudes) -- a real run over 2024-06-10..11 found raw min/max swamped by
    `process.clean`'s already-documented handful of corrupted-coordinate outliers (up to 89 deg
    latitude, see docs/DECISIONS.md's P2-3 entry), which would make every day's bbox look like
    nearly the whole globe and defeat the point of a fingerprint -- the same problem
    detect.spoofing's own bbox crop already solved for the same reason, reused here.
    """
    clean_path = partition_path(day, clean_root)
    if not clean_path.exists():
        return None
    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT count(*) AS message_count, count(DISTINCT mmsi) AS n_distinct_mmsi, "
            "min(timestamp) AS min_timestamp, max(timestamp) AS max_timestamp, "
            "round(quantile_cont(latitude, ?), 2) AS min_lat, "
            "round(quantile_cont(latitude, ?), 2) AS max_lat, "
            "round(quantile_cont(longitude, ?), 2) AS min_lon, "
            "round(quantile_cont(longitude, ?), 2) AS max_lon "
            f"FROM read_parquet('{clean_path.as_posix()}')",
            [
                FINGERPRINT_BBOX_OUTLIER_QUANTILE,
                1 - FINGERPRINT_BBOX_OUTLIER_QUANTILE,
                FINGERPRINT_BBOX_OUTLIER_QUANTILE,
                1 - FINGERPRINT_BBOX_OUTLIER_QUANTILE,
            ],
        ).fetchone()
    finally:
        con.close()
    message_count, n_distinct_mmsi, min_ts, max_ts, min_lat, max_lat, min_lon, max_lon = row
    return {
        "fingerprint_message_count": message_count,
        "fingerprint_n_distinct_mmsi": n_distinct_mmsi,
        "fingerprint_min_timestamp": min_ts.isoformat() if min_ts is not None else None,
        "fingerprint_max_timestamp": max_ts.isoformat() if max_ts is not None else None,
        "fingerprint_bbox": [min_lat, max_lat, min_lon, max_lon],
    }


def process_window(
    start: date,
    end: date,
    data_root: Path = DATA_ROOT,
    lead_in_days: int = DEFAULT_LEAD_IN_DAYS,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    thin_minutes: int = DEFAULT_THIN_MINUTES,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Build every artifact for one sampled window, verify all of them, then record per-day
    fingerprints and ``verified_at`` -- and nothing else. See module docstring for the full order
    and the safety posture (creates only, never deletes).

    ``dry_run=True`` logs the plan and returns without touching disk at all -- unlike
    ``pipeline.backfill``'s own dry-run, which still runs the disk guard, this stops before any
    call, since most of the producers this orchestrates have no dry-run mode of their own to pass
    through to.
    """
    lead_in_start = start - timedelta(days=lead_in_days)
    roots = _window_roots(data_root)
    manifest_path = data_root / "manifest.json"

    if dry_run:
        logger.info(
            "[dry-run] window %s..%s (lead-in from %s): would backfill+liveness over the "
            "lead-in range, then build thin/ship_type/tracks/identity/anchorages/spoofing/sts/"
            "behaviour/identity_anomalies over %s..%s, verify, and record fingerprints",
            start.isoformat(),
            end.isoformat(),
            lead_in_start.isoformat(),
            start.isoformat(),
            end.isoformat(),
        )
        return

    backfill.backfill_range(
        lead_in_start, end, data_root=data_root, min_free_gb=min_free_gb, force=force
    )

    liveness_paths = liveness.build_liveness(
        lead_in_start, end, in_root=roots["clean"], out_root=roots["liveness"], force=force
    )

    thin_paths = thin.build_thin_tracks(
        start,
        end,
        in_root=roots["clean"],
        out_root=roots["thin"],
        interval_minutes=thin_minutes,
        force=force,
    )
    ship_type_path = ship_type.build_ship_type_reference(
        start, end, in_root=roots["clean"], out_root=roots["ship_type"], force=force
    )
    voyages_path = tracks.reconstruct_range(
        start, end, in_root=roots["clean"], out_root=roots["voyages"], force=force
    )
    identity_path = identity.resolve_range(
        start, end, in_root=roots["clean"], out_root=roots["identity"], force=force
    )

    anchorages_path = anchorages.build_anchorages(
        start,
        end,
        in_root=roots["clean"],
        land_path=roots["land"],
        out_root=roots["anchorages"],
        force=force,
    )
    spoofing_path = spoofing.build_spoofing_events(
        start,
        end,
        in_root=roots["clean"],
        voyages_path=_glob(roots["voyages"]),
        land_path=roots["land"],
        out_root=roots["spoofing"],
        force=force,
    )
    sts_path = sts.build_sts_events(
        start,
        end,
        in_root=roots["clean"],
        anchorages_path=_glob(roots["anchorages"]),
        out_root=roots["sts"],
        force=force,
    )
    behaviour_path = behaviour.build_behaviour_events(
        start,
        end,
        in_root=roots["clean"],
        voyages_path=_glob(roots["voyages"]),
        ports_path=roots["ports"],
        anchorages_path=_glob(roots["anchorages"]),
        sts_path=_glob(roots["sts"]),
        out_root=roots["behaviour"],
        force=force,
    )
    identity_anomalies_path = identity_anomalies.build_identity_events(
        start,
        end,
        in_root=roots["clean"],
        ship_type_reference_root=roots["ship_type"],
        out_root=roots["identity_anomalies"],
        force=force,
    )

    n_clean_days_window = len(existing_partitions(start, end, roots["clean"]))
    n_clean_days_lead_in = len(existing_partitions(lead_in_start, end, roots["clean"]))

    failures = _verify_window(
        thin_paths=thin_paths,
        liveness_paths=liveness_paths,
        n_clean_days_window=n_clean_days_window,
        n_clean_days_lead_in=n_clean_days_lead_in,
        voyages_path=voyages_path,
        identity_path=identity_path,
        ship_type_path=ship_type_path,
        anchorages_path=anchorages_path,
        spoofing_path=spoofing_path,
        sts_path=sts_path,
        behaviour_path=behaviour_path,
        identity_anomalies_path=identity_anomalies_path,
    )
    if failures:
        for failure in failures:
            logger.error("Verification failed: %s", failure)
        raise RuntimeError(
            f"Window {start.isoformat()}..{end.isoformat()} failed verification "
            f"({len(failures)} issue(s)); nothing recorded to the manifest, see log above"
        )

    for day, _clean_path in existing_partitions(start, end, roots["clean"]):
        fingerprint = _day_fingerprint(day, roots["clean"])
        if fingerprint is None:
            continue
        manifest.record(manifest_path, day, verified_at=_now_iso(), **fingerprint)

    logger.info(
        "Window %s..%s verified and recorded (%d day(s))",
        start.isoformat(),
        end.isoformat(),
        n_clean_days_window,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build every artifact for one sampled AIS window (backfill, liveness, thin "
        "tracks, ship_type, tracks, identity, all five detectors), verify all of them, and "
        "record per-day fingerprints. Creates only -- never deletes; see pipeline.prune for that."
    )
    parser.add_argument("--start", required=True, help="First day of the window, YYYY-MM-DD")
    parser.add_argument(
        "--end", help="Last day of the window, YYYY-MM-DD (default: same as --start)"
    )
    parser.add_argument(
        "--lead-in-days",
        type=int,
        default=DEFAULT_LEAD_IN_DAYS,
        help=f"Days of history to backfill/liveness-build before --start, for detect.liveness's "
        f"own baseline (default: {DEFAULT_LEAD_IN_DAYS}, matching detect.liveness.BASELINE_DAYS)",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=f"Abort backfill if free disk space falls below this (default: {DEFAULT_MIN_FREE_GB})",
    )
    parser.add_argument(
        "--thin-minutes",
        type=int,
        default=DEFAULT_THIN_MINUTES,
        help=f"Bucket width for downsampled tracks (default: {DEFAULT_THIN_MINUTES})",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild every artifact even if already present"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Log the plan without touching disk"
    )
    parser.add_argument(
        "--data-root", default=str(DATA_ROOT), help="Root directory for all data (default: data)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    process_window(
        start,
        end,
        data_root=Path(args.data_root),
        lead_in_days=args.lead_in_days,
        min_free_gb=args.min_free_gb,
        thin_minutes=args.thin_minutes,
        force=args.force,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
