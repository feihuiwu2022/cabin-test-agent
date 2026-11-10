const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const out = process.argv[2];
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const t0 = Date.now(); const ev = []; const http = {};
  const mark = name => ev.push({ t: Date.now() - t0, mark: name });
  p.on('websocket', ws => {
    ws.on('framereceived', f => ev.push({ t: Date.now() - t0, d: 'in', m: f.payload.toString() }));
    ws.on('framesent', f => ev.push({ t: Date.now() - t0, d: 'out', m: f.payload.toString() }));
  });
  p.on('response', async r => {
    const u = new URL(r.url());
    if (/^\/(local|api)\//.test(u.pathname) && !/\/api\/tts/.test(u.pathname)) {
      try { http[r.request().method() + ' ' + u.pathname + u.search] = await r.text(); } catch {}
    }
  });
  await p.goto('http://127.0.0.1:7860/'); await p.waitForTimeout(3000);
  mark('run'); await p.getByRole('button', { name: '立即运行本台架用例' }).click();
  for (let i = 0; i < 60; i++) { await p.waitForTimeout(1000); if (/TC-HVAC-001 · 结论/.test(await p.locator('body').innerText())) break; }
  await p.waitForTimeout(4000); mark('end');
  fs.writeFileSync(out + '/rec.json', JSON.stringify({ ev, http }));
  console.log('events', ev.length, 'http', Object.keys(http));
  await b.close();
})();
