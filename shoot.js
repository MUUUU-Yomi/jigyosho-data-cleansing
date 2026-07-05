// shoot.js — dashboard.html を headless Chromium でレンダリングしてPNG保存
// 全体1枚 + チャート寄り1枚。ECharts(canvas)の描画完了を待ってから撮る。
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const HERE = __dirname;
  const fileUrl = 'file://' + path.join(HERE, 'dashboard.html').replace(/\\/g, '/');
  const errors = [];

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1024 },
    deviceScaleFactor: 2, // 高精細
  });

  page.on('console', m => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));

  await page.goto(fileUrl, { waitUntil: 'networkupload'.replace('upload', 'idle') });

  // ECharts CDN ロード + 描画完了を待つ:
  //  1) echarts グローバルが入る  2) KPI が 0 でない実値になる  3) bar チャートの canvas が描かれる
  await page.waitForFunction(() => {
    const total = document.querySelector('#kpiTotal');
    const hasEcharts = typeof window.echarts !== 'undefined';
    const barCanvas = document.querySelector('#barChart canvas');
    const kpiReady = total && /\d/.test(total.textContent) && total.textContent.trim() !== '0';
    return hasEcharts && barCanvas && kpiReady;
  }, { timeout: 30000 });

  // カウントアップ演出(900ms)とドーナツ描画の余韻を待つ
  await page.waitForTimeout(1800);

  const fullPath = path.join(HERE, 'screenshot_full.png');
  const chartPath = path.join(HERE, 'screenshot_charts.png');

  // 1枚目: ページ全体
  await page.screenshot({ path: fullPath, fullPage: true });

  // 2枚目: チャート部(バー+ドーナツ)に寄る。該当セクションのバウンディングボックスでクリップ。
  let chartTarget = await page.$('#barChart');
  // チャートを内包するカード/セクションを優先(両チャートが入る親)
  const section = await page.evaluateHandle(() => {
    const bar = document.querySelector('#barChart');
    if (!bar) return document.body;
    // バーとドーナツを両方含む共通の祖先(=グリッド行)まで遡る
    let el = bar;
    for (let i = 0; i < 5 && el && el.parentElement; i++) {
      el = el.parentElement;
      if (el.querySelector('#barChart') && el.querySelector('#donutChart')) return el;
    }
    return bar.closest('section') || bar.parentElement;
  });
  const elForBox = section.asElement() || chartTarget;
  const box = await elForBox.boundingBox();
  if (box) {
    // 少し余白を足してクリップ(画面内に収める)
    const pad = 16;
    const vp = page.viewportSize();
    const x = Math.max(0, box.x - pad);
    const y = Math.max(0, box.y - pad);
    const w = Math.min(vp.width - x, box.width + pad * 2);
    const h = box.height + pad * 2;
    await page.screenshot({ path: chartPath, clip: { x, y, width: w, height: h } });
  } else {
    await page.screenshot({ path: chartPath });
  }

  // 撮影時のKPIテキストを記録(検証用)
  const kpi = await page.evaluate(() => {
    const g = id => (document.querySelector(id) ? document.querySelector(id).textContent.trim() : null);
    return {
      total: g('#kpiTotal'), raw: g('#kpiRaw'), dup: g('#kpiDup'),
      tel: g('#kpiTel'), addr: g('#kpiAddr'), fill: g('#kpiFill'),
      src: g('#srcText'),
    };
  });

  await browser.close();
  console.log(JSON.stringify({ ok: true, fullPath, chartPath, kpi, errors }, null, 2));
})().catch(e => {
  console.error('SHOOT_FAILED:', e && e.stack || e);
  process.exit(1);
});
