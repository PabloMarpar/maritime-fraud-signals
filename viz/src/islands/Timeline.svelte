<script lang="ts">
  import { fmtClock, fmtDate } from '../lib/format';
  import { useT, type Lang } from '../i18n/ui';

  interface Props {
    lang: Lang;
    t0: string;
    duration: number; // minutes
    current: number; // minutes since t0
    playing: boolean;
    speed: number; // minutes per second
    bins: number[]; // event counts per bin, evenly spread over duration
    onseek: (minutes: number) => void;
    ontoggle: () => void;
    onspeed: (minutesPerSecond: number) => void;
  }

  let { lang, t0, duration, current, playing, speed, bins, onseek, ontoggle, onspeed }: Props = $props();
  const tr = $derived(useT(lang));

  const SPEEDS = [60, 180, 720, 1440];
  const start = $derived(new Date(t0.replace(/Z?$/, 'Z')).getTime());
  const clock = $derived(fmtClock(lang, new Date(start + current * 60_000)));
  const maxBin = $derived(Math.max(1, ...bins));
  const days = $derived(Math.round(duration / 1440));
  const ticks = $derived(
    Array.from({ length: days + 1 }, (_, d) => d).filter((d) => d % (days > 20 ? 5 : 2) === 0),
  );

  let track: HTMLDivElement;
  let dragging = false;

  function seekFromEvent(e: PointerEvent) {
    const rect = track.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    onseek(f * duration);
  }

  function onKey(e: KeyboardEvent) {
    const step = e.shiftKey ? 1440 : 60;
    if (e.key === 'ArrowRight') onseek(Math.min(duration, current + step));
    else if (e.key === 'ArrowLeft') onseek(Math.max(0, current - step));
    else return;
    e.preventDefault();
  }
</script>

<div class="timeline glass">
  <button class="play" onclick={ontoggle} aria-label={playing ? tr('explore.pause') : tr('explore.play')}>
    {#if playing}
      <svg width="16" height="16" viewBox="0 0 16 16"><rect x="3" y="2.5" width="3.6" height="11" rx="1" fill="currentColor" /><rect x="9.4" y="2.5" width="3.6" height="11" rx="1" fill="currentColor" /></svg>
    {:else}
      <svg width="16" height="16" viewBox="0 0 16 16"><path d="M4 2.6v10.8a.8.8 0 0 0 1.2.7l8.6-5.4a.8.8 0 0 0 0-1.4L5.2 1.9a.8.8 0 0 0-1.2.7Z" fill="currentColor" /></svg>
    {/if}
  </button>

  <div class="clock">
    <div class="clock-time mono">{clock.time}<span class="utc">UTC</span></div>
    <div class="clock-day">{clock.day}</div>
  </div>

  <div class="scrub">
    <div
      class="track"
      bind:this={track}
      role="slider"
      tabindex="0"
      aria-label={tr('explore.eventsTimeline')}
      aria-valuemin={0}
      aria-valuemax={duration}
      aria-valuenow={Math.round(current)}
      aria-valuetext={`${clock.day} ${clock.time} UTC`}
      onkeydown={onKey}
      onpointerdown={(e) => {
        dragging = true;
        track.setPointerCapture(e.pointerId);
        seekFromEvent(e);
      }}
      onpointermove={(e) => dragging && seekFromEvent(e)}
      onpointerup={() => (dragging = false)}
      onpointercancel={() => (dragging = false)}
    >
      <svg class="bins" viewBox={`0 0 ${bins.length} 100`} preserveAspectRatio="none" aria-hidden="true">
        {#each bins as b, i}
          {#if b > 0}
            <rect x={i + 0.12} y={100 - (b / maxBin) * 100} width="0.76" height={(b / maxBin) * 100} rx="0.3" />
          {/if}
        {/each}
      </svg>
      <div class="progress" style:width={`${(current / duration) * 100}%`}></div>
      <div class="thumb" style:left={`${(current / duration) * 100}%`}></div>
    </div>
    <div class="ticks" aria-hidden="true">
      {#each ticks as d}
        <span style:left={`${((d * 1440) / duration) * 100}%`}>{fmtDate(lang, start + d * 86_400_000).replace(/\s?\d{4}$/, '')}</span>
      {/each}
    </div>
  </div>

  <label class="speed">
    <span class="visually-hidden">{tr('explore.speed')}</span>
    <select value={speed} onchange={(e) => onspeed(Number((e.target as HTMLSelectElement).value))}>
      {#each SPEEDS as s}
        <option value={s}>{tr('explore.speedValue', { h: s / 60 })}</option>
      {/each}
    </select>
  </label>
</div>

<style>
  .timeline {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 10px 16px 10px 10px;
    border-radius: 16px;
  }

  .play {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: none;
    background: var(--text-1);
    color: var(--bg-0);
    cursor: pointer;
    flex: none;
    transition: transform 0.2s var(--ease-out);
  }

  .play:hover {
    transform: scale(1.06);
  }

  .clock {
    min-width: 128px;
    flex: none;
  }

  .clock-time {
    font-size: 20px;
    font-weight: 600;
    line-height: 1.1;
  }

  .utc {
    margin-left: 5px;
    font-size: 10px;
    font-weight: 600;
    color: var(--text-3);
    letter-spacing: 0.08em;
  }

  .clock-day {
    font-size: 12px;
    color: var(--text-2);
    text-transform: capitalize;
  }

  .scrub {
    position: relative;
    flex: 1;
    min-width: 0;
    padding-bottom: 16px;
  }

  .track {
    position: relative;
    height: 36px;
    cursor: pointer;
    touch-action: none;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.03);
    overflow: hidden;
  }

  .bins {
    position: absolute;
    inset: 4px 0 0 0;
    width: 100%;
    height: calc(100% - 4px);
    fill: var(--c-event);
    opacity: 0.55;
  }

  .progress {
    position: absolute;
    inset: 0 auto 0 0;
    background: linear-gradient(to right, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0.07));
    border-right: 1px solid rgba(255, 255, 255, 0.5);
    pointer-events: none;
  }

  .thumb {
    position: absolute;
    top: -2px;
    bottom: -2px;
    width: 2px;
    margin-left: -1px;
    background: var(--text-1);
    box-shadow: 0 0 12px rgba(255, 255, 255, 0.6);
    pointer-events: none;
  }

  .ticks {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 14px;
  }

  .ticks span {
    position: absolute;
    transform: translateX(-50%);
    font-size: 10.5px;
    color: var(--text-3);
    white-space: nowrap;
  }

  .ticks span:first-child {
    transform: none;
  }

  .speed select {
    appearance: none;
    height: 32px;
    padding: 0 26px 0 10px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: rgba(255, 255, 255, 0.04)
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23b4bfce' stroke-width='1.5'/%3E%3C/svg%3E")
      no-repeat right 10px center;
    font-size: 12px;
    font-family: var(--font-mono);
    cursor: pointer;
  }

  .speed option {
    background: var(--bg-2);
  }

  @media (max-width: 720px) {
    .timeline {
      gap: 10px;
      padding: 8px 10px 8px 8px;
    }
    .clock {
      min-width: 0;
    }
    .clock-time {
      font-size: 16px;
    }
    .clock-day {
      display: none;
    }
    .speed {
      display: none;
    }
    .play {
      width: 38px;
      height: 38px;
    }
  }
</style>
