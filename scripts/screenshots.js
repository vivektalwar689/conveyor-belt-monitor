// Capture README screenshots of a running dashboard (Playwright).
//   node scripts/screenshots.js http://localhost:8000 docs/images
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const url = process.argv[2] || 'http://localhost:8000';
  const out = process.argv[3] || 'docs/images';
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForTimeout(7000);
  const box = async (sel) => (await page.$(sel)).boundingBox();
  const grids = await page.$$('.grid2');
  const g1 = await grids[0].boundingBox();
  await page.screenshot({ path: path.join(out, 'dashboard_top.png'), fullPage: true,
    clip: { x: 0, y: 0, width: 1440, height: g1.y + g1.height + 4 } });
  const vis = await box('section:has(#loopstrip)');
  const det = await box('#detail-card');
  await page.screenshot({ path: path.join(out, 'dashboard_vision_detail.png'), fullPage: true,
    clip: { x: 0, y: vis.y - 8, width: 1440, height: det.y + det.height - vis.y + 16 } });
  const g2 = await grids[1].boundingBox();
  await page.screenshot({ path: path.join(out, 'dashboard_dq.png'), fullPage: true,
    clip: { x: 0, y: g2.y - 8, width: 1440, height: g2.height + 16 } });
  console.log(JSON.stringify({ errors }));
  await browser.close();
})();
