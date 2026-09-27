// Screenshot every step of the front-page story. Usage: node scripts/story-shots.mjs [lang] [WxH]
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const lang = process.argv[2] ?? 'es';
const [width, height] = (process.argv[3] ?? '1440x900').split('x').map(Number);
mkdirSync('shots', { recursive: true });
const browser = await chromium.launch({ channel: process.env.PW_CHANNEL ?? 'msedge', args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width, height } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
await page.goto(`http://127.0.0.1:4321/${lang}/`, { waitUntil: 'networkidle' });
await page.waitForTimeout(5000);
await page.screenshot({ path: `shots/story_${lang}_${width}_hero.png` });
const steps = await page.$$eval('[data-step]', (els) => els.map((e) => e.getAttribute('data-step')));
for (const s of steps.filter((x) => x !== '-1')) {
  await page.$eval(`[data-step="${s}"]`, (el) => el.scrollIntoView({ block: 'center' }));
  await page.waitForTimeout(Number(process.env.WAIT ?? 6500));
  await page.screenshot({ path: `shots/story_${lang}_${width}_step${s}.png` });
  console.log(`shots/story_${lang}_${width}_step${s}.png`);
}
console.log(errors.slice(0, 10).join('\n') || 'no errors');
await browser.close();
