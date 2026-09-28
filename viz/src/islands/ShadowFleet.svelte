<script lang="ts">
  /**
   * The shadow-fleet page: every sanctioned vessel (by IMO) seen in the exported windows, from
   * shadow.json (report.export_viz.shadow_fleet). A static hero map of their routes, four
   * headline figures, when/which flag/why/who charts, and a searchable, sortable table linking
   * to each dossier. Everything is computed here from the file, so it follows new exports.
   */
  import { onMount } from 'svelte';
  import type { Map as MLMap } from 'maplibre-gl';
  import { MapboxOverlay } from '@deck.gl/mapbox';
  import { PathLayer } from '@deck.gl/layers';
  import { createBaseMap, setDensity } from '../lib/basemap';
  import { densityUrl, loadFleet, loadMeta, loadShadow, loadTracks, type Fleet, type ShadowVessel, type Tracks } from '../lib/data';
  import { RGB, withAlpha } from '../lib/colors';
  import { glyphSvg } from '../lib/icons';
  import { countryName, flagClass, fmtDate, fmtMonth, num, shipTypeLabel, sourceLabel, title } from '../lib/format';
  import { LOCALE, localePath, type Lang } from '../i18n/ui';
  import { SHADOW } from '../content/shadow';
  import MonthBars, { type MonthBin } from './MonthBars.svelte';
  import RankBars from './RankBars.svelte';

  let { lang }: { lang: Lang } = $props();
  const text = $derived(SHADOW[lang]);
  const fill = (s: string, vars: Record<string, string | number>) =>
    s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? ''));

  const VIEW: [number, number, number, number] = [4.6, 53.9, 17.6, 58.4];
  const DAY_MONTH = 30.44;

  let container: HTMLDivElement;
  let map: MLMap | null = null;
  let overlay: MapboxOverlay | null = null;

  let vessels = $state.raw<ShadowVessel[]>([]);
  let snapshot = $state<string | null>(null);
  let failed = $state(false);

  // ---- phrases for the months covered -------------------------------------------------------
  const months = $derived([...new Set(vessels.flatMap((v) => v.windows.map((w) => w.slice(0, 7))))].sort());

  function monthName(m: string) {
    const [y, mo] = m.split('-').map(Number);
    return new Intl.DateTimeFormat(LOCALE[lang], { month: 'long', timeZone: 'UTC' }).format(Date.UTC(y, mo - 1, 1));
  }
  const monthYear = (m: string) => (lang === 'es' ? `${monthName(m)} de ${m.slice(0, 4)}` : `${monthName(m)} ${m.slice(0, 4)}`);
  const joinAnd = (xs: string[]) =>
    xs.length <= 1 ? (xs[0] ?? '') : `${xs.slice(0, -1).join(', ')} ${lang === 'es' ? 'y' : 'and'} ${xs[xs.length - 1]}`;

  function monthsList(ms: string[]): string {
    if (!ms.length) return '';
    if (ms.length > 3) {
      return lang === 'es' ? `de ${monthYear(ms[0])} a ${monthYear(ms[ms.length - 1])}` : `${monthYear(ms[0])} to ${monthYear(ms[ms.length - 1])}`;
    }
    const years = [...new Set(ms.map((m) => m.slice(0, 4)))];
    return joinAnd(
      years.map((y) => {
        const names = joinAnd(ms.filter((m) => m.startsWith(y)).map(monthName));
        return lang === 'es' ? `${names} de ${y}` : `${names} ${y}`;
      }),
    );
  }

  function whenPhrase(ms: string[]): string {
    if (ms.length > 3) {
      return lang === 'es' ? `Entre ${monthYear(ms[0])} y ${monthYear(ms[ms.length - 1])}` : `Between ${monthYear(ms[0])} and ${monthYear(ms[ms.length - 1])}`;
    }
    return `${lang === 'es' ? 'En' : 'In'} ${monthsList(ms)}`;
  }

  // ---- headline figures ------------------------------------------------------------------------
  const isLater = (v: ShadowVessel) => v.lead_days !== null && v.lead_days > 0;
  const stats = $derived.by(() => {
    const dated = vessels.filter((v) => v.lead_days !== null);
    const later = dated.filter(isLater);
    const leads = later.map((v) => v.lead_days!).sort((a, b) => a - b);
    const median = leads.length
      ? leads.length % 2
        ? leads[(leads.length - 1) / 2]
        : (leads[leads.length / 2 - 1] + leads[leads.length / 2]) / 2
      : 0;
    return {
      n: vessels.length,
      later: later.length,
      pctLater: dated.length ? (later.length / dated.length) * 100 : 0,
      medianMonths: Math.round(median / DAY_MONTH),
      flagChanges: vessels.filter((v) => v.flags.length > 1).length,
    };
  });

  // ---- charts -----------------------------------------------------------------------------------
  const FIRST_MONTH = '2022-01';
  const nextMonth = (m: string) => {
    const [y, mo] = m.split('-').map(Number);
    return mo === 12 ? `${y + 1}-01` : `${y}-${String(mo + 1).padStart(2, '0')}`;
  };

  const bins = $derived.by((): MonthBin[] => {
    const dated = vessels.filter((v) => v.designated);
    if (!dated.length) return [];
    const keys = dated.map((v) => v.designated!.slice(0, 7));
    const all = [...keys, ...months].sort();
    let start = all[0] < FIRST_MONTH ? FIRST_MONTH : all[0];
    const end = all[all.length - 1];
    const out: MonthBin[] = [];
    const byKey = new Map<string, MonthBin>();
    if (all[0] < FIRST_MONTH) {
      const b: MonthBin = { key: 'earlier', label: lang === 'es' ? 'Antes de 2022' : 'Before 2022', tick: '≤21', later: 0, before: 0, names: [] };
      out.push(b);
      byKey.set('earlier', b);
    }
    for (let m = start; m <= end; m = nextMonth(m)) {
      const b: MonthBin = {
        key: m,
        label: fmtMonth(lang, `${m}-01`),
        // January ticks; the first one is dropped when the collapsed "earlier" bar sits next to it.
        tick: m.endsWith('-01') && !(m === FIRST_MONTH && byKey.has('earlier')) ? `’${m.slice(2, 4)}` : null,
        later: 0,
        before: 0,
        names: [],
      };
      out.push(b);
      byKey.set(m, b);
    }
    for (const v of dated) {
      const k = v.designated!.slice(0, 7);
      const b = byKey.get(k < FIRST_MONTH ? 'earlier' : k);
      if (!b) continue;
      if (isLater(v)) b.later++;
      else b.before++;
      if (v.name) b.names.push(title(v.name));
    }
    return out;
  });
  const observed = $derived(new Set(months));

  const flagRows = $derived.by(() => {
    const counts = new Map<string, number>();
    for (const v of vessels) {
      const f = v.flags[0] ?? '??';
      counts.set(f, (counts.get(f) ?? 0) + 1);
    }
    const sorted = [...counts].sort((a, b) => b[1] - a[1]);
    const top = sorted.slice(0, 9).map(([iso2, value]) => ({
      key: iso2,
      label: countryName(lang, iso2) ?? iso2,
      value,
      flag: flagClass(iso2),
    }));
    const rest = sorted.slice(9).reduce((a, [, n]) => a + n, 0);
    return rest ? [...top, { key: 'other', label: text.flagsOther, value: rest }] : top;
  });

  const regimeRows = $derived.by(() => {
    const counts = { russia: 0, iran: 0, other: 0 };
    for (const v of vessels) if (v.designations.length) counts[v.designations[0][2]]++;
    return (['russia', 'iran', 'other'] as const)
      .filter((k) => counts[k])
      .map((k) => ({ key: k, label: text.regime[k], value: counts[k] }));
  });

  const sourceRows = $derived.by(() => {
    let uk = 0;
    let us = 0;
    let both = 0;
    for (const v of vessels) {
      const s = new Set(v.designations.map((d) => d[0]));
      if (s.has('uk') && s.has('ofac')) both++;
      else if (s.has('uk')) uk++;
      else if (s.has('ofac')) us++;
    }
    return [
      { key: 'uk', label: text.sourceUk, value: uk },
      { key: 'us', label: text.sourceUs, value: us },
      { key: 'both', label: text.sourceBoth, value: both },
    ];
  });

  // ---- table --------------------------------------------------------------------------------------
  type Filter = 'all' | 'later' | 'before';
  type SortKey = 'name' | 'first' | 'designated' | 'lead';
  let q = $state('');
  let filter = $state<Filter>('all');
  let sortKey = $state<SortKey>('designated');
  let sortDesc = $state(true);

  const norm = (s: string) => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const haystack = $derived(
    new Map(
      vessels.map((v) => [
        v,
        norm([v.imo ?? '', ...v.names, ...v.flags.map((f) => countryName(lang, f) ?? f)].join(' ')),
      ]),
    ),
  );

  const counts = $derived({
    all: vessels.length,
    later: vessels.filter(isLater).length,
    before: vessels.filter((v) => !isLater(v)).length,
  });

  const rows = $derived.by(() => {
    const needle = norm(q.trim());
    const out = vessels.filter(
      (v) =>
        (filter === 'all' || (filter === 'later') === isLater(v)) &&
        (!needle || haystack.get(v)!.includes(needle)),
    );
    const val = (v: ShadowVessel): string | number =>
      sortKey === 'name' ? v.name ?? '' : sortKey === 'first' ? v.first : sortKey === 'lead' ? v.lead_days ?? -1e9 : v.designated ?? '';
    out.sort((a, b) => {
      const x = val(a);
      const y = val(b);
      const c = x < y ? -1 : x > y ? 1 : 0;
      return sortDesc ? -c : c;
    });
    return out;
  });

  function sortBy(k: SortKey) {
    if (sortKey === k) sortDesc = !sortDesc;
    else {
      sortKey = k;
      sortDesc = k !== 'name';
    }
  }

  function leadLabel(v: ShadowVessel): string {
    if (!isLater(v)) return text.leadAlready;
    const m = Math.floor(v.lead_days! / DAY_MONTH);
    if (m >= 2) return fill(text.leadMonths, { n: m });
    if (m === 1) return text.leadMonth;
    return fill(text.leadDays, { n: v.lead_days! });
  }

  // The three detector signals worth a column: AIS gaps, ship-to-ship encounters, and draught
  // changes with no port call (the one family that separates later-sanctioned tankers, P4-0).
  const SIGNALS = [
    { key: 'gaps', glyph: 'dash' },
    { key: 'sts', glyph: 'ring' },
    { key: 'draught', glyph: 'triangle' },
  ] as const;

  const SHORT_SOURCE: Record<string, Record<Lang, string>> = {
    uk: { en: 'UK', es: 'Reino Unido' },
    ofac: { en: 'US', es: 'EE. UU.' },
    eu: { en: 'EU', es: 'UE' },
  };
  const sources = (v: ShadowVessel) =>
    [...new Set(v.designations.map((d) => d[0]))].map((s) => SHORT_SOURCE[s]?.[lang] ?? sourceLabel(lang, s));

  // ---- hero map -----------------------------------------------------------------------------------
  function listedPaths(fleet: Fleet, tracks: Tracks, wanted: Set<number>) {
    const keep: [number, number][] = [];
    for (const [vi, offset, count] of fleet.trips) {
      if (wanted.has(fleet.vessels[vi][0])) keep.push([offset, count]);
    }
    const total = keep.reduce((a, [, c]) => a + c, 0);
    const value = new Float32Array(total * 2);
    const startIndices = new Uint32Array(keep.length);
    let k = 0;
    keep.forEach(([o, c], i) => {
      startIndices[i] = k;
      value.set(tracks.positions.subarray(o * 2, (o + c) * 2), k * 2);
      k += c;
    });
    return { length: keep.length, startIndices, attributes: { getPath: { value, size: 2 } } };
  }

  function padding() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    return w < 820
      ? { top: 70, bottom: Math.round(h * 0.34), left: 10, right: 10 }
      : { top: 80, bottom: 40, left: Math.min(520, Math.round(w * 0.4)), right: 60 };
  }

  onMount(() => {
    let destroyed = false;
    const onResize = () =>
      map?.fitBounds([[VIEW[0], VIEW[1]], [VIEW[2], VIEW[3]]], { padding: padding(), duration: 0 });
    (async () => {
      try {
        const [meta, shadow] = await Promise.all([loadMeta(), loadShadow()]);
        vessels = shadow.vessels;
        snapshot = shadow.sanctions_snapshot;
        map = await createBaseMap({ container, lang, bounds: VIEW, interactive: false, attribution: true });
        if (destroyed) return;
        onResize();
        overlay = new MapboxOverlay({ interleaved: true });
        map.addControl(overlay as any);
        const first = meta.windows[0];
        if (first) setDensity(map, densityUrl(first.id), meta.bbox, 0.45);
        const wanted = new Set(shadow.vessels.flatMap((v) => v.mmsi));
        const layers: PathLayer[] = [];
        for (const w of meta.windows) {
          const fleet = await loadFleet(w.id);
          const tracks = await loadTracks(w.id, fleet);
          if (destroyed) return;
          layers.push(
            new PathLayer({
              id: `listed-${w.id}`,
              data: listedPaths(fleet, tracks, wanted) as any,
              _pathType: 'open',
              positionFormat: 'XY',
              getColor: withAlpha(RGB.listed, 0.55),
              getWidth: 1.3,
              widthUnits: 'pixels',
              capRounded: true,
              jointRounded: true,
            }),
          );
          overlay.setProps({ layers: [...layers] });
        }
      } catch (e) {
        console.error(e);
        failed = true;
      }
    })();
    window.addEventListener('resize', onResize);
    return () => {
      destroyed = true;
      window.removeEventListener('resize', onResize);
      overlay?.finalize();
      map?.remove();
    };
  });
