// 触屏执行：UI 地图录制的回看与确认、定位失败的自愈建议。录制在台架跑，这里只做人工确认
import { useCallback, useEffect, useState } from 'react'
import './board.css'

type Ctl = { key: string; text: string; desc: string; checkable: boolean; ratio: number[]; source: string }
type Step = { n: number; action: string; mark: number | null; control: Ctl | null; case_step: number | null; reason: string; marked_sha: string; after_checked: boolean | null }
type Rec = { rec_id: string; case_id: string; rig_id: string; package: string; sw_version: string; model: string; steps_text: string[]; trace: Step[]; outcome: string; note: string; status: string; by: string; props: { prop: string; expect: string }[] }
type RecRow = { rec_id: string; case_id: string; package: string; rig_id: string; outcome: string; status: string; steps: number; at: number }
type Sug = { status: string; found: boolean; reason: string; diff?: string; by?: string }
type HealRow = { run_id: string; case_id: string; rig_id: string; at: number; step: number | null; label: string; error: string; has_scene: boolean; suggestion: Sug | null }

const OUTCOME: Record<string, string> = { done: '走完', fail: '失败', max_steps: '步数超限', error: '出错' }
const PROPS = ['HVAC_POWER_ON', 'HVAC_AC_ON', 'HVAC_AUTO_ON', 'HVAC_TEMPERATURE_SET', 'HVAC_FAN_SPEED', 'HVAC_RECIRC_ON']
const when = (t: number) => new Date(t * 1000).toLocaleString('zh-CN', { hour12: false })
const name = (c: Ctl | null) => (c ? c.text || c.desc || c.key.split('/').pop() || `@${c.ratio}` : '')

async function send(url: string, body: object): Promise<string> {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (r.ok) return ''
  const j = await r.json().catch(() => null)
  return j?.detail ?? `操作失败：HTTP ${r.status}`
}

/* ---------- 录制 ---------- */
function Recording({ id, back }: { id: string; back: () => void }) {
  const [d, setD] = useState<{ recording: Rec; image_base: string; plan: object | null } | null>(null)
  const [props, setProps] = useState<{ prop: string; expect: string }[]>([])
  const [msg, setMsg] = useState('')
  const load = useCallback(() => {
    fetch(`/api/uimap/recordings/${id}/detail`).then(r => r.json()).then(j => { setD(j); setProps(j.recording.props ?? []) }).catch(() => setMsg('读取失败'))
  }, [id])
  useEffect(load, [load])
  if (!d) return <p className="meta">{msg || '加载中…'}</p>
  const r = d.recording
  const pending = r.status === '待确认'
  const decide = async (ok: boolean) => {
    const e = await send(`/api/uimap/recordings/${id}/confirm`, { ok, props: props.filter(p => p.prop && p.expect !== '') })
    setMsg(e || (ok ? '已确认，已并入 UI 地图；重新生成该用例的测试代码时会按这次录制生成' : '已驳回'))
    load()
  }
  return (
    <section>
      <p className="back"><button onClick={back}>返回列表</button></p>
      <h2>录制 {r.rec_id}</h2>
      <p className="meta">用例 {r.case_id || '—'} · 应用 {r.package} · 台架 {r.rig_id || '—'} · 软件 {r.sw_version || '—'} · 车型 {r.model || '—'}</p>
      <p>结果 <b className={r.outcome === 'done' ? 'v-passed' : 'v-failed'}>{OUTCOME[r.outcome] ?? r.outcome}</b> {r.note} · 状态 {r.status}{r.by && `（${r.by}）`}</p>
      <ol className="meta">{r.steps_text.map((s, i) => <li key={i}>{s}</li>)}</ol>
      <div className="defects">{r.trace.map(s => (
        <article className="defect" key={s.n}>
          {s.marked_sha && <a href={d.image_base + s.marked_sha} target="_blank" rel="noreferrer"><img src={d.image_base + s.marked_sha} alt={`第 ${s.n} 步带编号截图`} style={{ width: 280 }} /></a>}
          <div>
            <h3>第 {s.n} 步 · {s.action === 'tap' ? `点 ${s.mark} 号「${name(s.control)}」` : s.action}</h3>
            <p className="meta">用例步骤 {s.case_step ?? '—'} · {s.reason}</p>
            {s.control && <p className="mono">{s.control.key || '（无 id）'} · {s.control.source === 'ocr' ? 'OCR 文字框' : '控件树'}{s.control.checkable && ' · 开关'}</p>}
            {s.after_checked !== null && <p>点后开关为 <b>{s.after_checked ? '开' : '关'}</b>（生成界面状态断言）</p>}
          </div>
        </article>))}
      </div>
      {pending && r.outcome === 'done' && <>
        <h2>车辆属性断言（可选，双线判定）</h2>
        <p className="meta">界面开关之外，再读车辆真实状态。界面显示已开、车辆属性仍为关时判失败。</p>
        {props.map((p, i) => (
          <div className="acts" key={i}>
            <select value={p.prop} onChange={e => setProps(props.map((x, j) => (j === i ? { ...x, prop: e.target.value } : x)))} aria-label="车辆属性">{PROPS.map(k => <option key={k}>{k}</option>)}</select>
            <input value={p.expect} onChange={e => setProps(props.map((x, j) => (j === i ? { ...x, expect: e.target.value } : x)))} placeholder="期望值，如 true / 24" aria-label="期望值" />
            <button onClick={() => setProps(props.filter((_, j) => j !== i))}>删除</button>
          </div>))}
        <p><button onClick={() => setProps([...props, { prop: PROPS[0], expect: 'true' }])}>加一条</button></p>
      </>}
      {pending && <div className="acts">
        <button className="primary" disabled={r.outcome !== 'done'} title={r.outcome !== 'done' ? '没走完的录制不能确认' : ''} onClick={() => void decide(true)}>确认并入 UI 地图</button>
        <button onClick={() => void decide(false)}>驳回</button>
      </div>}
      {msg && <p className="meta" role="status">{msg}</p>}
      {d.plan && <details><summary>按这次录制会生成的测试代码</summary><pre className="mono scroll">{JSON.stringify(d.plan, null, 1)}</pre></details>}
    </section>
  )
}

