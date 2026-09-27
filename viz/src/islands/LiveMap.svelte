<script lang="ts">
  import { onMount } from 'svelte';
  import type { Map as MLMap } from 'maplibre-gl';
  import { MapboxOverlay } from '@deck.gl/mapbox';
  import { IconLayer, PathLayer, ScatterplotLayer, TextLayer } from '@deck.gl/layers';
  import { createBaseMap } from '../lib/basemap';
  import { loadIndex, loadSanctions, type Designation, type SanctionsByImo } from '../lib/data';
  import { RGB, withAlpha } from '../lib/colors';
  import { glyphSvg, iconAtlas } from '../lib/icons';
  import { countryName, flagClass, fmtDate, num, shipTypeLabel, sourceLabel, title } from '../lib/format';
  import { localePath, useT, type Lang, type UIKey } from '../i18n/ui';

  let { lang }: { lang: Lang } = $props();
  const tr = $derived(useT(lang));

  const RELAY_URL: string = import.meta.env.PUBLIC_LIVE_URL ?? 'ws://127.0.0.1:8765';
  const HISTORY_S = 60 * 60; // trail length
  const EXTRAPOLATE_MAX_S = 180;

  const REGIONS: Record<string, { key: UIKey; bounds: [number, number, number, number] }> = {
    dk: { key: 'live.region.dk', bounds: [7.4, 54.2, 15.6, 58.2] },
    gib: { key: 'live.region.gib', bounds: [-6.2, 35.72, -4.95, 36.32] },
  };

  /** A relay record (see ingest/aisstream.py's apply_message) plus a short position history. */
  interface Live {
    m: number;
    la: number;
    lo: number;
    s?: number;
    c?: number;
    h?: number;
    n?: number;
    t: number;
    r: string;
    k?: string;
    f?: string;
    nm?: string;
    cs?: string;
    imo?: string;
    ty?: string;
    tc?: number;
    d?: string;
    dr?: number;
    eta?: string;
    L?: number;
    W?: number;
    st?: number;
    trail: [number, number, number][]; // lon, lat, t
  }

  let container: HTMLDivElement;
  let map = $state.raw<MLMap | null>(null);
  let overlay: MapboxOverlay | null = null;

  const vessels = new Map<number, Live>();
  let version = $state(0);
  let status = $state<'connecting' | 'live' | 'offline'>('connecting');
  let upstream = $state<string>('connecting');
  let upstreamError = $state<string | null>(null);
  let rate = $state(0);
  let clockSkew = 0; // server time - local time, seconds
  let region = $state<'dk' | 'gib'>('dk');
  let tankersOnly = $state(false);
  let selected = $state<number | null>(null);
  let hover = $state<{ x: number; y: number; html: string } | null>(null);
  let now = $state(Date.now() / 1000);

  let sanctions = $state.raw<SanctionsByImo>({});
  let archive = $state.raw<Map<number, { listed: boolean; dossier: boolean }>>(new Map());

  function designations(v: Live): Designation[] {
    return (v.imo && sanctions[v.imo]) || [];
  }

  function isListedLive(v: Live): boolean {
    return designations(v).length > 0 || !!archive.get(v.m)?.listed;
  }

  function visible(v: Live): boolean {
    return v.r === region && (!tankersOnly || v.ty === 'Tanker' || isListedLive(v));
  }

  const inView = $derived.by(() => {
    void version;
    const out: Live[] = [];
    vessels.forEach((v) => visible(v) && out.push(v));
    return out;
  });

  const notable = $derived(
    inView
      .filter((v) => v.ty === 'Tanker' || isListedLive(v))
      .sort((a, b) => Number(isListedLive(b)) - Number(isListedLive(a)) || (a.nm ?? '~').localeCompare(b.nm ?? '~'))
      .slice(0, 40),
  );
  const listedInView = $derived(inView.filter((v) => isListedLive(v)).length);
  const sel = $derived.by(() => {
    void version;
    return selected !== null ? (vessels.get(selected) ?? null) : null;
  });

  /** Dead reckoning: advance the last fix along course at reported speed, capped. */
  function position(v: Live, t: number): [number, number] {
    const age = Math.min(Math.max(0, t - v.t), EXTRAPOLATE_MAX_S);
    if (!v.s || v.s < 0.5 || v.s > 40 || v.c === undefined || age === 0) return [v.lo, v.la];
    const dist = (v.s * 1852 * age) / 3600; // metres
    const rad = (v.c * Math.PI) / 180;
    const dLat = (dist * Math.cos(rad)) / 111_320;
    const dLon = (dist * Math.sin(rad)) / (111_320 * Math.cos((v.la * Math.PI) / 180));
    return [v.lo + dLon, v.la + dLat];
  }

  function heading(v: Live): number | null {
    if (v.h !== undefined) return v.h;
    if (v.c !== undefined && (v.s ?? 0) >= 0.5) return v.c;
    return null;
  }

  function ingest(records: any[]) {
    for (const r of records) {
      const prev = vessels.get(r.m);
      const trail = prev?.trail ?? [];
      if (!prev || prev.la !== r.la || prev.lo !== r.lo) trail.push([r.lo, r.la, r.t]);
      while (trail.length && trail[0][2] < r.t - HISTORY_S) trail.shift();
      vessels.set(r.m, { ...r, trail });
    }
  }

  let ws: WebSocket | null = null;
  let retry: ReturnType<typeof setTimeout>;

  function connect() {
    status = 'connecting';
    try {
      ws = new WebSocket(RELAY_URL);
    } catch {
      status = 'offline';
      retry = setTimeout(connect, 4000);
      return;
    }
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'snapshot') {
        vessels.clear();
        ingest(msg.vessels);
      } else if (msg.type === 'update') {
        ingest(msg.vessels);
        for (const m of msg.removed as number[]) vessels.delete(m);
      }
      if (msg.stats) {
        upstream = msg.stats.upstream;
        upstreamError = msg.stats.error;
        rate = msg.stats.msgs_per_s;
        clockSkew = msg.stats.server_time - Date.now() / 1000;
      }
      status = 'live';
      version++;
    };
    ws.onclose = () => {
      status = 'offline';
      retry = setTimeout(connect, 4000);
    };
    ws.onerror = () => ws?.close();
  }

  function vesselTip(v: Live): string {
    const listed = isListedLive(v)
      ? `<div class="tip-listed">${glyphSvg('triangle', 10)} ${tr('vessel.listed')}</div>`
      : '';
    return `<div class="tip-title"><span class="${flagClass(v.f)}"></span><strong>${title(v.nm) || 'MMSI ' + v.m}</strong></div>
      <div class="tip-sub">${shipTypeLabel(lang, v.ty)} · ${num(lang, v.s, 1)} kn</div>${listed}`;
  }

  function render() {
    if (!overlay) return;
    const t = Date.now() / 1000 + clockSkew;
    const atlas = iconAtlas();
    const list = inView;
    const listed = list.filter(isListedLive);
    const selectedLive = selected !== null ? vessels.get(selected) : undefined;
    overlay.setProps({
      layers: [
        new PathLayer({
          id: 'trails',
          data: list.filter((v) => v.trail.length > 1),
          getPath: (v: Live) => v.trail.map((p) => [p[0], p[1]]),
          getColor: (v: Live) => withAlpha(isListedLive(v) ? RGB.listed : RGB.vessel, 0.35),
          widthUnits: 'pixels',
          getWidth: 1.4,
          capRounded: true,
          jointRounded: true,
          updateTriggers: { getPath: [version], getColor: [version, sanctions] },
        }),
        new ScatterplotLayer({
          id: 'listed-halo',
          data: listed,
          getPosition: (v: Live) => position(v, t),
          getRadius: 12,
          radiusUnits: 'pixels',
          stroked: true,
          filled: false,
          getLineColor: withAlpha(RGB.listed, 0.8),
          lineWidthUnits: 'pixels',
          getLineWidth: 1.3,
          updateTriggers: { getPosition: [t] },
        }),
        new IconLayer({
          id: 'vessels',
          data: list,
          iconAtlas: atlas.url,
          iconMapping: atlas.mapping,
          getIcon: (v: Live) => (heading(v) === null ? 'boatStill' : 'boat'),
          getAngle: (v: Live) => -(heading(v) ?? 0),
          getPosition: (v: Live) => position(v, t),
          getSize: (v: Live) => (v.m === selected ? 22 : isListedLive(v) ? 17 : v.ty === 'Tanker' ? 14 : 11),
          sizeUnits: 'pixels',
          getColor: (v: Live) =>
            v.m === selected
              ? [255, 255, 255, 255]
              : withAlpha(isListedLive(v) ? RGB.listed : RGB.vessel, v.ty === 'Tanker' || isListedLive(v) ? 1 : 0.7),
          pickable: true,
          updateTriggers: { getPosition: [t], getSize: [selected, version], getColor: [selected, version, sanctions], getIcon: [version], getAngle: [version] },
        }),
        new TextLayer({
          id: 'labels',
          data: list.filter((v) => v.m === selected || (isListedLive(v) && v.nm)),
          getPosition: (v: Live) => position(v, t),
          getText: (v: Live) => title(v.nm) || String(v.m),
          getSize: 11.5,
          getColor: [238, 242, 247, 235],
          fontFamily: 'Inter Variable, system-ui, sans-serif',
          fontWeight: 600,
          characterSet: 'auto',
          getTextAnchor: 'start',
          getAlignmentBaseline: 'center',
          getPixelOffset: [14, 0],
          background: true,
          getBackgroundColor: [4, 8, 15, 190],
          backgroundPadding: [5, 2, 5, 2],
          updateTriggers: { getPosition: [t], getText: [version] },
        }),
        ...(selectedLive
          ? [
              new ScatterplotLayer({
                id: 'selected-ring',
                data: [selectedLive],
                getPosition: (v: Live) => position(v, t),
                getRadius: 18,
                radiusUnits: 'pixels',
                stroked: true,
                filled: false,
                getLineColor: [255, 255, 255, 200],
                lineWidthUnits: 'pixels',
                getLineWidth: 1.5,
                updateTriggers: { getPosition: [t] },
              }),
            ]
          : []),
      ],
    });
  }

  let raf = 0;
  let lastRender = 0;
  function frame(ts: number) {
    // ~15 fps is plenty for ships and keeps the CPU quiet.
    if (ts - lastRender > 66) {
      lastRender = ts;
      render();
    }
    raf = requestAnimationFrame(frame);
  }

  function fitRegion(r: 'dk' | 'gib', animate = true) {
    const [w, s, e, n] = REGIONS[r].bounds;
    map?.fitBounds(
      [
        [w, s],
        [e, n],
      ],
      { padding: window.innerWidth < 720 ? 20 : { top: 80, bottom: 40, left: 360, right: 40 }, duration: animate ? 1400 : 0 },
    );
  }

  function selectVessel(m: number | null) {
    selected = m;
    const v = m !== null ? vessels.get(m) : null;
    if (v && map) map.easeTo({ center: position(v, Date.now() / 1000 + clockSkew), zoom: Math.max(map.getZoom(), 9), duration: 900 });
  }

  function ago(t: number): string {
    const s = Math.max(0, Math.round(now + clockSkew - t));
    return s < 90 ? tr('live.secondsAgo', { s }) : tr('live.minutesAgo', { m: Math.round(s / 60) });
  }

  onMount(() => {
    let destroyed = false;
    const tick = setInterval(() => (now = Date.now() / 1000), 1000);
    const q = new URLSearchParams(location.search);
    if (q.get('r') === 'gib') region = 'gib';
    loadSanctions().then((s) => (sanctions = s)).catch(() => {});
    loadIndex()
      .then((idx) => (archive = new Map(idx.rows.map((r) => [r[0], { listed: !!r[6], dossier: !!r[7] }]))))
      .catch(() => {});
    (async () => {
      const [w, s, e, n] = REGIONS[region].bounds;
      map = await createBaseMap({ container, lang, bounds: [w, s, e, n], navigation: true, maxZoom: 15 });
      if (destroyed) return;
      overlay = new MapboxOverlay({
        interleaved: true,
        onHover: (info: any) => {
          container.style.cursor = info.object ? 'pointer' : '';
          hover = info.object && info.layer?.id === 'vessels' ? { x: info.x, y: info.y, html: vesselTip(info.object) } : null;
        },
        onClick: (info: any) => {
          if (info.object && (info.layer?.id === 'vessels' || info.layer?.id === 'labels')) selectVessel(info.object.m);
          else selected = null;
        },
      });
      map.addControl(overlay as any);
      fitRegion(region, false);
      connect();
      raf = requestAnimationFrame(frame);
    })();
    return () => {
      destroyed = true;
      clearInterval(tick);
      clearTimeout(retry);
      cancelAnimationFrame(raf);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
      overlay?.finalize();
      map?.remove();
    };
  });

  $effect(() => {
    history.replaceState(null, '', `${location.pathname}${region === 'gib' ? '?r=gib' : ''}`);
  });
