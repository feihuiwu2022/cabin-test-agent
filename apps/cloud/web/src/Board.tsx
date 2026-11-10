import { Fragment, useCallback, useEffect, useState } from 'react'
import './board.css'

type Rig = {
  id: string; model: string; ota: string; capabilities: string[]; online: boolean; status: string
  last_seen: number; outbox: Record<string, number>; run_id: string; model_warning: string; caseset: string[]
  schedule: { at: string; enabled: boolean }
}
type Option = { case_id: string; title: string; module: string; auto_status: string }
type Gap = { case_id: string; title: string; module: string; reasons: Record<string, string> }
type Run = {
  id: string; rig_id: string; case_id: string; status: string; verdict: string; block_reason: string; owner: string; kind: string
  duration_s: number | null; created: number; model: string
}
type Defect = { id: string; case_id: string; rig: string; rigs: string[]; summary: string; reason: string; level: string; ota: string; model: string; module: string; finder: string; mine: boolean; actual_shot: string; dup: string; dup_key: string; run_id: string; files: string[] }
type Stat = { ota: string; model: string; should: number; executed: number; passed: number; failed: number; blocked: number; flaky: number; pass_rate: number | null; coverage: number | null; bugs: number; bugs_open: number }
type Regression = { id: string; source: string; by_user: string; created: number; total: number; passed: number; failed: number; blocked: number; flaky: number; active: number; skipped: { rig: string; case_id?: string; reason: string }[] }
type Kpi = { finder: string; confirmed: number; linked: number; rejected: number; reject_rate: number }
type Matrix = { models: { code: string; name: string }[]; rows: { case_id: string; title: string; module: string; cells: Record<string, string> }[] }
type Data = { now: number; rigs: Rig[]; runs: Run[]; defects: Defect[]; gaps: Gap[]; stats: Stat[]; regressions: Regression[]; kpi: Kpi[] }

const POLL_MS = import.meta.env.VITE_DEMO ? 1000 : 5000  // 静态演示回放时刷新快一些
const TABS = [['home', '总览'], ['rigs', '台架'], ['batches', '批次'], ['review', '待审核'], ['matrix', '覆盖矩阵']]
const STATUS: Record<string, string> = { queued: '排队', running: '执行中', cancelling: '取消中', cancelled: '已取消', done: '完成' }
const VERDICT: Record<string, string> = { passed: '通过', failed: '失败', blocked: '阻塞', flaky: '不稳定' }
const KIND: Record<string, string> = { trial: ' · 试跑', retest: ' · 复测' }
const REJECT = ['判定误报', '用例预期有误', '环境问题', '已知问题']

const clock = (t: number) => new Date(t * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
const ago = (s: number) => (s < 60 ? `${Math.round(s)} 秒前` : s < 3600 ? `${Math.round(s / 60)} 分钟前` : `${Math.round(s / 3600)} 小时前`)
const pending = (o: Record<string, number>) => Object.values(o).reduce((a, b) => a + b, 0)

/* 台架用例集：只有已验证、这台台架能跑的用例能选 */
function Caseset({ rig, onSaved }: { rig: Rig; onSaved: () => void }) {
  const [opts, setOpts] = useState<Option[] | null>(null)
  const [sel, setSel] = useState<string[]>(rig.caseset)
  const [err, setErr] = useState('')
  useEffect(() => {
    fetch(`/api/board/rigs/${rig.id}/caseset`).then(r => r.json()).then(j => { setOpts(j.runnable); setSel(j.case_ids) }).catch(() => setErr('读取失败'))
  }, [rig.id])
  const save = async () => {
    const r = await fetch(`/api/board/rigs/${rig.id}/caseset`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ case_ids: sel }) })
    if (r.ok) onSaved()
    else setErr((await r.json()).detail)
  }
  if (!opts) return <p className="meta">{err || '加载中…'}</p>
  if (!opts.length) return <p className="meta">这台台架暂时没有能跑的用例。</p>
  return (
    <div className="caseset">
      {opts.map(o => (
        <label key={o.case_id} title={o.auto_status === '已验证' ? '' : `${o.auto_status}，验证后才能选`}>
          <input type="checkbox" disabled={o.auto_status !== '已验证'} checked={sel.includes(o.case_id)}
            onChange={e => setSel(e.target.checked ? [...sel, o.case_id] : sel.filter(x => x !== o.case_id))} />
          <span className="mono">{o.case_id}</span> {o.title}
        </label>))}
      {err && <p className="err" role="alert">{err}</p>}
      <div className="acts"><button className="primary" onClick={save}>保存用例集</button></div>
    </div>
  )
}

