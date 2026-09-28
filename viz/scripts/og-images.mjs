// Share-card images (Open Graph, 1200x630) for every language, rendered from the site itself:
// the story's hero, the shadow-fleet hero, and a generic card (the shadow-fleet map with the
// site's name) for every other page. Writes public/og/<lang>-<card>.jpg (git-ignored, like the
// data they are drawn from). Needs a server on the built site:
//   npx astro build && npx astro preview   (then, in another terminal)
//   node scripts/og-images.mjs [baseUrl]    && npx astro build   (to copy them into dist/)
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const base = process.argv[2] ?? 'http://127.0.0.1:4321';
const SITE = {
  en: { kicker: 'checkgraph.dev', title: 'Maritime Fraud Signals', dek: 'Tankers, sanctions and evasive behaviour in the Danish straits, from open AIS data.' },
  es: { kicker: 'checkgraph.dev', title: 'Maritime Fraud Signals', dek: 'Petroleros, sanciones y conductas evasivas en los estrechos daneses, con datos AIS abiertos.' },
};
// Hide the site chrome; keep only the hero. The shadow-fleet hero is sized to the card; the story's
// hero already fills the viewport, and resizing it lets the first chapter scroll into view.
const CHROME = `
  header:not(.hero), footer, .scroll-cue, .maplibregl-ctrl-bottom-right, .maplibregl-ctrl-top-right { display: none !important; }
  body { overflow: hidden !important; }
  .brand-mark { position: fixed; right: 28px; bottom: 22px; z-index: 50; font: 600 15px Inter, sans-serif;
    letter-spacing: 0.04em; color: rgba(238, 242, 247, 0.75); }
`;

mkdirSync('public/og', { recursive: true });
const browser = await chromium.launch({
  channel: process.env.PW_CHANNEL ?? 'msedge',
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
});

const FIT_HERO = '.hero { height: 630px !important; min-height: 0 !important; }';

async function shoot(lang, card, path, edit) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await page.goto(`${base}/${lang}/${path}`, { waitUntil: 'load', timeout: 60000 });
  await page.addStyleTag({ content: CHROME + (card === 'story' ? '' : FIT_HERO) });
  await page.waitForTimeout(Number(process.env.WAIT ?? 9000));
  await page.evaluate(
    ({ edit, card }) => {
      if (edit) {
        document.querySelector('.hero .kicker').textContent = edit.kicker;
        document.querySelector('.hero h1').textContent = edit.title;
        document.querySelector('.hero .dek').textContent = edit.dek;
        document.querySelector('.hero .map-legend')?.remove();
      }
      if (card !== 'site') {
        const mark = document.createElement('div');
        mark.className = 'brand-mark';
        mark.textContent = 'checkgraph.dev';
        document.body.append(mark);
      }
      window.scrollTo(0, 0);
    },
    { edit, card },
  );
  await page.waitForTimeout(500);
  const out = `public/og/${lang}-${card}.jpg`;
  await page.screenshot({ path: out, type: 'jpeg', quality: 86 });
  console.log(out);
  await page.close();
}

for (const lang of ['en', 'es']) {
  await shoot(lang, 'story', '', null);
  await shoot(lang, 'shadow', 'shadow-fleet/', null);
  await shoot(lang, 'site', 'shadow-fleet/', SITE[lang]);
}
await browser.close();
