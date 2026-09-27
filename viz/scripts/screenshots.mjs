// Screenshot pages of the running dev server with headless Chromium, and report console errors.
// Usage: node scripts/screenshots.mjs [baseUrl] [path[@WxH][#waitMs] ...]
//   node scripts/screenshots.mjs http://localhost:4321 /en/explore/ /es/@390x844
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const [base = 'http://localhost:4321', ...targets] = process.argv.slice(2);
const pages = targets.length ? targets : ['/en/', '/en/explore/'];
mkdirSync('shots', { recursive: true });

const browser = await chromium.launch({
  channel: process.env.PW_CHANNEL ?? 'msedge',
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
});

for (const target of pages) {
  const [pathAndSize, waitStr] = target.split('#');
  const [path, size] = pathAndSize.split('@');
  const [width, height] = (size ?? '1440x900').split('x').map(Number);
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error' || msg.type() === 'warning') problems.push(`${msg.type()}: ${msg.text()}`);
  });
  page.on('pageerror', (err) => problems.push(`pageerror: ${err.message}`));
  await page.goto(base + path, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(Number(waitStr ?? 4000));
  const name = `${path.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'root'}_${width}x${height}.png`;
  await page.screenshot({ path: `shots/${name}` });
  console.log(`shots/${name}`);
  for (const p of problems.slice(0, 15)) console.log('   ' + p.slice(0, 300));
  await context.close();
}
await browser.close();
