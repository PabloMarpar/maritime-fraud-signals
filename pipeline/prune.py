"""Delete quarantined clean-data partitions for days already verified and fingerprinted.

**Why this is a separate command from pipeline.window (A0.1).** ``pipeline.window.process_window``
only ever creates artifacts and never deletes -- a bug there cannot destroy data because it has no
code path that does. This module, ``pipeline.prune``, is the ONE place in the project a deletion
happens, and every dangerous step in it is opt-in (``--yes-delete``, see A0.5) or defaults to a
dry run that touches nothing on disk.

**Quarantine, not deletion (A0.2).** A day selected for pruning is first RENAMED (same filesystem,
instant, reversible) from ``data/clean/ais_dk/date=.../`` to ``data/.trash/date=.../``, never
unlinked directly. The directory is only permanently removed on a LATER invocation of this module
(:func:`_empty_trash`), and only once ``pipeline.manifest`` confirms (via the ``clean_discarded_at``
/ "reduced" marker) that the quarantine step which put it there actually completed and was
recorded -- not merely attempted. ``_empty_trash`` always runs before any NEW day is quarantined in
the same call, so a day this very invocation moves to ``.trash`` is never also a candidate for
permanent emptying by it; that only happens on the invocation after.

**Every gate is re-evaluated at deletion time (A5), never trusting ``pipeline.window``'s earlier
``verified_at``.** A day only becomes a *candidate* because ``verified_at`` is present and
``clean_discarded_at`` is absent (see :func:`_select_candidates`) -- but candidacy alone proves
nothing about the state of the disk right now: the clean partition, the downstream artifacts, or
even the manifest itself could have changed since ``pipeline.window`` ran.

**Window grouping, and why it exists.** ``pipeline.window`` writes window-partitioned artifacts
once per ``[start, end]`` call (e.g.
``data/tracks/voyages/window=2024-06-10_2024-06-11/part-0.parquet``) but records ``verified_at``
and the A0.3 fingerprint per DAY in the manifest, with no window boundary carried alongside them.
To re-verify a window-partitioned artifact at deletion time this module infers window boundaries by
grouping the selected candidate days into maximal CONTIGUOUS runs and looking for an artifact at
exactly ``window=<run_start>_<run_end>`` -- matching the ``[start, end]`` every single
``process_window`` call actually used, since a single call's days are always contiguous. A gate
failing for one run's window-partitioned artifact skips every day in that run, not the whole
invocation (other runs, if any, are still evaluated independently) -- see :func:`_evaluate_group`.

**Day-partitioned artifacts (liveness, thin) are unaffected by pruning and checked directly, no
grouping needed.** Both already live at ``date=.../part-0.parquet`` independent of which
``process_window`` call produced them (liveness: P3-4/A1; thin: P3-4/A3), and neither is ever
discarded by this module -- only ``data/clean/`` is.

**Known gap, not solved here.** ``detect.gaps`` is not one of the artifacts ``pipeline.window``
orchestrates (it still writes a single legacy ``data/detect/gaps.parquet``, never window-partitioned
-- see ``docs/DECISIONS.md``), so the statistical sanity gate below cannot compare a per-window gap
rate against the real June 2024 figure the way it does for spoofing/sts/behaviour/identity_anomalies,
even though the plan's own text names ``gaps`` among the rates to check. Logged once per run rather
than silently skipped.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from pipeline import manifest
from pipeline.backfill import DEFAULT_MIN_FREE_GB

# Reused, not re-derived: this module's whole job is to re-verify exactly the artifacts
# pipeline.window produced, at exactly the paths it would have used -- importing its private root
# mapping (and its lead-in default) keeps the two in lockstep by construction instead of risking
# silent drift between two independently maintained copies of the same table.
from pipeline.window import DEFAULT_LEAD_IN_DAYS, _window_roots
from process.partitions import (
    existing_partitions,
    partition_path,
    window_partition_path,
)

logger = logging.getLogger(__name__)

DATA_ROOT = Path("data")
DEFAULT_MAX_DAYS = 15

# Below this fraction of clean data's distinct mmsi surviving into the thinned track table,
# something looks wrong with process.thin itself, not real data variation -- see A5.
MIN_THIN_MMSI_RATIO = 0.95

# A window-partitioned artifact's observed per-day event rate more than this many times above or
# below the real June 2024 rate (docs/STATE.md / docs/DECISIONS.md) fails the statistical sanity
# gate -- a detector silently breaking on a different year's data would still produce a small,
# non-empty, individually-readable file that every other gate here would pass.
ORDER_OF_MAGNITUDE = 10.0

# One line per real 30-day 2024-06-01..2024-06-30 count already recorded in docs/STATE.md /
# docs/DECISIONS.md, divided by 30 days. (name, kind-filter-or-None) -> reference rate.
_REAL_JUNE_2024_TOTALS = {
    "spoofing:impossible_speed": 5_499,
    "spoofing:on_land": 10_092_010,
    "spoofing:synthetic_circle": 93,
    "spoofing:simultaneous_position": 1_158,
    "sts": 1_689,
    "identity_anomalies": 265,
    "behaviour": 916,
}
REFERENCE_EVENT_RATES_PER_DAY = {name: total / 30 for name, total in _REAL_JUNE_2024_TOTALS.items()}

# A0.3's fingerprint fields, written by pipeline.window._day_fingerprint alongside verified_at.
FINGERPRINT_FIELDS = (
    "fingerprint_message_count",
    "fingerprint_n_distinct_mmsi",
    "fingerprint_min_timestamp",
    "fingerprint_max_timestamp",
    "fingerprint_bbox",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backup_manifest(manifest_path: Path) -> None:
    """Copy the manifest to ``<manifest_path>.bak`` before touching anything else (A0.10).

    A plain copy, not the atomic-write helper the manifest itself uses for its own writes: this is
    a read-only snapshot of a file that already exists, not a concurrent write that needs crash
    safety of its own. A no-op if the manifest does not exist yet (nothing to back up).
    """
    if not manifest_path.exists():
        return
    backup_path = manifest_path.parent / (manifest_path.name + ".bak")
    shutil.copyfile(manifest_path, backup_path)
    logger.info("Manifest backed up to %s", backup_path)


def _select_candidates(manifest_data: dict, max_days: int) -> list[date]:
    """Days with ``verified_at`` present and ``clean_discarded_at`` absent, sorted, capped at
    ``max_days`` (A5's own selection rule and per-invocation tope)."""
    days = sorted(
        date.fromisoformat(key)
        for key, entry in manifest_data.items()
        if "verified_at" in entry and "clean_discarded_at" not in entry
    )
    return days[:max_days]


def _group_contiguous(days: list[date]) -> list[tuple[date, date, list[date]]]:
    """Split a sorted list of days into maximal contiguous runs: (run_start, run_end, days)."""
    if not days:
        return []
    groups: list[list[date]] = []
    run = [days[0]]
    for day in days[1:]:
        if day == run[-1] + timedelta(days=1):
            run.append(day)
        else:
            groups.append(run)
            run = [day]
    groups.append(run)
    return [(run[0], run[-1], run) for run in groups]


def _empty_trash(data_root: Path, manifest_path: Path, yes_delete: bool) -> None:
    """Permanently remove every ``.trash`` entry whose quarantine is confirmed committed --
    ``manifest.day_state`` is ``"reduced"`` (``clean_discarded_at`` present), per A0.2. Always runs
    BEFORE any new day is quarantined by this same invocation (see :func:`prune`), so an entry this
    call itself quarantines can never also be the one emptied by it -- real emptying only ever
    happens on the invocation after the one that quarantined it.
    """
    trash_root = data_root / ".trash"
    if not trash_root.exists():
        return
    for entry_dir in sorted(trash_root.glob("date=*")):
        day_str = entry_dir.name.removeprefix("date=")
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            logger.warning("%s: not a recognized date=YYYY-MM-DD entry, leaving untouched", entry_dir)
            continue
        state = manifest.day_state(manifest_path, day)
        if state != "reduced":
            logger.warning(
                "%s: manifest state is %r, not 'reduced' -- the quarantine that put this here was "
                "never confirmed committed, leaving in .trash rather than emptying it",
                entry_dir,
                state,
            )
            continue
        if not yes_delete:
            logger.info("[dry-run] would permanently empty %s", entry_dir)
            continue
        shutil.rmtree(entry_dir)
        logger.info("%s: permanently emptied from .trash", entry_dir)


def _check_day_partitions_readable(
    label: str, root: Path, start: date, end: date, allow_zero: bool
) -> tuple[list[str], int]:
    """Open every day-partition in [start, end] under root with DuckDB; readable and, unless
    allow_zero, nonempty (A0.9: verify by reading). Returns (failures, n_partitions_present)."""
    failures: list[str] = []
    partitions = existing_partitions(start, end, root)
    con = duckdb.connect()
    try:
        for day, path in partitions:
            try:
                (n_rows,) = con.execute(
                    f"SELECT count(*) FROM read_parquet('{path.as_posix()}')"
                ).fetchone()
            except duckdb.Error as exc:
                failures.append(f"{label} {day.isoformat()}: unreadable at {path} ({exc})")
                continue
            if n_rows == 0:
                if allow_zero:
                    logger.warning("%s %s: 0 rows at %s (allowed)", label, day.isoformat(), path)
                else:
                    failures.append(f"{label} {day.isoformat()}: 0 rows at {path}")
    finally:
        con.close()
    return failures, len(partitions)


def _check_window_artifact(
    label: str,
    root: Path,
    start: date,
    end: date,
    allow_zero: bool,
    has_window_columns: bool = True,
) -> list[str]:
    """Open the exact window=<start>_<end> artifact with DuckDB; readable, and, unless allow_zero,
    nonempty (A0.9). When has_window_columns, also cross-checks the file's own
    window_start/window_end columns against the path -- belt-and-suspenders provenance, since the
    path alone already encodes the window being pruned (voyages/identity carry no such columns,
    see module docstring, so has_window_columns=False for those two)."""
    path = window_partition_path(start, end, root)
    if not path.exists():
        expected_window = f"{start.isoformat()}_{end.isoformat()}"
        return [f"{label}: missing at {path} (expected exact window {expected_window})"]
    con = duckdb.connect()
    try:
        try:
            (n_rows,) = con.execute(
                f"SELECT count(*) FROM read_parquet('{path.as_posix()}')"
            ).fetchone()
        except duckdb.Error as exc:
            return [f"{label}: unreadable at {path} ({exc})"]
        if n_rows == 0:
            if allow_zero:
                logger.warning(
                    "%s: 0 rows at %s (allowed -- nothing was detected in this window)", label, path
                )
            else:
                return [f"{label}: 0 rows at {path}"]
        if has_window_columns and n_rows > 0:
            rows = con.execute(
                f"SELECT DISTINCT window_start, window_end FROM read_parquet('{path.as_posix()}')"
            ).fetchall()
            expected = (start, end)
            bad = [row for row in rows if row != expected]
            if bad:
                message = (
                    f"{label}: provenance mismatch at {path}, found window_start/window_end "
                    f"{bad}, expected {expected}"
                )
                return [message]
    finally:
        con.close()
    return []


def _distinct_mmsi(root: Path, start: date, end: date) -> int:
    """count(DISTINCT mmsi) across every day-partition in [start, end] under root, 0 if none."""
    partitions = existing_partitions(start, end, root)
    if not partitions:
        return 0
    con = duckdb.connect()
    try:
        union_sql = " UNION ALL ".join(
            f"SELECT mmsi FROM read_parquet('{path.as_posix()}')" for _day, path in partitions
        )
        (n,) = con.execute(f"SELECT count(DISTINCT mmsi) FROM ({union_sql})").fetchone()
        return n
    finally:
        con.close()


def _event_count(path: Path, kind: str | None) -> int | None:
    """count(*) in path, optionally filtered to one `kind`, or None if path doesn't exist."""
    if not path.exists():
        return None
    con = duckdb.connect()
    try:
        if kind is not None:
            (n,) = con.execute(
                f"SELECT count(*) FROM read_parquet('{path.as_posix()}') WHERE kind = ?", [kind]
            ).fetchone()
        else:
            (n,) = con.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchone()
        return n
    finally:
        con.close()


def _rate_deviation_ratio(observed: int, expected: float) -> float | None:
    """How many times off `observed` is from `expected`, symmetric (>=1.0), or None if there is no
    usable reference (expected <= 0) or the deviation isn't meaningful (both near zero)."""
    if expected <= 0:
        return None
    if observed == 0:
        return None if expected < 1.0 else float("inf")
    return max(observed / expected, expected / observed)


def _statistical_sanity_failures(group_start: date, group_end: date, roots: dict[str, Path]) -> list[str]:
    """A0.5's cordura gate: per-day event rate for this window's own artifacts vs. the real June
    2024 rate, for every check this module already re-reads for other gates. See module docstring
    for why `gaps` cannot be included (not window-partitioned by pipeline.window)."""
    n_days = (group_end - group_start).days + 1
    checks: list[tuple[str, Path, str | None]] = [
        ("spoofing:impossible_speed", window_partition_path(group_start, group_end, roots["spoofing"]), "impossible_speed"),
        ("spoofing:on_land", window_partition_path(group_start, group_end, roots["spoofing"]), "on_land"),
        ("spoofing:synthetic_circle", window_partition_path(group_start, group_end, roots["spoofing"]), "synthetic_circle"),
        ("spoofing:simultaneous_position", window_partition_path(group_start, group_end, roots["spoofing"]), "simultaneous_position"),
        ("sts", window_partition_path(group_start, group_end, roots["sts"]), None),
        ("identity_anomalies", window_partition_path(group_start, group_end, roots["identity_anomalies"]), None),
        ("behaviour", window_partition_path(group_start, group_end, roots["behaviour"]), None),
    ]
    failures: list[str] = []
    for name, path, kind in checks:
        reference_per_day = REFERENCE_EVENT_RATES_PER_DAY.get(name)
        if reference_per_day is None:
            continue
        observed = _event_count(path, kind)
        if observed is None:
            continue  # already reported by the artifact-existence gate
        expected = reference_per_day * n_days
        ratio = _rate_deviation_ratio(observed, expected)
        if ratio is not None and ratio > ORDER_OF_MAGNITUDE:
            failures.append(
                f"statistical sanity: {name} observed {observed} over {n_days} day(s) "
                f"(expected ~{expected:.1f} from the real June 2024 rate, "
                f"{reference_per_day:.2f}/day) -- {ratio:.1f}x off, exceeds the "
                f"{ORDER_OF_MAGNITUDE:.0f}x order-of-magnitude bound"
            )
    return failures


def _evaluate_group(
    group_start: date,
    group_end: date,
    group_days: list[date],
    roots: dict[str, Path],
    manifest_data: dict,
    lead_in_days: int,
    disk_ok: bool,
    min_free_gb: float,
) -> list[str]:
    """Re-evaluate every A5 gate for one contiguous run of candidate days, returning a list of
    failure strings (empty means every day in the run may be pruned). Never trusts the manifest's
    earlier verified_at -- every artifact is re-opened here."""
    failures: list[str] = []

    if not disk_ok:
        failures.append(f"free disk below --min-free-gb={min_free_gb:.1f}")

    for day in group_days:
        entry = manifest_data.get(day.isoformat(), {})
        missing = [field for field in FINGERPRINT_FIELDS if field not in entry]
        if missing:
            failures.append(f"{day.isoformat()}: fingerprint field(s) missing from manifest: {missing}")

    clean_partitions = existing_partitions(group_start, group_end, roots["clean"])
    if len(clean_partitions) != len(group_days):
        failures.append(
            f"clean: {len(clean_partitions)} day partition(s) on disk for "
            f"{group_start.isoformat()}..{group_end.isoformat()}, expected {len(group_days)} "
            "(one per candidate day)"
        )

    thin_failures, n_thin_days = _check_day_partitions_readable(
        "thin", roots["thin"], group_start, group_end, allow_zero=False
    )
    failures += thin_failures
    if n_thin_days != len(clean_partitions):
        failures.append(
            f"thin: {n_thin_days} day partition(s), expected {len(clean_partitions)} "
            "(== clean days being pruned)"
        )
    clean_mmsi = _distinct_mmsi(roots["clean"], group_start, group_end)
    thin_mmsi = _distinct_mmsi(roots["thin"], group_start, group_end)
    if clean_mmsi > 0 and thin_mmsi < MIN_THIN_MMSI_RATIO * clean_mmsi:
        failures.append(
            f"thin: {thin_mmsi} distinct mmsi < {MIN_THIN_MMSI_RATIO:.0%} of clean's {clean_mmsi}"
        )
    elif clean_mmsi == 0:
        failures.append("thin: clean has 0 distinct mmsi to compare against")

    lead_in_start = group_start - timedelta(days=lead_in_days)
    liveness_failures, n_liveness_days = _check_day_partitions_readable(
        "liveness", roots["liveness"], lead_in_start, group_end, allow_zero=False
    )
    failures += liveness_failures
    expected_liveness_days = (group_end - lead_in_start).days + 1
    if n_liveness_days != expected_liveness_days:
        failures.append(
            f"liveness: {n_liveness_days} day partition(s) for {lead_in_start.isoformat()}.."
            f"{group_end.isoformat()}, expected {expected_liveness_days} "
            f"(lead-in of {lead_in_days}d not fully covered)"
        )

    failures += _check_window_artifact(
        "voyages", roots["voyages"], group_start, group_end, allow_zero=False, has_window_columns=False
    )
    failures += _check_window_artifact(
        "identity", roots["identity"], group_start, group_end, allow_zero=False, has_window_columns=False
    )
    failures += _check_window_artifact(
        "ship_type reference", roots["ship_type"], group_start, group_end, allow_zero=False
    )
    failures += _check_window_artifact(
        "anchorages", roots["anchorages"], group_start, group_end, allow_zero=False
    )
    failures += _check_window_artifact(
        "spoofing", roots["spoofing"], group_start, group_end, allow_zero=True
    )
    failures += _check_window_artifact("sts", roots["sts"], group_start, group_end, allow_zero=True)
    failures += _check_window_artifact(
        "behaviour", roots["behaviour"], group_start, group_end, allow_zero=True
    )
    failures += _check_window_artifact(
        "identity_anomalies", roots["identity_anomalies"], group_start, group_end, allow_zero=True
    )

    failures += _statistical_sanity_failures(group_start, group_end, roots)

    return failures


def _prune_day(day: date, clean_root: Path, trash_root: Path, manifest_path: Path) -> None:
    """Quarantine one day's clean partition: rename data/clean/.../date=.../ to
    data/.trash/date=.../ (same filesystem, A0.2), then record clean_discarded_at (A5)."""
    clean_dir = partition_path(day, clean_root).parent
    if not clean_dir.exists():
        logger.warning(
            "%s: no clean partition on disk, nothing to prune (already gone?)", day.isoformat()
        )
        return
    trash_dir = trash_root / clean_dir.name
    trash_root.mkdir(parents=True, exist_ok=True)
    if trash_dir.exists():
        logger.warning(
            "%s: a .trash entry already exists at %s, refusing to overwrite it -- leaving the "
            "clean partition in place for manual inspection",
            day.isoformat(),
            trash_dir,
        )
        return
    clean_dir.rename(trash_dir)
    manifest.record(manifest_path, day, clean_discarded_at=_now_iso())
    logger.info("%s: clean partition moved to %s, clean_discarded_at recorded", day.isoformat(), trash_dir)


def prune(
    data_root: Path = DATA_ROOT,
    max_days: int = DEFAULT_MAX_DAYS,
    yes_delete: bool = False,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
    lead_in_days: int = DEFAULT_LEAD_IN_DAYS,
) -> None:
    """Delete (quarantine) clean-data partitions for days pipeline.window has already verified,
    up to max_days per invocation. Dry-run (prints the plan, touches nothing) unless
    yes_delete=True. See module docstring for the full safety posture (A0/A5).
    """
    no_prune_path = data_root / ".no-prune"
    if no_prune_path.exists():
        logger.warning("%s present, aborting prune without touching anything", no_prune_path)
        return

    manifest_path = data_root / "manifest.json"
    _backup_manifest(manifest_path)

    roots = _window_roots(data_root)

    _empty_trash(data_root, manifest_path, yes_delete)

    manifest_data = manifest.load(manifest_path)
    candidates = _select_candidates(manifest_data, max_days)
    if not candidates:
        logger.info(
            "No candidate day(s) to prune (need verified_at present, clean_discarded_at absent)."
        )
        return

    logger.info(
        "Statistical sanity gate covers %s; 'gaps' is not included -- detect.gaps is not "
        "window-partitioned by pipeline.window, see module docstring.",
        sorted(REFERENCE_EVENT_RATES_PER_DAY),
    )

    free_bytes = shutil.disk_usage(data_root).free
    disk_ok = free_bytes >= min_free_gb * 1e9
    if not disk_ok:
        logger.warning(
            "Only %.1f GB free under %s, below --min-free-gb=%.1f -- every candidate day will be "
            "skipped this run",
            free_bytes / 1e9,
            data_root,
            min_free_gb,
        )

    n_pruned = 0
    for group_start, group_end, group_days in _group_contiguous(candidates):
        failures = _evaluate_group(
            group_start, group_end, group_days, roots, manifest_data, lead_in_days, disk_ok, min_free_gb
        )
        if failures:
            for failure in failures:
                logger.warning(
                    "Skipping %s..%s: %s", group_start.isoformat(), group_end.isoformat(), failure
                )
            continue
        if not yes_delete:
            logger.info(
                "[dry-run] would prune %d day(s) in %s..%s (all gates passed)",
                len(group_days),
                group_start.isoformat(),
                group_end.isoformat(),
            )
            continue
        for day in group_days:
            _prune_day(day, roots["clean"], data_root / ".trash", manifest_path)
            n_pruned += 1

    if yes_delete:
        logger.info("Pruned %d day(s) this run.", n_pruned)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete (quarantine to data/.trash) clean-data partitions for days already "
        "verified by pipeline.window, re-checking every gate at deletion time. Dry-run by "
        "default -- see --yes-delete."
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=DEFAULT_MAX_DAYS,
        help=f"Cap on candidate days processed in one invocation (default: {DEFAULT_MAX_DAYS})",
    )
    parser.add_argument(
        "--yes-delete",
        action="store_true",
        help="Actually quarantine/empty data on disk; without this, only prints the plan",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=f"Skip every candidate day if free disk falls below this (default: {DEFAULT_MIN_FREE_GB})",
    )
    parser.add_argument(
        "--lead-in-days",
        type=int,
        default=DEFAULT_LEAD_IN_DAYS,
        help=f"Lead-in range liveness coverage must fully cover before a day is pruned "
        f"(default: {DEFAULT_LEAD_IN_DAYS}, matching pipeline.window's own default)",
    )
    parser.add_argument(
        "--data-root", default=str(DATA_ROOT), help="Root directory for all data (default: data)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    prune(
        data_root=Path(args.data_root),
        max_days=args.max_days,
        yes_delete=args.yes_delete,
        min_free_gb=args.min_free_gb,
        lead_in_days=args.lead_in_days,
    )


if __name__ == "__main__":
    main()
