// AISStream message handling for the live relay: a port of ingest/aisstream.py's pure functions.
//
// The Python module is the reference (and what runs locally); this file must behave the same so
// the page sees one protocol whichever relay it talks to. relay/test/ais.test.ts mirrors
// tests/test_aisstream.py, and tests/test_relay_tables.py checks mid_iso2.json against
// process.mid + report.countries.

import MID_ISO2 from './mid_iso2.json' with { type: 'json' };

/** name -> [south, west, north, east]. Same boxes as ingest/aisstream.py's REGIONS. */
export const REGIONS: Record<string, [number, number, number, number]> = {
  dk: [53.3, 2.0, 59.3, 19.0],
  gib: [35.6, -6.4, 36.4, -4.8],
};

export const MESSAGE_TYPES = [
  'PositionReport',
  'StandardClassBPositionReport',
  'ExtendedClassBPositionReport',
  'ShipStaticData',
  'StaticDataReport',
];

/** A vessel not heard for this long is dropped from the live picture. */
export const STALE_AFTER_S = 30 * 60;

// AIS "not available" sentinels (ITU-R M.1371).
const SOG_NA = 102.3;
const COG_NA = 360.0;
const HEADING_NA = 511;

/** One vessel as the page receives it (short keys; see ingest/aisstream.py's apply_message). */
export interface Vessel {
  m: number;
  f?: string | null;
  la?: number;
  lo?: number;
  s?: number | null;
  c?: number | null;
  h?: number | null;
  n?: number | null;
  t?: number;
  r?: string;
  k?: 'A' | 'B';
  nm?: string | null;
  cs?: string | null;
  imo?: string | null;
  tc?: number | null;
  ty?: string | null;
  d?: string | null;
  dr?: number | null;
  eta?: string | null;
  L?: number | null;
  W?: number | null;
  st?: number;
}

type Json = Record<string, any>;

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);
const isInt = (x: unknown): x is number => Number.isInteger(x);
const round = (x: number, digits: number) => {
  const p = 10 ** digits;
  return Math.round(x * p) / p;
};

/** AIS 6-bit text is padded with '@' and spaces; null when nothing is left. */
export function cleanText(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.replaceAll('@', ' ').trim().split(/\s+/).filter(Boolean).join(' ');
  return text || null;
}

/** AIS ship-type code (0-99) -> the category names the site uses (viz/src/i18n/ui.ts). */
export function shipCategory(code: unknown): string | null {
  if (!isInt(code) || code <= 0) return null;
  if (code >= 80 && code <= 89) return 'Tanker';
  if (code >= 70 && code <= 79) return 'Cargo';
  if (code >= 60 && code <= 69) return 'Passenger';
  if (code >= 40 && code <= 49) return 'HSC';
  const exact: Record<number, string> = {
    30: 'Fishing', 31: 'Tug', 32: 'Tug', 52: 'Tug', 33: 'Dredging', 35: 'Military',
    36: 'Sailing', 37: 'Pleasure', 50: 'Pilot', 51: 'SAR',
  };
  return exact[code] ?? 'Other';
}

export function regionOf(lat: number, lon: number): string | null {
  for (const [name, [south, west, north, east]] of Object.entries(REGIONS)) {
    if (south <= lat && lat <= north && west <= lon && lon <= east) return name;
  }
  return null;
}

/** The AISStream subscription message. Corners are [lat, lon] pairs. */
export function subscription(apiKey: string, regions: string[]): Json {
  return {
    APIKey: apiKey,
    BoundingBoxes: regions.map((r) => [
      [REGIONS[r][0], REGIONS[r][1]],
      [REGIONS[r][2], REGIONS[r][3]],
    ]),
    FilterMessageTypes: MESSAGE_TYPES,
  };
}

/** ISO 3166 alpha-2 of an MMSI's flag state, for ship-station MMSIs only. */
export function flagOf(mmsi: number): string | null {
  if (mmsi < 200_000_000 || mmsi >= 800_000_000) return null;
  return (MID_ISO2 as Record<string, string>)[String(Math.floor(mmsi / 1_000_000))] ?? null;
}

function dims(dim: unknown): [number | null, number | null] {
  if (!dim || typeof dim !== 'object') return [null, null];
  const d = dim as Json;
  const [a, b, c, e] = ['A', 'B', 'C', 'D'].map((k) => d[k] || 0);
  return [a + b > 0 ? a + b : null, c + e > 0 ? c + e : null];
}

