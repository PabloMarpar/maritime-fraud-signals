"""Deliberate AIS gaps: score every voyage-to-voyage silence with a probability, not a boolean.

**Why this exists.** ``process.tracks`` already draws a line between two voyages every time a
vessel goes silent for more than ``gap_hours`` (default 6h) -- that module's docstring is explicit
that classifying the resulting gap as suspicious or ordinary is "Phase 2 Detector 1's job, not
this module's" (see ``docs/DECISIONS.md``). This module is that job. Every consecutive pair of
voyages for the same MMSI in ``voyages.parquet`` is already a candidate gap; there is nothing left
to re-derive from raw points, only to score.

**Method.** For each candidate gap, ask ``detect.liveness.liveness_verdict`` whether some other
vessel was heard near either end of the gap (where V went dark, where V reappeared) during the
silence window. The tri-state verdict (``receiver_alive`` / ``area_dark`` / ``no_evidence``) is
mapped to a probability of deliberate AIS-off behaviour via a fixed table, :data:`VERDICT_PROBABILITY`:
``receiver_alive`` (coverage was fine, V still went dark) is the strongest evidence of a deliberate
gap; ``area_dark`` (a receiver outage is plausible) is the strongest evidence against; ``no_evidence``
sits in the middle. See :func:`gap_probability` for why long gaps get a deliberate confidence cap on
top of this table, not a different table.

**Why cells from both endpoints.** A corroborator seen near either the position where V went dark
or the position where V reappeared is relevant evidence that a receiver was listening in the
vicinity during the silence -- the vessel need not have transited the exact midpoint. See
:func:`score_gap`.

**Output shape.** One flat table, one row per candidate gap in the requested range, at
``data/detect/gaps.parquet`` -- mirroring ``detect.coverage``/``detect.liveness``'s whole-range,
not-partitioned-by-day output, and for the same reason: a gap's evidence window is a cross-date
fact in general (a gap can straddle a day boundary), and callers of this table want "all scored
gaps in this range" without a multi-file query.

**Known limitation, not solved here.** :func:`score_gap` excludes only the gap's own MMSI from
corroboration and baseline evidence, not other identities of the same physical vessel (a reused
or spoofed MMSI). That linkage does not exist yet -- see ``docs/STATE.md``'s open questions,
relevant once P2-5 (identity linkage) exists. Not a bug to fix here.

All of this calls into ``detect.liveness`` once per candidate gap, thousands of times per run (see
that module's docstring); this module's builder opens exactly one DuckDB connection and reuses it
across every gap, never reconnecting per gap.
"""

from __future__ import annotations

import argparse
import logging
import math
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from detect.liveness import (
    GRID_SIZE_DEG,
    LIVENESS_PATH,
    LivenessVerdict,
    cells_within,
    liveness_verdict,
)
from process.tracks import VOYAGES_PATH

logger = logging.getLogger(__name__)

DETECT_ROOT = Path("data/detect")
GAPS_PATH = DETECT_ROOT / "gaps.parquet"

# Base probability of a deliberate AIS-off gap given each liveness verdict. Unvalidated defaults
# awaiting empirical calibration -- see module docstring and detect.liveness's own
# "# unvalidated default" comments for MIN_EXPECTED_CORROBORATORS/MIN_BASELINE_VESSELS, which this
# table inherits the uncertainty of.
VERDICT_PROBABILITY: dict[str, float] = {
    "receiver_alive": 0.9,
    "area_dark": 0.15,
    "no_evidence": 0.5,
}

# Gaps at or above this duration get their probability clamped toward the inconclusive band below,
# instead of the raw VERDICT_PROBABILITY value -- see gap_probability's docstring for why. Also an
# unvalidated default: the docs/DECISIONS.md finding that motivates it (>90% receiver_alive
# saturation past 12h) is empirical, but the 12h cutoff itself has not been tuned.
LONG_GAP_HOURS = 12.0
LOW_CONFIDENCE_FLOOR = 0.4
LOW_CONFIDENCE_CEIL = 0.6


