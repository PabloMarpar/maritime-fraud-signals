<script module lang="ts">
  import type { Glyph } from '../lib/icons';

  export interface MapEvent {
    id: string;
    lon: number;
    lat: number;
    glyph: Glyph;
    /** gaps are drawn as a dashed segment to lon2/lat2 */
    lon2?: number;
    lat2?: number;
    label: string;
  }
</script>

<script lang="ts">
  import { onMount } from 'svelte';
  import type { Map as MLMap } from 'maplibre-gl';
  import { MapboxOverlay } from '@deck.gl/mapbox';
  import { IconLayer, PathLayer, ScatterplotLayer } from '@deck.gl/layers';
  import { PathStyleExtension } from '@deck.gl/extensions';
  import { createBaseMap } from '../lib/basemap';
  import { RGB, withAlpha } from '../lib/colors';
  import { iconAtlas } from '../lib/icons';

  interface Props {
    paths: [number, number][][];
    events: MapEvent[];
    lastPos: [number, number] | null;
    listed: boolean;
    focus?: string | null;
    onpick?: (id: string) => void;
  }

  let { paths, events, lastPos, listed, focus = null, onpick }: Props = $props();

  let container: HTMLDivElement;
  let map = $state.raw<MLMap | null>(null);
  let overlay: MapboxOverlay | null = null;
  let tip = $state<{ x: number; y: number; text: string } | null>(null);

  function bounds(): [[number, number], [number, number]] | null {
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    const add = (x: number, y: number) => {
      if (x < w) w = x;
      if (x > e) e = x;
      if (y < s) s = y;
      if (y > n) n = y;
    };
    paths.forEach((p) => p.forEach(([x, y]) => add(x, y)));
    if (lastPos) add(lastPos[0], lastPos[1]);
    if (!Number.isFinite(w)) return null;
    return [
      [w, s],
      [e, n],
    ];
  }

  function render() {
    if (!overlay) return;
    const atlas = iconAtlas();
    const color = listed ? RGB.listed : RGB.vessel;
    const gaps = events.filter((e) => e.lon2 !== undefined);
    const points = events.filter((e) => e.lon2 === undefined);
    overlay.setProps({
      layers: [
        new PathLayer({
          id: 'track-glow',
          data: paths,
          getPath: (d: any) => d,
          getColor: withAlpha(color, 0.18),
          widthUnits: 'pixels',
          getWidth: 7,
          capRounded: true,
          jointRounded: true,
        }),
        new PathLayer({
          id: 'track',
          data: paths,
          getPath: (d: any) => d,
          getColor: withAlpha(color, 1),
          widthUnits: 'pixels',
          getWidth: 2,
          capRounded: true,
          jointRounded: true,
        }),
        new PathLayer({
          id: 'gaps',
          data: gaps,
          getPath: (d: MapEvent) => [
            [d.lon, d.lat],
            [d.lon2!, d.lat2!],
          ],
          getColor: (d: MapEvent) => withAlpha(RGB.event, d.id === focus ? 1 : 0.7),
          widthUnits: 'pixels',
          getWidth: (d: MapEvent) => (d.id === focus ? 2.5 : 1.4),
          getDashArray: [3, 3],
          dashJustified: true,
          extensions: [new PathStyleExtension({ dash: true })],
          pickable: true,
          updateTriggers: { getColor: [focus], getWidth: [focus] },
        }),
        new ScatterplotLayer({
          id: 'focus-ring',
          data: points.filter((p) => p.id === focus),
          getPosition: (d: MapEvent) => [d.lon, d.lat],
          getRadius: 16,
          radiusUnits: 'pixels',
          stroked: true,
          filled: false,
          getLineColor: [238, 242, 247, 230],
          lineWidthUnits: 'pixels',
          getLineWidth: 1.5,
        }),
        new IconLayer({
          id: 'events',
          data: points,
          iconAtlas: atlas.url,
          iconMapping: atlas.mapping,
          getIcon: (d: MapEvent) => d.glyph,
          getPosition: (d: MapEvent) => [d.lon, d.lat],
          getSize: (d: MapEvent) => (d.id === focus ? 22 : d.glyph === 'ring' ? 20 : 14),
          sizeUnits: 'pixels',
          getColor: withAlpha(RGB.event, 1),
          pickable: true,
          updateTriggers: { getSize: [focus] },
        }),
        new ScatterplotLayer({
          id: 'last',
          data: lastPos ? [lastPos] : [],
          getPosition: (d: any) => d,
          getRadius: 6,
          radiusUnits: 'pixels',
          getFillColor: [238, 242, 247, 255],
          stroked: true,
          getLineColor: withAlpha(color, 1),
          lineWidthUnits: 'pixels',
          getLineWidth: 3,
        }),
      ],
    });
  }

  $effect(() => {
    void [paths, events, lastPos, listed, focus, map];
    render();
  });

  $effect(() => {
    const f = focus;
    if (!map || !f) return;
    const e = events.find((x) => x.id === f);
    if (e) map.flyTo({ center: [e.lon, e.lat], zoom: Math.max(map.getZoom(), 8), duration: 900 });
  });

  $effect(() => {
    void paths;
    const b = bounds();
    if (map && b) map.fitBounds(b, { padding: 40, maxZoom: 10, duration: 0 });
  });

  onMount(() => {
    let destroyed = false;
    (async () => {
      const b = bounds();
      map = await createBaseMap({
        container,
        bounds: b ? [b[0][0], b[0][1], b[1][0], b[1][1]] : undefined,
        navigation: true,
      });
      if (destroyed) return;
      map.scrollZoom.disable();
      overlay = new MapboxOverlay({
        interleaved: true,
        onHover: (info: any) => {
          container.style.cursor = info.object ? 'pointer' : '';
          tip = info.object && info.object.label ? { x: info.x, y: info.y, text: info.object.label } : null;
        },
        onClick: (info: any) => {
          if (info.object?.id) onpick?.(info.object.id);
        },
      });
      map.addControl(overlay as any);
      render();
    })();
    return () => {
      destroyed = true;
      overlay?.finalize();
      map?.remove();
    };
  });
</script>

<div class="track-map">
  <div class="map" bind:this={container}></div>
  {#if tip}
    <div class="tip glass" style:left={`${tip.x}px`} style:top={`${tip.y}px`}>{tip.text}</div>
  {/if}
</div>

<style>
  .track-map {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 320px;
    border-radius: var(--radius-l);
    overflow: hidden;
    border: 1px solid var(--line);
    background: #050b15;
  }

  .map {
    position: absolute;
    inset: 0;
  }

  .tip {
    position: absolute;
    transform: translate(12px, -50%);
    padding: 6px 10px;
    font-size: 12px;
    pointer-events: none;
    z-index: 5;
    border-radius: 8px;
    white-space: nowrap;
  }
</style>