function Recordings() {
  const [rows, setRows] = useState<RecRow[] | null>(null)
  const [status, setStatus] = useState('待确认')
  const [open, setOpen] = useState(() => new URLSearchParams(location.search).get('rec') ?? '')
  useEffect(() => { fetch(`/api/uimap/recordings?status=${encodeURIComponent(status)}`).then(r => r.json()).then(j => setRows(j.recordings)).catch(() => setRows([])) }, [status, open])
  if (open) return <Recording id={open} back={() => setOpen('')} />
  return (
    <section>
      <h2>UI 地图录制</h2>
      <p className="meta">台架上执行 <span className="mono">apps/agent/scripts/touch_record.py</span> 录制，录完自动上传到这里。确认后才并入 UI 地图、用于生成测试代码。</p>
      <div className="acts filters">{['待确认', '已确认', '已驳回', ''].map(s => (
        <button key={s || 'all'} aria-current={status === s ? 'page' : undefined} onClick={() => setStatus(s)}>{s || '全部'}</button>))}
      </div>
      {!rows ? <p className="meta">加载中…</p> : rows.length === 0 ? <p className="empty">没有{status}的录制。</p> : (
        <div className="scroll"><table>
          <thead><tr><th>录制</th><th>用例</th><th>应用</th><th>台架</th><th>步数</th><th>结果</th><th>状态</th><th>时间</th></tr></thead>
          <tbody>{rows.map(r => (
            <tr key={r.rec_id}>
              <td><button className="link" onClick={() => setOpen(r.rec_id)}>{r.rec_id}</button></td>
              <td>{r.case_id || '—'}</td><td className="mono">{r.package}</td><td>{r.rig_id || '—'}</td><td>{r.steps}</td>
              <td className={r.outcome === 'done' ? 'v-passed' : 'v-failed'}>{OUTCOME[r.outcome] ?? r.outcome}</td><td>{r.status}</td><td>{when(r.at)}</td>
            </tr>))}
          </tbody>
        </table></div>)}
    </section>
  )
}

