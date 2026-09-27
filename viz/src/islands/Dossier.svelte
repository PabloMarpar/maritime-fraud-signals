<script lang="ts">
  import { onMount } from 'svelte';
  import { loadDossier, loadIndex, loadMeta, type Dossier, type IndexRow, type Meta } from '../lib/data';
  import {
    countryName,
    flagClass,
    fmtDate,
    fmtDateTime,
    fmtMonth,
    kindLabel,
    monthsBetween,
    monthsLabel,
    num,
    shipTypeLabel,
    sourceLabel,
    title,
  } from '../lib/format';
  import { decodePolyline } from '../lib/polyline';
  import { glyphSvg, type Glyph } from '../lib/icons';
  import { localePath, useT, type Lang, type UIKey } from '../i18n/ui';
  import TrackMap, { type MapEvent } from './TrackMap.svelte';

  let { lang }: { lang: Lang } = $props();
  const tr = $derived(useT(lang));

  let mmsi = $state<number | null>(null);
  let dossier = $state.raw<Dossier | null>(null);
  let row = $state.raw<IndexRow | null>(null);
  let meta = $state.raw<Meta | null>(null);
  let loading = $state(true);
  let windowId = $state<string | null>(null);
  let focus = $state<string | null>(null);
  let hoverDay = $state<number | null>(null);

  const windowIds = $derived(
    dossier ? Object.keys(dossier.windows).sort() : [],
  );
  const w = $derived(dossier && windowId ? dossier.windows[windowId] : null);
  const summary = $derived(meta?.windows.find((x) => x.id === windowId) ?? null);
  const listed = $derived((dossier?.sanctions.length ?? 0) > 0 || (row?.[6] ?? 0) > 0);
  const firstDesignation = $derived(
    dossier?.sanctions
      .map((s) => s[4])
      .filter((d): d is string => !!d)
      .sort()[0] ?? null,
  );
  const lastWindowEnd = $derived(windowIds.length ? windowIds[windowIds.length - 1].split('_')[1] : null);
  const monthsAfter = $derived(
    firstDesignation && lastWindowEnd ? monthsBetween(lastWindowEnd, firstDesignation) : null,
  );

  const paths = $derived(w ? w.tracks.map((p) => decodePolyline(p)) : []);

  interface Item {
    id: string;
    kind: 'gap' | 'sts' | 'spoof' | 'behav' | 'identity';
    glyph: Glyph | 'dash';
    t: string;
    label: string;
    detail: string;
    other?: number;
  }

  const items = $derived.by<Item[]>(() => {
    if (!w) return [];
    const out: Item[] = [];
    const e = w.events;
    e.gaps.forEach((g, i) =>
      out.push({
        id: `gap-${i}`,
        kind: 'gap',
        glyph: 'dash',
        t: g[0],
        label: tr('event.gap'),
        detail: `${num(lang, g[2], 0)} h · ${tr(`verdict.${g[4]}` as UIKey)}`,
      }),
    );
    e.sts.forEach((s, i) =>
      out.push({
        id: `sts-${i}`,
        kind: 'sts',
        glyph: 'ring',
        t: s[4],
        label: tr('event.sts'),
        detail: `${title(s[1]) || 'MMSI ' + s[0]} · ${num(lang, s[6], 1)} h`,
        other: s[0],
      }),
    );
    e.spoof.forEach((s, i) =>
      out.push({ id: `spoof-${i}`, kind: 'spoof', glyph: 'diamond', t: s[1], label: kindLabel(lang, s[0]), detail: tr('event.spoof') }),
    );
    e.behav.forEach((b, i) =>
      out.push({ id: `behav-${i}`, kind: 'behav', glyph: 'triangle', t: b[1], label: kindLabel(lang, b[0]), detail: tr('event.behav') }),
    );
    e.identity.forEach((d, i) =>
      out.push({
        id: `identity-${i}`,
        kind: 'identity',
        glyph: 'dot',
        t: d[1],
        label: kindLabel(lang, d[0]),
        detail: d[3] && d[4] ? `${d[3]} → ${d[4]}` : (d[5] ?? tr('event.identity')),
      }),
    );
    return out.sort((a, b) => (a.t < b.t ? -1 : 1));
  });

  const mapEvents = $derived.by<MapEvent[]>(() => {
    if (!w) return [];
    const out: MapEvent[] = [];
    const e = w.events;
    e.gaps.forEach((g, i) => {
      if (g[5] != null && g[7] != null)
        out.push({ id: `gap-${i}`, lon: g[5], lat: g[6], lon2: g[7], lat2: g[8], glyph: 'dot', label: `${tr('event.gap')} · ${fmtDateTime(lang, g[0])}` });
    });
    e.sts.forEach((s, i) => {
      if (s[8] != null) out.push({ id: `sts-${i}`, lon: s[8], lat: s[9], glyph: 'ring', label: `${tr('event.sts')} · ${title(s[1]) || s[0]}` });
    });
    e.spoof.forEach((s, i) => {
      if (s[2] != null) out.push({ id: `spoof-${i}`, lon: s[2], lat: s[3], glyph: 'diamond', label: kindLabel(lang, s[0]) });
    });
    e.behav.forEach((b, i) => {
      if (b[2] != null) out.push({ id: `behav-${i}`, lon: b[2], lat: b[3], glyph: 'triangle', label: kindLabel(lang, b[0]) });
    });
    return out;
  });

  const days = $derived(w?.hours_by_day ?? []);
  const windowStart = $derived(windowId ? windowId.split('_')[0] : null);
  const dayLabel = (i: number) => (windowStart ? fmtDate(lang, Date.parse(`${windowStart}T00:00:00Z`) + i * 86_400_000) : '');

  const kindCounts = $derived.by(() => {
    if (!w) return [];
    const e = w.events;
    return [
      { key: 'gap', glyph: 'dash', n: e.n_gaps, label: tr('event.gap'), desc: tr('event.gap.desc') },
      { key: 'sts', glyph: 'ring', n: e.sts.length, label: tr('event.sts'), desc: tr('event.sts.desc') },
      {
        key: 'spoof',
        glyph: 'diamond',
        n: Object.entries(e.spoof_counts).reduce((a, [k, v]) => (k === 'on_land' ? a : a + v), 0),
        label: tr('event.spoof'),
        desc: tr('event.spoof.desc'),
      },
      { key: 'behav', glyph: 'triangle', n: e.behav.length, label: tr('event.behav'), desc: tr('event.behav.desc') },
      { key: 'identity', glyph: 'dot', n: e.identity.length, label: tr('event.identity'), desc: tr('event.identity.desc') },
    ] as const;
  });

  onMount(async () => {
    const q = new URLSearchParams(location.search);
    const m = Number(q.get('mmsi'));
    if (!m) {
      loading = false;
      return;
    }
    mmsi = m;
    const [d, mt] = await Promise.all([loadDossier(m), loadMeta().catch(() => null)]);
    meta = mt;
    dossier = d;
    if (d) {
      const ids = Object.keys(d.windows).sort();
      windowId = ids.includes(q.get('w') ?? '') ? q.get('w') : ids[ids.length - 1];
      document.title = `${title(d.name) || m} · ${document.title.split(' · ').pop()}`;
    } else {
      const idx = await loadIndex().catch(() => null);
      row = idx?.rows.find((r) => r[0] === m) ?? null;
    }
    loading = false;
  });

  function pickEvent(id: string) {
    focus = focus === id ? null : id;
    document.getElementById(`ev-${id}`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
</script>

<div class="dossier">
  <a class="back" href={localePath(lang, 'vessels/')}>← {tr('nav.vessels')}</a>

  {#if loading}
    <p class="muted">{tr('common.loading')}</p>
  {:else if !dossier}
    <section class="empty">
      {#if row}
        <h1><span class={flagClass(row[3])}></span> {title(row[1]) || `MMSI ${row[0]}`}</h1>
        <p class="ids mono">MMSI {row[0]}{row[2] ? ` · IMO ${row[2]}` : ''} · {shipTypeLabel(lang, row[4])}</p>
      {/if}
      <p>{tr('vessel.notFound')}</p>
      <p class="muted">{tr('vessel.notFoundHint')}</p>
    </section>
  {:else}
    <header class="hero">
      <div class="hero-flag"><span class={flagClass(dossier.iso2)}></span></div>
      <div class="hero-main">
        <div class="eyebrow">{shipTypeLabel(lang, dossier.ship_type)} · {countryName(lang, dossier.iso2) ?? tr('common.unknown')}</div>
        <h1>{title(dossier.name) || `MMSI ${dossier.mmsi}`}</h1>
        <div class="ids mono">
          <span><em>MMSI</em> {dossier.mmsi}</span>
          {#if dossier.imo}<span><em>IMO</em> {dossier.imo}</span>{/if}
          {#if dossier.callsign}<span><em>{tr('vessel.callsign')}</em> {dossier.callsign}</span>{/if}
        </div>
        <div class="badges">
          {#if listed}
            <span class="badge badge-listed">{@html glyphSvg('triangle', 11)} {tr('vessel.listed')}</span>
          {/if}
          {#if dossier.foc}<span class="badge">{tr('vessel.foc')}</span>{/if}
          {#each windowIds as id}
            <span class="badge">{fmtMonth(lang, id.split('_')[0])}</span>
          {/each}
        </div>
      </div>
      <a class="btn explore-link" href={`${localePath(lang, 'explore/')}?w=${windowId}&m=${dossier.mmsi}`}>
        {tr('nav.explore')} →
      </a>
    </header>

    {#if listed && dossier.sanctions.length}
      <section class="sanctions">
        <div class="s-head">
          <div class="eyebrow">{tr('vessel.sanctions')}</div>
          {#if monthsAfter !== null && monthsAfter > 0}
            <p class="s-lede">{tr('vessel.designatedAfter', { months: monthsLabel(lang, monthsAfter) })}</p>
          {:else if monthsAfter !== null}
            <p class="s-lede">{tr('vessel.designatedBefore')}</p>
          {/if}
        </div>
        <table>
          <thead>
            <tr>
              <th>{tr('vessel.source')}</th>
              <th>{tr('vessel.designatedOn')}</th>
              <th>{tr('vessel.listedAs')}</th>
              <th>{tr('vessel.program')}</th>
            </tr>
          </thead>
          <tbody>
            {#each dossier.sanctions as s}
              <tr>
                <td><strong>{sourceLabel(lang, s[0])}</strong></td>
                <td class="mono">{fmtDate(lang, s[4])}</td>
                <td>{s[1] ?? '—'}{s[2] ? ` · ${s[2]}` : ''}</td>
                <td class="prog">{s[3] ?? '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}

    {#if windowIds.length > 1}
      <div class="tabs" role="tablist">
        {#each windowIds as id}
          <button role="tab" aria-selected={id === windowId} class:active={id === windowId} onclick={() => ((windowId = id), (focus = null))}>
            {fmtMonth(lang, id.split('_')[0])}
          </button>
        {/each}
      </div>
    {/if}

    {#if w}
      <div class="grid">
        <div class="map-col">
          <TrackMap {paths} events={mapEvents} lastPos={w.last_pos ? [w.last_pos[0], w.last_pos[1]] : null} {listed} {focus} onpick={pickEvent} />
        </div>
        <aside class="facts-col">
          <dl class="facts">
            <div><dt>{tr('vessel.flag')}</dt><dd><span class={flagClass(dossier.iso2)}></span> {countryName(lang, dossier.iso2) ?? '—'}</dd></div>
            <div><dt>{tr('vessel.type')}</dt><dd>{shipTypeLabel(lang, dossier.ship_type)}</dd></div>
            <div><dt>{tr('vessel.size')}</dt><dd class="mono">{dossier.length ? `${num(lang, dossier.length)} × ${num(lang, dossier.width)} m` : '—'}</dd></div>
            <div><dt>{tr('vessel.draught')}</dt><dd class="mono">{w.draught ? `${num(lang, w.draught[0], 1)}–${num(lang, w.draught[1], 1)} m` : '—'}</dd></div>
            <div><dt>{tr('vessel.messages')}</dt><dd class="mono">{num(lang, w.messages)}</dd></div>
            <div><dt>{tr('vessel.voyages')}</dt><dd class="mono">{num(lang, w.voyages)}</dd></div>
            <div><dt>{tr('vessel.observedDays')}</dt><dd class="mono">{num(lang, w.observed_days)}</dd></div>
            <div><dt>{tr('vessel.lastSeen')}</dt><dd class="mono">{w.last_pos ? fmtDateTime(lang, w.last_pos[2]) : '—'}</dd></div>
          </dl>

          <div class="presence">
            <div class="eyebrow">{tr('vessel.presence')}</div>
            <div class="bars" role="img" aria-label={tr('vessel.presence')}>
              <svg viewBox={`0 0 ${days.length * 10} 60`} preserveAspectRatio="none">
                <line x1="0" x2={days.length * 10} y1="30" y2="30" class="grid-line" />
                {#each days as h, i}
                  <rect
                    x={i * 10 + 1}
                    y={60 - (h / 24) * 60}
                    width="8"
                    height={Math.max((h / 24) * 60, h > 0 ? 1.5 : 0)}
                    rx="1.5"
                    class:listed
                    class:dim={hoverDay !== null && hoverDay !== i}
                  />
                  <rect
                    x={i * 10}
                    y="0"
                    width="10"
                    height="60"
                    class="hit"
                    role="presentation"
                    onmouseenter={() => (hoverDay = i)}
                    onmouseleave={() => (hoverDay = null)}
                  />
                {/each}
              </svg>
              <div class="bars-axis"><span>{dayLabel(0)}</span><span>{dayLabel(days.length - 1)}</span></div>
              {#if hoverDay !== null}
                <div class="bar-tip mono" style:left={`${((hoverDay + 0.5) / days.length) * 100}%`}>
                  {dayLabel(hoverDay)} · {num(lang, days[hoverDay], 1)} h
                </div>
              {/if}
            </div>
          </div>
        </aside>
      </div>

      <div class="cols">
        <section class="card">
          <h2 class="eyebrow">{tr('vessel.destinations')}</h2>
          {#if w.destinations.length}
            <table class="dest">
              <tbody>
                {#each w.destinations.slice(0, 10) as d}
                  <tr>
                    <td class="mono strong">{d[0]}</td>
                    <td class="muted">{fmtDate(lang, d[2])} – {fmtDate(lang, d[3])}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:else}
            <p class="muted">{tr('common.none')}</p>
          {/if}

          {#if w.names.length > 1 || w.callsigns.length > 1}
            <h2 class="eyebrow sub">{tr('vessel.names')}</h2>
            <ul class="plain">
              {#each w.names as n}<li><span class="mono strong">{n[0]}</span> <span class="muted">{fmtDate(lang, n[2])} – {fmtDate(lang, n[3])}</span></li>{/each}
            </ul>
            {#if w.callsigns.length > 1}
              <h2 class="eyebrow sub">{tr('vessel.callsigns')}</h2>
              <ul class="plain">
                {#each w.callsigns as n}<li><span class="mono strong">{n[0]}</span> <span class="muted">{fmtDate(lang, n[2])} – {fmtDate(lang, n[3])}</span></li>{/each}
              </ul>
            {/if}
          {/if}
        </section>

        <section class="card">
          <h2 class="eyebrow">{tr('vessel.events')}</h2>
          <ul class="kinds">
            {#each kindCounts as k}
              <li class:zero={k.n === 0} title={k.desc}>
                <span class="glyph">{@html glyphSvg(k.glyph, 14)}</span>
                <span class="k-label">{k.label}</span>
                <span class="mono k-n">{k.n}</span>
              </li>
            {/each}
          </ul>
          {#if items.length}
            <ol class="timeline">
              {#each items as it (it.id)}
                <li id={`ev-${it.id}`} class:focus={focus === it.id}>
                  <button onclick={() => pickEvent(it.id)}>
                    <span class="glyph">{@html glyphSvg(it.glyph, 13)}</span>
                    <span class="t-main">
                      <span class="t-label">{it.label}</span>
                      <span class="t-detail">{it.detail}</span>
                    </span>
                    <span class="t-time mono">{fmtDateTime(lang, it.t)}</span>
                  </button>
                  {#if it.other}
                    <a class="t-other" href={`${localePath(lang, 'vessel/')}?mmsi=${it.other}`}>→</a>
                  {/if}
                </li>
              {/each}
            </ol>
          {:else}
            <p class="muted">{tr('vessel.noEvents')}</p>
          {/if}
        </section>
      </div>

      <p class="disclaimer">{tr('footer.disclaimer')}</p>
    {/if}
  {/if}
</div>

<style>
  .dossier {
    max-width: 1180px;
    margin: 0 auto;
    padding: 28px var(--gutter) 40px;
  }

  .back {
    display: inline-block;
    margin-bottom: 20px;
    font-size: 13px;
    color: var(--text-3);
    text-decoration: none;
  }

  .back:hover {
    color: var(--text-1);
  }

  .hero {
    display: flex;
    align-items: flex-start;
    gap: 22px;
    padding-bottom: 26px;
    border-bottom: 1px solid var(--line);
  }

  .hero-flag {
    font-size: 54px;
    line-height: 1;
    flex: none;
    padding-top: 6px;
  }

  .hero-flag :global(.flag) {
    border-radius: 6px;
  }

  .hero-main {
    flex: 1;
    min-width: 0;
  }

  h1 {
    margin: 6px 0 10px;
    font-size: clamp(30px, 5vw, 52px);
    font-weight: 750;
    letter-spacing: 0.01em;
    line-height: 1.02;
    overflow-wrap: anywhere;
  }

  .ids {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
    font-size: 13.5px;
    color: var(--text-2);
  }

  .ids em {
    font-style: normal;
    font-family: var(--font-ui);
    font-size: 11px;
    color: var(--text-3);
    margin-right: 4px;
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 14px;
  }

  .explore-link {
    flex: none;
    margin-top: 8px;
  }

  .sanctions {
    margin: 26px 0 0;
    padding: 20px 22px;
    border-radius: var(--radius-l);
    background: linear-gradient(135deg, rgba(217, 89, 38, 0.16), rgba(217, 89, 38, 0.05));
    border: 1px solid rgba(217, 89, 38, 0.45);
  }

  .sanctions .eyebrow {
    color: #ffc4ab;
  }

  .s-lede {
    margin: 6px 0 14px;
    font-family: var(--font-serif);
    font-size: 24px;
    line-height: 1.2;
  }

  .sanctions table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  .sanctions th {
    text-align: left;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-3);
    padding: 6px 10px 6px 0;
    border-bottom: 1px solid rgba(217, 89, 38, 0.3);
  }

  .sanctions td {
    padding: 8px 10px 8px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    vertical-align: top;
  }

  .prog {
    color: var(--text-2);
    max-width: 360px;
  }

  .tabs {
    display: flex;
    gap: 6px;
    margin: 26px 0 0;
  }

  .tabs button {
    height: 32px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: transparent;
    cursor: pointer;
    font-size: 13px;
    color: var(--text-2);
  }

  .tabs button.active {
    background: var(--text-1);
    color: var(--bg-0);
    border-color: var(--text-1);
    font-weight: 600;
  }

  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1.6fr) minmax(280px, 1fr);
    gap: 18px;
    margin-top: 22px;
  }

  .map-col {
    height: 470px;
  }

  .facts-col {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .facts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px 18px;
    margin: 0;
    padding: 18px;
    border-radius: var(--radius-l);
    background: var(--bg-1);
    border: 1px solid var(--line);
  }

  dt {
    font-size: 11px;
    color: var(--text-3);
  }

  dd {
    margin: 2px 0 0;
    font-size: 14px;
  }

  .presence {
    padding: 16px 18px 12px;
    border-radius: var(--radius-l);
    background: var(--bg-1);
    border: 1px solid var(--line);
  }

  .bars {
    position: relative;
    margin-top: 12px;
  }

  .bars svg {
    width: 100%;
    height: 72px;
  }

  .bars rect {
    fill: var(--c-vessel);
    transition: opacity 0.15s;
  }

  .bars rect.listed {
    fill: var(--c-listed);
  }

  .bars rect.dim {
    opacity: 0.35;
  }

  .bars rect.hit {
    fill: transparent;
  }

  .grid-line {
    stroke: rgba(255, 255, 255, 0.08);
    stroke-width: 0.5;
    stroke-dasharray: 2 2;
    vector-effect: non-scaling-stroke;
  }

  .bars-axis {
    display: flex;
    justify-content: space-between;
    font-size: 10.5px;
    color: var(--text-3);
    margin-top: 4px;
  }

  .bar-tip {
    position: absolute;
    top: -30px;
    transform: translateX(-50%);
    padding: 3px 8px;
    border-radius: 6px;
    background: var(--glass-strong);
    border: 1px solid var(--line-strong);
    font-size: 11px;
    white-space: nowrap;
    pointer-events: none;
  }

  .cols {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr);
    gap: 18px;
    margin-top: 18px;
  }

  .card {
    padding: 18px 20px;
    border-radius: var(--radius-l);
    background: var(--bg-1);
    border: 1px solid var(--line);
    min-width: 0;
  }

  .card h2 {
    margin: 0 0 12px;
  }

  .card h2.sub {
    margin-top: 22px;
  }

  .dest {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  .dest td {
    padding: 7px 0;
    border-bottom: 1px dashed var(--line);
  }

  .dest td + td {
    text-align: right;
  }

  .strong {
    color: var(--text-1);
  }

  .muted {
    color: var(--text-3);
    font-size: 12.5px;
  }

  .plain {
    list-style: none;
    margin: 0;
    padding: 0;
    font-size: 13px;
    display: grid;
    gap: 5px;
  }

  .kinds {
    list-style: none;
    margin: 0 0 14px;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 6px;
  }

  .kinds li {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    border-radius: 9px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--line);
    font-size: 12.5px;
  }

  .kinds li.zero {
    opacity: 0.45;
  }

  .k-label {
    flex: 1;
    line-height: 1.25;
  }

  .k-n {
    font-weight: 700;
  }

  .glyph {
    display: inline-grid;
    place-items: center;
    width: 24px;
    height: 24px;
    flex: none;
    border-radius: 7px;
    color: var(--c-event);
    background: var(--c-event-soft);
  }

  .timeline {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 420px;
    overflow-y: auto;
    scrollbar-width: thin;
    border-top: 1px solid var(--line);
  }

  .timeline li {
    display: flex;
    align-items: center;
    border-bottom: 1px solid var(--line);
  }

  .timeline li.focus {
    background: rgba(25, 158, 112, 0.08);
  }

  .timeline button {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
    min-width: 0;
    padding: 10px 6px;
    border: none;
    background: transparent;
    text-align: left;
    cursor: pointer;
  }

  .timeline button:hover {
    background: rgba(255, 255, 255, 0.03);
  }

  .t-main {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }

  .t-label {
    font-size: 13px;
    font-weight: 550;
  }

  .t-detail {
    font-size: 12px;
    color: var(--text-3);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .t-time {
    font-size: 11.5px;
    color: var(--text-2);
    white-space: nowrap;
  }

  .t-other {
    padding: 0 10px;
    color: var(--text-2);
    text-decoration: none;
  }

  .disclaimer {
    margin: 26px 0 0;
    font-size: 12.5px;
    color: var(--text-3);
    max-width: 70ch;
  }

  .empty {
    padding: 40px 0;
  }

  .empty h1 {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  @media (max-width: 900px) {
    .grid,
    .cols {
      grid-template-columns: 1fr;
    }
    .map-col {
      height: 360px;
    }
    .hero {
      flex-wrap: wrap;
    }
    .hero-flag {
      font-size: 38px;
    }
    .sanctions table thead {
      display: none;
    }
    .sanctions tr {
      display: grid;
      grid-template-columns: 1fr auto;
      padding: 8px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .sanctions td {
      border: none;
      padding: 2px 0;
    }
    .sanctions td.prog {
      grid-column: 1 / -1;
    }
  }
</style>
