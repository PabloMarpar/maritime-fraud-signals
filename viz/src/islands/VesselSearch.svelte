<script lang="ts">
  import { onMount } from 'svelte';
  import { loadIndex, loadMeta, type IndexRow, type Meta, type VesselIndex } from '../lib/data';
  import { countryName, flagClass, fmtMonth, num, shipTypeLabel, title } from '../lib/format';
  import { glyphSvg } from '../lib/icons';
  import { localePath, useT, type Lang } from '../i18n/ui';

  let { lang }: { lang: Lang } = $props();
  const tr = $derived(useT(lang));

  const LIMIT = 150;
  type Filter = 'all' | 'tankers' | 'listed' | 'dossier';

  // Read the URL during init (client-only island): an effect below rewrites it on mount.
  const initial = new URLSearchParams(location.search);
  const initialFilter = initial.get('f');

  let index = $state.raw<VesselIndex | null>(null);
  let meta = $state.raw<Meta | null>(null);
  let query = $state(initial.get('q') ?? '');
  let filter = $state<Filter>(
    initialFilter === 'tankers' || initialFilter === 'listed' || initialFilter === 'dossier' ? initialFilter : 'all',
  );
  let error = $state(false);
  let input: HTMLInputElement;

  const counts = $derived.by(() => {
    if (!index) return null;
    let tankers = 0;
    let listed = 0;
    let dossier = 0;
    for (const r of index.rows) {
      if (r[4] === 'Tanker') tankers++;
      if (r[6]) listed++;
      if (r[7]) dossier++;
    }
    return { all: index.rows.length, tankers, listed, dossier };
  });

  const matches = $derived.by<IndexRow[]>(() => {
    if (!index) return [];
    const q = query.trim().toUpperCase();
    const digits = /^\d+$/.test(q);
    const out: IndexRow[] = [];
    for (const r of index.rows) {
      if (filter === 'tankers' && r[4] !== 'Tanker') continue;
      if (filter === 'listed' && !r[6]) continue;
      if (filter === 'dossier' && !r[7]) continue;
      if (q) {
        const hit = digits
          ? String(r[0]).startsWith(q) || (r[2] ?? '').startsWith(q)
          : (r[1] ?? '').includes(q);
        if (!hit) continue;
      }
      out.push(r);
    }
    // Without a query, lead with the vessels people come here for.
    if (!q) out.sort((a, b) => b[6] - a[6] || b[7] - a[7] || (a[1] ?? '~').localeCompare(b[1] ?? '~'));
    return out;
  });

  function months(mask: number): string {
    if (!index) return '';
    return index.windows
      .filter((_, i) => mask & (1 << i))
      .map((w) => fmtMonth(lang, w.split('_')[0]).replace(/\s(de\s)?\d{4}$/, '').slice(0, 3))
      .join(' · ');
  }

  function href(r: IndexRow): string | null {
    return r[7] ? `${localePath(lang, 'vessel/')}?mmsi=${r[0]}` : null;
  }

  $effect(() => {
    const p = new URLSearchParams();
    if (query) p.set('q', query);
    if (filter !== 'all') p.set('f', filter);
    const s = p.toString();
    history.replaceState(null, '', `${location.pathname}${s ? '?' + s : ''}`);
  });

  onMount(async () => {
    try {
      [index, meta] = await Promise.all([loadIndex(), loadMeta()]);
    } catch {
      error = true;
    }
    input?.focus();
  });

  const filters: { key: Filter; label: 'vessels.filter.all' | 'vessels.filter.tankers' | 'vessels.filter.listed' | 'vessels.filter.dossier' }[] = [
    { key: 'all', label: 'vessels.filter.all' },
    { key: 'tankers', label: 'vessels.filter.tankers' },
    { key: 'listed', label: 'vessels.filter.listed' },
    { key: 'dossier', label: 'vessels.filter.dossier' },
  ];
</script>

