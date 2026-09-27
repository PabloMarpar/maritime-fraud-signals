<script lang="ts">
  /**
   * Horizontal grouped bars: share of tankers with at least one event of each kind, sanctioned-
   * later vs other tankers. Two series = the two validated categorical hues (listed orange,
   * vessel blue); legend always shown, values labelled directly, hover highlights a row.
   */
  import { num } from '../lib/format';
  import type { Lang } from '../i18n/ui';

  interface Row {
    key: string;
    label: string;
    later: number; // 0..1
    other: number; // 0..1
  }

  interface Props {
    lang: Lang;
    title: string;
    laterLabel: string;
    otherLabel: string;
    rows: Row[];
  }

  let { lang, title, laterLabel, otherLabel, rows }: Props = $props();
  let hover = $state<string | null>(null);
  const max = $derived(Math.max(0.01, ...rows.flatMap((r) => [r.later, r.other])));
  const pct = (v: number) => `${num(lang, v * 100, v < 0.1 && v > 0 ? 1 : 0)} %`;
</script>

<figure class="chart">
  <figcaption>{title}</figcaption>
  <div class="legend">
    <span><i class="sw later"></i>{laterLabel}</span>
    <span><i class="sw other"></i>{otherLabel}</span>
  </div>
  <div class="rows">
    {#each rows as r (r.key)}
      <div
        class="row"
        class:dim={hover !== null && hover !== r.key}
        role="group"
        aria-label={`${r.label}: ${laterLabel} ${pct(r.later)}, ${otherLabel} ${pct(r.other)}`}
        onmouseenter={() => (hover = r.key)}
        onmouseleave={() => (hover = null)}
      >
        <div class="label">{r.label}</div>
        <div class="bars">
          <div class="bar-line">
            <div class="bar later" style:width={`${(r.later / max) * 100}%`}></div>
            <span class="val mono">{pct(r.later)}</span>
          </div>
          <div class="bar-line">
            <div class="bar other" style:width={`${(r.other / max) * 100}%`}></div>
            <span class="val mono">{pct(r.other)}</span>
          </div>
        </div>
      </div>
    {/each}
  </div>
</figure>

<style>
  .chart {
    margin: 18px 0 0;
    padding: 16px 16px 12px;
    border-radius: 12px;
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid var(--line);
  }

  figcaption {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text-1);
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin: 8px 0 12px;
    font-size: 11.5px;
    color: var(--text-2);
  }

  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .sw {
    width: 12px;
    height: 12px;
    border-radius: 3px;
  }

  .sw.later,
  .bar.later {
    background: var(--c-listed);
  }

  .sw.other,
  .bar.other {
    background: var(--c-vessel);
  }

  .rows {
    display: grid;
    gap: 12px;
  }

  .row {
    transition: opacity 0.15s;
  }

  .row.dim {
    opacity: 0.4;
  }

  .label {
    font-size: 12px;
    color: var(--text-2);
    margin-bottom: 4px;
  }

  .bars {
    display: grid;
    gap: 2px;
  }

  .bar-line {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 12px;
  }

  .bar {
    height: 10px;
    min-width: 2px;
    border-radius: 0 4px 4px 0;
  }

  .val {
    font-size: 11px;
    color: var(--text-1);
    white-space: nowrap;
  }
</style>