/* ---------- 自愈 ---------- */
function Heal({ row, back }: { row: HealRow; back: () => void }) {
  const [d, setD] = useState<{ fail_shot: string; marked_shot: string; suggestion: (Sug & { diff?: string }) | null } | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [hint, setHint] = useState('')
  const load = useCallback(() => { fetch(`/api/heal/${row.run_id}`).then(r => r.json()).then(setD).catch(() => setMsg('读取失败')) }, [row.run_id])
  useEffect(load, [load])
  const ask = async () => {
    setBusy(true); setMsg('正在让视觉模型在失败现场找新控件…')
    const e = await send(`/api/heal/${row.run_id}`, { case_step: hint })
    setBusy(false); setMsg(e); load()
  }
  const decide = async (ok: boolean) => {
    const e = await send(`/api/heal/${row.run_id}/confirm`, { ok })
    setMsg(e || (ok ? '已写回测试代码（新版本），下次回放按新定位执行' : '已拒绝，计入评估集'))
    load()
  }
  const s = d?.suggestion
  return (
    <section>
      <p className="back"><button onClick={back}>返回列表</button></p>
      <h2>自愈建议 · {row.case_id} · 执行 {row.run_id}</h2>
      <p className="meta">第 {row.step} 步 {row.label}：{row.error}</p>
      <p className="meta">这次执行仍判阻塞，建议不改变结论。接受后写回测试代码（新版本），拒绝的计入评估集。</p>
      <div className="defect">
        {d?.fail_shot ? <img src={d.fail_shot} alt="失败现场截图" style={{ width: 320 }} /> : <p className="empty">台架没有上传失败现场，无法给出建议。</p>}
        {d?.marked_shot && <img src={d.marked_shot} alt="带编号的失败现场" style={{ width: 320 }} />}
      </div>
      {!s && d?.fail_shot && <div className="acts">
        <input value={hint} onChange={e => setHint(e.target.value)} placeholder="这一步要做的事（可选），如：打开空调开关" aria-label="这一步要做的事" style={{ minWidth: 280 }} />
        <button className="primary" disabled={busy} onClick={() => void ask()}>生成自愈建议</button>
      </div>}
      {s && <>
        <p>建议：{s.found ? <b className="v-passed">找到对应控件</b> : <b className="v-failed">找不到对应控件</b>} · {s.reason} · 状态 {s.status}{s.by && `（${s.by}）`}</p>
        {s.diff && <pre className="mono scroll">{s.diff}</pre>}
        {s.status === '待确认' && <div className="acts">
          {s.found && <button className="primary" onClick={() => void decide(true)}>接受并写回</button>}
          <button onClick={() => void decide(false)}>拒绝</button>
          <button disabled={busy} onClick={() => void ask()}>重新生成</button>
        </div>}
      </>}
      {msg && <p className="meta" role="status">{msg}</p>}
    </section>
  )
}

function Heals() {
  const [rows, setRows] = useState<HealRow[] | null>(null)
  const [open, setOpen] = useState<HealRow | null>(null)
  useEffect(() => { fetch('/api/heal/candidates').then(r => r.json()).then(j => setRows(j.runs)).catch(() => setRows([])) }, [open])
  if (open) return <Heal row={open} back={() => setOpen(null)} />
  return (
    <section>
      <h2>定位失败的执行</h2>
      <p className="meta">回放时各级定位都找不到控件会判阻塞，台架只上传失败现场。界面改版时，可以在这里让视觉模型找出新控件。</p>
      {!rows ? <p className="meta">加载中…</p> : rows.length === 0 ? <p className="empty">没有定位失败的执行。</p> : (
        <div className="scroll"><table>
          <thead><tr><th>用例</th><th>执行</th><th>台架</th><th>失败步骤</th><th>建议</th><th>时间</th><th /></tr></thead>
          <tbody>{rows.map(r => (
            <tr key={r.run_id}>
              <td>{r.case_id}</td><td className="mono">{r.run_id}</td><td>{r.rig_id}</td>
              <td className="why">{r.step ? `第 ${r.step} 步 ${r.label}` : '—'}</td>
              <td>{r.suggestion ? `${r.suggestion.status}${r.suggestion.found ? '' : '（没找到）'}` : r.has_scene ? '未生成' : '无失败现场'}</td>
              <td>{when(r.at)}</td>
              <td><button onClick={() => setOpen(r)} disabled={!r.step}>查看</button></td>
            </tr>))}
          </tbody>
        </table></div>)}
    </section>
  )
}

const TABS = [['rec', 'UI 地图录制'], ['heal', '自愈建议']] as const

export default function Touch() {
  const [user, setUser] = useState<string | null | undefined>(undefined)
  const [tab, setTab] = useState(() => (location.hash === '#heal' ? 'heal' : 'rec'))
  useEffect(() => { fetch('/api/v1/auth/me').then(r => r.json()).then(j => setUser(j.user?.name ?? null)).catch(() => setUser(null)) }, [])
  if (user === undefined) return <main className="gate">加载中…</main>
  if (user === null) return (
    <main className="gate"><h1>触屏执行</h1><p>确认录制和自愈建议需要登录。</p>
      <a className="login" href="/api/v1/auth/login?redirect=/touch">登录</a></main>)
  const go = (t: string) => { location.hash = t; setTab(t) }
  return (
    <main>
      <header className="top">
        <h1>触屏执行</h1>
        <a className="link" href="/board">调度看板</a>
        <a className="link" href="/cases">用例工作台</a>
        <span className="me">{user}</span>
      </header>
      <nav className="tabs" aria-label="触屏执行页面">{TABS.map(([k, n]) => (
        <button key={k} aria-current={tab === k ? 'page' : undefined} onClick={() => go(k)}>{n}</button>))}
      </nav>
      {tab === 'rec' ? <Recordings /> : <Heals />}
    </main>
  )
}
