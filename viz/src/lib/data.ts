/**
 * Types and loaders for the static data exported by `python -m report.export_viz`
 * (see report/export_viz.py for how every field is built).
 */

export const STATUS_NONE = 0;
export const STATUS_LISTED_LATER = 1;
export const STATUS_LISTED_BY_END = 2;

export type BBox = [number, number, number, number];

export interface WindowSummary {
  id: string;
  start: string;
  end: string;
  t0: string;
  n_vessels: number;
  n_focus: number;
  n_trips: number;
  n_points: number;
  counts: { gaps: number; sts: number; spoof: number; behav: number };
  density_px: [number, number];
  density_cells: number;
  n_dossiers: number;
}

export interface Meta {
  built_at: string;
  git_sha: string;
  sanctions_snapshot: string;
  bbox: BBox;
  windows: WindowSummary[];
  n_index: number;
  n_dossiers: number;
}

/** [mmsi, name, ship_type, iso2, status, length] */
export type FleetVessel = [number, string | null, string | null, string | null, number, number | null];

export interface Fleet {
  window: string;
  t0: string;
  step: number;
  bbox: BBox;
  n: number;
  vessels: FleetVessel[];
  /** [vessel index, point offset, point count] */
  trips: [number, number, number][];
}

export interface Tracks {
  /** interleaved lon, lat */
  positions: Float32Array;
  /** minutes since t0 */
  times: Float32Array;
  startIndices: Uint32Array;
  /** per trip */
  tripVessel: Uint32Array;
  tripStart: Float32Array;
  tripEnd: Float32Array;
}

export interface Events {
  codes: { gap: string[]; spoof: string[]; behav: string[] };
  /** [mmsi, tStart, tEnd, lon1, lat1, lon2, lat2, probability, verdictIdx, hours] */
  gaps: [number, number, number, number, number, number, number, number, number, number][];
  /** [mmsiA, mmsiB, tStart, tEnd, lon, lat, confidence, hours] */
  sts: [number, number, number, number, number, number, number, number][];
  /** [mmsi, kindIdx, t, lon, lat, confidence] */
  spoof: [number, number, number, number, number, number][];
  behav: [number, number, number, number, number, number][];
  /** mmsi -> [name, iso2, ship_type, status] */
  names: Record<string, [string | null, string | null, string | null, number]>;
}

/** [value, count, first ISO, last ISO] */
export type Sighting = [string, number, string, string];

export interface DossierWindow {
  status: number;
  designated: string | null;
  messages: number | null;
  voyages: number | null;
  observed_days: number | null;
  draught: [number | null, number | null] | null;
  names: Sighting[];
  callsigns: Sighting[];
  destinations: Sighting[];
  tracks: string[];
  last_pos: [number, number, string] | null;
  hours_by_day: number[];
  events: {
    /** [start, end, hours, probability, verdict, lon1, lat1, lon2, lat2] */
    gaps: [string, string, number, number, string, number, number, number, number][];
    n_gaps: number;
    /** [other mmsi, other name, other iso2, other type, start, end, hours, confidence, lon, lat] */
    sts: [number, string | null, string | null, string | null, string, string, number, number, number, number][];
    /** [kind, t, lon, lat, confidence] */
    spoof: [string, string, number, number, number][];
    spoof_counts: Record<string, number>;
    /** [kind, t, lon, lat, confidence, detail] */
    behav: [string, string, number, number, number, string | null][];
    /** [kind, t, field, old, new, shared, related mmsi, detail] */
    identity: [string, string, string | null, string | null, string | null, string | null, number | null, string | null][];
  };
}

/** [source, listed name, listed flag, programme, designation date] */
export type Designation = [string, string | null, string | null, string | null, string | null];

export interface Dossier {
  mmsi: number;
  imo?: string;
  name?: string;
  callsign?: string;
  flag?: string;
  iso2?: string;
  foc?: boolean;
  ship_type?: string;
  length?: number;
  width?: number;
  windows: Record<string, DossierWindow>;
  sanctions: Designation[];
}

