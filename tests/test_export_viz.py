"""Unit tests for report.export_viz and report.countries. All fixtures are synthetic and
in-memory or under tmp_path; nothing here touches data/.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import numpy as np

from process.mid import MID_COUNTRY
from report import export_viz as ev
from report.countries import COUNTRY_ISO2, iso2_of


def test_every_mid_country_has_an_iso2_code():
    missing = sorted(set(MID_COUNTRY.values()) - set(COUNTRY_ISO2))
    assert missing == []
    assert all(len(code) == 2 and code.isupper() for code in COUNTRY_ISO2.values())


def test_iso2_of_handles_unknown_and_none():
    assert iso2_of("Gabon") == "GA"
    assert iso2_of("Azores") == "PT"
    assert iso2_of("Atlantis") is None
    assert iso2_of(None) is None


def test_encode_polyline_matches_the_reference_example():
    # Google's documented example, given here as (lon, lat).
    coords = [(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]
    assert ev.encode_polyline(coords) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def test_quantize_round_trips_within_resolution():
    lo, hi = ev.MAP_BBOX[0], ev.MAP_BBOX[2]
    values = np.array([lo, 10.123456, hi, hi + 5.0])
    q = ev.quantize(values, lo, hi)
    assert q.dtype == np.dtype("<u2")
    back = lo + q.astype(float) / 65535.0 * (hi - lo)
    assert abs(back[1] - 10.123456) < (hi - lo) / 65535.0
    assert q[0] == 0 and q[2] == 65535 and q[3] == 65535  # clipped


def test_trip_runs_drops_single_point_trips_and_compacts_offsets():
    vidx = np.array([0, 0, 0, 0, 1, 1, 1])
    trip = np.array([1, 1, 2, 3, 1, 1, 1])
    runs = ev.trip_runs(vidx, trip)
    # vessel 0: trip 1 has 2 points, trips 2 and 3 have one each (dropped); vessel 1: 3 points.
    assert runs.tolist() == [[0, 0, 2], [1, 4, 3]]
    keep, rebased = ev.compact_runs(runs)
    assert keep.tolist() == [0, 1, 4, 5, 6]
    assert rebased.tolist() == [[0, 0, 2], [1, 2, 3]]


def test_trip_runs_empty():
    assert ev.trip_runs(np.array([], dtype=int), np.array([], dtype=int)).shape == (0, 3)


def _thin_con(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE thin (mmsi BIGINT, timestamp TIMESTAMP, longitude DOUBLE, latitude DOUBLE)"
    )
    con.executemany("INSERT INTO thin VALUES (?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE focus AS SELECT DISTINCT mmsi FROM thin")
    return con


def test_track_points_split_on_silence_and_impossible_jump():
    t = datetime(2024, 6, 1, 0, 0)  # noqa: DTZ001
    rows = [
        (1, t.replace(hour=0, minute=0), 10.00, 55.00),
        (1, t.replace(hour=0, minute=15), 10.01, 55.00),
        (1, t.replace(hour=0, minute=30), 10.02, 55.00),
        # 2h silence -> new trip
        (1, t.replace(hour=2, minute=30), 10.10, 55.00),
        (1, t.replace(hour=2, minute=45), 10.11, 55.00),
        # jump of ~1 degree of longitude (~64 km) in 15 minutes (~138 kn) -> new trip
        (1, t.replace(hour=3, minute=0), 11.11, 55.00),
        (1, t.replace(hour=3, minute=15), 11.12, 55.00),
        # outside MAP_BBOX -> dropped
        (1, t.replace(hour=3, minute=30), 40.0, 10.0),
    ]
    con = _thin_con(rows)
    out = con.execute(ev.track_points_sql(t, 15, "focus")).fetchall()
    trips = [r[1] for r in out]
    tmins = [r[2] for r in out]
    assert trips == [1, 1, 1, 2, 2, 3, 3]
    assert tmins == [0, 15, 30, 150, 165, 180, 195]


def test_track_points_keep_first_fix_per_bucket():
    t = datetime(2024, 6, 1, 0, 0)  # noqa: DTZ001
    rows = [
        (7, t.replace(minute=1), 10.0, 55.0),
        (7, t.replace(minute=6), 10.001, 55.0),
        (7, t.replace(minute=11), 10.002, 55.0),
        (7, t.replace(minute=16), 10.003, 55.0),
    ]
    con = _thin_con(rows)
    out = con.execute(ev.track_points_sql(t, 15, "focus")).fetchall()
    assert [r[2] for r in out] == [1, 16]


def test_density_size_is_mercator_aspect():
    width, height = ev.density_size(ev.MAP_BBOX, 2000)
    assert width == 2000
    # 17 degrees of longitude vs 6 degrees of latitude around 56N: Mercator stretches latitude
    # by ~1.8, so the image is roughly 2000 * 6 * 1.8 / 17 ~ 1270 px tall.
    assert 1150 < height < 1400


def test_colorize_density_is_transparent_where_empty():
    grid = np.zeros((3, 3))
    grid[1, 1] = 50
    grid[0, 0] = 1
    rgba = ev.colorize_density(grid)
    assert rgba.shape == (3, 3, 4)
    assert rgba[2, 2, 3] == 0
    assert rgba[1, 1, 3] > rgba[0, 0, 3] > 0


def test_status_of():
    assert ev.status_of(True, False) == ev.STATUS_LISTED_BY_END
    assert ev.status_of(False, True) == ev.STATUS_LISTED_LATER
    assert ev.status_of(None, None) == ev.STATUS_NONE


def test_discover_windows_needs_a_parquet_file(tmp_path):
    (tmp_path / "window=2024-11-01_2024-11-30").mkdir()
    (tmp_path / "window=2024-11-01_2024-11-30" / "part-0.parquet").write_bytes(b"x")
    (tmp_path / "window=2024-06-01_2024-06-30").mkdir()
    (tmp_path / "window=2024-06-01_2024-06-30" / "part-0.parquet").write_bytes(b"x")
    (tmp_path / "window=2024-07-01_2024-07-31").mkdir()  # empty: build not finished
    (tmp_path / "notes").mkdir()
    windows = ev.discover_windows(tmp_path)
    assert [w.id for w in windows] == ["2024-06-01_2024-06-30", "2024-11-01_2024-11-30"]
    assert windows[0].start == date(2024, 6, 1)
    assert windows[0].t0 == datetime(2024, 6, 1)  # noqa: DTZ001


def test_sanction_regime():
    assert ev.sanction_regime("RUSSIA-EO14024") == "russia"
    assert ev.sanction_regime("The Russia (Sanctions) (EU Exit) Regulations 2019") == "russia"
    assert ev.sanction_regime("UKRAINE-EO13685") == "russia"
    assert ev.sanction_regime("IRAN-EO13902") == "iran"
    assert ev.sanction_regime("SDGT") == "other"
    assert ev.sanction_regime(None) == "other"


def _dossier(mmsi, iso2, windows, sanctions, imo="9339337", **extra):
    d = {"mmsi": mmsi, "imo": imo, "iso2": iso2, "ship_type": "Tanker", "length": 183.0,
         "windows": windows, "sanctions": sanctions}
    d.update(extra)
    return d


def _window(hours_by_day, names=(("NS LOTUS", 100),), dests=(), **events):
    ev_ = {"n_gaps": 0, "sts": [], "behav": [], "spoof_counts": {}}
    ev_.update(events)
    return {"hours_by_day": hours_by_day, "names": [[n, c, None, None] for n, c in names],
            "destinations": [[x, c, None, None] for x, c in dests], "events": ev_}


def test_shadow_fleet_groups_mmsi_by_imo_across_a_flag_change():
    june = [0.0] * 30
    june[16], june[25] = 5.0, 2.0  # seen 17 and 26 June
    nov = [0.0] * 30
    nov[3] = 1.0  # seen 4 November under a new flag
    uk = ["uk", "NS LOTUS", "Gabon", "The Russia (Sanctions) (EU Exit) Regulations 2019", "2024-07-31"]
    ofac = ["ofac", "LEGACY", "Barbados", "RUSSIA-EO14024", "2025-01-10"]
    dossiers = [
        _dossier(626395000, "GA", {"2024-06-01_2024-06-30": _window(
            june, dests=(("RUPRI", 10), ("EGPSD", 4)),
            n_gaps=2, behav=[["draught_change_unexplained", None, 0, 0, 1, None]],
            spoof_counts={"on_land": 50, "impossible_speed": 1})}, [uk, ofac]),
        _dossier(314000001, "BB", {"2024-11-01_2024-11-30": _window(nov, names=(("LEGACY", 30),))},
                 [ofac, ofac]),  # the same record matched twice must count once
        _dossier(219000001, "DK", {"2024-06-01_2024-06-30": _window(june)}, [], imo="9000001"),
    ]
    [v] = ev.shadow_fleet(dossiers)
    assert v["imo"] == "9339337"
    assert v["flags"] == ["GA", "BB"]  # in order of first sighting
    assert v["mmsi"] == [626395000, 314000001] and v["dossier"] == 626395000  # most hours first
    assert v["names"] == ["NS LOTUS", "LEGACY"] and v["name"] == "NS LOTUS"
    assert (v["first"], v["last"], v["days"]) == ("2024-06-17", "2024-11-04", 3)
    assert v["windows"] == ["2024-06-01_2024-06-30", "2024-11-01_2024-11-30"]
    assert v["designations"] == [["uk", "2024-07-31", "russia"], ["ofac", "2025-01-10", "russia"]]
    assert v["designated"] == "2024-07-31"
    assert v["lead_days"] == (date(2024, 7, 31) - date(2024, 6, 17)).days
    assert v["events"] == {"gaps": 2, "sts": 0, "draught": 1, "dest": 0, "spoof": 1}  # no on_land
    assert v["destinations"] == ["RUPRI", "EGPSD"]


def test_shadow_fleet_skips_vessels_never_heard_and_marks_prior_designations():
    heard = [0.0] * 30
    heard[0] = 1.0
    old = ["ofac", "YAZ", "Russia", "RUSSIA-EO14024", "2019-09-26"]
    fleet = ev.shadow_fleet([
        _dossier(273342890, "RU", {"2024-06-01_2024-06-30": _window(heard)}, [old], imo="9735323"),
        _dossier(273000002, "RU", {"2024-06-01_2024-06-30": _window([0.0] * 30)}, [old],
                 imo="9000002"),
    ])
    assert [v["imo"] for v in fleet] == ["9735323"]
    assert fleet[0]["lead_days"] < 0  # already sanctioned when first seen