def gap_probability(verdict: LivenessVerdict, duration_hours: float) -> float:
    """Map a liveness verdict and gap duration to a probability the gap was deliberate.

    ``VERDICT_PROBABILITY[verdict.verdict]`` is the base rate. For gaps of ``LONG_GAP_HOURS`` or
    more, that base is clamped into ``[LOW_CONFIDENCE_FLOOR, LOW_CONFIDENCE_CEIL]`` instead of
    being reported as-is. This is a deliberate confidence cap, not a recalibration of the rule:
    ``docs/DECISIONS.md`` records that the corroboration signal this verdict is built from
    discriminates a genuine silence well for short gaps but saturates toward ``receiver_alive``
    for 12h+ gaps regardless of whether the gap is real evasion or routine -- both real and
    placebo verdicts converge past that point simply because the window is long. Reporting a
    confident-looking 0.9 or 0.15 for a verdict known to be unreliable at that duration would
    overstate what the evidence actually supports, so the result is pulled toward the inconclusive
    band instead. All three thresholds here (the VERDICT_PROBABILITY values, LONG_GAP_HOURS, and
    the clamp band) are unvalidated defaults awaiting calibration, not tuned constants.
    """
    base = VERDICT_PROBABILITY[verdict.verdict]
    if duration_hours >= LONG_GAP_HOURS:
        return min(max(base, LOW_CONFIDENCE_FLOOR), LOW_CONFIDENCE_CEIL)
    return base


# Same epsilon-before-floor, round-after-multiply convention as detect.liveness/detect.coverage
# (binary floating point cannot represent a decimal grid size like 0.1 exactly), reimplemented in
# Python here because cells_within's contract is grid cell coordinates -- the presence table's
# cell_lat/cell_lon, already floored -- not raw vessel positions like a gap's endpoints. Feeding
# raw positions straight into cells_within would only ever join the liveness table on the rare
# position that happens to land exactly on a grid boundary.
_EPSILON = 1e-9


def _to_cell(lat: float, lon: float, grid_size_deg: float = GRID_SIZE_DEG) -> tuple[float, float]:
    """Floor a raw (lat, lon) position to its grid cell's lower-left corner."""
    cell_lat = round(math.floor(lat / grid_size_deg + _EPSILON) * grid_size_deg, 6)
    cell_lon = round(math.floor(lon / grid_size_deg + _EPSILON) * grid_size_deg, 6)
    return cell_lat, cell_lon


@dataclass(frozen=True)
class GapCandidate:
    """One vessel's silence between two consecutive voyages, not yet scored."""

    mmsi: int
    prev_voyage_id: str
    next_voyage_id: str
    gap_start: datetime
    gap_end: datetime
    duration_hours: float
    start_latitude: float
    start_longitude: float
    end_latitude: float
    end_longitude: float


@dataclass(frozen=True)
class GapScore:
    """A scored GapCandidate: the liveness verdict behind it, flattened for easy Parquet output."""

    gap: GapCandidate
    probability: float
    verdict: str
    n_corroborators: int
    expected_corroborators: float
    n_baseline_vessels: int


