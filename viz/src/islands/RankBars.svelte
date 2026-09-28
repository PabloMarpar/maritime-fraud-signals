<script lang="ts">
  /**
   * Ranked horizontal bars for one series (a count per category). One hue, value and share
   * labelled at the bar end (the labels replace an axis), a row highlights on hover, and the
   * figure is readable without colour. Used by the shadow-fleet page.
   */
  import { num } from '../lib/format';
  import type { Lang } from '../i18n/ui';

  interface Row {
    key: string;
    label: string;
    value: number;
    flag?: string; // flag-icons class, when the category is a country
  }

  interface Props {
    lang: Lang;
    title: string;
    subtitle?: string;
    rows: Row[];
    color?: string;
    note?: string;
  }

  let { lang, title, subtitle, rows, color = 'var(--c-listed)', note }: Props = $props();
  let hover = $state<string | null>(null);
  const total = $derived(rows.reduce((a, r) => a + r.value, 0) || 1);
  const max = $derived(Math.max(1, ...rows.map((r) => r.value)));
</script>

<figure class="chart">
  <figcaption>
    <span class="title">{title}</span>
    {#if subtitle}<span class="sub">{subtitle}</span>{/if}
  </figcaption>
  <div class="rows">
    {#each rows as r (r.key)}
      <div
        class="row"
        class:dim={hover !== null && hover !== r.key}
        role="group"
        aria-label={`${r.label}: ${r.value}`}
        onmouseenter={() => (hover = r.key)}
        onmouseleave={() => (hover = null)}
      >
        <div class="label">
          {#if r.flag}<span class={`flag ${r.flag}`}></span>{/if}
          <span>{r.label}</span>
        </div>
        <div class="bar-line">
          <div class="bar" style:width={`${(r.value / max) * 100}%`} style:background={color}></div>
          <span class="val mono">{num(lang, r.value)}<span class="share">&nbsp;·&nbsp;{num(lang, (r.value / total) * 100)} %</span></span>
        </div>
      </div>
    {/each}
  </div>
  {#if note}<p class="note">{note}</p>{/if}
</figure>

<style>
  .chart {
    margin: 0;
    padding: 18px 18px 16px;
    border-radius: var(--radius-l);
    background: var(--bg-1);
    border: 1px solid var(--line);
  }

  figcaption {
    display: grid;
    gap: 3px;
    margin-bottom: 14px;
  }

  .title {
    font-size: 15px;
    font-weight: 600;
    color: var(--text-1);
  }

  .sub {
    font-size: 12.5px;
    color: var(--text-3);
  }

  .rows {
    display: grid;
    gap: 9px;
  }

  .row {
    display: grid;
    grid-template-columns: minmax(96px, 38%) 1fr;
    align-items: center;
    gap: 10px;
    transition: opacity 0.15s;
  }

  .row.dim {
    opacity: 0.4;
  }

  .label {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
    font-size: 13px;
    color: var(--text-2);
  }

  .label span:last-child {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .label .flag {
    flex: none;
    width: 18px;
    height: 13px;
    border-radius: 2px;
  }

  .bar-line {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .bar {
    height: 12px;
    min-width: 2px;
    border-radius: 0 4px 4px 0;
  }

  .val {
    font-size: 12px;
    color: var(--text-1);
    white-space: nowrap;
  }

  .share {
    color: var(--text-3);
  }

  .note {
    margin: 14px 0 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-3);
  }
</style>