</script>

<div class="live">
  <div class="map" bind:this={container}></div>

  <div class="top-left">
    <section class="card glass">
      <div class="title-row">
        <h1>{tr('live.pageTitle')}</h1>
        <span class="status" class:ok={status === 'live' && upstream === 'live'} class:warn={status === 'connecting' || (status === 'live' && upstream !== 'live')} class:bad={status === 'offline'}>
          <i></i>
          {status === 'offline' ? tr('live.status.offline') : status === 'connecting' ? tr('live.status.connecting') : upstream === 'live' ? tr('live.status.live') : tr('live.reconnecting')}
        </span>
      </div>

      <div class="regions" role="tablist" aria-label={tr('live.region')}>
        {#each Object.entries(REGIONS) as [key, r]}
          <button role="tab" aria-selected={region === key} class:active={region === key} onclick={() => ((region = key as 'dk' | 'gib'), (selected = null), fitRegion(key as 'dk' | 'gib'))}>
            {tr(r.key)}
          </button>
        {/each}
      </div>

      {#if status === 'live'}
        <div class="stats">
          <div><span class="stat mono">{num(lang, inView.length)}</span><span class="stat-label">{tr('live.inView')}</span></div>
          <div><span class="stat mono listed">{num(lang, listedInView)}</span><span class="stat-label">{tr('live.listedInView')}</span></div>
          <div><span class="stat mono">{num(lang, rate, 0)}</span><span class="stat-label">msg/s</span></div>
        </div>
        <label class="check">
          <input type="checkbox" bind:checked={tankersOnly} />
          <span>{tr('live.filter.tankers')}</span>
        </label>
        {#if upstreamError}
          <p class="err">{tr('live.error', { e: upstreamError })}</p>
        {/if}
      {/if}
    </section>

    {#if status === 'live'}
      <section class="card glass notable">
        <div class="eyebrow">{tr('live.notable')}</div>
        {#if notable.length}
          <ul>
            {#each notable as v (v.m)}
              <li>
                <button class:active={selected === v.m} onclick={() => selectVessel(v.m)}>
                  <span class={flagClass(v.f)}></span>
                  <span class="n-name">{title(v.nm) || v.m}</span>
                  {#if isListedLive(v)}<span class="dot-listed" title={tr('vessel.listed')}></span>{/if}
                  <span class="n-speed mono">{num(lang, v.s, 1)} kn</span>
                </button>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="muted">{tr('live.notable.empty')}</p>
        {/if}
      </section>
    {/if}
  </div>

  {#if status === 'offline'}
    <div class="offline glass">
      <div class="eyebrow">{tr('live.status.offline')}</div>
      <h2>{tr('live.offline.title')}</h2>
      <p>{tr('live.offline.body')}</p>
      <ol>
        <li>{tr('live.offline.step1')}</li>
        <li>{tr('live.offline.step2')} <code>python -m ingest.aisstream</code></li>
      </ol>
      <p class="relay mono">{RELAY_URL}</p>
    </div>
  {/if}

  {#if sel}
    {@const d = designations(sel)}
    {@const known = archive.get(sel.m)}
    <aside class="panel glass">
      <header>
        <div class="p-title">
          <span class={flagClass(sel.f)}></span>
          <h2>{title(sel.nm) || `MMSI ${sel.m}`}</h2>
          <button class="icon-btn close" onclick={() => (selected = null)} aria-label={tr('common.close')}>
            <svg width="14" height="14" viewBox="0 0 14 14"><path d="M2 2l10 10M12 2 2 12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" /></svg>
          </button>
        </div>
        <div class="badges">
          <span class="badge badge-vessel">{@html glyphSvg('boat', 12)} {shipTypeLabel(lang, sel.ty)}</span>
          {#if isListedLive(sel)}
            <span class="badge badge-listed">{@html glyphSvg('triangle', 11)} {tr('vessel.listed')}</span>
          {/if}
        </div>
      </header>

      <div class="motion">
        <div><span class="m-val mono">{num(lang, sel.s, 1)}</span><span class="m-unit">kn</span><span class="m-label">{tr('live.speed')}</span></div>
        <div><span class="m-val mono">{sel.c !== undefined ? Math.round(sel.c) : '—'}</span><span class="m-unit">°</span><span class="m-label">{tr('live.course')}</span></div>
        <div><span class="m-val mono">{sel.h !== undefined ? sel.h : '—'}</span><span class="m-unit">°</span><span class="m-label">{tr('live.heading')}</span></div>
      </div>

      <dl class="facts">
        <div><dt>{tr('vessel.destination')}</dt><dd class="mono">{sel.d ?? '—'}</dd></div>
        <div><dt>{tr('live.eta')}</dt><dd class="mono">{sel.eta ?? '—'}</dd></div>
        <div><dt>{tr('vessel.flag')}</dt><dd>{countryName(lang, sel.f) ?? '—'}</dd></div>
        <div><dt>{tr('live.navStatus')}</dt><dd>{sel.n !== undefined ? tr(`navstatus.${sel.n}` as UIKey) : '—'}</dd></div>
        <div><dt>{tr('vessel.mmsi')}</dt><dd class="mono">{sel.m}</dd></div>
        <div><dt>{tr('vessel.imo')}</dt><dd class="mono">{sel.imo ?? '—'}</dd></div>
        <div><dt>{tr('vessel.callsign')}</dt><dd class="mono">{sel.cs ?? '—'}</dd></div>
        <div><dt>{tr('vessel.size')}</dt><dd class="mono">{sel.L ? `${num(lang, sel.L)} × ${num(lang, sel.W)} m` : '—'}</dd></div>
        <div><dt>{tr('vessel.draught')}</dt><dd class="mono">{sel.dr ? `${num(lang, sel.dr, 1)} m` : '—'}</dd></div>
        <div><dt>{tr('live.lastReport')}</dt><dd class="mono">{ago(sel.t)}</dd></div>
      </dl>

      {#if !sel.st}
        <p class="muted small">{tr('live.waitingStatic')}</p>
      {/if}

      {#if d.length}
        <section class="sanctions">
          <div class="eyebrow">{tr('live.sanctionedMatch')}</div>
          <ul>
            {#each d.slice(0, 4) as s}
              <li><strong>{sourceLabel(lang, s[0])}</strong> · {fmtDate(lang, s[4])}{s[1] ? ` · ${s[1]}` : ''}</li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if known?.dossier}
        <a class="btn btn-primary" href={`${localePath(lang, 'vessel/')}?mmsi=${sel.m}`}>{tr('live.knownArchive')} →</a>
      {/if}
    </aside>
  {/if}

  {#if status === 'live'}
    <p class="hint">{tr('live.hint')}</p>
  {/if}

  {#if hover}
    <div class="tooltip glass" style:left={`${hover.x}px`} style:top={`${hover.y}px`}>{@html hover.html}</div>
  {/if}
</div>

<style>
  .live {
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
    bottom: 16px;
    width: 318px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    z-index: 5;
    pointer-events: none;
  }

  .top-left > * {
    pointer-events: auto;
  }

  .card {
    padding: 14px 16px;
  }

  .title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  h1 {
    margin: 0;
    font-family: var(--font-serif);
    font-size: 26px;
    font-weight: 500;
  }

  .status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    height: 26px;
    padding: 0 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--line-strong);
    color: var(--text-2);
  }

  .status i {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-3);
  }

  .status.ok i {
    background: var(--s-good);
    animation: blink 1.6s ease-in-out infinite;
  }

  .status.warn i {
    background: var(--s-warning);
  }

  .status.bad i {
    background: var(--s-critical);
  }

  @keyframes blink {
    50% {
      opacity: 0.35;
    }
  }

  .regions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 14px 0 12px;
  }

  .regions button {
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: transparent;
    font-size: 12.5px;
    color: var(--text-2);
    cursor: pointer;
    white-space: nowrap;
  }

  .regions button.active {
    background: var(--text-1);
    color: var(--bg-0);
    border-color: var(--text-1);
    font-weight: 600;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    padding: 10px 0;
    border-top: 1px solid var(--line);
  }

  .stat {
    display: block;
    font-size: 19px;
    font-weight: 600;
  }

  .stat.listed {
    color: #ff9a70;
  }

  .stat-label {
    display: block;
    font-size: 10.5px;
    color: var(--text-3);
    line-height: 1.3;
  }

  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    color: var(--text-2);
    cursor: pointer;
  }

  .check input {
    accent-color: var(--text-1);
  }

  .err {
    margin: 10px 0 0;
    font-size: 12px;
    color: #ffb4b4;
  }

  .notable {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 0 1 auto;
    overflow: hidden;
  }

  .notable ul {
    list-style: none;
    margin: 8px -6px 0;
    padding: 0;
    overflow-y: auto;
    scrollbar-width: thin;
  }

  .notable button {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 7px 6px;
    border: none;
    border-radius: 7px;
    background: transparent;
    text-align: left;
    font-size: 12.5px;
    cursor: pointer;
  }

  .notable button:hover,
  .notable button.active {
    background: rgba(255, 255, 255, 0.07);
  }

  .n-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 600;
  }

  .n-speed {
    font-size: 11px;
    color: var(--text-3);
  }

  .dot-listed {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--c-listed);
    flex: none;
  }

  .muted {
    margin: 8px 0 0;
    font-size: 12.5px;
    color: var(--text-3);
  }

  .small {
    font-size: 12px;
  }

  .offline {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(480px, calc(100vw - 32px));
    padding: 24px 26px;
    z-index: 10;
  }

  .offline h2 {
    margin: 6px 0 10px;
    font-family: var(--font-serif);
    font-size: 26px;
    font-weight: 500;
    line-height: 1.15;
  }

  .offline p {
    margin: 0 0 12px;
    color: var(--text-2);
    font-size: 14px;
  }

  .offline ol {
    margin: 0 0 14px;
    padding-left: 20px;
    font-size: 13.5px;
    display: grid;
    gap: 6px;
  }

  code {
    font-family: var(--font-mono);
    font-size: 12.5px;
    padding: 2px 6px;
    border-radius: 5px;
    background: rgba(255, 255, 255, 0.08);
  }

  .relay {
    font-size: 11.5px;
    color: var(--text-3);
  }

  .panel {
    position: absolute;
    top: calc(var(--header-h) + 12px);
    right: 60px;
    width: 330px;
    max-height: calc(100% - var(--header-h) - 40px);
    overflow-y: auto;
    padding: 18px;
    z-index: 6;
    display: flex;
    flex-direction: column;
    gap: 14px;
    animation: slide-in 0.35s var(--ease-out);
  }

  @keyframes slide-in {
    from {
      opacity: 0;
      transform: translateX(16px);
    }
  }

  .p-title {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .p-title .flag {
    font-size: 20px;
  }

  .p-title h2 {
    flex: 1;
    margin: 0;
    font-size: 19px;
    font-weight: 700;
    letter-spacing: 0.02em;
    overflow-wrap: anywhere;
  }

  .close {
    width: 30px;
    height: 30px;
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
  }

  .motion {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }

  .motion > div {
    padding: 10px;
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--line);
  }

  .m-val {
    font-size: 20px;
    font-weight: 650;
  }

  .m-unit {
    margin-left: 2px;
    font-size: 11px;
    color: var(--text-3);
  }

  .m-label {
    display: block;
    font-size: 10.5px;
    color: var(--text-3);
  }

  .facts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 14px;
    margin: 0;
  }

  dt {
    font-size: 11px;
    color: var(--text-3);
  }

  dd {
    margin: 1px 0 0;
    font-size: 13px;
    overflow-wrap: anywhere;
  }

  .sanctions {
    padding: 12px 14px;
    border-radius: 10px;
    background: var(--c-listed-soft);
    border: 1px solid rgba(217, 89, 38, 0.45);
  }

  .sanctions .eyebrow {
    color: #ffc4ab;
  }

  .sanctions ul {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    font-size: 12.5px;
    color: var(--text-2);
    display: grid;
    gap: 4px;
  }

  .hint {
    position: absolute;
    right: 16px;
    bottom: 34px;
    margin: 0;
    padding: 5px 12px;
    border-radius: 999px;
    background: rgba(4, 8, 15, 0.7);
    border: 1px solid var(--line);
    font-size: 11.5px;
    color: var(--text-3);
    z-index: 4;
  }

  .tooltip {
    position: absolute;
    z-index: 30;
    transform: translate(14px, -50%);
    padding: 9px 12px;
    font-size: 12.5px;
    pointer-events: none;
    border-radius: 10px;
  }

  .tooltip :global(.tip-title) {
    display: flex;
    align-items: center;
    gap: 7px;
  }

  .tooltip :global(.tip-sub) {
    margin-top: 3px;
    color: var(--text-2);
  }

  .tooltip :global(.tip-listed) {
    display: flex;
    align-items: center;
    gap: 5px;
    margin-top: 6px;
    font-size: 11px;
    font-weight: 600;
    color: #ffc4ab;
  }

  @media (max-width: 720px) {
    .top-left {
      left: 10px;
      right: 10px;
      width: auto;
      bottom: auto;
    }
    .notable {
      display: none;
    }
    .panel {
      left: 10px;
      right: 10px;
      top: auto;
      bottom: 10px;
      width: auto;
      max-height: 55dvh;
    }
    .hint {
      display: none;
    }
  }
</style>
