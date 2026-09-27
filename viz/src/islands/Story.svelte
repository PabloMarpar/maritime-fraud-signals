<script lang="ts">
  import { onMount } from 'svelte';
  import type { Map as MLMap } from 'maplibre-gl';
  import { MapboxOverlay } from '@deck.gl/mapbox';
  import { TripsLayer } from '@deck.gl/geo-layers';
  import { IconLayer, PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
  import { PathStyleExtension } from '@deck.gl/extensions';
  import { createBaseMap, setDensity } from '../lib/basemap';
  import {
    densityUrl,
    isListed,
    loadEvents,
    loadFleet,
    loadMeta,
    loadTracks,
    type Events,
    type Fleet,
    type Tracks,
    type WindowSummary,
  } from '../lib/data';
  import { RGB, withAlpha } from '../lib/colors';
  import { iconAtlas } from '../lib/icons';
  import { num } from '../lib/format';
  import { CASE_MMSI, STORY } from '../content/story';
  import { localePath, useT, type Lang } from '../i18n/ui';
  import DetectorChart from './DetectorChart.svelte';

  let { lang }: { lang: Lang } = $props();
  const tr = $derived(useT(lang));
  const text = $derived(STORY[lang]);

  const WINDOW = '2024-06-01_2024-06-30';
  // [west, south, east, north]
  const VIEWS: Record<string, [number, number, number, number]> = {
    wide: [4.6, 53.9, 17.6, 58.4],
    straits: [8.4, 54.2, 15.0, 58.0],
    inbound: [5.0, 54.5, 18.6, 58.0],
    east: [9.5, 53.9, 30.6, 61.0],
    outbound: [5.0, 54.5, 18.6, 58.0],
  };
  // Step -1 is the hero; the last index is the call to action.
  const STEP_VIEW = ['wide', 'straits', 'wide', 'inbound', 'east', 'outbound', 'outbound', 'wide', 'wide'];
  const PORTS: { name: string; p: [number, number] }[] = [
    { name: 'Primorsk', p: [28.7, 60.35] },
    { name: 'Ust-Luga', p: [28.4, 59.68] },
  ];
  // Where NS LOTUS left and re-entered Danish coverage (its June dossier's AIS gap).
  const LAST_CONTACT: [number, number] = [16.2344, 55.7138];
  const BACK_CONTACT: [number, number] = [16.8215, 55.8582];

  let container: HTMLDivElement;
  let map = $state.raw<MLMap | null>(null);
  let overlay: MapboxOverlay | null = null;

  let summary = $state.raw<WindowSummary | null>(null);
  let fleet = $state.raw<Fleet | null>(null);
  let tracks = $state.raw<Tracks | null>(null);
  let events = $state.raw<Events | null>(null);
  let active = $state(-1);
  let anim = $state(0); // 0..1 progress of the current step's drawing animation
  let loopT = 0;

  interface Trip {
    path: [number, number][];
    ts: number[];
  }

  const caseTrips = $derived.by<Trip[]>(() => {
    if (!fleet || !tracks) return [];
    const vi = fleet.vessels.findIndex((v) => v[0] === CASE_MMSI);
    if (vi < 0) return [];
    const out: Trip[] = [];
    fleet.trips.forEach(([v, o, c]) => {
      if (v !== vi) return;
      const path: [number, number][] = [];
      const ts: number[] = [];
      for (let k = o; k < o + c; k++) {
        path.push([tracks!.positions[k * 2], tracks!.positions[k * 2 + 1]]);
        ts.push(tracks!.times[k]);
      }
      out.push({ path, ts });
    });
    return out;
  });
  const inbound = $derived(caseTrips.slice(0, 1));
  const outbound = $derived(caseTrips.slice(1));

  const stats = $derived.by(() => {
    if (!fleet || !events || !summary) return null;
    const tankers = fleet.vessels.filter((v) => v[2] === 'Tanker');
    const later = new Set(tankers.filter((v) => v[4] === 1).map((v) => v[0]));
    const other = new Set(tankers.filter((v) => v[4] === 0).map((v) => v[0]));
    const has = { gap: new Set<number>(), spoof: new Set<number>(), sts: new Set<number>(), draught: new Set<number>() };
    events.gaps.forEach((g) => has.gap.add(g[0]));
    events.spoof.forEach((s) => has.spoof.add(s[0]));
    events.sts.forEach((s) => {
      has.sts.add(s[0]);
      has.sts.add(s[1]);
    });
    const draughtKind = events.codes.behav.indexOf('draught_change_unexplained');
    events.behav.forEach((b) => b[1] === draughtKind && has.draught.add(b[0]));
    const share = (set: Set<number>, group: Set<number>) => {
      let n = 0;
      group.forEach((m) => set.has(m) && n++);
      return group.size ? n / group.size : 0;
    };
    return {
      vessels: summary.n_vessels,
      tankers: tankers.length,
      later: later.size,
      other: other.size,
      rows: (['gap', 'spoof', 'sts', 'draught'] as const).map((key) => ({
        key,
        label: text.chartRows[key],
        later: share(has[key], later),
        other: share(has[key], other),
      })),
    };
  });

  function fill(s: string): string {
    if (!stats) return s.replace(/\{(\w+)\}/g, '…');
    return s
      .replace('{vessels}', num(lang, stats.vessels))
      .replace('{tankers}', num(lang, stats.tankers))
      .replace('{later}', num(lang, stats.later));
  }

  // Per-vertex trail colours: orange for vessels on a list, blue otherwise, with step-specific alpha.
  function vertexColors(listedAlpha: number, otherAlpha: number): Uint8Array | null {
    if (!fleet || !tracks) return null;
    const colors = new Uint8Array(fleet.n * 4);
    fleet.trips.forEach(([v, o, c]) => {
      const vessel = fleet!.vessels[v];
      if (vessel[2] !== 'Tanker' && !isListed(vessel[4])) return;
      const listed = isListed(vessel[4]);
      const rgb = listed ? RGB.listed : RGB.vessel;
      const a = Math.round((listed ? listedAlpha : otherAlpha) * 255);
      for (let k = o; k < o + c; k++) {
        colors.set([rgb[0], rgb[1], rgb[2], a], k * 4);
      }
    });
    return colors;
  }

  let colorsBright: Uint8Array | null = null;
  let colorsFaint: Uint8Array | null = null;
  let colorsLive: Uint8Array | null = null;

  const binaryCache = new Map<Uint8Array, object>();
  function binary(colors: Uint8Array) {
    // Cached: deck.gl compares `data` by reference; a new object per frame re-tessellates.
    if (binaryCache.has(colors)) return binaryCache.get(colors)!;
    const data = {
      length: tracks!.startIndices.length,
      startIndices: tracks!.startIndices,
      attributes: {
        getPath: { value: tracks!.positions, size: 2 },
        getTimestamps: { value: tracks!.times, size: 1 },
        getColor: { value: colors, size: 4, normalized: true },
      },
    };
    binaryCache.set(colors, data);
    return data;
  }

  function interp(trip: Trip, t: number): [number, number] {
    const { path, ts } = trip;
    if (t <= ts[0]) return path[0];
    if (t >= ts[ts.length - 1]) return path[path.length - 1];
    let i = 1;
    while (ts[i] < t) i++;
    const f = (t - ts[i - 1]) / (ts[i] - ts[i - 1] || 1);
    return [path[i - 1][0] + (path[i][0] - path[i - 1][0]) * f, path[i - 1][1] + (path[i][1] - path[i - 1][1]) * f];
  }

  function label(id: string, items: { p: [number, number]; text: string }[], offset: [number, number] = [14, 0]) {
    return new TextLayer({
      id,
      data: items,
      getPosition: (d: any) => d.p,
      getText: (d: any) => d.text,
      getSize: 12,
      getColor: [238, 242, 247, 240],
      fontFamily: 'Inter Variable, system-ui, sans-serif',
      fontWeight: 600,
      characterSet: 'auto',
      getTextAnchor: 'start',
      getAlignmentBaseline: 'center',
      getPixelOffset: offset,
      background: true,
      getBackgroundColor: [4, 8, 15, 210],
      backgroundPadding: [6, 3, 6, 3],
    });
  }

  function caseLayers(id: string, trips: Trip[], progress: number, withHead: boolean, headText: string) {
    const out: any[] = [];
    if (!trips.length) return out;
    const t0 = trips[0].ts[0];
    const t1 = trips[trips.length - 1].ts[trips[trips.length - 1].ts.length - 1];
    const cur = t0 + (t1 - t0) * progress;
    const common = {
      data: trips,
      getPath: (d: Trip) => d.path,
      getTimestamps: (d: Trip) => d.ts,
      currentTime: cur,
      trailLength: 1e6,
      fadeTrail: false,
      capRounded: true,
      jointRounded: true,
      widthUnits: 'pixels' as const,
    };
    out.push(
      new TripsLayer({ id: `${id}-glow`, ...common, getColor: withAlpha(RGB.listed, 0.3), getWidth: 12 } as any),
      new TripsLayer({ id: `${id}-line`, ...common, getColor: withAlpha(RGB.listed, 1), getWidth: 3.2 } as any),
    );
    if (withHead) {
      const trip = trips.find((tr_) => cur >= tr_.ts[0] && cur <= tr_.ts[tr_.ts.length - 1]) ?? (cur < trips[0].ts[0] ? trips[0] : trips[trips.length - 1]);
      const p = interp(trip, cur);
      out.push(
        new ScatterplotLayer({
          id: 'case-head',
          data: [p],
          getPosition: (d: any) => d,
          getRadius: 6,
          radiusUnits: 'pixels',
          getFillColor: [255, 255, 255, 255],
          stroked: true,
          getLineColor: withAlpha(RGB.listed, 1),
          lineWidthUnits: 'pixels',
          getLineWidth: 3,
        }),
        label('case-label', [{ p, text: headText }]),
      );
    }
    return out;
  }

  function ring(id: string, points: [number, number][], radius = 14) {
    return new ScatterplotLayer({
      id,
      data: points,
      getPosition: (d: any) => d,
      getRadius: radius,
      radiusUnits: 'pixels',
      stroked: true,
      filled: false,
      getLineColor: [238, 242, 247, 220],
      lineWidthUnits: 'pixels',
      getLineWidth: 1.5,
    });
  }

  function render() {
    if (!overlay || !tracks || !fleet) return;
    colorsBright ??= vertexColors(0.95, 0.28);
    colorsFaint ??= vertexColors(0.13, 0.05);
    colorsLive ??= vertexColors(1, 1);
    const s = active;
    const layers: any[] = [];
    const duration = 30 * 1440;

    if (s === -1 || s === STORY[lang].steps.length) {
      layers.push(
        new TripsLayer({
          id: 'fleet-anim',
          data: binary(colorsLive!),
          positionFormat: 'XY',
          _pathType: 'open',
          currentTime: loopT % duration,
          trailLength: 600,
          fadeTrail: true,
          widthUnits: 'pixels',
          getWidth: 1.8,
          capRounded: true,
          opacity: 0.85,
        } as any),
      );
    } else if (s === 1 || s === 6) {
      layers.push(
        new PathLayer({
          id: 'fleet-paths',
          data: binary(colorsBright!),
          positionFormat: 'XY',
          _pathType: 'open',
          widthUnits: 'pixels',
          getWidth: 1.2,
          opacity: Math.min(1, anim * 2),
        }),
      );
    } else if (s >= 2 && s <= 5) {
      layers.push(
        new PathLayer({
          id: 'fleet-faint',
          data: binary(colorsFaint!),
          positionFormat: 'XY',
          _pathType: 'open',
          widthUnits: 'pixels',
          getWidth: 1,
        }),
      );
    }

    if (s === 2) {
      layers.push(...caseLayers('inbound', inbound, anim, true, `NS LOTUS · ${text.labels.ballast}`));
    } else if (s === 3) {
      layers.push(...caseLayers('inbound', inbound, 1, false, ''));
      layers.push(
        new PathLayer({
          id: 'gap',
          data: [[LAST_CONTACT, BACK_CONTACT]],
          getPath: (d: any) => d,
          getColor: withAlpha(RGB.event, 1),
          widthUnits: 'pixels',
          getWidth: 2,
          getDashArray: [3, 3],
          extensions: [new PathStyleExtension({ dash: true })],
        }),
        ring('contact-rings', [LAST_CONTACT], 16),
        new ScatterplotLayer({
          id: 'ports',
          data: PORTS,
          getPosition: (d: any) => d.p,
          getRadius: 5,
          radiusUnits: 'pixels',
          getFillColor: [238, 242, 247, 230],
        }),
        label('ports-label', PORTS.map((p) => ({ p: p.p, text: p.name }))),
        label('contact-label', [{ p: LAST_CONTACT, text: text.labels.lastContact }], [20, 0]),
        label('beyond-label', [{ p: [21.2, 57.4] as [number, number], text: text.labels.beyond }]),
      );
    } else if (s === 4) {
      layers.push(...caseLayers('inbound', inbound, 1, false, ''));
      layers.push(...caseLayers('outbound', outbound, anim, true, `NS LOTUS · ${text.labels.laden}`));
      const atlas = iconAtlas();
      layers.push(
        new IconLayer({
          id: 'draught-event',
          data: [LAST_CONTACT],
          iconAtlas: atlas.url,
          iconMapping: atlas.mapping,
          getIcon: () => 'triangle',
          getPosition: (d: any) => d,
          getSize: 18,
          sizeUnits: 'pixels',
          getColor: withAlpha(RGB.event, 1),
        }),
        label('back-label', [{ p: BACK_CONTACT, text: text.labels.back }], [18, -16]),
      );
    } else if (s === 5) {
      layers.push(...caseLayers('all', caseTrips, 1, false, ''));
      const last = outbound.length ? outbound[outbound.length - 1].path.at(-1)! : null;
      if (last) layers.push(ring('end-ring', [last], 12), label('end-label', [{ p: last, text: 'NS LOTUS → LEGACY' }], [18, 0]));
    }
    overlay.setProps({ layers });
  }

  function padding(step: number) {
    const w = window.innerWidth;
    const h = window.innerHeight;
    // Narrow screens: the hero text covers the map, so frame tightly; later cards sit at the
    // bottom, so frame into the upper part.
    if (w < 820) return step === -1 ? { top: 60, bottom: 40, left: 0, right: 0 } : { top: 70, bottom: Math.round(h * 0.4), left: 10, right: 10 };
    return { top: 90, bottom: 60, left: Math.min(560, Math.round(w * 0.42)), right: 170 };
  }

  function flyToStep(s: number) {
    if (!map) return;
    const view = VIEWS[STEP_VIEW[s + 1] ?? 'wide'];
    map.fitBounds(
      [
        [view[0], view[1]],
        [view[2], view[3]],
      ],
      { padding: padding(s), duration: 2200, essential: true },
    );
  }

  // Step changes: move the camera and replay the step's drawing animation.
  let animStart = 0;
  $effect(() => {
    const s = active;
    flyToStep(s);
    anim = 0;
    animStart = performance.now();
  });

  const DENSITY_OPACITY = [0.85, 1, 0.55, 0.35, 0.3, 0.35, 0.35, 0.55, 0.85];
  $effect(() => {
    const o = DENSITY_OPACITY[active + 1] ?? 0.7;
    if (map?.getLayer('density')) map.setPaintProperty('density', 'raster-opacity', o);
  });

  $effect(() => {
    void [active, anim, fleet, tracks, events, lang];
    render();
  });

  let raf = 0;
  let last = 0;
  function frame(ts: number) {
    const dt = last ? Math.min(0.1, (ts - last) / 1000) : 0;
    last = ts;
    const steps = STORY[lang].steps.length;
    if (active === -1 || active === steps) {
      loopT += dt * 480; // 8 hours of June per second
      render();
    }
    if (anim < 1) {
      anim = Math.min(1, (ts - animStart) / 4500);
    }
    raf = requestAnimationFrame(frame);
  }

  onMount(() => {
    let destroyed = false;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) active = Number((e.target as HTMLElement).dataset.step);
        }
      },
      { rootMargin: '-48% 0px -48% 0px' },
    );
    document.querySelectorAll('[data-step]').forEach((el) => observer.observe(el));

    (async () => {
      const meta = await loadMeta();
      summary = meta.windows.find((w) => w.id === WINDOW) ?? meta.windows[0] ?? null;
      const view = VIEWS.wide;
      map = await createBaseMap({ container, lang, bounds: view, interactive: false, attribution: true });
      if (destroyed) return;
      overlay = new MapboxOverlay({ interleaved: true });
      map.addControl(overlay as any);
      flyToStep(active);
      if (summary) {
        setDensity(map, densityUrl(summary.id), meta.bbox, 0.85);
        const f = await loadFleet(summary.id);
        const [t, e] = await Promise.all([loadTracks(summary.id, f), loadEvents(summary.id)]);
        fleet = f;
        tracks = t;
        events = e;
        loopT = 16 * 1440; // start mid-June, when NS LOTUS is in the straits
      }
      raf = requestAnimationFrame(frame);
    })();
    const onResize = () => flyToStep(active);
    window.addEventListener('resize', onResize);
    return () => {
      destroyed = true;
      observer.disconnect();
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
      overlay?.finalize();
      map?.remove();
    };
  });
