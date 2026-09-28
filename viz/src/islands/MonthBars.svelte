<script lang="ts" module>
  export interface MonthBin {
    key: string; // 'YYYY-MM', or 'earlier' for the first, collapsed bin
    label: string; // long label for the tooltip
    tick: string | null; // short axis label (years), or null
    later: number;
    before: number;
    names: string[];
  }
</script>

<script lang="ts">
  /**
   * Stacked monthly bars, two series: vessels already sanctioned when first seen (neutral grey,
   * the context) under vessels sanctioned after (listed orange, the finding), with a 2px gap
   * between segments. Months with AIS data are shaded. One axis; a recessive gridline at the
   * rounded maximum; hover shows a tooltip with the month, both counts and some names.
   * The orange/grey pair passes the dataviz validator's CVD and normal-vision checks on the dark
   * surface (worst ΔE 13.7 protan); the grey is a deliberate neutral, so it "reads gray".
   */
  import { num } from '../lib/format';
  import type { Lang } from '../i18n/ui';

  interface Props {
    lang: Lang;
    title: string;
    subtitle: string;
    bins: MonthBin[];
    observed: Set<string>;
    laterLabel: string;
    beforeLabel: string;
    observedLabel: string;
    unit: string; // '{n} vessels'
  }

  let { lang, title, subtitle, bins, observed, laterLabel, beforeLabel, observedLabel, unit }: Props = $props();
  let hover = $state<number | null>(null);
  const max = $derived(Math.max(1, ...bins.map((b) => b.later + b.before)));
  const top = $derived(Math.max(5, Math.ceil(max / 5) * 5));
  const H = 180;
  const px = (n: number) => (n / top) * H;
  const tip = $derived(hover === null ? null : bins[hover]);
</script>

<figure class="chart">
  <figcaption>
    <span class="title">{title}</span>
    <span class="sub">{subtitle}</span>
  </figcaption>
  <div class="legend">
    <span><i class="sw later"></i>{laterLabel}</span>
    <span><i class="sw before"></i>{beforeLabel}</span>
    <span><i class="sw obs"></i>{observedLabel}</span>
  </div>
  <div class="plot" style:height={`${H + 22}px`} role="img" aria-label={title}>
    <div class="grid" style:bottom={`${22 + H}px`}><span class="mono">{unit.replace('{n}', num(lang, top))}</span></div>
    <div class="grid base" style:bottom="22px"></div>
    <div class="cols" style:grid-template-columns={`repeat(${bins.length}, minmax(0, 1fr))`}>
      {#each bins as b, i (b.key)}
        <div
          class="col"
          class:obs={observed.has(b.key)}
          class:dim={hover !== null && hover !== i}
          role="presentation"
          onmouseenter={() => (hover = i)}
          onmouseleave={() => (hover = null)}
        >
          <div class="stack" style:height={`${H}px`}>
            {#if b.later}<div class="seg later" style:height={`${px(b.later)}px`}></div>{/if}
            {#if b.before}<div class="seg before" style:height={`${px(b.before)}px`}></div>{/if}
          </div>
          <div class="tick mono">{b.tick ?? ''}</div>
        </div>
      {/each}
    </div>
    {#if tip}
      <div
        class="tip glass"
        style:left={`${((hover! + 0.5) / bins.length) * 100}%`}
        style:transform={`translateX(${hover! / bins.length > 0.7 ? '-100%' : hover! / bins.length < 0.3 ? '0' : '-50%'})`}
      >
        <div class="tip-title">{tip.label}</div>
        {#if tip.later}<div><i class="sw later"></i>{laterLabel}: <b>{tip.later}</b></div>{/if}
        {#if tip.before}<div><i class="sw before"></i>{beforeLabel}: <b>{tip.before}</b></div>{/if}
        {#if tip.names.length}
          <div class="tip-names">{tip.names.slice(0, 6).join(' · ')}{tip.names.length > 6 ? ' …' : ''}</div>
        {/if}
      </div>
    {/if}
  </div>
</figure>

<style>
  .chart {
    margin: 0;
    padding: 18px 18px 14px;
    border-radius: var(--radius-l);
    background: var(--bg-1);
    border: 1px solid var(--line);
  }

  figcaption {
    display: grid;
    gap: 3px;
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

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin: 12px 0 16px;
    font-size: 12px;
    color: var(--text-2);
  }

  .legend span,
  .tip div {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .sw {
    flex: none;
    width: 11px;
    height: 11px;
    border-radius: 3px;
  }

  .sw.later,
  .seg.later {
    background: var(--c-listed);
  }

  .sw.before,
  .seg.before {
    background: #5c6b82;
  }

  .sw.obs {
    background: rgba(255, 255, 255, 0.1);
    border: 1px solid rgba(255, 255, 255, 0.18);
  }

  .plot {
    position: relative;
  }

  .grid {
    position: absolute;
    left: 0;
    right: 0;
    border-top: 1px dashed rgba(255, 255, 255, 0.1);
  }

  .grid span {
    position: absolute;
    top: -16px;
    left: 0;
    font-size: 10.5px;
    color: var(--text-3);
  }

  .grid.base {
    border-top: 1px solid rgba(255, 255, 255, 0.18);
  }

  .cols {
    position: absolute;
    inset: 0;
    display: grid;
    gap: 2px;
  }

  .col {
    display: flex;
    flex-direction: column;
    border-radius: 4px 4px 0 0;
    transition: opacity 0.15s;
  }

  .col.obs {
    background: rgba(255, 255, 255, 0.07);
  }

  .col.dim {
    opacity: 0.45;
  }

  .stack {
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: 2px;
  }

  .seg {
    width: 100%;
    min-height: 2px;
  }

  .seg:first-child {
    border-radius: 4px 4px 0 0;
  }

  .tick {
    height: 22px;
    padding-top: 6px;
    font-size: 10.5px;
    color: var(--text-3);
    white-space: nowrap;
    overflow: visible;
  }

  .tip {
    position: absolute;
    bottom: calc(100% - 8px);
    z-index: 5;
    min-width: 170px;
    max-width: 280px;
    padding: 10px 12px;
    border-radius: 10px;
    font-size: 12px;
    color: var(--text-2);
    pointer-events: none;
    display: grid;
    gap: 4px;
  }

  .tip-title {
    font-weight: 600;
    color: var(--text-1);
  }

  .tip b {
    color: var(--text-1);
  }

  .tip-names {
    display: block !important;
    margin-top: 2px;
    font-size: 11px;
    color: var(--text-3);
  }
</style>
