"""Unit tests for ingest.aisstream's message handling. Synthetic messages in AISStream's JSON
shape; no network, no API key."""

from __future__ import annotations

from ingest import aisstream as ais


def _position(mmsi=219000001, lat=55.5, lon=11.0, kind="PositionReport", **extra):
    body = {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": 11.2, "Cog": 45.0,
            "TrueHeading": 44, "NavigationalStatus": 0}
    body.update(extra)
    return {"MessageType": kind, "Message": {kind: body},
            "MetaData": {"MMSI": mmsi, "ShipName": "TEST SHIP@@@"}}


def _static(mmsi=219000001, **extra):
    body = {"UserID": mmsi, "Name": "NORDIC  STAR@@@@", "CallSign": "OXAB2@", "ImoNumber": 9321483,
            "Type": 84, "Destination": "PRIMORSK@@@@", "MaximumStaticDraught": 14.2,
            "Dimension": {"A": 200, "B": 50, "C": 22, "D": 22},
            "Eta": {"Month": 7, "Day": 3, "Hour": 14, "Minute": 30}}
    body.update(extra)
    return {"MessageType": "ShipStaticData", "Message": {"ShipStaticData": body}, "MetaData": {}}


def test_clean_text_strips_ais_padding():
    assert ais.clean_text("NORDIC  STAR@@@@") == "NORDIC STAR"
    assert ais.clean_text("@@@@") is None
    assert ais.clean_text(None) is None


def test_ship_category():
    assert ais.ship_category(84) == "Tanker"
    assert ais.ship_category(70) == "Cargo"
    assert ais.ship_category(37) == "Pleasure"
    assert ais.ship_category(52) == "Tug"
    assert ais.ship_category(99) == "Other"
    assert ais.ship_category(0) is None


def test_region_of():
    assert ais.region_of(55.5, 11.0) == "dk"
    assert ais.region_of(36.0, -5.5) == "gib"
    assert ais.region_of(0.0, 0.0) is None


def test_subscription_uses_lat_lon_corners_and_all_regions():
    sub = ais.subscription("k", ["dk", "gib"])
    assert sub["APIKey"] == "k"
    assert sub["BoundingBoxes"][0] == [[53.3, 2.0], [59.3, 19.0]]
    assert len(sub["BoundingBoxes"]) == 2
    assert "ShipStaticData" in sub["FilterMessageTypes"]


def test_position_then_static_build_one_record():
    vessels: dict = {}
    assert ais.apply_message(vessels, _position(), now=1000.0) == 219000001
    v = vessels[219000001]
    assert (v["la"], v["lo"], v["s"], v["c"], v["h"], v["n"], v["r"]) == (55.5, 11.0, 11.2, 45.0, 44, 0, "dk")
    assert v["f"] == "DK"  # MID 219 is Denmark
    assert v["nm"] == "TEST SHIP"  # from MetaData until static data arrives
    ais.apply_message(vessels, _static(), now=1001.0)
    assert v["nm"] == "NORDIC STAR"
    assert (v["imo"], v["ty"], v["d"], v["dr"], v["L"], v["W"], v["eta"]) == (
        "9321483", "Tanker", "PRIMORSK", 14.2, 250, 44, "07-03 14:30")


def test_not_available_sentinels_become_none():
    vessels: dict = {}
    ais.apply_message(vessels, _position(Sog=102.3, Cog=360.0, TrueHeading=511,
                                         NavigationalStatus=15), now=1.0)
    v = vessels[219000001]
    assert v["s"] is None and v["c"] is None and v["h"] is None and v["n"] is None


def test_position_outside_regions_or_invalid_is_ignored():
    vessels: dict = {}
    assert ais.apply_message(vessels, _position(lat=10.0, lon=10.0), now=1.0) is None
    assert ais.apply_message(vessels, _position(lat=91.0, lon=181.0), now=1.0) is None
    assert ais.apply_message(vessels, {"MessageType": "Unknown", "Message": {}}, now=1.0) is None


def test_invalid_imo_is_not_kept():
    vessels: dict = {}
    ais.apply_message(vessels, _position(), now=1.0)
    ais.apply_message(vessels, _static(ImoNumber=0), now=2.0)
    assert vessels[219000001]["imo"] is None


def test_class_b_static_report():
    vessels: dict = {}
    ais.apply_message(vessels, _position(kind="StandardClassBPositionReport"), now=1.0)
    msg = {"MessageType": "StaticDataReport", "Message": {"StaticDataReport": {
        "UserID": 219000001,
        "ReportA": {"Valid": True, "Name": "SEA BREEZE@@"},
        "ReportB": {"Valid": True, "CallSign": "OU1234", "ShipType": 37,
                    "Dimension": {"A": 8, "B": 4, "C": 2, "D": 2}}}}}
    ais.apply_message(vessels, msg, now=2.0)
    v = vessels[219000001]
    assert (v["k"], v["nm"], v["cs"], v["ty"], v["L"]) == ("B", "SEA BREEZE", "OU1234", "Pleasure", 12)


def test_prune_drops_stale_vessels():
    vessels: dict = {}
    ais.apply_message(vessels, _position(mmsi=219000001), now=0.0)
    ais.apply_message(vessels, _position(mmsi=219000002), now=ais.STALE_AFTER_S - 10)
    assert ais.prune(vessels, now=ais.STALE_AFTER_S + 1) == [219000001]
    assert list(vessels) == [219000002]


def test_compact_drops_empty_fields():
    assert ais.compact({"m": 1, "nm": None, "s": 0.0}) == {"m": 1, "s": 0.0}


def test_load_api_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GFW_API_TOKEN=abc\n# comment\nAISSTREAM_API_KEY = 'xyz123'\n", encoding="utf-8")
    assert ais.load_api_key(env) == "xyz123"
    env.write_text("AISSTREAM_API_KEY=\n", encoding="utf-8")
    assert ais.load_api_key(env) is None
    assert ais.load_api_key(tmp_path / "missing.env") is None