def candidate_gaps(
    con: duckdb.DuckDBPyConnection,
    voyages_path: Path,
    start: date | None = None,
    end: date | None = None,
) -> list[GapCandidate]:
    """Pair every voyage with its successor for the same mmsi; each pair is one candidate gap.

    Reads ``voyages.parquet`` via a single windowed query (``LEAD`` over ``voyage_seq`` per mmsi)
    and returns the result as a small list of Python objects -- thousands of rows at most, per
    ``detect.liveness``'s own docstring on how it expects to be called, not something that needs
    to stay inside DuckDB. The last voyage per mmsi has no successor and produces no gap.

    ``start``/``end`` optionally restrict the result to gaps whose ``gap_start`` falls in
    ``[start, end)`` (dates), for CLI range control. Both are inclusive-start/exclusive-end on the
    gap's own start date, not the voyage table's coverage.
    """
    rows = con.execute(
        "WITH paired AS ("
        "  SELECT mmsi, voyage_id, end_time, end_latitude, end_longitude, "
        "         lead(voyage_id) OVER w AS next_voyage_id, "
        "         lead(start_time) OVER w AS next_start_time, "
        "         lead(start_latitude) OVER w AS next_start_latitude, "
        "         lead(start_longitude) OVER w AS next_start_longitude "
        "  FROM read_parquet(?) "
        "  WINDOW w AS (PARTITION BY mmsi ORDER BY voyage_seq)"
        ") "
        "SELECT mmsi, voyage_id, next_voyage_id, end_time, next_start_time, "
        "       end_latitude, end_longitude, next_start_latitude, next_start_longitude, "
        "       date_diff('second', end_time, next_start_time) / 3600.0 AS duration_hours "
        "FROM paired "
        "WHERE next_voyage_id IS NOT NULL",
        [str(voyages_path)],
    ).fetchall()

    candidates = [
        GapCandidate(
            mmsi=mmsi,
            prev_voyage_id=prev_voyage_id,
            next_voyage_id=next_voyage_id,
            gap_start=gap_start,
            gap_end=gap_end,
            duration_hours=duration_hours,
            start_latitude=start_latitude,
            start_longitude=start_longitude,
            end_latitude=end_latitude,
            end_longitude=end_longitude,
        )
        for (
            mmsi,
            prev_voyage_id,
            next_voyage_id,
            gap_start,
            gap_end,
            start_latitude,
            start_longitude,
            end_latitude,
            end_longitude,
            duration_hours,
        ) in rows
    ]

    if start is not None:
        candidates = [c for c in candidates if c.gap_start.date() >= start]
    if end is not None:
        candidates = [c for c in candidates if c.gap_start.date() < end]
    return candidates


def score_gap(
    con: duckdb.DuckDBPyConnection,
    gap: GapCandidate,
    liveness_path: Path = LIVENESS_PATH,
    ring: int = 0,
    **verdict_kwargs,
) -> GapScore:
    """Score one candidate gap by asking detect.liveness whether the silence is corroborated.

    Cells are drawn from BOTH endpoints of the gap -- where V went dark and where V reappeared --
    since a corroborator near either point is relevant evidence a receiver was listening (see
    module docstring). Each raw endpoint position is first floored to its grid cell (see
    :func:`_to_cell`) before being handed to ``cells_within``, whose own contract is grid cell
    coordinates, not raw positions. ``**verdict_kwargs`` passes through to ``liveness_verdict``
    unchanged (``as_of``, ``baseline_days``, ``min_corroborators``, ``min_expected``,
    ``min_baseline_vessels``), so this function does not need to know about each one individually.

    Excludes only ``gap.mmsi`` -- not other identities of the same physical vessel under a reused
    or spoofed MMSI. That linkage does not exist yet; see module docstring.
    """
    cells = cells_within(
        [
            _to_cell(gap.start_latitude, gap.start_longitude),
            _to_cell(gap.end_latitude, gap.end_longitude),
        ],
        ring=ring,
    )
    verdict = liveness_verdict(
        con,
        cells,
        gap.gap_start,
        gap.gap_end,
        exclude_mmsi=gap.mmsi,
        liveness_path=liveness_path,
        **verdict_kwargs,
    )
    probability = gap_probability(verdict, gap.duration_hours)
    return GapScore(
        gap=gap,
        probability=probability,
        verdict=verdict.verdict,
        n_corroborators=verdict.n_corroborators,
        expected_corroborators=verdict.expected_corroborators,
        n_baseline_vessels=verdict.n_baseline_vessels,
    )