function etaOf(eta: unknown): string | null {
  if (!eta || typeof eta !== 'object') return null;
  const e = eta as Json;
  const month = e.Month || 0;
  const day = e.Day || 0;
  const hour = e.Hour ?? 24;
  const minute = e.Minute ?? 60;
  if (!(month >= 1 && month <= 12 && day >= 1 && day <= 31)) return null;
  const p = (x: number) => String(x).padStart(2, '0');
  const clock = hour < 24 && minute < 60 ? ` ${p(hour)}:${p(minute)}` : '';
  return `${p(month)}-${p(day)}${clock}`;
}

/** Fold one AISStream message into `vessels` (mmsi -> record). Returns the mmsi whose record
 * changed, or null when the message was ignored. `now` is in seconds. */
export function applyMessage(vessels: Map<number, Vessel>, msg: Json, now: number): number | null {
  const kind = msg.MessageType;
  const body = (msg.Message ?? {})[kind];
  const meta = msg.MetaData ?? {};
  if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
  const mmsi = body.UserID || meta.MMSI;
  if (!isInt(mmsi) || mmsi < 100_000_000 || mmsi > 999_999_999) return null;

  let v = vessels.get(mmsi);
  if (!v) {
    v = { m: mmsi, f: flagOf(mmsi) };
    vessels.set(mmsi, v);
  }

  if (kind === 'PositionReport' || kind === 'StandardClassBPositionReport' || kind === 'ExtendedClassBPositionReport') {
    const lat = body.Latitude;
    const lon = body.Longitude;
    if (!isNum(lat) || !isNum(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
    const region = regionOf(lat, lon);
    if (region === null) return null;
    const sog = body.Sog;
    const cog = body.Cog;
    const heading = body.TrueHeading;
    Object.assign(v, {
      la: round(lat, 5),
      lo: round(lon, 5),
      s: sog == null || sog >= SOG_NA ? null : round(sog, 1),
      c: cog == null || cog >= COG_NA ? null : round(cog, 1),
      h: heading == null || heading >= HEADING_NA ? null : heading,
      t: round(now, 1),
      r: region,
      k: kind === 'PositionReport' ? 'A' : 'B',
    });
    if (kind === 'PositionReport') {
      const nav = body.NavigationalStatus;
      v.n = isInt(nav) && nav !== 15 ? nav : null;
    }
    if (kind === 'ExtendedClassBPositionReport') {
      v.nm = cleanText(body.Name) ?? v.nm;
      v.tc = body.Type || v.tc;
      v.ty = shipCategory(v.tc);
      const [length, width] = dims(body.Dimension);
      v.L = length || v.L;
      v.W = width || v.W;
    }
  } else if (kind === 'ShipStaticData') {
    const imo = body.ImoNumber;
    v.nm = cleanText(body.Name) ?? v.nm;
    v.cs = cleanText(body.CallSign) ?? v.cs;
    v.imo = isInt(imo) && imo >= 1_000_000 && imo <= 9_999_999 ? String(imo) : v.imo;
    v.tc = body.Type || v.tc;
    v.ty = shipCategory(v.tc);
    v.d = cleanText(body.Destination);
    const draught = body.MaximumStaticDraught;
    v.dr = isNum(draught) && draught > 0 ? draught : null;
    v.eta = etaOf(body.Eta);
    const [length, width] = dims(body.Dimension);
    v.L = length || v.L;
    v.W = width || v.W;
    v.st = round(now, 1);
  } else if (kind === 'StaticDataReport') {
    const a = body.ReportA ?? {};
    const b = body.ReportB ?? {};
    if (a.Valid) v.nm = cleanText(a.Name) ?? v.nm;
    if (b.Valid) {
      v.cs = cleanText(b.CallSign) ?? v.cs;
      v.tc = b.ShipType || v.tc;
      v.ty = shipCategory(v.tc);
      const [length, width] = dims(b.Dimension);
      v.L = length || v.L;
      v.W = width || v.W;
    }
    v.st = round(now, 1);
  } else {
    return null;
  }
  if (!v.nm) v.nm = cleanText(meta.ShipName);
  return mmsi;
}

/** Drop empty fields before sending a record to the browser. */
export function compact(record: Vessel): Vessel {
  return Object.fromEntries(Object.entries(record).filter(([, val]) => val != null)) as Vessel;
}

/** Remove vessels not heard for STALE_AFTER_S. Returns the removed mmsi. */
export function prune(vessels: Map<number, Vessel>, now: number): number[] {
  const stale: number[] = [];
  for (const [m, v] of vessels) {
    if (now - (v.t ?? v.st ?? now) > STALE_AFTER_S) stale.push(m);
  }
  for (const m of stale) vessels.delete(m);
  return stale;
}