</script>

<div class="story">
  <div class="sticky" aria-hidden="true">
    <div class="map" bind:this={container}></div>
    <div class="shade" class:hero-shade={active === -1}></div>
  </div>

  <div class="content">
    <header class="hero" data-step="-1">
      <div class="hero-inner">
        <div class="eyebrow kicker">{text.heroKicker}</div>
        <h1>{text.heroTitle}</h1>
        <p class="dek">{text.heroDek}</p>
        <p class="meta">{text.heroMeta}</p>
      </div>
      <div class="scroll-cue" aria-hidden="true">
        <span>{tr('story.scroll')}</span>
        <svg width="16" height="16" viewBox="0 0 16 16"><path d="M3 6l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
      </div>
    </header>

    {#each text.steps as step, i}
      <section class="step" data-step={i}>
        <div class="card glass" class:active={active === i}>
          {#if step.kicker}<div class="step-kicker mono">{step.kicker}</div>{/if}
          <h2>{step.title}</h2>
          {#each step.body as p}
            <p>{fill(p)}</p>
          {/each}
          {#if i === text.steps.length - 1 && stats}
            <DetectorChart
              {lang}
              title={text.chartTitle}
              laterLabel={text.chartLater.replace('{n}', num(lang, stats.later))}
              otherLabel={text.chartOther.replace('{n}', num(lang, stats.other))}
              rows={stats.rows}
            />
            <p class="note">{text.chartNote}</p>
          {/if}
          {#if i === 5}
            <a class="btn case-link" href={`${localePath(lang, 'vessel/')}?mmsi=${CASE_MMSI}`}>{tr('vessel.openDossier')}: NS LOTUS →</a>
          {/if}
        </div>
      </section>
    {/each}

    <section class="step cta" data-step={text.steps.length}>
      <div class="card glass cta-card">
        <h2>{text.ctaTitle}</h2>
        <p>{text.ctaBody}</p>
        <div class="cta-buttons">
          <a class="btn btn-primary" href={localePath(lang, 'explore/')}>{tr('story.explore')} →</a>
          <a class="btn" href={localePath(lang, 'vessels/')}>{tr('story.search')}</a>
          <a class="btn" href={localePath(lang, 'live/')}><span class="live-dot"></span>{tr('story.live')}</a>
        </div>
      </div>
    </section>
  </div>
</div>

<style>
  .story {
    position: relative;
  }

  .sticky {
    position: sticky;
    top: 0;
    height: 100dvh;
    overflow: hidden;
    z-index: 0;
  }

  .map {
    position: absolute;
    inset: 0;
    background: #050b15;
  }

  .shade {
    position: absolute;
    inset: 0;
    pointer-events: none;
    background: linear-gradient(to right, rgba(4, 8, 15, 0.7) 0%, rgba(4, 8, 15, 0.25) 40%, transparent 62%);
    transition: background 0.8s;
  }

  .shade.hero-shade {
    background:
      linear-gradient(to right, rgba(4, 8, 15, 0.94) 0%, rgba(4, 8, 15, 0.82) 38%, rgba(4, 8, 15, 0.2) 62%, transparent 75%),
      linear-gradient(to top, rgba(4, 8, 15, 0.85) 0%, transparent 35%);
  }

  .content {
    position: relative;
    z-index: 1;
    margin-top: -100dvh;
    pointer-events: none;
  }

  .content :global(a),
  .content .card {
    pointer-events: auto;
  }

  .hero {
    min-height: calc(100dvh - var(--header-h));
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: calc(var(--header-h) + 40px) var(--gutter) 90px;
    position: relative;
  }

  .hero-inner {
    max-width: 820px;
  }

  .kicker {
    color: var(--text-2);
  }

  h1 {
    margin: 14px 0 22px;
    font-family: var(--font-serif);
    font-size: clamp(44px, 7.4vw, 96px);
    font-weight: 480;
    line-height: 0.98;
    letter-spacing: -0.025em;
    text-wrap: balance;
  }

  .dek {
    max-width: 58ch;
    margin: 0;
    font-size: clamp(17px, 1.7vw, 21px);
    line-height: 1.5;
    color: var(--text-2);
  }

  .meta {
    margin: 22px 0 0;
    font-size: 12.5px;
    color: var(--text-3);
  }

  .scroll-cue {
    position: absolute;
    left: 50%;
    bottom: 28px;
    transform: translateX(-50%);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text-3);
    animation: bob 2.2s ease-in-out infinite;
  }

  @keyframes bob {
    50% {
      transform: translate(-50%, 6px);
    }
  }

  .step {
    min-height: 100dvh;
    display: flex;
    align-items: center;
    padding: 80px var(--gutter);
  }

  .card {
    width: min(460px, 100%);
    padding: 26px 28px;
    border-radius: var(--radius-l);
    background: rgba(8, 15, 27, 0.9);
    opacity: 0.35;
    transform: translateY(10px);
    transition:
      opacity 0.6s var(--ease-out),
      transform 0.6s var(--ease-out);
  }

  .card.active {
    opacity: 1;
    transform: none;
  }

  .step-kicker {
    font-size: 12px;
    color: #ffb190;
    margin-bottom: 6px;
  }

  h2 {
    margin: 0 0 14px;
    font-family: var(--font-serif);
    font-size: clamp(26px, 2.6vw, 34px);
    font-weight: 500;
    line-height: 1.1;
    letter-spacing: -0.01em;
  }

  .card p {
    margin: 0 0 12px;
    font-size: 16px;
    line-height: 1.6;
    color: var(--text-2);
  }

  .card p:last-child {
    margin-bottom: 0;
  }

  .note {
    margin-top: 14px !important;
    font-size: 12.5px !important;
    color: var(--text-3) !important;
  }

  .case-link {
    margin-top: 6px;
  }

  .cta {
    justify-content: center;
  }

  .cta-card {
    width: min(620px, 100%);
    text-align: center;
  }

  .cta-card h2 {
    font-size: clamp(32px, 4vw, 48px);
  }

  .cta-buttons {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 10px;
    margin-top: 18px;
  }

  .live-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--s-good);
  }

  @media (max-width: 820px) {
    .shade.hero-shade {
      background: linear-gradient(to top, rgba(4, 8, 15, 0.92) 0%, rgba(4, 8, 15, 0.72) 60%, rgba(4, 8, 15, 0.45) 100%);
    }
    .step {
      align-items: flex-end;
      padding: 40vh 12px 28px;
    }
    .card {
      padding: 20px;
      background: var(--glass-strong);
    }
    .card p {
      font-size: 15px;
    }
    .shade {
      background: linear-gradient(to top, rgba(4, 8, 15, 0.6), transparent 60%);
    }
  }
</style>