</script>

<section class="hero">
  <div class="map" bind:this={container} aria-hidden="true"></div>
  <div class="shade" aria-hidden="true"></div>
  <div class="hero-text">
    <div class="kicker">{text.kicker}</div>
    {#if vessels.length}
      <h1>{fill(text.title, { n: num(lang, stats.n) })}</h1>
      <p class="dek">{fill(text.dek, { when: whenPhrase(months), n: num(lang, stats.n), later: num(lang, stats.later) })}</p>
    {:else if failed}
      <h1>{text.pageTitle}</h1>
    {:else}
      <h1 class="placeholder">&nbsp;</h1>
    {/if}
    <div class="map-legend"><i></i>{text.mapLegend}</div>
  </div>
</section>

{#if vessels.length}
  <div class="page">
    <section class="stats" aria-label={text.pageTitle}>
      <div class="stat"><b class="mono">{num(lang, stats.n)}</b><span>{text.statSeen}</span></div>
      <div class="stat accent"><b class="mono">{num(lang, stats.pctLater)} %</b><span>{text.statBefore}</span></div>
      <div class="stat"><b class="mono">{fill(text.statLeadValue, { n: num(lang, stats.medianMonths) })}</b><span>{text.statLead}</span></div>
      <div class="stat"><b class="mono">{num(lang, stats.flagChanges)}</b><span>{text.statFlags}</span></div>
    </section>

    <section class="charts">
      <div class="wide">
        <MonthBars
          {lang}
          title={text.monthsTitle}
          subtitle={text.monthsSub}
          {bins}
          {observed}
          laterLabel={text.monthsLater}
          beforeLabel={text.monthsBefore}
          observedLabel={text.monthsObserved}
          unit={text.monthsTip}
        />
      </div>
      <RankBars {lang} title={text.flagsTitle} subtitle={text.flagsSub} rows={flagRows} />
      <div class="stack">
        <RankBars {lang} title={text.regimeTitle} subtitle={text.regimeSub} rows={regimeRows} />
        <RankBars {lang} title={text.sourceTitle} subtitle={text.sourceSub} rows={sourceRows} note={text.sourceNote} />
      </div>
    </section>

    <section class="list">
      <h2>{fill(text.tableTitle, { n: num(lang, stats.n) })}</h2>
      <div class="controls">
        <label class="box">
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" stroke-width="1.6" /><path d="M11 11l3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>
          <input type="search" bind:value={q} placeholder={text.placeholder} aria-label={text.placeholder} />
        </label>
        <div class="chips" role="group">
          {#each [['all', text.filterAll], ['later', text.filterLater], ['before', text.filterBefore]] as [key, label] (key)}
            <button class:active={filter === key} aria-pressed={filter === key} onclick={() => (filter = key as Filter)}>
              {#if key === 'later'}<span class="chip-dot"></span>{/if}
              {label} <span class="chip-n">{counts[key as Filter]}</span>
            </button>
          {/each}
        </div>
      </div>

      <div class="table" role="table">
        <div class="tr th" role="row">
          <button role="columnheader" onclick={() => sortBy('name')} class:sorted={sortKey === 'name'}>{text.colVessel}{sortKey === 'name' ? (sortDesc ? ' ↓' : ' ↑') : ''}</button>
          <button role="columnheader" onclick={() => sortBy('first')} class:sorted={sortKey === 'first'}>{text.colSeen}{sortKey === 'first' ? (sortDesc ? ' ↓' : ' ↑') : ''}</button>
          <button role="columnheader" onclick={() => sortBy('designated')} class:sorted={sortKey === 'designated'}>{text.colSanctioned}{sortKey === 'designated' ? (sortDesc ? ' ↓' : ' ↑') : ''}</button>
          <button role="columnheader" onclick={() => sortBy('lead')} class:sorted={sortKey === 'lead'}>{text.colLead}{sortKey === 'lead' ? (sortDesc ? ' ↓' : ' ↑') : ''}</button>
          <span role="columnheader" class="signals-col">{text.colSignals}</span>
        </div>
        {#each rows as v (v.imo ?? v.dossier)}
          <a class="tr link" role="row" href={`${localePath(lang, 'vessel/')}?mmsi=${v.dossier}`}>
            <span class="vessel" role="cell">
              <span class="flags">
                {#each v.flags as f (f)}<span class={`flag ${flagClass(f)}`} title={countryName(lang, f) ?? f}></span>{/each}
              </span>
              <span class="who">
                <span class="name">{title(v.name) || v.imo}</span>
                <span class="meta mono">
                  IMO {v.imo ?? '—'} · {shipTypeLabel(lang, v.type)}{#if v.names.length > 1} · {text.alsoKnownAs} {v.names.slice(1, 3).map(title).join(', ')}{/if}
                </span>
              </span>
            </span>
            <span role="cell" class="seen">
              <span>{fmtDate(lang, v.first)}{v.last !== v.first ? ` – ${fmtDate(lang, v.last)}` : ''}</span>
              <span class="meta">{v.days === 1 ? text.day : fill(text.days, { n: v.days })}</span>
            </span>
            <span role="cell" class="sanctioned">
              <span>{v.designated ? fmtDate(lang, v.designated) : text.none}</span>
              <span class="meta">{sources(v).join(' · ')}</span>
            </span>
            <span role="cell" class="lead" class:later={isLater(v)}>{leadLabel(v)}</span>
            <span role="cell" class="signals">
              {#each SIGNALS as s (s.key)}
                {#if v.events[s.key]}
                  <span class="sig" title={`${text.signals[s.key]}: ${v.events[s.key]}`} aria-label={`${text.signals[s.key]}: ${v.events[s.key]}`}>
                    {@html glyphSvg(s.glyph, 11)}<span class="mono">{v.events[s.key]}</span>
                  </span>
                {/if}
              {/each}
            </span>
          </a>
        {:else}
          <p class="empty">{text.noResults}</p>
        {/each}
      </div>
    </section>

    <section class="notes">
      <h2>{text.notesTitle}</h2>
      {#each text.notes as p}
        <p>{fill(p, { snapshot: snapshot ? fmtDate(lang, snapshot) : '—', months: monthsList(months) })}</p>
      {/each}
      <p class="legend-signals">
        {#each SIGNALS as s (s.key)}
          <span>{@html glyphSvg(s.glyph, 11)} {text.signals[s.key]}</span>
        {/each}
      </p>
    </section>
  </div>
{/if}

<style>
  .hero {
    position: relative;
    height: min(86dvh, 820px);
    min-height: 520px;
    overflow: hidden;
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
    background:
      linear-gradient(to right, rgba(4, 8, 15, 0.95) 0%, rgba(4, 8, 15, 0.88) 30%, rgba(4, 8, 15, 0.35) 48%, transparent 62%),
      linear-gradient(to top, var(--bg-0) 0%, transparent 22%);
  }

  .hero-text {
    position: absolute;
    left: var(--gutter);
    bottom: 64px;
    max-width: min(620px, 46vw);
  }

  .kicker {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #ffb190;
  }

  h1 {
    margin: 12px 0 18px;
    font-family: var(--font-serif);
    font-size: clamp(38px, 5.2vw, 72px);
    font-weight: 480;
    line-height: 1;
    letter-spacing: -0.02em;
    text-wrap: balance;
  }

  .dek {
    margin: 0;
    font-size: clamp(16px, 1.5vw, 19px);
    line-height: 1.55;
    color: var(--text-2);
  }

  .map-legend {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin-top: 18px;
    font-size: 12px;
    color: var(--text-3);
  }

  .map-legend i {
    width: 22px;
    height: 2px;
    border-radius: 2px;
    background: var(--c-listed);
  }

  .page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 8px var(--gutter) 60px;
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
  }

  .stat {
    display: grid;
    gap: 6px;
    align-content: start;
    padding: 18px;
    border-radius: var(--radius-l);
    background: var(--bg-1);
    border: 1px solid var(--line);
  }

  .stat b {
    font-size: clamp(26px, 3vw, 36px);
    font-weight: 600;
    color: var(--text-1);
    letter-spacing: -0.02em;
  }

  .stat.accent b {
    color: #ffb190;
  }

  .stat span {
    font-size: 13px;
    line-height: 1.4;
    color: var(--text-2);
  }

  .charts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-top: 12px;
  }

  .charts .wide {
    grid-column: 1 / -1;
  }

  .charts .stack {
    display: grid;
    gap: 12px;
    align-content: start;
  }

  h2 {
    margin: 0 0 6px;
    font-family: var(--font-serif);
    font-size: clamp(26px, 3vw, 36px);
    font-weight: 500;
    letter-spacing: -0.01em;
  }

  .list {
    margin-top: 56px;
  }

  .controls {
    position: sticky;
    top: var(--header-h);
    z-index: 10;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    margin: 18px 0 8px;
    padding: 12px 0;
    background: linear-gradient(to bottom, var(--bg-0) 75%, transparent);
  }

  .box {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1 1 280px;
    height: 46px;
    padding: 0 16px;
    border-radius: 14px;
    border: 1px solid var(--line-strong);
    background: var(--bg-1);
    color: var(--text-3);
  }

  .box:focus-within {
    border-color: rgba(255, 255, 255, 0.4);
  }

  .box input {
    flex: 1;
    min-width: 0;
    border: none;
    outline: none;
    background: transparent;
    font-size: 16px;
    color: var(--text-1);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .chips button {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    height: 36px;
    padding: 0 13px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: transparent;
    font-size: 13px;
    color: var(--text-2);
    cursor: pointer;
  }

  .chips button.active {
    background: var(--text-1);
    color: var(--bg-0);
    border-color: var(--text-1);
    font-weight: 600;
  }

  .chip-n {
    font-size: 11px;
    opacity: 0.7;
  }

  .chip-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--c-listed);
  }

  .table {
    border-top: 1px solid var(--line);
  }

  .tr {
    display: grid;
    grid-template-columns: minmax(0, 2.3fr) minmax(0, 1.25fr) minmax(0, 1.1fr) minmax(0, 0.95fr) minmax(0, 0.8fr);
    gap: 14px;
    align-items: center;
    padding: 11px 8px;
    border-bottom: 1px solid var(--line);
    font-size: 13.5px;
    color: inherit;
    text-decoration: none;
  }

  .tr.th {
    padding-top: 6px;
    padding-bottom: 6px;
  }

  .tr.th button,
  .tr.th span {
    padding: 4px 0;
    border: none;
    background: none;
    text-align: left;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-3);
    cursor: pointer;
  }

  .tr.th button.sorted {
    color: var(--text-1);
  }

  .tr.link:hover {
    background: rgba(255, 255, 255, 0.03);
  }

  .vessel {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .flags {
    display: flex;
    flex-direction: column;
    gap: 3px;
    flex: none;
  }

  .flag {
    width: 20px;
    height: 14px;
    border-radius: 2px;
  }

  .who,
  .seen,
  .sanctioned {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .name {
    font-weight: 600;
    color: var(--text-1);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta {
    font-size: 11.5px;
    color: var(--text-3);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .lead {
    font-size: 13px;
    color: var(--text-3);
  }

  .lead.later {
    color: #ffb190;
    font-weight: 600;
  }

  .signals {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    color: var(--c-event);
  }

  .sig {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .sig .mono {
    font-size: 11.5px;
    color: var(--text-2);
  }

  .empty {
    padding: 24px 8px;
    color: var(--text-3);
  }

  .notes {
    max-width: 760px;
    margin-top: 56px;
  }

  .notes p {
    margin: 10px 0 0;
    font-size: 14.5px;
    line-height: 1.65;
    color: var(--text-2);
  }

  .legend-signals {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
    font-size: 12.5px !important;
    color: var(--text-3) !important;
  }

  .legend-signals span {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .legend-signals :global(svg) {
    color: var(--c-event);
  }

  @media (max-width: 980px) {
    .stats {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .charts {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  @media (max-width: 820px) {
    .hero {
      height: 88dvh;
    }
    .shade {
      background: linear-gradient(to top, var(--bg-0) 0%, rgba(4, 8, 15, 0.85) 34%, rgba(4, 8, 15, 0.1) 62%, transparent 75%);
    }
    .hero-text {
      right: var(--gutter);
      bottom: 40px;
      max-width: none;
    }
    .tr {
      grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
      row-gap: 6px;
    }
    .tr.th .signals-col,
    .tr.th button:nth-child(2),
    .tr.th button:nth-child(4),
    .seen,
    .signals {
      display: none;
    }
    .lead {
      grid-column: 2;
      font-size: 12px;
    }
  }
</style>
