import maplibregl, { type Map as MLMap, type StyleSpecification } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

const STYLE_URL = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';

// Land sits clearly above the sea (author's call 2026-09-28: the first navy land was too close to
// the water to tell apart); the sea stays near-black so tracks and density read on it.
export const PALETTE = {
  water: '#050b15',
  land: '#1c2839',
  landAlt: '#1a2536',
  border: 'rgba(160, 180, 210, 0.38)',
  label: '#8d9ab1',
  labelStrong: '#c3cddc',
  halo: '#050b15',
  road: 'rgba(120, 135, 160, 0.35)',
};

/** The first symbol layer id: raster/data layers are inserted before it so labels stay on top. */
export const LABELS_BEFORE = 'waterway_label';

const stylePromises = new Map<string, Promise<StyleSpecification>>();

/** CARTO Dark Matter, recoloured to a navy night palette, stripped of POIs, and labelled in the
 * page language (the tiles carry name:es / name:en; street names keep their local form). */
export function nauticalStyle(lang: 'en' | 'es' = 'en'): Promise<StyleSpecification> {
  if (!stylePromises.has(lang)) {
    const localized = ['coalesce', ['get', `name:${lang}`], ['get', 'name_en'], ['get', 'name']];
    stylePromises.set(lang, fetch(STYLE_URL)
      .then((r) => r.json())
      .then((style: StyleSpecification) => {
        style.layers = style.layers
          .filter((l) => !l.id.startsWith('poi_') && l.id !== 'housenumber' && !l.id.startsWith('building'))
          .map((layer) => {
            const l = structuredClone(layer) as any;
            const id: string = l.id;
            l.paint = l.paint ?? {};
            if (l.type === 'background') l.paint['background-color'] = PALETTE.land;
            else if (id === 'water') l.paint['fill-color'] = PALETTE.water;
            else if (id === 'water_shadow') l.paint['fill-color'] = 'transparent';
            else if (['landcover', 'landuse', 'park_national_park', 'park_nature_reserve'].includes(id))
              l.paint['fill-color'] = PALETTE.landAlt;
            else if (id === 'landuse_residential') l.paint['fill-color'] = 'rgba(255,255,255,0.015)';
            else if (id === 'waterway') l.paint['line-color'] = '#0b1a2e';
            else if (id.startsWith('boundary_country')) {
              l.paint['line-color'] = PALETTE.border;
            } else if (id.startsWith('boundary_')) l.paint['line-color'] = 'rgba(140,160,190,0.12)';
            else if (l.type === 'line' && l['source-layer'] === 'transportation') {
              l.paint['line-color'] = PALETTE.road;
              l.paint['line-opacity'] = 0.5;
            } else if (l.type === 'symbol') {
              l.paint['text-color'] = id.startsWith('watername')
                ? '#3f5575'
                : id.includes('country') || id.includes('city_r') || id.includes('capital')
                  ? PALETTE.labelStrong
                  : PALETTE.label;
              l.paint['text-halo-color'] = PALETTE.halo;
              l.paint['text-halo-width'] = 1.2;
              if (!id.startsWith('roadname') && id !== 'housenumber' && l.layout?.['text-field']) {
                l.layout['text-field'] = localized;
              }
            }
            return l;
          });
        return style;
      }));
  }
  return stylePromises.get(lang)!;
}

export interface BaseMapOptions {
  container: HTMLElement;
  lang?: 'en' | 'es';
  center?: [number, number];
  zoom?: number;
  bounds?: [number, number, number, number];
  interactive?: boolean;
  attribution?: boolean;
  navigation?: boolean;
  minZoom?: number;
  maxZoom?: number;
}

export async function createBaseMap(opts: BaseMapOptions): Promise<MLMap> {
  const style = await nauticalStyle(opts.lang);
  const map = new maplibregl.Map({
    container: opts.container,
    style,
    center: opts.center ?? [11.2, 56.2],
    zoom: opts.zoom ?? 6,
    bounds: opts.bounds,
    fitBoundsOptions: { padding: 24 },
    interactive: opts.interactive ?? true,
    attributionControl: false,
    dragRotate: false,
    pitchWithRotate: false,
    minZoom: opts.minZoom ?? 3,
    maxZoom: opts.maxZoom ?? 14,
    fadeDuration: 0,
  });
  map.touchZoomRotate.disableRotation();
  if (opts.attribution ?? true) {
    map.addControl(
      new maplibregl.AttributionControl({
        compact: true,
        customAttribution: 'AIS: Danish Maritime Authority',
      }),
      'bottom-right',
    );
  }
  if (opts.navigation) {
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  }
  await new Promise<void>((resolve) => {
    if (map.loaded()) resolve();
    else map.once('load', () => resolve());
  });
  return map;
}

/** Add (or replace) the whole-month traffic density raster under the labels. */
export function setDensity(
  map: MLMap,
  url: string | null,
  bbox: [number, number, number, number],
  opacity = 0.7,
): void {
  const id = 'density';
  if (map.getLayer(id)) map.removeLayer(id);
  if (map.getSource(id)) map.removeSource(id);
  if (!url) return;
  const [w, s, e, n] = bbox;
  map.addSource(id, {
    type: 'image',
    url,
    coordinates: [
      [w, n],
      [e, n],
      [e, s],
      [w, s],
    ],
  });
  map.addLayer(
    {
      id,
      type: 'raster',
      source: id,
      paint: { 'raster-opacity': opacity, 'raster-fade-duration': 0, 'raster-resampling': 'linear' },
    },
    map.getLayer(LABELS_BEFORE) ? LABELS_BEFORE : undefined,
  );
}

export function setLayerVisible(map: MLMap, id: string, visible: boolean): void {
  if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
}
