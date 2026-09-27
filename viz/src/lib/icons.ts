/**
 * A small icon atlas drawn on a canvas at runtime: white shapes, tinted by deck.gl (mask: true).
 * Event types are told apart by SHAPE (the dataviz rule: colour carries one meaning, "detector
 * event"; the glyph carries which kind), so these shapes are also used in legends and panels.
 */

export type Glyph = 'boat' | 'boatStill' | 'diamond' | 'triangle' | 'ring' | 'dot';

const SIZE = 64;
const GLYPHS: Glyph[] = ['boat', 'boatStill', 'diamond', 'triangle', 'ring', 'dot'];

function draw(ctx: CanvasRenderingContext2D, glyph: Glyph, x: number) {
  const c = x + SIZE / 2;
  const m = SIZE / 2;
  ctx.save();
  ctx.fillStyle = '#fff';
  ctx.strokeStyle = '#fff';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  switch (glyph) {
    case 'boat':
      // Pointed hull, pointing north; rotated by course.
      ctx.moveTo(c, 6);
      ctx.lineTo(c + 17, m + 24);
      ctx.lineTo(c, m + 15);
      ctx.lineTo(c - 17, m + 24);
      ctx.closePath();
      ctx.fill();
      break;
    case 'boatStill':
      ctx.arc(c, m, 13, 0, Math.PI * 2);
      ctx.fill();
      break;
    case 'diamond':
      ctx.moveTo(c, 8);
      ctx.lineTo(c + 22, m);
      ctx.lineTo(c, SIZE - 8);
      ctx.lineTo(c - 22, m);
      ctx.closePath();
      ctx.fill();
      break;
    case 'triangle':
      ctx.moveTo(c, 9);
      ctx.lineTo(c + 24, SIZE - 12);
      ctx.lineTo(c - 24, SIZE - 12);
      ctx.closePath();
      ctx.fill();
      break;
    case 'ring':
      ctx.lineWidth = 7;
      ctx.arc(c, m, 21, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(c, m, 7, 0, Math.PI * 2);
      ctx.fill();
      break;
    case 'dot':
      ctx.arc(c, m, 12, 0, Math.PI * 2);
      ctx.fill();
      break;
  }
  ctx.restore();
}

let atlas: { url: string; mapping: Record<string, object> } | null = null;

export function iconAtlas() {
  if (atlas) return atlas;
  const canvas = document.createElement('canvas');
  canvas.width = SIZE * GLYPHS.length;
  canvas.height = SIZE;
  const ctx = canvas.getContext('2d')!;
  const mapping: Record<string, object> = {};
  GLYPHS.forEach((g, i) => {
    draw(ctx, g, i * SIZE);
    mapping[g] = { x: i * SIZE, y: 0, width: SIZE, height: SIZE, anchorY: SIZE / 2, mask: true };
  });
  atlas = { url: canvas.toDataURL(), mapping };
  return atlas;
}

/** Inline SVG markup of a glyph for legends and panels (currentColor). */
export function glyphSvg(glyph: Glyph | 'dash', size = 14): string {
  const s = size;
  const paths: Record<string, string> = {
    boat: '<path d="M8 1.5 13 14 8 11 3 14Z" fill="currentColor"/>',
    boatStill: '<circle cx="8" cy="8" r="4" fill="currentColor"/>',
    diamond: '<path d="M8 1.5 14 8 8 14.5 2 8Z" fill="currentColor"/>',
    triangle: '<path d="M8 2 14.5 13.5h-13Z" fill="currentColor"/>',
    ring: '<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="8" cy="8" r="1.9" fill="currentColor"/>',
    dot: '<circle cx="8" cy="8" r="3.5" fill="currentColor"/>',
    dash: '<path d="M1 8h3.2M6.4 8h3.2M11.8 8H15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };
  return `<svg width="${s}" height="${s}" viewBox="0 0 16 16" aria-hidden="true">${paths[glyph]}</svg>`;
}

export const EVENT_GLYPH = {
  gap: 'dash',
  sts: 'ring',
  spoof: 'diamond',
  behav: 'triangle',
  identity: 'dot',
} as const;
