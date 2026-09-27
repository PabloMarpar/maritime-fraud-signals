/** Data colours as RGB triples for deck.gl. Must match --c-* in styles/global.css; the three
 * categorical hues were validated all-pairs on the dark surface (see that file's header). */
export const RGB = {
  vessel: [57, 135, 229] as [number, number, number],
  listed: [217, 89, 38] as [number, number, number],
  event: [25, 158, 112] as [number, number, number],
  white: [238, 242, 247] as [number, number, number],
};

export function withAlpha(rgb: [number, number, number], a: number): [number, number, number, number] {
  return [rgb[0], rgb[1], rgb[2], Math.round(a * 255)];
}