def _git_sha() -> str:
    """Short git commit SHA of the working tree, or "unknown" if it can't be determined.

    Provenance metadata only, never correctness-critical, so any failure (not a git repo, git
    not on PATH, etc.) falls back to a literal string rather than raising.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_gap_scores(
    start: date,
    end: date,
    voyages_path: Path = VOYAGES_PATH,
    liveness_path: Path = LIVENESS_PATH,
    out_path: Path = GAPS_PATH,
    ring: int = 0,
    force: bool = False,
) -> Path:
    """Score every candidate gap with ``gap_start`` in ``[start, end)`` and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Returns out_path
    either way. Raises FileNotFoundError if voyages_path or liveness_path don't exist, naming
    which one and what builds it (process.tracks.reconstruct_range / detect.liveness.build_liveness
    respectively).

    Opens exactly one DuckDB connection and reuses it across every candidate gap -- liveness_verdict
    is called once per gap, thousands of times per run, and must not pay to reconnect each time.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    if not voyages_path.exists():
        raise FileNotFoundError(
            f"No voyages table at {voyages_path}; run process.tracks.reconstruct_range first"
        )
    if not liveness_path.exists():
        raise FileNotFoundError(
            f"No liveness table at {liveness_path}; run detect.liveness.build_liveness first"
        )

    con = duckdb.connect()
    try:
        gaps = candidate_gaps(con, voyages_path, start=start, end=end)
        scores = [score_gap(con, gap, liveness_path=liveness_path, ring=ring) for gap in gaps]

        verdict_counts: dict[str, int] = {}
        for s in scores:
            verdict_counts[s.verdict] = verdict_counts.get(s.verdict, 0) + 1
        mean_probability = sum(s.probability for s in scores) / len(scores) if scores else 0.0
        logger.info(
            "Scored %d gap(s) in %s..%s: verdict breakdown %s, mean probability %.3f",
            len(scores),
            start.isoformat(),
            end.isoformat(),
            verdict_counts,
            mean_probability,
        )

        built_at = datetime.now(timezone.utc)
        git_sha = _git_sha()
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _gap_scores ("
            "mmsi BIGINT, prev_voyage_id VARCHAR, next_voyage_id VARCHAR, "
            "gap_start TIMESTAMP, gap_end TIMESTAMP, duration_hours DOUBLE, "
            "start_latitude DOUBLE, start_longitude DOUBLE, "
            "end_latitude DOUBLE, end_longitude DOUBLE, "
            "probability DOUBLE, verdict VARCHAR, "
            "n_corroborators INTEGER, expected_corroborators DOUBLE, n_baseline_vessels INTEGER)"
        )
        con.executemany(
            "INSERT INTO _gap_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.gap.mmsi,
                    s.gap.prev_voyage_id,
                    s.gap.next_voyage_id,
                    s.gap.gap_start,
                    s.gap.gap_end,
                    s.gap.duration_hours,
                    s.gap.start_latitude,
                    s.gap.start_longitude,
                    s.gap.end_latitude,
                    s.gap.end_longitude,
                    s.probability,
                    s.verdict,
                    s.n_corroborators,
                    s.expected_corroborators,
                    s.n_baseline_vessels,
                )
                for s in scores
            ],
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"DATE '{start.isoformat()}' AS window_start, "
            f"DATE '{end.isoformat()}' AS window_end, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at, "
            f"'{git_sha}' AS git_sha "
            "FROM _gap_scores ORDER BY mmsi, gap_start) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score every candidate AIS gap (consecutive voyage pair) with a probability "
        "of deliberate AIS-off behaviour, using cross-vessel corroboration."
    )
    parser.add_argument("--start", required=True, help="First day, YYYY-MM-DD")
    parser.add_argument("--end", help="Last day, YYYY-MM-DD (default: same as --start)")
    parser.add_argument(
        "--voyages-path", default=str(VOYAGES_PATH), help="Path to the voyages table"
    )
    parser.add_argument(
        "--liveness-path", default=str(LIVENESS_PATH), help="Path to the liveness table"
    )
    parser.add_argument("--out-path", default=str(GAPS_PATH), help="Output path for the gap scores")
    parser.add_argument(
        "--ring",
        type=int,
        default=0,
        help="Ring of neighbouring cells to search around each gap endpoint (default: 0)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    build_gap_scores(
        start,
        end,
        voyages_path=Path(args.voyages_path),
        liveness_path=Path(args.liveness_path),
        out_path=Path(args.out_path),
        ring=args.ring,
        force=args.force,
    )


if __name__ == "__main__":
    main()
