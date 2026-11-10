const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const out = process.argv[2]; const B = 'http://127.0.0.1:8800';
  const b = await chromium.launch();
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  await p.goto(B + '/board#rigs'); await p.waitForTimeout(2500);
  const get = async u => (await p.request.get(B + u)).text();
  const snaps = [{ t: 0, j: await get('/api/board') }];
  const t0 = Date.now();
  for (const btn of await p.getByRole('button', { name: '立即运行' }).all()) { await btn.click(); await p.waitForTimeout(150); }
  let idle = 0;
  for (let i = 0; i < 240; i++) {
    await p.waitForTimeout(1000);
    const j = await get('/api/board'); snaps.push({ t: Date.now() - t0, j });
    const d = JSON.parse(j);
    const busy = d.runs.some(r => ['queued', 'running', 'cancelling'].includes(r.status));
    idle = busy ? 0 : idle + 1;
    if (i > 5 && idle >= 4) break;
  }
  const last = JSON.parse(snaps[snaps.length - 1].j);
  const http = { '/api/v1/auth/me': await get('/api/v1/auth/me'), '/api/board/matrix': await get('/api/board/matrix') };
  for (const r of last.rigs) http[`/api/board/rigs/${r.id}/caseset`] = await get(`/api/board/rigs/${r.id}/caseset`);
  for (const g of last.regressions || []) http[`/api/board/regressions/${g.id}`] = await get(`/api/board/regressions/${g.id}`);
  fs.writeFileSync(out + '/board.json', JSON.stringify({ snaps, http }));
  console.log('snaps', snaps.length, 'runs', last.runs.length, 'defects', last.defects.length, Object.keys(last));
  await p.screenshot({ path: out + '/board-end.png', fullPage: true });
  await b.close();
})();