/* 每天到点跑这台台架的用例集，几台台架同一时间到点时算一轮回归 */
function Schedule({ rig, onSaved, run }: { rig: Rig; onSaved: () => void; run: () => void }) {
  const [at, setAt] = useState(rig.schedule.at || '20:00')
  const [on, setOn] = useState(rig.schedule.enabled)
  const [err, setErr] = useState('')
  const save = async (enabled: boolean, time: string) => {
    setOn(enabled)
    const r = await fetch(`/api/board/rigs/${rig.id}/schedule`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ at: time, enabled }) })
    if (r.ok) { setErr(''); onSaved() } else setErr((await r.json()).detail)
  }
  return (
    <>
      <div className="acts schedule">
        <label className="check"><input type="checkbox" checked={on} onChange={e => void save(e.target.checked, at)} /> 每天定时</label>
        <input type="time" value={at} aria-label="定时时间" onChange={e => { setAt(e.target.value); if (on && e.target.value) void save(true, e.target.value) }} />
        <button disabled={!rig.online || !rig.caseset.length} title={rig.online ? (rig.caseset.length ? '' : '先配置用例集') : '台架离线'} onClick={run}>立即运行</button>
      </div>
      {err && <p className="err" role="alert">{err}</p>}
    </>
  )
}

function Rigs({ rigs, now, reload, run }: { rigs: Rig[]; now: number; reload: () => void; run: (id: string) => void }) {
  const [open, setOpen] = useState('')
  if (!rigs.length) return <p className="empty">还没有台架注册。在台架 PC 上启动 cabin-agent 后会出现在这里。</p>
  return (
    <div className="rigs">
      {rigs.map(r => (
        <article key={r.id} className={`rig ${r.online ? '' : 'off'}`}>
          <header><b>{r.id}</b><span className={`dot ${r.online ? 'ok' : 'bad'}`}>{r.online ? (r.status === 'running' ? '执行中' : '空闲') : '离线'}</span></header>
          <dl>
            <dt>车型</dt><dd>{r.model}</dd>
            <dt>版本</dt><dd>{r.ota || '—'}</dd>
            <dt>能力</dt><dd>{r.capabilities.join('、') || '—'}</dd>
            <dt>心跳</dt><dd>{r.last_seen ? ago(now - r.last_seen) : '—'}</dd>
            <dt>待补传</dt><dd>{pending(r.outbox)}</dd>
            <dt>当前</dt><dd className="mono">{r.run_id || '—'}</dd>
            <dt>用例集</dt><dd>{r.caseset.length} 条 <button onClick={() => setOpen(open === r.id ? '' : r.id)}>{open === r.id ? '收起' : '配置'}</button></dd>
          </dl>
          <Schedule rig={r} onSaved={reload} run={() => run(r.id)} />
          {r.model_warning && <p className="err" role="alert">{r.model_warning}</p>}
          {open === r.id && <Caseset rig={r} onSaved={() => { setOpen(''); reload() }} />}
        </article>
      ))}
    </div>
  )
}

function Runs({ runs, cancel }: { runs: Run[]; cancel: (id: string) => void }) {
  if (!runs.length) return <p className="empty">还没有执行记录。</p>
  return (
    <div className="scroll"><table>
      <thead><tr><th>执行单元</th><th>台架</th><th>用例</th><th>发现人</th><th>状态</th><th>结论</th><th>耗时</th><th>创建时间</th><th /></tr></thead>
      <tbody>
        {runs.map(r => (
          <tr key={r.id}>
            <td className="mono">{r.id}</td><td>{r.rig_id}</td><td className="mono">{r.case_id}{KIND[r.kind]}</td><td>{r.owner || '—'}</td>
            <td>{STATUS[r.status] ?? r.status}</td>
            <td className={`v-${r.verdict}`} title={r.block_reason}>{VERDICT[r.verdict] ?? '—'}</td>
            <td>{r.duration_s != null ? `${r.duration_s} 秒` : '—'}</td><td>{clock(r.created)}</td>
            <td>{['queued', 'running'].includes(r.status) && <button onClick={() => cancel(r.id)}>取消</button>}</td>
          </tr>
        ))}
      </tbody>
    </table></div>
  )
}

