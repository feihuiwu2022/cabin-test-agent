const { chromium } = require('playwright');
const fs = require('fs');
(async () => { try {
  const out = process.argv[2]; const B = 'http://127.0.0.1:8800';
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const t0 = Date.now(); const ev = []; const http = {};
  const mark = n => ev.push({ t: Date.now() - t0, mark: n });
  p.on('websocket', ws => {
    ws.on('framereceived', f => ev.push({ t: Date.now() - t0, d: 'in', m: f.payload.toString() }));
    ws.on('framesent', f => ev.push({ t: Date.now() - t0, d: 'out', m: f.payload.toString() }));
  });
  p.on('response', async r => { const u = new URL(r.url()); if (/^\/api\//.test(u.pathname)) { try { http[r.request().method() + ' ' + u.pathname + u.search] = await r.text() } catch {} } });
  await p.goto(B + '/cases'); await p.waitForTimeout(3000);
  mark('import'); await p.getByRole('button', { name: '用示例 PRD 试一下' }).click();
  await p.getByRole('button', { name: '无意见的全部通过' }).waitFor({ timeout: 120000 });
  await p.waitForTimeout(1500); await p.screenshot({ path: out + '/cases-review.png' });
  mark('submit'); await p.getByRole('button', { name: '采纳建议并提交审核' }).click();
  let last = ev.length, quiet = 0;
  for (let i = 0; i < 240; i++) { await p.waitForTimeout(1000); if (ev.length === last) quiet++; else { quiet = 0; last = ev.length } if (i > 20 && quiet >= 12) break; }
  mark('end');
  await p.screenshot({ path: out + '/cases-end.png' });
  fs.writeFileSync(out + '/cases.json', JSON.stringify({ ev, http }));
  console.log('events', ev.length, Object.keys(http));
  await b.close();
} catch (e) { console.log('ERR', e.message.split('\n')[0]); process.exit(1) } })();
