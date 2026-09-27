"""Live AIS relay: AISStream.io -> a local WebSocket for the web map's live page (``viz/``).

**Why a relay.** AISStream authenticates with an API key sent in the subscription message, so a
browser connecting directly would publish the key to anyone who opens the page. This process
holds the key (read from ``.env``, never logged), keeps the latest state of every vessel it hears
in memory, and re-broadcasts compact updates once per second to any browser connected to
``ws://127.0.0.1:<port>``. Nothing is written to disk.

**AISStream specifics** (``docs/DATA_SOURCES.md``): one subscription message within 3 s of
connecting; up to 3 connections per account; since September 2026 uncompressed connections are
bandwidth-limited and excess messages are dropped, so the upstream connection negotiates
``permessage-deflate`` (``compression="deflate"``, which ``websockets`` also defaults to) --
stated explicitly here so nobody "optimizes" it away.

**Browser protocol.** On connect: ``{"type": "snapshot", "vessels": [...], "stats": {...}}``.
Then every second: ``{"type": "update", "vessels": [changed], "removed": [mmsi], "stats": {...}}``.
Vessel records use short keys (see :func:`compact`). Regions are fixed bounding boxes
(:data:`REGIONS`); every vessel carries the region it was last seen in and the page filters.

Usage::

    python -m ingest.aisstream            # both regions, port 8765
    python -m ingest.aisstream --regions gib --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from process.mid import country_of
from report.countries import iso2_of

logger = logging.getLogger(__name__)

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
ENV_PATH = Path(".env")
ENV_KEY = "AISSTREAM_API_KEY"

# name -> (south, west, north, east). "dk" matches report.export_viz.MAP_BBOX so live and
# archive views frame the same waters; "gib" covers the Strait of Gibraltar, Algeciras Bay and
# Ceuta (task P6-1's area).
REGIONS: dict[str, tuple[float, float, float, float]] = {
    "dk": (53.3, 2.0, 59.3, 19.0),
    "gib": (35.6, -6.4, 36.4, -4.8),
}

MESSAGE_TYPES = [
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
    "ShipStaticData",
    "StaticDataReport",
]

# A vessel not heard for this long is dropped from the live picture.
STALE_AFTER_S = 30 * 60
BROADCAST_EVERY_S = 1.0

# AIS "not available" sentinels (ITU-R M.1371).
SOG_NA = 102.3
COG_NA = 360.0
HEADING_NA = 511


def clean_text(value: object) -> str | None:
    """AIS 6-bit text is padded with '@' and spaces; None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = value.replace("@", " ").strip()
    text = " ".join(text.split())
    return text or None


def ship_category(code: object) -> str | None:
    """AIS ship-type code (0-99) -> the category names the site uses (viz/src/i18n/ui.ts)."""
    if not isinstance(code, int) or code <= 0:
        return None
    if 80 <= code <= 89:
        return "Tanker"
    if 70 <= code <= 79:
        return "Cargo"
    if 60 <= code <= 69:
        return "Passenger"
    if 40 <= code <= 49:
        return "HSC"
    exact = {30: "Fishing", 31: "Tug", 32: "Tug", 52: "Tug", 33: "Dredging", 35: "Military",
             36: "Sailing", 37: "Pleasure", 50: "Pilot", 51: "SAR"}
    return exact.get(code, "Other")


def region_of(lat: float, lon: float) -> str | None:
    for name, (south, west, north, east) in REGIONS.items():
        if south <= lat <= north and west <= lon <= east:
            return name
    return None


def subscription(api_key: str, regions: list[str]) -> dict:
    """The AISStream subscription message. Corners are [lat, lon] pairs."""
    return {
        "APIKey": api_key,
        "BoundingBoxes": [
            [[REGIONS[r][0], REGIONS[r][1]], [REGIONS[r][2], REGIONS[r][3]]] for r in regions
        ],
        "FilterMessageTypes": MESSAGE_TYPES,
    }


def load_api_key(env_path: Path = ENV_PATH) -> str | None:
    """Read AISSTREAM_API_KEY from a dotenv file without echoing it anywhere."""
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == ENV_KEY:
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