/** [mmsi, name, imo, iso2, ship_type, length, listed, dossier, windowsMask] */
export type IndexRow = [number, string | null, string | null, string | null, string | null, number | null, number, number, number];

export interface VesselIndex {
  windows: string[];
  fields: string[];
  rows: IndexRow[];
}

export type SanctionsByImo = Record<string, Designation[]>;

/** One sanctioned vessel seen in the exported windows, grouped by IMO (report.export_viz.shadow_fleet). */
export interface ShadowVessel {
  imo: string | null;
  name: string | null;
  names: string[];
  flags: string[];
  mmsi: number[];
  dossier: number;
  type: string | null;
  length: number | null;
  first: string;
  last: string;
  days: number;
  hours: number;
  windows: string[];
  /** [source, date, regime] */
  designations: [string, string, 'russia' | 'iran' | 'other'][];
  designated: string | null;
  lead_days: number | null;
  events: { gaps: number; sts: number; draught: number; dest: number; spoof: number };
  destinations: string[];
}

export interface Shadow {
  sanctions_snapshot: string | null;
  vessels: ShadowVessel[];
}

export function dataUrl(path: string): string {
  return `${import.meta.env.BASE_URL.replace(/\/$/, '')}/data/${path}`;
}

const cache = new Map<string, Promise<unknown>>();

async function fetchJson<T>(path: string): Promise<T> {
  if (!cache.has(path)) {
    cache.set(
      path,
      fetch(dataUrl(path)).then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${path}`);
        return r.json();
      }),
    );
  }
  return cache.get(path) as Promise<T>;
}

export const loadMeta = () => fetchJson<Meta>('windows.json');
export const loadFleet = (w: string) => fetchJson<Fleet>(`w/${w}/fleet.json`);
export const loadEvents = (w: string) => fetchJson<Events>(`w/${w}/events.json`);
export const loadIndex = () => fetchJson<VesselIndex>('vessels/index.json');
export const loadSanctions = () => fetchJson<SanctionsByImo>('sanctions.json');
export const loadShadow = () => fetchJson<Shadow>('shadow.json');
export const densityUrl = (w: string) => dataUrl(`w/${w}/density.webp`);

export async function loadDossier(mmsi: number | string): Promise<Dossier | null> {
  try {
    return await fetchJson<Dossier>(`vessels/d/${mmsi}.json`);
  } catch {
    cache.delete(`vessels/d/${mmsi}.json`);
    return null;
  }
}

/** Decode tracks.bin (three uint16 blocks: lon, lat, minutes) against the fleet's bbox. */
export async function loadTracks(w: string, fleet: Fleet): Promise<Tracks> {
  const res = await fetch(dataUrl(`w/${w}/tracks.bin`));
  if (!res.ok) throw new Error(`${res.status} tracks.bin`);
  const buf = await res.arrayBuffer();
  const n = fleet.n;
  const lonQ = new Uint16Array(buf, 0, n);
  const latQ = new Uint16Array(buf, n * 2, n);
  const tQ = new Uint16Array(buf, n * 4, n);
  const [west, south, east, north] = fleet.bbox;
  const sx = (east - west) / 65535;
  const sy = (north - south) / 65535;
  const positions = new Float32Array(n * 2);
  const times = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    positions[i * 2] = west + lonQ[i] * sx;
    positions[i * 2 + 1] = south + latQ[i] * sy;
    times[i] = tQ[i];
  }
  const nt = fleet.trips.length;
  const startIndices = new Uint32Array(nt);
  const tripVessel = new Uint32Array(nt);
  const tripStart = new Float32Array(nt);
  const tripEnd = new Float32Array(nt);
  fleet.trips.forEach(([v, o, c], i) => {
    startIndices[i] = o;
    tripVessel[i] = v;
    tripStart[i] = times[o];
    tripEnd[i] = times[o + c - 1];
  });
  return { positions, times, startIndices, tripVessel, tripStart, tripEnd };
}

export function isListed(status: number | undefined | null): boolean {
  return status === STATUS_LISTED_LATER || status === STATUS_LISTED_BY_END;
}