<div class="search-page">
  <header class="head">
    <div class="eyebrow">{tr('nav.vessels')}</div>
    <h1>{tr('vessels.pageTitle')}</h1>
    <p class="intro">{tr('vessels.intro')}</p>
    {#if meta}
      <p class="months">
        {#each meta.windows as w, i}{i ? ' · ' : ''}{fmtMonth(lang, w.start)}{/each}
      </p>
    {/if}
  </header>

  <div class="controls">
    <label class="box">
      <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="m11 11 3.5 3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
      <input bind:this={input} type="search" bind:value={query} placeholder={tr('vessels.placeholder')} aria-label={tr('vessels.placeholder')} autocomplete="off" spellcheck="false" />
    </label>
    <div class="chips" role="group">
      {#each filters as f}
        <button class:active={filter === f.key} aria-pressed={filter === f.key} onclick={() => (filter = f.key)}>
          {#if f.key === 'listed'}<span class="chip-dot"></span>{/if}
          {tr(f.label)}
          {#if counts}<span class="chip-n mono">{num(lang, counts[f.key])}</span>{/if}
        </button>
      {/each}
    </div>
  </div>

  {#if error}
    <p class="muted">{tr('common.error')}</p>
  {:else if !index}
    <p class="muted">{tr('common.loading')}</p>
  {:else}
    <p class="count">
      {tr('vessels.results', { n: num(lang, matches.length) })}
      {#if matches.length > LIMIT}<span class="muted"> · {tr('vessels.showing', { n: LIMIT })}</span>{/if}
    </p>
    {#if matches.length}
      <div class="table" role="table">
        <div class="tr th" role="row">
          <span role="columnheader">{tr('vessels.col.vessel')}</span>
          <span role="columnheader">{tr('vessels.col.type')}</span>
          <span role="columnheader" class="num">{tr('vessels.col.length')}</span>
          <span role="columnheader">{tr('vessels.col.ids')}</span>
          <span role="columnheader" class="hide-s">{tr('explore.window')}</span>
        </div>
        {#each matches.slice(0, LIMIT) as r (r[0])}
          {@const link = href(r)}
          <svelte:element this={link ? 'a' : 'div'} href={link} class="tr" class:link role="row">
            <span class="name" role="cell">
              <span class={flagClass(r[3])} title={countryName(lang, r[3]) ?? ''}></span>
              <span class="n-text">{title(r[1]) || tr('vessel.noName')}</span>
              {#if r[6]}
                <span class="badge badge-listed small">{@html glyphSvg('triangle', 10)} {tr('vessel.listed')}</span>
              {/if}
            </span>
            <span role="cell" class="muted-cell">{shipTypeLabel(lang, r[4])}</span>
            <span role="cell" class="num mono">{r[5] ? `${num(lang, r[5])} m` : '—'}</span>
            <span role="cell" class="mono ids">{r[0]}{r[2] ? ` · ${r[2]}` : ''}</span>
            <span role="cell" class="hide-s muted-cell">{months(r[8])}</span>
          </svelte:element>
        {/each}
      </div>
    {:else}
      <p class="muted">{tr('vessels.noResults')}</p>
    {/if}
  {/if}
</div>

<style>
  .search-page {
    max-width: 1180px;
    margin: 0 auto;
    padding: 48px var(--gutter) 40px;
  }

  h1 {
    margin: 8px 0 12px;
    font-family: var(--font-serif);
    font-size: clamp(36px, 6vw, 60px);
    font-weight: 500;
    line-height: 1.02;
    letter-spacing: -0.02em;
  }

  .intro {
    margin: 0;
    max-width: 62ch;
    color: var(--text-2);
    font-size: 16px;
  }

  .months {
    margin: 10px 0 0;
    font-size: 12.5px;
    color: var(--text-3);
  }

  .controls {
    position: sticky;
    top: var(--header-h);
    z-index: 10;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    margin: 30px 0 8px;
    padding: 12px 0;
    background: linear-gradient(to bottom, var(--bg-0) 75%, transparent);
  }

  .box {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1 1 320px;
    height: 48px;
    padding: 0 16px;
    border-radius: 14px;
    border: 1px solid var(--line-strong);
    background: var(--bg-1);
    color: var(--text-3);
    transition: border-color 0.2s;
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
    transition: all 0.2s;
  }

  .chips button:hover {
    color: var(--text-1);
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

  .count {
    font-size: 13px;
    color: var(--text-2);
    margin: 4px 0 10px;
  }

  .table {
    border-top: 1px solid var(--line);
  }

  .tr {
    display: grid;
    grid-template-columns: minmax(0, 2.4fr) minmax(0, 1fr) 80px minmax(0, 1.3fr) minmax(0, 0.9fr);
    gap: 14px;
    align-items: center;
    padding: 11px 8px;
    border-bottom: 1px solid var(--line);
    font-size: 13.5px;
    text-decoration: none;
    color: inherit;
  }

  .tr.th {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-3);
    padding-top: 8px;
    padding-bottom: 8px;
  }

  .tr.link {
    transition: background 0.15s;
  }

  .tr.link:hover {
    background: rgba(255, 255, 255, 0.035);
  }

  .name {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .n-text {
    font-weight: 600;
    letter-spacing: 0.01em;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .badge.small {
    height: 19px;
    font-size: 10px;
    padding: 0 6px;
  }

  .num {
    text-align: right;
  }

  .ids {
    font-size: 12px;
    color: var(--text-2);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .muted-cell {
    color: var(--text-2);
    font-size: 12.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .muted {
    color: var(--text-3);
  }

  @media (max-width: 760px) {
    .tr {
      grid-template-columns: minmax(0, 1fr) auto;
      grid-template-areas:
        'name num'
        'type ids';
      row-gap: 3px;
    }
    .tr.th {
      display: none;
    }
    .tr > :nth-child(1) {
      grid-area: name;
    }
    .tr > :nth-child(2) {
      grid-area: type;
    }
    .tr > :nth-child(3) {
      grid-area: num;
    }
    .tr > :nth-child(4) {
      grid-area: ids;
      text-align: right;
    }
    .hide-s {
      display: none;
    }
  }
</style>
