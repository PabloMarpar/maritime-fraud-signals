"""P2-7: measure agreement between detect.sts and the GFW Events API's own encounters.

**Why this exists.** Neither table is ground truth. ``detect.sts`` is a rule-based detector over
raw AIS with unvalidated thresholds (see that module's docstring); GFW's encounters table is a
third party's own detector, with its own unvalidated internals, over the same underlying kind of
data. Comparing the two is not a precision/recall exercise against a labelled answer key -- it is
measuring how much two independent methods agree, in both directions, which is itself the
reportable result ``docs/DATA_SOURCES.md`` calls for.

**Matching rule.** Two events match when they name the same unordered vessel pair
(``{mmsi_a, mmsi_b}`` as a set -- side order is not meaningful in either source) AND their
``[start_time, end_time]`` intervals overlap once each is padded by
:data:`MATCH_TOLERANCE_MINUTES` on both ends. The tolerance exists because the two detectors draw
episode boundaries differently (``detect.sts`` from 10-minute slot aggregation, GFW from its own,
undocumented, segmentation) -- an unvalidated default, same posture as every threshold elsewhere in
this project. This is a coarser test than spatial proximity: two detectors agreeing on *which pair,
roughly when* is the question P2-7 asks, not whether their reported midpoints match to the metre.

**Scope caveat -- read before interpreting the numbers this module produces.** GFW's encounters
dataset only covers vessel-type pairs it classifies as fishing-economy activity (fishing-fishing,
fishing-carrier, fishing-support, fishing-bunker, tanker-fishing, carrier-bunker, support-bunker --
see ``ingest.gfw``'s module docstring). ``detect.sts`` has no such restriction, and its real-window
gated survivors skew away from that population ("no Belts, few tankers" -- P2-4). This module
reports agreement over the full set AND over the subset where at least one side's
``ship_type`` is ``'Fishing'`` (:data:`IN_GFW_SCOPE_SHIP_TYPE`) -- a simplified proxy for "GFW could
plausibly have seen this pair at all", since GFW's carrier-bunker/support-bunker subtypes are not
recoverable from ``detect.sts``'s ship-type pairing alone. Low agreement on the full set is
expected to partly reflect this scope mismatch, not detector failure -- always report the
scope-split numbers alongside the headline ones, never the headline alone.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from detect.sts import STS_PATH
from ingest.gfw import GFW_ENCOUNTERS_PATH

logger = logging.getLogger(__name__)

DETECT_ROOT = Path("data/detect")
AGREEMENT_PATH = DETECT_ROOT / "sts_gfw_agreement.parquet"

# Unvalidated default -- see module docstring.
MATCH_TOLERANCE_MINUTES = 30.0

# Simplified proxy for "this pair falls inside GFW's encounter scope" -- see module docstring.
IN_GFW_SCOPE_SHIP_TYPE = "Fishing"


@dataclass(frozen=True)
class MatchableEvent:
    """The minimal shape either side of a comparison needs. ``source_id`` is
    ``encounter_id`` for detect.sts rows, ``gfw_event_id`` for GFW rows."""

    source_id: str
    mmsi_a: int
    mmsi_b: int
    start_time: datetime
    end_time: datetime


def _pair(event: MatchableEvent) -> frozenset[int]:
    return frozenset({event.mmsi_a, event.mmsi_b})


def intervals_overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime, tolerance_minutes: float
) -> bool:
    """Whether ``[a_start, a_end]`` and ``[b_start, b_end]`` overlap once each is padded by
    ``tolerance_minutes`` on both ends. Padding both, rather than just one, keeps the test
    symmetric regardless of which side is passed first.
    """
    pad = timedelta(minutes=tolerance_minutes)
    return a_start - pad <= b_end + pad and b_start - pad <= a_end + pad


def is_match(a: MatchableEvent, b: MatchableEvent, tolerance_minutes: float) -> bool:
    """Whether two events (one from each source) refer to the same real-world encounter, per the
    matching rule in the module docstring: same unordered vessel pair, overlapping (tolerance-padded)
    time window. Spatial position is deliberately not checked -- see module docstring.
    """
    return _pair(a) == _pair(b) and intervals_overlap(
        a.start_time, a.end_time, b.start_time, b.end_time, tolerance_minutes
    )


@dataclass(frozen=True)
class AgreementResult:
    """Whether each event on each side found at least one match on the other side. Many-to-many:
    one event can match several on the other side, but each event's own flag is a simple bool --
    "was this one corroborated by the other source at all", not a count.
    """

    matched_sts_ids: frozenset[str]
    matched_gfw_ids: frozenset[str]


def match_all(
    sts_events: list[MatchableEvent],
    gfw_events: list[MatchableEvent],
    tolerance_minutes: float = MATCH_TOLERANCE_MINUTES,
) -> AgreementResult:
    """Find, for every event on each side, whether at least one event on the other side matches.

    O(n*m) pairwise comparison -- fine at the scale either table runs at (detect.sts's gated
    output is in the low thousands per month; GFW's encounters restricted to one bbox and month
    are expected to be smaller still, see module docstring). No spatial index needed at this size.
    """
    matched_sts_ids = set()
    matched_gfw_ids = set()
    for s in sts_events:
        for g in gfw_events:
            if is_match(s, g, tolerance_minutes):
                matched_sts_ids.add(s.source_id)
                matched_gfw_ids.add(g.source_id)
    return AgreementResult(
        matched_sts_ids=frozenset(matched_sts_ids), matched_gfw_ids=frozenset(matched_gfw_ids)
    )


def _load_sts_events(con: duckdb.DuckDBPyConnection, sts_path: Path) -> list[tuple[MatchableEvent, bool]]:
    """Return (event, in_gfw_scope) for every row in sts_path -- see IN_GFW_SCOPE_SHIP_TYPE."""
    rows = con.execute(
        "SELECT encounter_id, mmsi_a, mmsi_b, start_time, end_time, ship_type_a, ship_type_b "
        "FROM read_parquet(?)",
        [str(sts_path)],
    ).fetchall()
    return [
        (
            MatchableEvent(
                source_id=encounter_id,
                mmsi_a=mmsi_a,
                mmsi_b=mmsi_b,
                start_time=start_time,
                end_time=end_time,
            ),
            IN_GFW_SCOPE_SHIP_TYPE in (ship_type_a, ship_type_b),
        )
        for (
            encounter_id,
            mmsi_a,
            mmsi_b,
            start_time,
            end_time,
            ship_type_a,
            ship_type_b,
        ) in rows
    ]


def _load_gfw_events(con: duckdb.DuckDBPyConnection, gfw_path: Path) -> list[MatchableEvent]:
    rows = con.execute(
        "SELECT gfw_event_id, mmsi_a, mmsi_b, start_time, end_time FROM read_parquet(?)",
        [str(gfw_path)],
    ).fetchall()
    return [
        MatchableEvent(
            source_id=gfw_event_id,
            mmsi_a=mmsi_a,
            mmsi_b=mmsi_b,
            start_time=start_time,
            end_time=end_time,
        )
        for (gfw_event_id, mmsi_a, mmsi_b, start_time, end_time) in rows
    ]


def _rate(n_matched: int, n_total: int) -> float:
    return n_matched / n_total if n_total else 0.0


def build_agreement(
    sts_path: Path = STS_PATH,
    gfw_path: Path = GFW_ENCOUNTERS_PATH,
    out_path: Path = AGREEMENT_PATH,
    tolerance_minutes: float = MATCH_TOLERANCE_MINUTES,
    force: bool = False,
) -> Path:
    """Match detect.sts's gated events against GFW's encounters and write out_path.

    Idempotent: if out_path already exists, this is a no-op unless force=True. Writes one row per
    detect.sts event (matched_by_gfw bool, in_gfw_scope bool) -- GFW's own table, restricted to
    the same window/bbox, already exists standalone at gfw_path for the reverse direction. Logs
    the headline and scope-split agreement rates on both sides; see module docstring for why both
    must be reported together.
    """
    if out_path.exists() and not force:
        logger.info(
            "%s already exists, skipping (pass force=True / --force to rebuild)", out_path
        )
        return out_path

    if not sts_path.exists():
        raise FileNotFoundError(f"No detect.sts output at {sts_path}; run detect.sts.build_sts_events first")
    if not gfw_path.exists():
        raise FileNotFoundError(
            f"No GFW encounters table at {gfw_path}; run ingest.gfw.build_gfw_encounters first"
        )

    con = duckdb.connect()
    try:
        sts_rows = _load_sts_events(con, sts_path)
        gfw_events = _load_gfw_events(con, gfw_path)
        sts_events = [event for event, _in_scope in sts_rows]

        result = match_all(sts_events, gfw_events, tolerance_minutes)

        n_sts = len(sts_events)
        n_gfw = len(gfw_events)
        n_sts_matched = len(result.matched_sts_ids)
        n_gfw_matched = len(result.matched_gfw_ids)

        in_scope_rows = [(e, s) for e, s in sts_rows if s]
        n_sts_in_scope = len(in_scope_rows)
        n_sts_in_scope_matched = sum(
            1 for e, _ in in_scope_rows if e.source_id in result.matched_sts_ids
        )

        logger.info(
            "Agreement: %d/%d detect.sts event(s) matched by a GFW encounter (%s) "
            "[in-scope subset: %d/%d (%s)]; %d/%d GFW encounter(s) matched by detect.sts (%s). "
            "Tolerance %.0f min. See module docstring on interpreting this against GFW's "
            "fishing-economy scope.",
            n_sts_matched,
            n_sts,
            f"{100 * _rate(n_sts_matched, n_sts):.1f}%",
            n_sts_in_scope_matched,
            n_sts_in_scope,
            f"{100 * _rate(n_sts_in_scope_matched, n_sts_in_scope):.1f}%",
            n_gfw_matched,
            n_gfw,
            f"{100 * _rate(n_gfw_matched, n_gfw):.1f}%",
            tolerance_minutes,
        )

        con.execute(
            "CREATE OR REPLACE TEMP TABLE _agreement "
            "(encounter_id VARCHAR, mmsi_a BIGINT, mmsi_b BIGINT, start_time TIMESTAMP, "
            "end_time TIMESTAMP, in_gfw_scope BOOLEAN, matched_by_gfw BOOLEAN)"
        )
        if sts_rows:
            con.executemany(
                "INSERT INTO _agreement VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event.source_id,
                        event.mmsi_a,
                        event.mmsi_b,
                        event.start_time,
                        event.end_time,
                        in_scope,
                        event.source_id in result.matched_sts_ids,
                    )
                    for event, in_scope in sts_rows
                ],
            )

        built_at = datetime.now(timezone.utc)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            "COPY (SELECT *, "
            f"{tolerance_minutes} AS match_tolerance_minutes, "
            f"TIMESTAMP '{built_at.strftime('%Y-%m-%d %H:%M:%S.%f')}' AS built_at "
            "FROM _agreement ORDER BY start_time) "
            f"TO '{out_path.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return out_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure agreement between detect.sts and the GFW Events API's encounters "
        "(P2-7)."
    )
    parser.add_argument("--sts-path", default=str(STS_PATH), help="Path to detect.sts's output")
    parser.add_argument(
        "--gfw-path", default=str(GFW_ENCOUNTERS_PATH), help="Path to ingest.gfw's output"
    )
    parser.add_argument("--out-path", default=str(AGREEMENT_PATH), help="Output path for the result")
    parser.add_argument(
        "--tolerance-minutes",
        type=float,
        default=MATCH_TOLERANCE_MINUTES,
        help="Time-window padding applied to both sides before testing overlap",
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even if the output already exists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    build_agreement(
        sts_path=Path(args.sts_path),
        gfw_path=Path(args.gfw_path),
        out_path=Path(args.out_path),
        tolerance_minutes=args.tolerance_minutes,
        force=args.force,
    )


if __name__ == "__main__":
    main()