const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`)

function Stats({ stats }: { stats: Stat[] }) {
  if (!stats.length) return <p className="empty">还没有执行结果。</p>
  return (
    <div className="scroll"><table>
      <thead><tr><th>OTA 版本</th><th>车型</th><th>应执行</th><th>已执行</th><th>覆盖率</th><th>通过</th><th>失败</th><th>阻塞</th><th>不稳定</th><th>通过率</th><th>Bug（未关闭）</th></tr></thead>
      <tbody>{stats.map(s => (
        <tr key={`${s.ota}-${s.model}`}><td className="mono">{s.ota || '—'}</td><td>{s.model}</td><td>{s.should}</td><td>{s.executed}</td><td>{pct(s.coverage)}</td>
          <td className="v-passed">{s.passed}</td><td className="v-failed">{s.failed}</td><td className="v-blocked">{s.blocked}</td><td>{s.flaky}</td><td>{pct(s.pass_rate)}</td><td>{s.bugs}（{s.bugs_open}）</td></tr>))}
      </tbody>
    </table></div>
  )
}

/* 批次详情：按车型和台架展开 */
function BatchDetail({ id, rigs }: { id: string; rigs: Rig[] }) {
  const [runs, setRuns] = useState<Run[] | null>(null)
  useEffect(() => { fetch(`/api/board/regressions/${id}`).then(r => r.json()).then(j => setRuns(j.runs)).catch(() => setRuns([])) }, [id])
  if (!runs) return <p className="meta">加载中…</p>
  if (!runs.length) return <p className="meta">这一轮没有派出用例。</p>
  const groups = new Map<string, Run[]>()
  for (const r of runs) {
    const k = `${r.model || rigs.find(x => x.id === r.rig_id)?.model || '未知车型'} · ${r.rig_id}`
    groups.set(k, [...(groups.get(k) ?? []), r])
  }
  return <>{[...groups].map(([k, rs]) => (
    <div key={k}><h3 className="group">{k}（{rs.length}）</h3>
      <ul className="runs">{rs.map(r => (
        <li key={r.id}><span className="mono">{r.case_id}{KIND[r.kind]}</span><span className={`v-${r.verdict}`} title={r.block_reason}>{VERDICT[r.verdict] ?? STATUS[r.status] ?? r.status}</span></li>))}
      </ul></div>))}</>
}

function Regressions({ list, rigs }: { list: Regression[]; rigs: Rig[] }) {
  const [open, setOpen] = useState('')
  if (!list.length) return <p className="empty">还没有回归批次。</p>
  return (
    <div className="scroll"><table>
      <thead><tr><th>批次</th><th>触发</th><th>时间</th><th>用例</th><th>进行中</th><th>通过</th><th>失败</th><th>阻塞</th><th>不稳定</th><th>未执行</th></tr></thead>
      <tbody>{list.map(g => (
        <Fragment key={g.id}><tr><td className="mono"><button aria-expanded={open === g.id} onClick={() => setOpen(open === g.id ? '' : g.id)}>{g.id}</button></td><td>{g.source}{g.by_user && ` · ${g.by_user}`}</td><td>{clock(g.created)}</td><td>{g.total}</td><td>{g.active}</td>
          <td className="v-passed">{g.passed}</td><td className="v-failed">{g.failed}</td><td className="v-blocked">{g.blocked}</td><td>{g.flaky}</td>
          <td className="why" title={g.skipped.map(x => `${x.rig} ${x.case_id ?? ''}：${x.reason}`).join('\n')}>{g.skipped.length || '—'}</td></tr>
          {open === g.id && <tr><td colSpan={10}><BatchDetail id={g.id} rigs={rigs} /></td></tr>}</Fragment>))}
      </tbody>
    </table></div>
  )
}

/* 总览：最新一轮回归的进度、在线台架、待审核 */
function Overview({ data }: { data: Data }) {
  const last = data.regressions[0]
  const online = data.rigs.filter(r => r.online).length
  return (
    <div className="tiles">
      <div className="tile"><b>{last ? `${last.total - last.active} / ${last.total}` : '—'}</b><span>最新回归进度{last && ` · ${last.id}`}</span></div>
      <div className="tile"><b>{online} / {data.rigs.length}</b><span>在线台架</span></div>
      <div className="tile"><b>{data.defects.length}</b><span>待审核缺陷</span></div>
      <div className="tile"><b>{data.gaps.length}</b><span>缺台架能力的用例</span></div>
    </div>
  )
}

function Kpis({ list }: { list: Kpi[] }) {
  if (!list.length) return <p className="empty">还没有审核过的缺陷。</p>
  return (
    <div className="scroll"><table>
      <thead><tr><th>发现人</th><th>确认 Bug</th><th>关联已有单</th><th>驳回</th><th>驳回率</th></tr></thead>
      <tbody>{list.map(k => <tr key={k.finder}><td>{k.finder}</td><td>{k.confirmed}</td><td>{k.linked}</td><td>{k.rejected}</td><td>{pct(k.reject_rate)}</td></tr>)}</tbody>
    </table></div>
  )
}

/* 覆盖矩阵：用例 × 车型，每格是这个车型最新一次的结论 */
function CoverageMatrix() {
  const [m, setM] = useState<Matrix | null>(null)
  const [mod, setMod] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => { fetch('/api/board/matrix').then(r => r.json()).then(setM).catch(() => setErr('读取失败')) }, [])
  if (!m) return <p className="meta">{err || '加载中…'}</p>
  if (!m.rows.length) return <p className="empty">用例库里还没有用例。</p>
  const rows = m.rows.filter(r => !mod || r.module === mod)
  return (
    <>
      <div className="acts filters"><select value={mod} onChange={e => setMod(e.target.value)} aria-label="模块"><option value="">全部模块</option>{[...new Set(m.rows.map(r => r.module))].sort().map(x => <option key={x}>{x}</option>)}</select></div>
      <div className="scroll"><table className="matrix">
        <thead><tr><th>用例</th><th>标题</th>{m.models.map(c => <th key={c.code}>{c.name}</th>)}</tr></thead>
        <tbody>{rows.map(r => (
          <tr key={r.case_id}><td className="mono">{r.case_id}</td><td>{r.title}</td>
            {m.models.map(c => { const v = r.cells[c.code]; return <td key={c.code} className={`v-${v}`}>{v === 'na' ? '—' : VERDICT[v] ?? '未执行'}</td> })}</tr>))}
        </tbody>
      </table></div>
    </>
  )
}

function Gaps({ gaps }: { gaps: Gap[] }) {
  if (!gaps.length) return <p className="empty">所有用例都有台架能跑。</p>
  return (
    <div className="scroll"><table>
      <thead><tr><th>用例</th><th>标题</th><th>模块</th><th>原因</th></tr></thead>
      <tbody>{gaps.map(g => (
        <tr key={g.case_id}><td className="mono">{g.case_id}</td><td>{g.title}</td><td>{g.module}</td>
          <td className="why">{Object.entries(g.reasons).map(([r, w]) => `${r}：${w}`).join('；') || '没有注册的台架'}</td></tr>))}
      </tbody>
    </table></div>
  )
}

function DefectItem({ d, review }: { d: Defect; review: (id: string, action: string, reason?: string, key?: string) => void }) {
  const [reason, setReason] = useState(REJECT[0])
  return (
    <article className="defect">
      {d.actual_shot && <a href={d.actual_shot} target="_blank" rel="noreferrer"><img src={d.actual_shot} alt={`${d.case_id} 实际截图`} /></a>}
      <div>
        <h3>{d.summary}</h3>
        <p className="meta"><span className="mono">{d.case_id}</span> · OTA {d.ota} · {[d.rig, ...d.rigs].join('、')} · 发现人 {d.finder || '定时任务'} · 置信 {d.level}{d.dup && ` · ${d.dup}`}</p>
        <p>{d.reason}</p>
        {d.files.length > 0 && <p className="meta">证据：{d.files.map(f => <a key={f} className="link" href={`/evidence/${d.run_id}/${f}`} target="_blank" rel="noreferrer">{f}</a>)}</p>}
        <div className="acts">
          <button className="primary" onClick={() => review(d.id, 'confirm')}>确认并建票</button>
          {d.dup_key && <button onClick={() => review(d.id, 'link', '', d.dup_key)}>追加到 {d.dup_key}</button>}
          <select value={reason} onChange={e => setReason(e.target.value)} aria-label="驳回原因">{REJECT.map(x => <option key={x}>{x}</option>)}</select>
          <button onClick={() => review(d.id, 'reject', reason)}>驳回</button>
        </div>
      </div>
    </article>
  )
}

/* 默认看自己的，按 车型 → 模块 分组，也可以按 OTA 版本筛 */
function Defects({ defects, review }: { defects: Defect[]; review: (id: string, action: string, reason?: string, key?: string) => void }) {
  const [mine, setMine] = useState(true)
  const [model, setModel] = useState('')
  const [ota, setOta] = useState('')
  const uniq = (k: 'model' | 'ota') => [...new Set(defects.map(d => d[k]))].sort()
  const list = defects.filter(d => (!mine || d.mine) && (!model || d.model === model) && (!ota || d.ota === ota))
  const groups = new Map<string, Defect[]>()
  for (const d of list) {
    const k = `${d.model} · ${d.module || '未分模块'}`
    groups.set(k, [...(groups.get(k) ?? []), d])
  }
  return (
    <>
      <div className="acts filters">
        <label className="check"><input type="checkbox" checked={mine} onChange={e => setMine(e.target.checked)} /> 只看我的（{defects.filter(d => d.mine).length}）</label>
        <select value={model} onChange={e => setModel(e.target.value)} aria-label="车型"><option value="">全部车型</option>{uniq('model').map(x => <option key={x}>{x}</option>)}</select>
        <select value={ota} onChange={e => setOta(e.target.value)} aria-label="OTA 版本"><option value="">全部 OTA</option>{uniq('ota').map(x => <option key={x}>{x}</option>)}</select>
      </div>
      {list.length === 0 ? <p className="empty">{defects.length ? '当前筛选下没有待审核的缺陷。' : '没有待审核的缺陷候选。'}</p> : [...groups].map(([k, ds]) => (
        <div key={k}><h3 className="group">{k}（{ds.length}）</h3><div className="defects">{ds.map(d => <DefectItem key={d.id} d={d} review={review} />)}</div></div>))}
    </>
  )
}

export default function Board() {
  const [data, setData] = useState<Data | null>(null)
  const [user, setUser] = useState<string | null | undefined>(undefined)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState(() => (TABS.some(t => t[0] === location.hash.slice(1)) ? location.hash.slice(1) : 'home'))

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/board')
      if (r.status === 401) { setUser(null); return }
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json())
      setErr('')
    } catch (e) { setErr(`刷新失败：${e instanceof Error ? e.message : e}`) }
  }, [])

  useEffect(() => {
    fetch('/api/v1/auth/me').then(r => r.json()).then(j => setUser(j.user?.name ?? null)).catch(() => setUser(null))
  }, [])
  useEffect(() => {
    if (!user) return
    void load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [user, load])

  async function post(url: string, body?: object) {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) })
    if (!r.ok) setErr((await r.json().catch(() => null))?.detail ?? `操作失败：HTTP ${r.status}`)
    void load()
  }

  if (user === undefined) return <main className="gate">加载中…</main>
  if (user === null) {
    return (
      <main className="gate">
        <h1>CabinPilot 调度看板</h1>
        <p>查看台架在线状态和执行情况需要登录。</p>
        <a className="login" href="/api/v1/auth/login?redirect=/board">登录</a>
      </main>
    )
  }
  const online = data?.rigs.filter(r => r.online).length ?? 0
  const go = (t: string) => { location.hash = t; setTab(t) }
  return (
    <main>
      <header className="top">
        <h1>CabinPilot 调度看板</h1>
        <span className="sub">{data ? `在线 ${online} / ${data.rigs.length} · 待审核 ${data.defects.length}` : '加载中…'}</span>
        <a className="link" href="/cases">用例工作台</a>
        <a className="link" href="/touch">触屏执行</a>
        <a className="link" href="/skills">用例编写规范</a>
        <a className="link" href="/settings">设置</a>
        <span className="me">{user}<button onClick={() => void post('/api/v1/auth/logout').then(() => setUser(null))}>退出</button></span>
      </header>
      {err && <p className="err" role="alert">{err}</p>}
      <nav className="tabs" aria-label="看板页面">{TABS.map(([k, name]) => (
        <button key={k} aria-current={tab === k ? 'page' : undefined} onClick={() => go(k)}>{name}{k === 'review' && data ? `（${data.defects.length}）` : ''}</button>))}
      </nav>
      {data && tab === 'home' && <>
        <section><h2>总览</h2><Overview data={data} /></section>
        <section><h2>版本质量</h2><Stats stats={data.stats} /></section>
        <section><h2>人员 KPI</h2><Kpis list={data.kpi} /></section>
      </>}
      {data && tab === 'rigs' && <>
        <section><h2>台架</h2><Rigs rigs={data.rigs} now={data.now} reload={() => void load()} run={id => void post(`/api/board/rigs/${id}/run`)} /></section>
        <section><h2>缺台架能力</h2><Gaps gaps={data.gaps} /></section>
      </>}
      {data && tab === 'batches' && <>
        <section><h2>回归批次</h2><Regressions list={data.regressions} rigs={data.rigs} /></section>
        <section><h2>执行情况</h2><Runs runs={data.runs} cancel={id => void post(`/api/board/runs/${id}/cancel`)} /></section>
      </>}
      {data && tab === 'review' && <section><h2>待审核缺陷</h2><Defects defects={data.defects} review={(id, action, reason, key) => void post(`/api/board/defects/${id}/review`, { action, reason: reason ?? '', link_key: key ?? '' })} /></section>}
      {tab === 'matrix' && <section><h2>覆盖矩阵</h2><CoverageMatrix /></section>}
    </main>
  )
}