def _dims(dim: object) -> tuple[int | None, int | None]:
    if not isinstance(dim, dict):
        return None, None
    a, b, c, d = (dim.get(k) or 0 for k in ("A", "B", "C", "D"))
    length = a + b if a + b > 0 else None
    width = c + d if c + d > 0 else None
    return length, width


def _eta(eta: object) -> str | None:
    if not isinstance(eta, dict):
        return None
    month, day = eta.get("Month") or 0, eta.get("Day") or 0
    hour, minute = eta.get("Hour", 24), eta.get("Minute", 60)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    clock = f" {hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else ""
    return f"{month:02d}-{day:02d}{clock}"


def apply_message(vessels: dict[int, dict], msg: dict, now: float) -> int | None:
    """Fold one AISStream message into ``vessels`` (mmsi -> record). Returns the mmsi whose
    record changed, or None when the message was ignored."""
    kind = msg.get("MessageType")
    body = (msg.get("Message") or {}).get(kind)
    meta = msg.get("MetaData") or {}
    if not isinstance(body, dict):
        return None
    mmsi = body.get("UserID") or meta.get("MMSI")
    if not isinstance(mmsi, int) or not (100_000_000 <= mmsi <= 999_999_999):
        return None

    v = vessels.get(mmsi)
    if v is None:
        v = {"m": mmsi, "f": iso2_of(country_of(mmsi)) if 200_000_000 <= mmsi < 800_000_000 else None}
        vessels[mmsi] = v

    if kind in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        lat, lon = body.get("Latitude"), body.get("Longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        if abs(lat) > 90 or abs(lon) > 180:
            return None
        region = region_of(lat, lon)
        if region is None:
            return None
        sog, cog, heading = body.get("Sog"), body.get("Cog"), body.get("TrueHeading")
        v.update(
            la=round(lat, 5),
            lo=round(lon, 5),
            s=None if sog is None or sog >= SOG_NA else round(sog, 1),
            c=None if cog is None or cog >= COG_NA else round(cog, 1),
            h=None if heading is None or heading >= HEADING_NA else heading,
            t=round(now, 1),
            r=region,
            k="A" if kind == "PositionReport" else "B",
        )
        if kind == "PositionReport":
            nav = body.get("NavigationalStatus")
            v["n"] = nav if isinstance(nav, int) and nav != 15 else None
        if kind == "ExtendedClassBPositionReport":
            v["nm"] = clean_text(body.get("Name")) or v.get("nm")
            v["tc"] = body.get("Type") or v.get("tc")
            v["ty"] = ship_category(v["tc"])
            length, width = _dims(body.get("Dimension"))
            v["L"], v["W"] = length or v.get("L"), width or v.get("W")
    elif kind == "ShipStaticData":
        imo = body.get("ImoNumber")
        v["nm"] = clean_text(body.get("Name")) or v.get("nm")
        v["cs"] = clean_text(body.get("CallSign")) or v.get("cs")
        v["imo"] = str(imo) if isinstance(imo, int) and 1_000_000 <= imo <= 9_999_999 else v.get("imo")
        v["tc"] = body.get("Type") or v.get("tc")
        v["ty"] = ship_category(v["tc"])
        v["d"] = clean_text(body.get("Destination"))
        draught = body.get("MaximumStaticDraught")
        v["dr"] = draught if isinstance(draught, (int, float)) and draught > 0 else None
        v["eta"] = _eta(body.get("Eta"))
        length, width = _dims(body.get("Dimension"))
        v["L"], v["W"] = length or v.get("L"), width or v.get("W")
        v["st"] = round(now, 1)
    elif kind == "StaticDataReport":
        report_a = body.get("ReportA") or {}
        report_b = body.get("ReportB") or {}
        if report_a.get("Valid"):
            v["nm"] = clean_text(report_a.get("Name")) or v.get("nm")
        if report_b.get("Valid"):
            v["cs"] = clean_text(report_b.get("CallSign")) or v.get("cs")
            v["tc"] = report_b.get("ShipType") or v.get("tc")
            v["ty"] = ship_category(v["tc"])
            length, width = _dims(report_b.get("Dimension"))
            v["L"], v["W"] = length or v.get("L"), width or v.get("W")
        v["st"] = round(now, 1)
    else:
        return None
    if not v.get("nm"):
        v["nm"] = clean_text(meta.get("ShipName"))
    return mmsi


def compact(record: dict) -> dict:
    """Drop empty fields before sending a record to the browser."""
    return {k: val for k, val in record.items() if val is not None}


def prune(vessels: dict[int, dict], now: float) -> list[int]:
    """Remove vessels without a position, or not heard for STALE_AFTER_S. Returns removed mmsi."""
    stale = [m for m, v in vessels.items() if now - v.get("t", v.get("st", now)) > STALE_AFTER_S]
    for m in stale:
        del vessels[m]
    return stale


class Relay:
    def __init__(self, api_key: str, regions: list[str]) -> None:
        self.api_key = api_key
        self.regions = regions
        self.vessels: dict[int, dict] = {}
        self.dirty: set[int] = set()
        self.clients: set = set()
        self.status = "connecting"
        self.error: str | None = None
        self.received = 0
        self._rate_window: list[tuple[float, int]] = []

    def stats(self) -> dict:
        now = time.time()
        self._rate_window = [(t, n) for t, n in self._rate_window if now - t <= 10]
        self._rate_window.append((now, self.received))
        oldest_t, oldest_n = self._rate_window[0]
        rate = (self.received - oldest_n) / (now - oldest_t) if now > oldest_t else 0.0
        positioned = sum(1 for v in self.vessels.values() if "la" in v)
        return {
            "upstream": self.status,
            "error": self.error,
            "vessels": positioned,
            "msgs_per_s": round(rate, 1),
            "regions": self.regions,
            "server_time": round(now, 1),
        }

    def _positioned(self) -> list[dict]:
        return [compact(v) for v in self.vessels.values() if "la" in v]

    async def upstream(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        sub = json.dumps(subscription(self.api_key, self.regions))
        async for ws in connect(AISSTREAM_URL, compression="deflate", open_timeout=15,
                                max_size=2**21):
            try:
                await ws.send(sub)
                self.status = "live"
                self.error = None
                logger.info("Upstream connected, regions %s", self.regions)
                async for raw in ws:
                    msg = json.loads(raw)
                    if "error" in msg:
                        # e.g. an invalid key; AISStream closes the connection right after.
                        self.error = str(msg["error"])[:200]
                        logger.error("AISStream error: %s", self.error)
                        continue
                    self.received += 1
                    mmsi = apply_message(self.vessels, msg, time.time())
                    if mmsi is not None:
                        self.dirty.add(mmsi)
            except ConnectionClosed:
                self.status = "reconnecting"
                logger.warning("Upstream closed, reconnecting")
                continue

    async def ticker(self) -> None:
        from websockets.asyncio.server import broadcast

        while True:
            await asyncio.sleep(BROADCAST_EVERY_S)
            removed = prune(self.vessels, time.time())
            changed = [compact(self.vessels[m]) for m in self.dirty if m in self.vessels
                       and "la" in self.vessels[m]]
            self.dirty.clear()
            if self.clients:
                broadcast(
                    self.clients,
                    json.dumps({"type": "update", "vessels": changed, "removed": removed,
                                "stats": self.stats()}, separators=(",", ":")),
                )

    async def handler(self, ws) -> None:
        self.clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "snapshot", "vessels": self._positioned(),
                                      "stats": self.stats()}, separators=(",", ":")))
            async for _ in ws:  # the page never sends anything that matters
                pass
        finally:
            self.clients.discard(ws)


async def run(api_key: str, regions: list[str], host: str, port: int) -> None:
    from websockets.asyncio.server import serve

    relay = Relay(api_key, regions)
    async with serve(relay.handler, host, port, compression="deflate"):
        logger.info("Relay listening on ws://%s:%d -- open the site's live page", host, port)
        await asyncio.gather(relay.upstream(), relay.ticker())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relay live AIS from AISStream to the web map.")
    parser.add_argument("--regions", nargs="+", default=list(REGIONS), choices=list(REGIONS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    api_key = load_api_key()
    if not api_key:
        raise SystemExit(
            f"No {ENV_KEY} in {ENV_PATH.resolve()}. Create a free key at https://aisstream.io "
            f"(sign in with GitHub -> API Keys) and add a line {ENV_KEY}=<your key>."
        )
    try:
        asyncio.run(run(api_key, args.regions, args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
