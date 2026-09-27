<script lang="ts">
  import { onMount } from 'svelte';
  import type { Map as MLMap } from 'maplibre-gl';
  import { MapboxOverlay } from '@deck.gl/mapbox';
  import { TripsLayer } from '@deck.gl/geo-layers';
  import { IconLayer, PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
  import { DataFilterExtension, PathStyleExtension } from '@deck.gl/extensions';
  import { createBaseMap, setDensity, setLayerVisible } from '../lib/basemap';
  import {
    densityUrl,
    isListed,
    loadEvents,
    loadFleet,
    loadMeta,
    loadTracks,
    type Events,
    type Fleet,
    type Meta,
    type Tracks,
  } from '../lib/data';
  import { RGB, withAlpha } from '../lib/colors';
  import { glyphSvg, iconAtlas } from '../lib/icons';
  import { countryName, flagClass, fmtDateTime, fmtMonth, kindLabel, num, shipTypeLabel, title } from '../lib/format';
  import { useT, type Lang } from '../i18n/ui';
  import Timeline from './Timeline.svelte';
  import VesselPanel from './VesselPanel.svelte';

  let { lang }: { lang: Lang } = $props();
  const tr = $derived(useT(lang));

  const FADE = 720; // an event stays on the map for 12 h after it happens
  const TRAIL = 480; // 8 h of trail behind each vessel
  const BIN = 360; // 6 h bins in the timeline histogram

  let container: HTMLDivElement;
  let map = $state.raw<MLMap | null>(null);
  let overlay: MapboxOverlay | null = null;
  let zoom = $state(6);

  let meta = $state.raw<Meta | null>(null);
  let windowId = $state<string | null>(null);
  let fleet = $state.raw<Fleet | null>(null);
  let tracks = $state.raw<Tracks | null>(null);
  let events = $state.raw<Events | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);

  let current = $state(0);
  let playing = $state(false);
  let speed = $state(180);

  let layers = $state({ density: true, tracks: true, paths: false, gaps: true, sts: true, spoof: true, behav: true });
  let allEvents = $state(false);
  let selected = $state<number | null>(null);
  let following = $state(false);
  let hover = $state<{ x: number; y: number; html: string } | null>(null);
  let query = $state('');
  let searchFocused = $state(false);
  let layersOpen = $state(true);

  const summary = $derived(meta?.windows.find((w) => w.id === windowId) ?? null);
  const duration = $derived(summary ? (Date.parse(summary.end) - Date.parse(summary.start)) / 60_000 + 1440 : 1440);
  const vesselIndex = $derived(new Map(fleet?.vessels.map((v, i) => [v[0], i]) ?? []));
  const listedCount = $derived(fleet?.vessels.filter((v) => isListed(v[4])).length ?? 0);

  const bins = $derived.by(() => {
    if (!events) return [];
    const b = new Array(Math.ceil(duration / BIN)).fill(0);
    const add = (t: number) => {
      const i = Math.floor(t / BIN);
      if (i >= 0 && i < b.length) b[i]++;
    };
    events.gaps.forEach((g) => add(g[1]));
    events.sts.forEach((s) => add(s[2]));
    events.spoof.forEach((s) => add(s[2]));
    events.behav.forEach((s) => add(s[2]));
    return b;
  });

  // Per-vertex colours for the trails, by the owning vessel's sanctions status.
  const vertexColors = $derived.by(() => {
    if (!fleet || !tracks) return null;
    const colors = new Uint8Array(fleet.n * 4);
    fleet.trips.forEach(([v, o, c]) => {
      const rgb = isListed(fleet!.vessels[v][4]) ? RGB.listed : RGB.vessel;
      for (let k = o; k < o + c; k++) {
        colors[k * 4] = rgb[0];
        colors[k * 4 + 1] = rgb[1];
        colors[k * 4 + 2] = rgb[2];
        colors[k * 4 + 3] = 255;
      }
    });
    return colors;
  });

  const selectedPaths = $derived.by(() => {
    if (selected === null || !fleet || !tracks) return [];
    const vi = vesselIndex.get(selected);
    if (vi === undefined) return [];
    const paths: [number, number][][] = [];
    fleet.trips.forEach(([v, o, c]) => {
      if (v !== vi) return;
      const path: [number, number][] = [];
      for (let k = o; k < o + c; k++) path.push([tracks!.positions[k * 2], tracks!.positions[k * 2 + 1]]);
      paths.push(path);
    });
    return paths;
  });

  const selectedCounts = $derived.by(() => {
    const zero = { gaps: 0, sts: 0, spoof: 0, behav: 0 };
    if (selected === null || !events) return zero;
    const m = selected;
    return {
      gaps: events.gaps.filter((g) => g[0] === m).length,
      sts: events.sts.filter((s) => s[0] === m || s[1] === m).length,
      spoof: events.spoof.filter((s) => s[0] === m).length,
      behav: events.behav.filter((s) => s[0] === m).length,
    };
  });

  const searchResults = $derived.by(() => {
    const q = query.trim().toUpperCase();
    if (!fleet || q.length < 2) return [];
    return fleet.vessels
      .filter((v) => (v[1] ?? '').includes(q) || String(v[0]).startsWith(q))
      .slice(0, 8);
  });

  interface Head {
    p: [number, number];
    v: number;
    a: number;
    moving: boolean;
  }

  function computeHeads(cur: number): Head[] {
    if (!tracks || !fleet) return [];
    const { positions, times, startIndices, tripStart, tripEnd, tripVessel } = tracks;
    const nt = startIndices.length;
    const out: Head[] = [];
    for (let i = 0; i < nt; i++) {
      if (cur < tripStart[i] || cur > tripEnd[i]) continue;
      const s = startIndices[i];
      const e = (i + 1 < nt ? startIndices[i + 1] : fleet.n) - 1;
      let lo = s;
      let hi = e;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (times[mid] <= cur) lo = mid;
        else hi = mid - 1;
      }
      const k2 = Math.min(lo + 1, e);
      const ta = times[lo];
      const tb = times[k2];
      const f = tb > ta ? (cur - ta) / (tb - ta) : 0;
      const x1 = positions[lo * 2];
      const y1 = positions[lo * 2 + 1];
      const x2 = positions[k2 * 2];
      const y2 = positions[k2 * 2 + 1];
      const lat = y1 + (y2 - y1) * f;
      const dx = (x2 - x1) * Math.cos((lat * Math.PI) / 180);
      const dy = y2 - y1;
      const bearing = (Math.atan2(dx, dy) * 180) / Math.PI;
      // < ~60 m per 15 min is a vessel at rest: drawn as a dot, not an arrow.
      const moving = Math.hypot(dx, dy) > 0.00055;
      out.push({ p: [x1 + (x2 - x1) * f, lat], v: tripVessel[i], a: -bearing, moving });
    }
    return out;
  }

  let heads: Head[] = [];

  function vesselName(mmsi: number): string {
    const vi = vesselIndex.get(mmsi);
    const name = vi !== undefined ? fleet!.vessels[vi][1] : events?.names[String(mmsi)]?.[0];
    return title(name) || `MMSI ${mmsi}`;
  }

  function vesselTip(mmsi: number): string {
    const vi = vesselIndex.get(mmsi);
    const v = vi !== undefined ? fleet!.vessels[vi] : null;
    const n = events?.names[String(mmsi)];
    const iso2 = v ? v[3] : (n?.[1] ?? null);
    const type = v ? v[2] : (n?.[2] ?? null);
    const status = v ? v[4] : (n?.[3] ?? 0);
    const listed = isListed(status)
      ? `<span class="tip-listed">${glyphSvg('triangle', 10)} ${tr('vessel.listed')}</span>`
      : '';
    return `<div class="tip-title"><span class="${flagClass(iso2)}"></span><strong>${vesselName(mmsi)}</strong></div>
      <div class="tip-sub">${shipTypeLabel(lang, type)} · ${countryName(lang, iso2) ?? tr('common.unknown')}</div>${listed}`;
  }

  function eventTip(kind: 'gap' | 'sts' | 'spoof' | 'behav', d: any): string {
    const t0 = Date.parse(fleet!.t0.replace(/Z?$/, 'Z'));
    const at = (m: number) => fmtDateTime(lang, t0 + m * 60_000);
    let head = '';
    let body = '';
    if (kind === 'gap') {
      head = tr('event.gap');
      body = `${vesselName(d[0])}<br>${at(d[1])} → ${at(d[2])} · ${num(lang, d[9], 0)} h<br><span class="tip-muted">${tr(`verdict.${events!.codes.gap[d[8]]}` as any)}</span>`;
    } else if (kind === 'sts') {
      head = tr('event.sts');
      body = `${vesselName(d[0])} ↔ ${vesselName(d[1])}<br>${at(d[2])} · ${num(lang, d[7], 1)} h`;
    } else {
      const code = kind === 'spoof' ? events!.codes.spoof[d[1]] : events!.codes.behav[d[1]];
      head = kindLabel(lang, code);
      body = `${vesselName(d[0])}<br>${at(d[2])}`;
    }
    return `<div class="tip-kind">${glyphSvg({ gap: 'dash', sts: 'ring', spoof: 'diamond', behav: 'triangle' }[kind] as any, 12)} ${head}</div><div class="tip-sub">${body}</div>`;
  }

  function buildLayers() {
    if (!fleet || !tracks || !events || !vertexColors) return [];
    const cur = current;
    const atlas = iconAtlas();
    const out: any[] = [];
    const binary = {
      length: tracks.startIndices.length,
      startIndices: tracks.startIndices,
      attributes: {
        getPath: { value: tracks.positions, size: 2 },
        getTimestamps: { value: tracks.times, size: 1 },
        getColor: { value: vertexColors, size: 4, normalized: true },
      },
    };
    const eventFilter = (size: 1 | 2) => new DataFilterExtension({ filterSize: size });

    if (layers.paths) {
      out.push(
        new PathLayer({
          id: 'paths',
          data: binary,
          positionFormat: 'XY',
          _pathType: 'open',
          widthUnits: 'pixels',
          getWidth: 1,
          opacity: 0.16,
          pickable: false,
        }),
      );
    }

    if (layers.gaps) {
      out.push(
        new PathLayer({
          id: 'gaps',
          data: events.gaps,
          getPath: (d: any) => [
            [d[3], d[4]],
            [d[5], d[6]],
          ],
          getColor: withAlpha(RGB.event, 0.5),
          widthUnits: 'pixels',
          getWidth: 1.2,
          getDashArray: [3, 4],
          dashJustified: true,
          pickable: true,
          getFilterValue: (d: any) => [d[1], d[2]],
          filterRange: [
            [-1e7, cur],
            [cur - 120, 1e7],
          ],
          filterSoftRange: [
            [-1e7, cur],
            [cur, 1e7],
          ],
          filterEnabled: !allEvents,
          extensions: [new PathStyleExtension({ dash: true }), eventFilter(2)],
        }),
      );
    }

    if (layers.tracks) {
      out.push(
        new TripsLayer({
          id: 'trips',
          data: binary,
          positionFormat: 'XY',
          _pathType: 'open',
          currentTime: cur,
          trailLength: TRAIL,
          fadeTrail: true,
          widthUnits: 'pixels',
          getWidth: 2,
          capRounded: true,
          jointRounded: true,
          opacity: 0.85,
          pickable: true,
        } as any),
      );
    }

    const iconEvents = (id: string, data: any[], icon: string, size: number) =>
      new IconLayer({
        id,
        data,
        iconAtlas: atlas.url,
        iconMapping: atlas.mapping,
        getIcon: () => icon,
        getPosition: (d: any) => [d[3], d[4]],
        getSize: size,
        sizeUnits: 'pixels',
        getColor: withAlpha(RGB.event, 1),
        pickable: true,
        getFilterValue: (d: any) => d[2],
        filterRange: [cur - FADE, cur],
        filterSoftRange: [cur - FADE / 3, cur],
        filterEnabled: !allEvents,
        extensions: [eventFilter(1)],
      });

    if (layers.sts) {
      out.push(
        new IconLayer({
          id: 'sts',
          data: events.sts,
          iconAtlas: atlas.url,
          iconMapping: atlas.mapping,
          getIcon: () => 'ring',
          getPosition: (d: any) => [d[4], d[5]],
          getSize: 22,
          sizeUnits: 'pixels',
          getColor: withAlpha(RGB.event, 1),
          pickable: true,
          getFilterValue: (d: any) => [d[2], d[3]],
          filterRange: [
            [-1e7, cur],
            [cur - FADE, 1e7],
          ],
          filterSoftRange: [
            [-1e7, cur],
            [cur - FADE / 3, 1e7],
          ],
          filterEnabled: !allEvents,
          extensions: [eventFilter(2)],
        }),
      );
    }
    if (layers.spoof) out.push(iconEvents('spoof', events.spoof, 'diamond', 13));
    if (layers.behav) out.push(iconEvents('behav', events.behav, 'triangle', 14));

    if (selectedPaths.length) {
      out.push(
        new PathLayer({
          id: 'selected-path',
          data: selectedPaths,
          getPath: (d: any) => d,
          getColor: [238, 242, 247, 210],
          widthUnits: 'pixels',
          getWidth: 2.2,
          capRounded: true,
          jointRounded: true,
        }),
      );
    }

    if (layers.tracks) {
      heads = computeHeads(cur);
      const listedHeads = heads.filter((h) => isListed(fleet!.vessels[h.v][4]));
      out.push(
        new ScatterplotLayer({
          id: 'listed-halo',
          data: listedHeads,
          getPosition: (d: Head) => d.p,
          getRadius: 11,
          radiusUnits: 'pixels',
          stroked: true,
          filled: false,
          getLineColor: withAlpha(RGB.listed, 0.75),
          lineWidthUnits: 'pixels',
          getLineWidth: 1.2,
        }),
        new IconLayer({
          id: 'heads',
          data: heads,
          iconAtlas: atlas.url,
          iconMapping: atlas.mapping,
          getIcon: (d: Head) => (d.moving ? 'boat' : 'boatStill'),
          getPosition: (d: Head) => d.p,
          getAngle: (d: Head) => (d.moving ? d.a : 0),
          getSize: (d: Head) => (fleet!.vessels[d.v][0] === selected ? 22 : isListed(fleet!.vessels[d.v][4]) ? 16 : 12),
          sizeUnits: 'pixels',
          getColor: (d: Head) =>
            fleet!.vessels[d.v][0] === selected
              ? [255, 255, 255, 255]
              : withAlpha(isListed(fleet!.vessels[d.v][4]) ? RGB.listed : RGB.vessel, 1),
          pickable: true,
          updateTriggers: { getSize: [selected], getColor: [selected] },
        }),
      );
      const labelled = heads.filter((h) => {
        const v = fleet!.vessels[h.v];
        return v[0] === selected || (zoom >= 7.5 && isListed(v[4]));
      });
      out.push(
        new TextLayer({
          id: 'labels',
          data: labelled,
          getPosition: (d: Head) => d.p,
          getText: (d: Head) => title(fleet!.vessels[d.v][1]) || String(fleet!.vessels[d.v][0]),
          getSize: 11.5,
          getColor: [238, 242, 247, 235],
          fontFamily: 'Inter Variable, system-ui, sans-serif',
          fontWeight: 600,
          characterSet: 'auto',
          getTextAnchor: 'start',
          getAlignmentBaseline: 'center',
          getPixelOffset: [13, 0],
          background: true,
          getBackgroundColor: [4, 8, 15, 190],
          backgroundPadding: [5, 2, 5, 2],
          updateTriggers: { getText: [windowId] },
        }),
      );
    }
    return out;
  }

  function render() {
    if (!overlay) return;
    overlay.setProps({ layers: buildLayers() });
    if (following && selected !== null) {
      const vi = vesselIndex.get(selected);
      const h = heads.find((x) => x.v === vi);
      if (h && map) map.jumpTo({ center: h.p });
    }
  }

  $effect(() => {
    // Re-render whenever anything the layers depend on changes.
    void [current, layers.density, layers.tracks, layers.paths, layers.gaps, layers.sts, layers.spoof, layers.behav, allEvents, selected, zoom, fleet, tracks, events, following];
    render();
  });

  $effect(() => {
    if (map && summary && meta) {
      setDensity(map, densityUrl(summary.id), meta.bbox);
      setLayerVisible(map, 'density', layers.density);
    }
  });

  $effect(() => {
    if (map) setLayerVisible(map, 'density', layers.density);
  });

  // Animation loop: only advances time; the effect above redraws.
  let raf = 0;
  let last = 0;
  function frame(ts: number) {
    if (playing && last) {
      const dt = Math.min(0.1, (ts - last) / 1000);
      current = (current + dt * speed) % duration;
    }
    last = ts;
    raf = requestAnimationFrame(frame);
  }

  async function selectWindow(id: string) {
    if (id === windowId && fleet) return;
    loading = true;
    error = null;
    selected = null;
    following = false;
    try {
      const f = await loadFleet(id);
      const [tr_, ev] = await Promise.all([loadTracks(id, f), loadEvents(id)]);
      windowId = id;
      fleet = f;
      tracks = tr_;
      events = ev;
      if (current > duration) current = 0;
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  function fitPadding() {
    const narrow = window.innerWidth < 720;
    return narrow
      ? { top: 140, bottom: 330, left: 30, right: 30 }
      : { top: 90, bottom: 150, left: 360, right: 420 };
  }

  function selectVessel(mmsi: number | null, fly = false) {
    selected = mmsi;
    if (mmsi === null) {
      following = false;
      return;
    }
    if (fly && map && tracks && fleet) {
      const vi = vesselIndex.get(mmsi);
      if (vi === undefined) return;
      if (!heads.some((x) => x.v === vi)) {
        // Not at sea right now: jump to the start of its nearest trip.
        let best = -1;
        let bestDist = Infinity;
        tracks.tripVessel.forEach((v, i) => {
          if (v !== vi) return;
          const d = Math.abs(tracks!.tripStart[i] - current);
          if (d < bestDist) {
            bestDist = d;
            best = i;
          }
        });
        if (best >= 0) current = tracks.tripStart[best];
      }
      // Frame the vessel's whole route this month.
      let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
      fleet.trips.forEach(([v, o, c]) => {
        if (v !== vi) return;
        for (let k = o; k < o + c; k++) {
          const x = tracks!.positions[k * 2];
          const y = tracks!.positions[k * 2 + 1];
          if (x < w) w = x;
          if (x > e) e = x;
          if (y < s) s = y;
          if (y > n) n = y;
        }
      });
      if (Number.isFinite(w)) {
        map.fitBounds(
          [
            [w, s],
            [e, n],
          ],
          { padding: fitPadding(), maxZoom: 9, duration: 1400 },
        );
      }
    }
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (windowId) p.set('w', windowId);
    if (selected !== null) p.set('m', String(selected));
    p.set('t', String(Math.round(current)));
    history.replaceState(null, '', `${location.pathname}?${p}`);
  }

  let urlTimer: ReturnType<typeof setTimeout>;
  $effect(() => {
    void [windowId, selected, playing];
    clearTimeout(urlTimer);
    urlTimer = setTimeout(syncUrl, 400);
  });

  onMount(() => {
    let destroyed = false;
    const params = new URLSearchParams(location.search);
    if (window.innerWidth < 720) layersOpen = false;
    (async () => {
      try {
        meta = await loadMeta();
        const [w, s, e, n] = meta.bbox;
        map = await createBaseMap({ container, bounds: [w + 2.5, s + 0.3, e - 2.2, n - 0.5], navigation: true });
        if (destroyed) return;
        zoom = map.getZoom();
        map.on('zoomend', () => (zoom = map!.getZoom()));
        if (import.meta.env.DEV) (window as any).__map = map;
        overlay = new MapboxOverlay({
          interleaved: true,
          onHover: (info: any) => {
            container.style.cursor = info.object ? 'pointer' : '';
            if (!info.object || !info.layer) {
              hover = null;
              return;
            }
            const id = info.layer.id as string;
            let html = '';
            if (id === 'heads' || id === 'labels') html = vesselTip(fleet!.vessels[info.object.v][0]);
            else if (id === 'listed-halo') html = vesselTip(fleet!.vessels[info.object.v][0]);
            else if (['gaps', 'sts', 'spoof', 'behav'].includes(id)) html = eventTip(id as any, info.object);
            hover = html ? { x: info.x, y: info.y, html } : null;
          },
          onClick: (info: any) => {
            if (!info.layer) {
              selectVessel(null);
              return;
            }
            const id = info.layer.id as string;
            if (id === 'heads' || id === 'labels' || id === 'listed-halo') selectVessel(fleet!.vessels[info.object.v][0]);
            else if (id === 'trips' && info.index >= 0) selectVessel(fleet!.vessels[tracks!.tripVessel[info.index]][0]);
            else if (['gaps', 'spoof', 'behav'].includes(id)) selectVessel(info.object[0]);
            else if (id === 'sts') selectVessel(info.object[0]);
          },
        });
        map.addControl(overlay as any);
        const wanted = params.get('w');
        const initial = meta.windows.find((x) => x.id === wanted)?.id ?? meta.windows[0]?.id;
        if (initial) await selectWindow(initial);
        const t = Number(params.get('t'));
        if (Number.isFinite(t) && t > 0) current = Math.min(t, duration);
        else current = 2 * 1440 + 9 * 60;
        const m = Number(params.get('m'));
        if (m) selectVessel(m, true);
        playing = !params.get('m');
      } catch (e) {
        error = String(e);
        loading = false;
      }
      raf = requestAnimationFrame(frame);
    })();
    return () => {
      destroyed = true;
      cancelAnimationFrame(raf);
      overlay?.finalize();
      map?.remove();
    };
  });

  const layerDefs = [
    { key: 'density', glyph: null, label: 'explore.layer.density', desc: 'explore.layer.density.desc' },
    { key: 'tracks', glyph: 'boat', label: 'explore.layer.tracks', desc: 'explore.layer.tracks.desc' },
    { key: 'paths', glyph: null, label: 'explore.layer.paths', desc: 'explore.layer.paths.desc' },
    { key: 'gaps', glyph: 'dash', label: 'explore.layer.gaps', desc: 'event.gap.desc' },
    { key: 'sts', glyph: 'ring', label: 'explore.layer.sts', desc: 'event.sts.desc' },
    { key: 'spoof', glyph: 'diamond', label: 'explore.layer.spoof', desc: 'event.spoof.desc' },
    { key: 'behav', glyph: 'triangle', label: 'explore.layer.behav', desc: 'event.behav.desc' },
  ] as const;

  const countFor = (key: string) =>
    summary ? ({ gaps: summary.counts.gaps, sts: summary.counts.sts, spoof: summary.counts.spoof, behav: summary.counts.behav } as any)[key] : undefined;
</script>

<div class="explorer">
  <div class="map" bind:this={container}></div>

  <div class="top-left">
    <section class="card glass intro">
      <div class="eyebrow">{tr('explore.window')}</div>
      <div class="months" role="tablist">
        {#each meta?.windows ?? [] as w}
          <button role="tab" aria-selected={w.id === windowId} class:active={w.id === windowId} onclick={() => selectWindow(w.id)}>
            {fmtMonth(lang, w.start)}
          </button>
        {/each}
      </div>
      {#if summary}
        <div class="stats">
          <div><span class="stat mono">{num(lang, summary.n_vessels)}</span><span class="stat-label">{tr('explore.stat.vessels')}</span></div>
          <div><span class="stat mono">{num(lang, summary.n_focus)}</span><span class="stat-label">{tr('explore.stat.focus')}</span></div>
          <div>
            <span class="stat mono listed">{num(lang, listedCount)}</span>
            <span class="stat-label">{tr('explore.legend.listed')}</span>
          </div>
        </div>
      {/if}
      <div class="search">
        <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" stroke-width="1.6" /><path d="m11 11 3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
        <input
          type="search"
          placeholder={tr('explore.searchPlaceholder')}
          bind:value={query}
          onfocus={() => (searchFocused = true)}
          onblur={() => setTimeout(() => (searchFocused = false), 150)}
          aria-label={tr('explore.searchPlaceholder')}
        />
        {#if searchFocused && searchResults.length}
          <ul class="results glass">
            {#each searchResults as v}
              <li>
                <button
                  onclick={() => {
                    selectVessel(v[0], true);
                    query = '';
                  }}
                >
                  <span class={flagClass(v[3])}></span>
                  <span class="r-name">{title(v[1]) || v[0]}</span>
                  {#if isListed(v[4])}<span class="dot-listed" title={tr('vessel.listed')}></span>{/if}
                  <span class="r-type">{shipTypeLabel(lang, v[2])}</span>
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </section>

    <section class="card glass layers" class:collapsed={!layersOpen}>
      <button class="layers-head" onclick={() => (layersOpen = !layersOpen)} aria-expanded={layersOpen}>
        <span class="eyebrow">{tr('explore.layers')}</span>
        <svg width="12" height="12" viewBox="0 0 12 12" class="chev" aria-hidden="true"><path d="M2 4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" /></svg>
      </button>
      {#if layersOpen}
        <ul>
          {#each layerDefs as l}
            <li>
              <label class="toggle" title={tr(l.desc)}>
                <input type="checkbox" bind:checked={layers[l.key]} />
                <span class="switch" aria-hidden="true"></span>
                <span class="l-glyph" class:event={['gaps', 'sts', 'spoof', 'behav'].includes(l.key)}>
                  {#if l.key === 'density'}
                    <span class="ramp"></span>
                  {:else if l.key === 'paths'}
                    <span class="paths-swatch"></span>
                  {:else if l.key === 'tracks'}
                    <span class="pair"><span style="color: var(--c-vessel)">{@html glyphSvg('boat', 12)}</span><span style="color: var(--c-listed)">{@html glyphSvg('boat', 12)}</span></span>
                  {:else if l.glyph}
                    {@html glyphSvg(l.glyph, 14)}
                  {/if}
                </span>
                <span class="l-text">
                  <span class="l-label">{tr(l.label)}</span>
                  {#if countFor(l.key) !== undefined}<span class="l-count mono">{num(lang, countFor(l.key))}</span>{/if}
                </span>
              </label>
            </li>
          {/each}
        </ul>
        <div class="legend">
          <span><i style="background: var(--c-vessel)"></i>{tr('explore.legend.vessel')}</span>
          <span><i style="background: var(--c-listed)"></i>{tr('explore.legend.listed')}</span>
        </div>
        <label class="check">
          <input type="checkbox" bind:checked={allEvents} />
          <span>{tr('explore.allEvents')}</span>
        </label>
        <p class="note">{tr('explore.recentEvents')}</p>
      {/if}
    </section>
  </div>

  {#if selected !== null && fleet && summary}
    {@const vi = vesselIndex.get(selected)}
    {@const v = vi !== undefined ? fleet.vessels[vi] : null}
    {@const n = events?.names[String(selected)]}
    <div class="right">
      <VesselPanel
        {lang}
        mmsi={selected}
        windowId={summary.id}
        windowEnd={summary.end}
        basic={{
          name: v ? v[1] : (n?.[0] ?? null),
          ship_type: v ? v[2] : (n?.[2] ?? null),
          iso2: v ? v[3] : (n?.[1] ?? null),
          status: v ? v[4] : (n?.[3] ?? 0),
          length: v ? v[5] : null,
        }}
        counts={selectedCounts}
        {following}
        onfollow={v ? () => (following = !following) : undefined}
        onclose={() => selectVessel(null)}
      />
    </div>
  {/if}

  {#if fleet && summary}
    <div class="bottom">
      {#if !selected}
        <p class="hint">{tr('explore.hint')}</p>
      {/if}
      <Timeline
        {lang}
        t0={fleet.t0}
        {duration}
        {current}
        {playing}
        {speed}
        {bins}
        onseek={(m) => {
          current = m;
          playing = false;
        }}
        ontoggle={() => (playing = !playing)}
        onspeed={(s) => (speed = s)}
      />
    </div>
  {/if}

  {#if loading}
    <div class="loading"><span class="spinner"></span>{tr('common.loading')}</div>
  {/if}
  {#if error}
    <div class="error glass">{tr('common.error')}<br /><code>{error}</code></div>
  {/if}

  {#if hover}
    <div class="tooltip glass" style:left={`${hover.x}px`} style:top={`${hover.y}px`}>{@html hover.html}</div>
  {/if}
</div>

<style>
  .explorer {
    position: absolute;
    inset: 0;
    overflow: hidden;
  }

  .map {
    position: absolute;
    inset: 0;
    background: #050b15;
  }

  .top-left {
    position: absolute;
    top: calc(var(--header-h) + 12px);
    left: 16px;
    width: 318px;
    max-height: calc(100% - var(--header-h) - 130px);
    display: flex;
    flex-direction: column;
    gap: 10px;
    z-index: 5;
  }

  .card {
    padding: 14px 16px;
  }

  .months {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 8px 0 12px;
  }

  .months button {
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: transparent;
    font-size: 12.5px;
    cursor: pointer;
    color: var(--text-2);
    transition: all 0.2s;
  }

  .months button:hover {
    color: var(--text-1);
    border-color: rgba(255, 255, 255, 0.3);
  }

  .months button.active {
    background: var(--text-1);
    border-color: var(--text-1);
    color: var(--bg-0);
    font-weight: 600;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    padding: 10px 0 12px;
    border-top: 1px solid var(--line);
  }

  .stat {
    display: block;
    font-size: 19px;
    font-weight: 600;
    line-height: 1.2;
  }

  .stat.listed {
    color: #ff9a70;
  }

  .stat-label {
    display: block;
    font-size: 10.5px;
    line-height: 1.3;
    color: var(--text-3);
  }

  .search {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    height: 36px;
    padding: 0 12px;
    border-radius: 10px;
    border: 1px solid var(--line-strong);
    background: rgba(0, 0, 0, 0.25);
    color: var(--text-3);
  }

  .search input {
    flex: 1;
    min-width: 0;
    border: none;
    background: transparent;
    outline: none;
    font-size: 13px;
    color: var(--text-1);
  }

  .results {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    right: 0;
    list-style: none;
    margin: 0;
    padding: 6px;
    z-index: 10;
    background: var(--glass-strong);
  }

  .results button {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 7px 8px;
    border: none;
    border-radius: 7px;
    background: transparent;
    text-align: left;
    cursor: pointer;
    font-size: 13px;
  }

  .results button:hover {
    background: rgba(255, 255, 255, 0.07);
  }

  .r-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .r-type {
    font-size: 11px;
    color: var(--text-3);
  }

  .dot-listed {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--c-listed);
  }

  .layers {
    overflow-y: auto;
    scrollbar-width: thin;
    padding-top: 10px;
  }

  .layers-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 2px 0 6px;
    border: none;
    background: transparent;
    cursor: pointer;
  }

  .collapsed .chev {
    transform: rotate(-90deg);
  }

  .chev {
    color: var(--text-3);
    transition: transform 0.2s;
  }

  .layers ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
    cursor: pointer;
  }

  .toggle input {
    position: absolute;
    opacity: 0;
    pointer-events: none;
  }

  .switch {
    position: relative;
    width: 28px;
    height: 16px;
    flex: none;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.12);
    transition: background 0.2s;
  }

  .switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--text-2);
    transition: transform 0.2s var(--ease-out), background 0.2s;
  }

  .toggle input:checked + .switch {
    background: rgba(255, 255, 255, 0.85);
  }

  .toggle input:checked + .switch::after {
    transform: translateX(12px);
    background: var(--bg-0);
  }

  .toggle input:focus-visible + .switch {
    outline: 2px solid var(--text-1);
    outline-offset: 2px;
  }

  .l-glyph {
    display: inline-grid;
    place-items: center;
    width: 24px;
    height: 22px;
    flex: none;
    color: var(--text-2);
  }

  .l-glyph.event {
    color: var(--c-event);
  }

  .pair {
    display: flex;
    gap: 1px;
  }

  .ramp {
    width: 22px;
    height: 10px;
    border-radius: 3px;
    background: linear-gradient(to right, #1b2638, #3a4c68, #8496b2, #e8eef8);
  }

  .paths-swatch {
    width: 22px;
    height: 10px;
    border-radius: 3px;
    background: repeating-linear-gradient(-30deg, rgba(57, 135, 229, 0.5) 0 1px, transparent 1px 4px);
    border: 1px solid var(--line);
  }

  .l-text {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex: 1;
    min-width: 0;
  }

  .l-label {
    flex: 1;
    font-size: 13px;
    line-height: 1.3;
  }

  .l-count {
    font-size: 11.5px;
    color: var(--text-3);
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
    margin: 8px 0 0;
    padding: 10px 0 0;
    border-top: 1px solid var(--line);
    font-size: 11.5px;
    color: var(--text-2);
  }

  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .legend i {
    width: 14px;
    height: 3px;
    border-radius: 2px;
  }

  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
    font-size: 12.5px;
    color: var(--text-2);
    cursor: pointer;
  }

  .check input {
    accent-color: var(--text-1);
  }

  .note {
    margin: 4px 0 0;
    font-size: 11px;
    color: var(--text-3);
  }

  .right {
    position: absolute;
    top: calc(var(--header-h) + 12px);
    right: 60px;
    bottom: 110px;
    z-index: 6;
    display: flex;
    align-items: flex-start;
  }

  .bottom {
    position: absolute;
    left: 16px;
    right: 16px;
    bottom: 26px;
    z-index: 5;
    max-width: 1100px;
    margin: 0 auto;
  }

  .hint {
    width: fit-content;
    margin: 0 auto 10px;
    padding: 5px 12px;
    border-radius: 999px;
    background: rgba(4, 8, 15, 0.7);
    border: 1px solid var(--line);
    font-size: 12px;
    color: var(--text-2);
    pointer-events: none;
  }

  .loading {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    border-radius: 999px;
    background: var(--glass-strong);
    font-size: 13px;
    color: var(--text-2);
    z-index: 20;
  }

  .spinner {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.2);
    border-top-color: var(--text-1);
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .error {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    padding: 16px 20px;
    z-index: 20;
    font-size: 13px;
    max-width: 90vw;
  }

  .tooltip {
    position: absolute;
    z-index: 30;
    transform: translate(14px, -50%);
    padding: 9px 12px;
    font-size: 12.5px;
    pointer-events: none;
    max-width: 280px;
    border-radius: 10px;
  }

  .tooltip :global(.tip-title) {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 13.5px;
  }

  .tooltip :global(.tip-sub) {
    margin-top: 3px;
    color: var(--text-2);
    line-height: 1.45;
  }

  .tooltip :global(.tip-muted) {
    color: var(--text-3);
  }

  .tooltip :global(.tip-kind) {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    color: #c7f3e2;
  }

  .tooltip :global(.tip-kind svg) {
    color: var(--c-event);
  }

  .tooltip :global(.tip-listed) {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-top: 6px;
    font-size: 11px;
    font-weight: 600;
    color: #ffc4ab;
  }

  .tooltip :global(.tip-listed svg) {
    color: var(--c-listed);
  }

  @media (max-width: 720px) {
    .top-left {
      left: 10px;
      right: 10px;
      width: auto;
      top: calc(var(--header-h) + 8px);
    }
    .intro .stats {
      display: none;
    }
    .right {
      left: 10px;
      right: 10px;
      top: auto;
      bottom: 88px;
    }
    .bottom {
      left: 10px;
      right: 10px;
      bottom: 14px;
    }
    .hint {
      display: none;
    }
  }
</style>
