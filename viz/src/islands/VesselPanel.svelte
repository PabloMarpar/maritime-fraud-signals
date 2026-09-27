<script lang="ts">
  import { loadDossier, isListed, STATUS_LISTED_BY_END, type Dossier } from '../lib/data';
  import {
    countryName,
    flagClass,
    fmtDate,
    kindLabel,
    monthsBetween,
    monthsLabel,
    num,
    shipTypeLabel,
    sourceLabel,
    title,
  } from '../lib/format';
  import { glyphSvg, EVENT_GLYPH } from '../lib/icons';
  import { localePath, useT, type Lang } from '../i18n/ui';

  interface Basic {
    name: string | null;
    ship_type: string | null;
    iso2: string | null;
    status: number;
    length: number | null;
  }

  interface Props {
    lang: Lang;
    mmsi: number;
    windowId: string;
    windowEnd: string;
    basic: Basic;
    counts: { gaps: number; sts: number; spoof: number; behav: number };
    following?: boolean;
    onfollow?: () => void;
    onclose: () => void;
  }

  let { lang, mmsi, windowId, windowEnd, basic, counts, following = false, onfollow, onclose }: Props = $props();
  const tr = $derived(useT(lang));

  let dossier = $state<Dossier | null>(null);
  let loaded = $state(false);

  $effect(() => {
    const m = mmsi;
    loaded = false;
    dossier = null;
    loadDossier(m).then((d) => {
      if (m === mmsi) {
        dossier = d;
        loaded = true;
      }
    });
  });

  const w = $derived(dossier?.windows[windowId]);
  const name = $derived(title(dossier?.name ?? basic.name) || tr('vessel.noName'));
  const iso2 = $derived(dossier?.iso2 ?? basic.iso2);
  const type = $derived(dossier?.ship_type ?? basic.ship_type);
  const listed = $derived(isListed(basic.status) || (dossier?.sanctions.length ?? 0) > 0);
  const firstDesignation = $derived(
    dossier?.sanctions
      .map((s) => s[4])
      .filter(Boolean)
      .sort()[0] ?? null,
  );
  const monthsAfter = $derived(firstDesignation ? monthsBetween(windowEnd, firstDesignation) : null);
  const eventRows = $derived(
    [
      { key: 'gap', n: counts.gaps, label: tr('event.gap') },
      { key: 'sts', n: counts.sts, label: tr('event.sts') },
      { key: 'spoof', n: counts.spoof, label: tr('event.spoof') },
      { key: 'behav', n: counts.behav, label: tr('event.behav') },
    ].filter((r) => r.n > 0),
  );
</script>

<aside class="panel glass" aria-label={name}>
  <header>
    <div class="title-row">
      <span class={flagClass(iso2)} title={countryName(lang, iso2) ?? ''}></span>
      <h2>{name}</h2>
      <button class="close icon-btn" onclick={onclose} aria-label={tr('common.close')}>
        <svg width="14" height="14" viewBox="0 0 14 14"><path d="M2 2l10 10M12 2 2 12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" /></svg>
      </button>
    </div>
    <div class="badges">
      <span class="badge badge-vessel">{@html glyphSvg('boat', 12)} {shipTypeLabel(lang, type)}</span>
      {#if listed}
        <span class="badge badge-listed">
          <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11 10H1Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" /><path d="M6 4.6v2.4M6 8.4v.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" /></svg>
          {basic.status === STATUS_LISTED_BY_END ? tr('vessel.listedByEnd') : tr('vessel.listedLater')}
        </span>
      {/if}
      {#if dossier?.foc}
        <span class="badge">{tr('vessel.foc')}</span>
      {/if}
    </div>
  </header>

  <dl class="facts">
    <div><dt>{tr('vessel.mmsi')}</dt><dd class="mono">{mmsi}</dd></div>
    <div><dt>{tr('vessel.imo')}</dt><dd class="mono">{dossier?.imo ?? '—'}</dd></div>
    <div><dt>{tr('vessel.flag')}</dt><dd>{countryName(lang, iso2) ?? tr('common.unknown')}</dd></div>
    <div><dt>{tr('vessel.callsign')}</dt><dd class="mono">{dossier?.callsign ?? '—'}</dd></div>
    <div>
      <dt>{tr('vessel.size')}</dt>
      <dd class="mono">
        {#if dossier?.length ?? basic.length}
          {num(lang, dossier?.length ?? basic.length)} × {num(lang, dossier?.width)} m
        {:else}—{/if}
      </dd>
    </div>
    <div>
      <dt>{tr('vessel.draught')}</dt>
      <dd class="mono">
        {#if w?.draught}{num(lang, w.draught[0], 1)}–{num(lang, w.draught[1], 1)} m{:else}—{/if}
      </dd>
    </div>
  </dl>

  {#if w && w.destinations.length}
    <section>
      <h3 class="eyebrow">{tr('vessel.destinations')}</h3>
      <ul class="dest">
        {#each w.destinations.slice(0, 3) as d}
          <li><span class="mono">{d[0]}</span><span class="when">{fmtDate(lang, d[2])}</span></li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if listed && dossier?.sanctions.length}
    <section class="sanctions">
      <h3 class="eyebrow">{tr('vessel.sanctions')}</h3>
      {#if monthsAfter !== null && monthsAfter > 0}
        <p class="after">{tr('vessel.designatedAfter', { months: monthsLabel(lang, monthsAfter) })}</p>
      {:else if monthsAfter !== null}
        <p class="after">{tr('vessel.designatedBefore')}</p>
      {/if}
      <ul>
        {#each dossier.sanctions.slice(0, 4) as s}
          <li><strong>{sourceLabel(lang, s[0])}</strong> · {fmtDate(lang, s[4])}{#if s[3]}<span class="program"> · {s[3]}</span>{/if}</li>
        {/each}
      </ul>
    </section>
  {/if}

  <section>
    <h3 class="eyebrow">{tr('vessel.events')}</h3>
    {#if eventRows.length}
      <ul class="events">
        {#each eventRows as r}
          <li>
            <span class="glyph">{@html glyphSvg(EVENT_GLYPH[r.key as keyof typeof EVENT_GLYPH], 14)}</span>
            <span>{r.label}</span>
            <span class="n mono">{r.n}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">{tr('vessel.noEvents')}</p>
    {/if}
    {#if w?.events.behav.length}
      <ul class="kinds">
        {#each w.events.behav.slice(0, 3) as b}
          <li>{kindLabel(lang, b[0])} · <span class="when">{fmtDate(lang, b[1])}</span></li>
        {/each}
      </ul>
    {/if}
  </section>

  <footer>
    {#if onfollow}
      <button class="btn" class:on={following} onclick={onfollow}>
        <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.6" /><circle cx="8" cy="8" r="2" fill="currentColor" /><path d="M8 0v2.5M8 13.5V16M0 8h2.5M13.5 8H16" stroke="currentColor" stroke-width="1.6" /></svg>
        {following ? tr('vessel.unfollow') : tr('vessel.follow')}
      </button>
    {/if}
    {#if loaded && dossier}
      <a class="btn btn-primary" href={`${localePath(lang, 'vessel/')}?mmsi=${mmsi}`}>{tr('vessel.openDossier')} →</a>
    {/if}
  </footer>
</aside>

<style>
  .panel {
    width: 340px;
    max-height: 100%;
    overflow-y: auto;
    padding: 18px 18px 16px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    scrollbar-width: thin;
    animation: slide-in 0.35s var(--ease-out);
  }

  @keyframes slide-in {
    from {
      opacity: 0;
      transform: translateX(16px);
    }
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .title-row .flag {
    font-size: 20px;
  }

  h2 {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    line-height: 1.15;
    letter-spacing: 0.02em;
    overflow-wrap: anywhere;
  }

  .close {
    width: 30px;
    height: 30px;
    flex: none;
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 12px;
  }

  .facts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 14px;
    margin: 0;
    padding: 14px;
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--line);
  }

  dt {
    font-size: 11px;
    color: var(--text-3);
  }

  dd {
    margin: 1px 0 0;
    font-size: 13.5px;
    overflow-wrap: anywhere;
  }

  section h3 {
    margin: 0 0 8px;
  }

  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .dest li {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    padding: 5px 0;
    border-bottom: 1px dashed var(--line);
    font-size: 13px;
  }

  .when {
    color: var(--text-3);
    font-size: 12px;
    white-space: nowrap;
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

  .after {
    margin: 0 0 8px;
    font-family: var(--font-serif);
    font-size: 16px;
    line-height: 1.3;
  }

  .sanctions li {
    font-size: 12.5px;
    color: var(--text-2);
    padding: 2px 0;
  }

  .program {
    color: var(--text-3);
  }

  .events li {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
    font-size: 13px;
  }

  .glyph {
    display: inline-grid;
    place-items: center;
    width: 22px;
    height: 22px;
    border-radius: 6px;
    color: var(--c-event);
    background: var(--c-event-soft);
  }

  .n {
    margin-left: auto;
    color: var(--text-1);
    font-weight: 600;
  }

  .kinds {
    margin-top: 4px;
    padding-left: 32px;
  }

  .kinds li {
    font-size: 12px;
    color: var(--text-2);
    padding: 2px 0;
  }

  .muted {
    margin: 0;
    font-size: 13px;
    color: var(--text-3);
  }

  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: auto;
    padding-top: 4px;
  }

  .btn.on {
    border-color: var(--c-vessel);
    background: var(--c-vessel-soft);
  }

  @media (max-width: 720px) {
    .panel {
      width: 100%;
      max-height: 46dvh;
      animation-name: slide-up;
    }
    @keyframes slide-up {
      from {
        opacity: 0;
        transform: translateY(16px);
      }
    }
  }
</style>
