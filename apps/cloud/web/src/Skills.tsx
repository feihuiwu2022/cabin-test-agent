// 用例编写规范包：按模块查看生效规范和审核通过率，编辑规则 / 示例 / 自检，命中预览，历史与回滚
import { useCallback, useEffect, useMemo, useState } from 'react'
import './board.css'

type Review = { pass: number; edit: number; reject: number; to_req: number; total: number; pass_rate: number; reasons: [string, number][] }
type Meta = { name: string; file: string; version: number; sha: string; parts: Record<string, string> }
type Mod = { code: string; name: string; members: string[]; skill: Meta | null; review: Review | null }
type FileRow = { name: string; file: string; modules: string[]; version: number; sha: string; rules: number; problems: string[]; can_edit: boolean; why: string }
type List = { me: string; admins: string[]; open_to_all: boolean; files: FileRow[]; modules: Mod[]; max_chars: number; fields: string[] }
type Check = { field: string; mode: 'forbid' | 'require'; pattern: string; msg: string; suggest: string; scope: string }
type Form = { name: string; modules: string[]; version: number; extract: string; write: string; examples: string; checks: Check[] }
type Hist = { rev: string; short: string; author: string; at: number; message: string; sha: string; review: Review | null }
type Detail = { file: string; text: string; can_edit: boolean; why: string; history: Hist[]; review: Review | null; form: Form; parsed: Preview }
type TCase = { req_id: string; title: string; type: string; precondition: string; steps: string[]; utterance: string; expected: string; expect_screen: string; expect_tts: string; checks: { kind: string; msg: string }[] }
type Side = { skill: Meta | null; cases: TCase[]; flagged: number }
type Trial = { code: string; source: string; reqs: { req_id: string; section: string; quote: string }[]; old: Side; new: Side }
type SugCheck = Check & { why: string; problem: string; catches?: number; false_hits?: number }
type Suggest = { summary: string; extract: string[]; write: string[]; examples: string[]; checks: SugCheck[]; samples: number; rejected: number; passed_checked: number }
type Preview = { problems: string[]; rules: { field: string; msg: string; hits: number; samples: { case_id: string; title: string; value: string }[] }[]; checked_cases: number; sha: string }

const FIELD_CN: Record<string, string> = { title: '标题', precondition: '前置条件', steps: '步骤', expected: '预期', utterance: '测试语句', expect_screen: '期望屏幕', expect_tts: '期望播报', page: '页面' }
const when = (t: number) => new Date(t * 1000).toLocaleString('zh-CN', { hour12: false })
const rate = (r: Review | null) => (r && r.total ? `${Math.round(r.pass_rate * 100)}%（${r.total} 条）` : '—')

/** 表单 → Markdown（开头的 YAML 用 JSON 写法，PyYAML 能直接读）。 */
function toText(f: Form): string {
  const checks = f.checks.filter(c => c.pattern.trim() && c.msg.trim()).map(c => ({
    field: c.field, [c.mode]: c.pattern, msg: c.msg, suggest: c.suggest, scope: c.scope,
  }))
  const head = [`name: ${JSON.stringify(f.name)}`, `modules: ${JSON.stringify(f.modules)}`, `version: ${f.version}`,
    `checks: ${JSON.stringify(checks)}`].join('\n')
  const sec = (t: string, v: string) => (v.trim() ? `## ${t}\n${v.trim()}\n` : '')
  return `---\n${head}\n---\n${sec('抽取', f.extract)}${sec('编写', f.write)}${sec('示例', f.examples)}`
}

/** 逐行比对：- 只在旧版里，+ 只在新版里。 */
function lineDiff(a: string, b: string) {
  const A = a.split('\n'), B = b.split('\n'), setA = new Set(A), setB = new Set(B)
  return [...A.filter(l => !setB.has(l)).map(l => ['-', l]), ...B.filter(l => !setA.has(l)).map(l => ['+', l])]
}

async function api(url: string, method = 'GET', body?: object) {
  const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.detail ?? j.message ?? `HTTP ${r.status}`)
  return j
}

function CaseCard({ c }: { c: TCase }) {
  return (
    <div className="defect" style={{ display: 'block' }}>
      <h3>{c.title} <span className="meta">{c.type} · {c.req_id}</span></h3>
      {c.precondition && <p className="meta">前置：{c.precondition}</p>}
      {c.utterance ? <p>说：“{c.utterance}”</p> : <ol className="meta">{c.steps.map((x, i) => <li key={i}>{x}</li>)}</ol>}
      {c.expect_screen || c.expect_tts ? <p>屏幕：{c.expect_screen || '—'}；播报：{c.expect_tts || '—'}</p> : <p>预期：{c.expected}</p>}
      {c.checks.map((k, i) => <p key={i} className="v-failed">⚑ {k.kind}：{k.msg}</p>)}
    </div>)
}

function TrialTab({ file, text, modulesHint, all }: { file: string; text: string; modulesHint: string[]; all: Mod[] }) {
  const [code, setCode] = useState(modulesHint.find(m => m !== '*') ?? all[0]?.code ?? '')
  const [reqs, setReqs] = useState('')
  const [busy, setBusy] = useState(false)
  const [r, setR] = useState<Trial | null>(null)
  const [err, setErr] = useState('')
  const run = async () => {
    setBusy(true); setErr('')
    const lines = reqs.split('\n').map(x => x.trim()).filter(Boolean).map(x => {
      const m = x.match(/^§?(\d+(?:\.\d+)*)\s+(.*)$/)
      return m ? { section: m[1], quote: m[2] } : { section: '', quote: x }
    })
    try { setR(await api(`/api/skills/${encodeURIComponent(file)}/trial`, 'POST', { text, code, reqs: lines })) } catch (e) { setErr((e as Error).message) }
    setBusy(false)
  }
  return (
    <div>
      <p className="meta">同一组需求点，用当前已保存的规范和你正在改的草稿各生成一次，并排对比，不入库。会调用 2 次模型。</p>
      <div className="acts">
        <label>用哪个模块的需求点 <select value={code} onChange={e => setCode(e.target.value)}>{all.map(m => <option key={m.code} value={m.code}>{m.name}（{m.code}）</option>)}</select></label>
      </div>
      <textarea value={reqs} onChange={e => setReqs(e.target.value)} rows={3} placeholder="留空：取这个模块最近一版 PRD 的前 3 个需求点。也可以手填，每行一个，如：§3.1 说“空调调到 N 度”时设为 N 度"
        style={{ width: '100%', font: 'inherit', background: 'var(--panel2)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 6, padding: 8, marginTop: 8 }} />
      <div className="acts"><button className="primary" disabled={busy} onClick={() => void run()}>{busy ? '生成中…' : '用当前规范和草稿各生成一次'}</button></div>
      {err && <p className="err">{err}</p>}
      {r && <>
        <p className="meta">需求点（{r.source}）：{r.reqs.map(x => `§${x.section || '—'} ${x.quote}`).join('；')}</p>
        <div className="trial">
          {(['old', 'new'] as const).map(k => (
            <div key={k}>
              <h3>{k === 'old' ? '当前规范' : '草稿'} <span className="meta">{r[k].skill?.name} · {r[k].cases.length} 条，自检标出 {r[k].flagged} 条</span></h3>
              {r[k].cases.map((c, i) => <CaseCard key={i} c={c} />)}
            </div>))}
        </div>
      </>}
    </div>)
}

function SuggestTab({ file, text, apply }: { file: string; text: string; apply: (s: { extract: string[]; write: string[]; examples: string[]; checks: Check[] }) => void }) {
  const [busy, setBusy] = useState(false)
  const [r, setR] = useState<Suggest | null>(null)
  const [pick, setPick] = useState<Record<string, boolean>>({})
  const [err, setErr] = useState('')
  const run = async () => {
    setBusy(true); setErr('')
    try {
      const j: Suggest = await api(`/api/skills/${encodeURIComponent(file)}/suggest`, 'POST', { text })
      setR(j)
      const p: Record<string, boolean> = {}
      ;(['extract', 'write', 'examples'] as const).forEach(k => j[k].forEach((_, i) => { p[`${k}${i}`] = true }))
      j.checks.forEach((c, i) => { p[`c${i}`] = !c.problem && (c.false_hits ?? 0) === 0 })
      setPick(p)
    } catch (e) { setErr((e as Error).message) }
    setBusy(false)
  }
  const add = () => {
    if (!r) return
    const take = (k: 'extract' | 'write' | 'examples') => r[k].filter((_, i) => pick[`${k}${i}`])
    apply({ extract: take('extract'), write: take('write'), examples: take('examples'),
      checks: r.checks.filter((c, i) => pick[`c${i}`] && !c.problem).map(c => ({ field: c.field, mode: c.mode, pattern: c.pattern, msg: c.msg, suggest: c.suggest, scope: c.scope })) })
  }
  const box = (key: string, label: React.ReactNode, disabled = false) => (
    <label className="check" key={key} style={{ alignItems: 'flex-start', marginBottom: 4 }}>
      <input type="checkbox" checked={!!pick[key]} disabled={disabled} onChange={e => setPick({ ...pick, [key]: e.target.checked })} /> <span>{label}</span>
    </label>)
  return (
    <div>
      <p className="meta">读这个模块最近被驳回或修改的用例（评估集），让模型提出规范补充。只是建议：勾选后加入草稿，检查无误再保存。会调用 1 次模型。</p>
      <div className="acts"><button className="primary" disabled={busy} onClick={() => void run()}>{busy ? '分析中…' : '根据驳回 / 修改记录生成建议'}</button></div>
      {err && <p className="err">{err}</p>}
      {r && <>
        <p><b>{r.summary}</b> <span className="meta">（参考 {r.samples} 条记录，其中驳回 {r.rejected} 条）</span></p>
        {(['extract', 'write', 'examples'] as const).map(k => r[k].length > 0 && (
          <div key={k}><h3>{{ extract: '补充到“抽取”', write: '补充到“编写”', examples: '补充的示例' }[k]}</h3>
            {r[k].map((x, i) => box(`${k}${i}`, x))}</div>))}
        {r.checks.length > 0 && <><h3>自检规则</h3>
          {r.checks.map((c, i) => box(`c${i}`, <>
            {FIELD_CN[c.field] ?? c.field} {c.mode === 'forbid' ? '禁止' : '必须'} <span className="mono">{c.pattern}</span> → “{c.msg}”
            {c.problem ? <span className="v-failed">（不能用：{c.problem}）</span>
              : <span className="meta">（能拦住被驳回的 {c.catches} / {r.rejected} 条；误伤最近已通过的 {c.false_hits} / {r.passed_checked} 条）</span>}
            {c.why && <span className="meta"> · {c.why}</span>}</>, !!c.problem))}</>}
        <div className="acts"><button className="primary" onClick={add}>把勾选的加入草稿</button></div>
      </>}
    </div>)
}

function Editor({ list, file, newFor, reload }: { list: List; file: string; newFor?: string; reload: () => void }) {
  const [d, setD] = useState<Detail | null>(null)
  const [f, setF] = useState<Form | null>(null)
  const [raw, setRaw] = useState<string | null>(null)
  const [tab, setTab] = useState('rules')
  const [msg, setMsg] = useState('')
  const [note, setNote] = useState('')
  const [pv, setPv] = useState<Preview | null>(null)
  const [diff, setDiff] = useState<{ rev: string; lines: string[][] } | null>(null)
  const load = useCallback(() => {
    setPv(null); setDiff(null); setRaw(null); setNote('')
    if (newFor) {
      const mod = list.modules.find(m => m.code === newFor)
      setD(null)
      setF({ name: mod?.name ?? newFor, modules: [newFor], version: 1, extract: '', write: '', examples: '', checks: [] })
      return
    }
    api(`/api/skills/${encodeURIComponent(file)}`).then((j: Detail) => { setD(j); setF(j.form) }).catch(e => setMsg(String(e.message)))
  }, [file, newFor, list.modules])
  useEffect(load, [load])
  useEffect(() => { document.getElementById('skill-editor')?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }, [file, newFor])
  const text = useMemo(() => (raw ?? (f ? toText(f) : '')), [raw, f])
  if (!f) return <p className="meta">{msg || '加载中…'}</p>
  const canEdit = newFor ? true : d?.can_edit
  const set = (k: keyof Form, v: unknown) => setF({ ...f, [k]: v })
  const setCheck = (i: number, k: keyof Check, v: string) => set('checks', f.checks.map((c, j) => (j === i ? { ...c, [k]: v } : c)))
  const check = async () => {
    try { setPv(await api(`/api/skills/${encodeURIComponent(file)}/preview`, 'POST', { text })); setMsg('') } catch (e) { setMsg((e as Error).message) }
  }
  const save = async () => {
    const body = { text: raw ?? toText({ ...f, version: newFor ? 1 : (d?.form.version ?? 0) + 1 }), base_sha: newFor ? '' : d?.parsed.sha ?? '', message: note }
    try {
      const r = await api(`/api/skills/${encodeURIComponent(file)}`, 'PUT', body)
      setMsg(`已保存 v${r.version}，下一批生成用例就按新规范`)
      reload(); load()
    } catch (e) { setMsg((e as Error).message) }
  }
  const showRev = async (h: Hist) => {
    const j = await api(`/api/skills/${encodeURIComponent(file)}/rev/${h.rev}`)
    setDiff({ rev: h.short, lines: lineDiff(j.text, d?.text ?? '') })
  }
  const rollback = async (h: Hist) => {
    try { await api(`/api/skills/${encodeURIComponent(file)}/rollback`, 'POST', { rev: h.rev, message: note || `回滚到 ${h.short}` }); setMsg(`已回滚到 ${h.short}`); reload(); load() } catch (e) { setMsg((e as Error).message) }
  }
  const counter = (v: string) => <span className={v.length > list.max_chars ? 'v-failed' : 'meta'}>{v.length} / {list.max_chars} 字</span>
  return (
    <section id="skill-editor">
      <h2>{newFor ? `新建规范包 · ${f.name}` : `${f.name}（${file}）`} {d?.review && <span className="meta">当前版本通过率 {rate(d.review)}</span>}</h2>
      {!canEdit && <p className="err">你只能查看：{d?.why}</p>}
      <nav className="tabs" aria-label="规范包编辑">{[['rules', '规则'], ['checks', '自检'], ['trial', '试生成'], ['suggest', '建议'], ['history', '历史'], ['raw', '原文']].map(([k, n]) => (
        <button key={k} aria-current={tab === k ? 'page' : undefined} onClick={() => setTab(k)}>{n}</button>))}</nav>
      {tab === 'rules' && <div className="form" style={{ maxWidth: 900 }}>
        <label>名称 <input value={f.name} onChange={e => set('name', e.target.value)} disabled={!canEdit} /></label>
        <p className="meta">适用模块：{f.modules.includes('*') ? '所有模块（默认包，模块包叠加在它后面）' : f.modules.join('、')}</p>
        {([['extract', '抽取', '追加到需求抽取的提示词'], ['write', '编写', '追加到用例生成的提示词'], ['examples', '示例', '正反示例：照着写法，不照抄内容']] as const).map(([k, n, hint]) => (
          <div key={k}>
            <p><b>{n}</b> <span className="meta">{hint}</span> {counter(f[k])}</p>
            <textarea value={f[k]} onChange={e => set(k, e.target.value)} disabled={!canEdit || raw !== null} rows={k === 'examples' ? 8 : 6}
              style={{ width: '100%', font: 'inherit', background: 'var(--panel2)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 6, padding: 8 }} />
          </div>))}
      </div>}
      {tab === 'checks' && <div>
        <p className="meta">每条规则检查用例的一个字段：“禁止”命中就提示，“必须”没命中就提示。不调用模型。点“检查并预览”看在最近已入库用例上会标出哪些——标出的是好用例，说明规则写宽了。</p>
        <div className="scroll"><table>
          <thead><tr><th>字段</th><th>方式</th><th>正则</th><th>提示语</th><th>建议</th><th>范围</th><th>命中</th><th /></tr></thead>
          <tbody>{f.checks.map((c, i) => {
            const hit = pv?.rules.find(r => r.msg === c.msg && r.field === c.field)
            return (
              <tr key={i}>
                <td><select value={c.field} onChange={e => setCheck(i, 'field', e.target.value)} disabled={!canEdit}>{list.fields.map(x => <option key={x} value={x}>{FIELD_CN[x] ?? x}</option>)}</select></td>
                <td><select value={c.mode} onChange={e => setCheck(i, 'mode', e.target.value)} disabled={!canEdit}><option value="forbid">禁止</option><option value="require">必须</option></select></td>
                <td><input className="mono" value={c.pattern} onChange={e => setCheck(i, 'pattern', e.target.value)} disabled={!canEdit} /></td>
                <td><input value={c.msg} onChange={e => setCheck(i, 'msg', e.target.value)} disabled={!canEdit} /></td>
                <td><select value={c.suggest} onChange={e => setCheck(i, 'suggest', e.target.value)} disabled={!canEdit}>{['修改', '驳回', '确认', '转需求方'].map(x => <option key={x}>{x}</option>)}</select></td>
                <td><select value={c.scope} onChange={e => setCheck(i, 'scope', e.target.value)} disabled={!canEdit}><option value="all">全部</option><option value="voice">语音</option><option value="ui">非语音</option></select></td>
                <td className="why">{hit ? `${hit.hits} 条${hit.samples.length ? '：' + hit.samples.map(s => `${s.case_id}「${s.value}」`).join('；') : ''}` : '—'}</td>
                <td><button onClick={() => set('checks', f.checks.filter((_, j) => j !== i))} disabled={!canEdit}>删除</button></td>
              </tr>)
          })}</tbody>
        </table></div>
        <div className="acts">
          <button onClick={() => set('checks', [...f.checks, { field: 'expected', mode: 'forbid', pattern: '', msg: '', suggest: '修改', scope: 'all' }])} disabled={!canEdit}>加一条</button>
        </div>
      </div>}
      {tab === 'trial' && <TrialTab file={file} text={text} modulesHint={f.modules} all={list.modules} />}
      {tab === 'suggest' && <SuggestTab file={file} text={text} apply={sg => {
        const join = (a: string, b: string[]) => [a.trim(), ...b.map(x => (x.startsWith('-') || x.startsWith('{') ? x : `- ${x}`))].filter(Boolean).join('\n')
        setRaw(null)
        setF({ ...f, extract: join(f.extract, sg.extract), write: join(f.write, sg.write), examples: join(f.examples, sg.examples), checks: [...f.checks, ...sg.checks] })
        setTab('rules')
        setMsg('建议已加入草稿：检查“规则”和“自检”，写好改动说明后保存')
      }} />}
      {tab === 'history' && <div>
        {!d?.history.length ? <p className="empty">还没有历史（新建的规范包保存后才有）。</p> : (
          <div className="scroll"><table>
            <thead><tr><th>版本</th><th>作者</th><th>时间</th><th>说明</th><th>该版通过率</th><th /></tr></thead>
            <tbody>{d.history.map((h, i) => (
              <tr key={h.rev}>
                <td className="mono">{h.short}{i === 0 && ' · 当前'}</td><td>{h.author}</td><td>{when(h.at)}</td><td className="why">{h.message}</td>
                <td>{rate(h.review)}{h.review?.reasons.length ? <div className="meta">驳回最多：{h.review.reasons[0][0]}</div> : null}</td>
                <td>{i > 0 && <><button onClick={() => void showRev(h)}>和当前比</button> <button onClick={() => void rollback(h)} disabled={!canEdit}>回滚到这版</button></>}</td>
              </tr>))}</tbody>
          </table></div>)}
        {diff && <><h3>{diff.rev} → 当前</h3><pre className="mono scroll">{diff.lines.length ? diff.lines.map(([k, l]) => `${k} ${l}`).join('\n') : '没有差别'}</pre></>}
      </div>}
      {tab === 'raw' && <div>
        <p className="meta">直接编辑原文（Markdown + 开头的 YAML）。改了原文后，“规则 / 自检”里的表单不再生效，以原文为准；点“放弃原文修改”回到表单。</p>
        <textarea className="mono" value={text} onChange={e => setRaw(e.target.value)} rows={24} disabled={!canEdit}
          style={{ width: '100%', background: 'var(--panel2)', color: 'var(--ink)', border: '1px solid var(--line)', borderRadius: 6, padding: 8 }} />
        {raw !== null && <p><button onClick={() => setRaw(null)}>放弃原文修改</button></p>}
      </div>}
      {pv && <div className="defect" style={{ marginTop: 12, display: 'block' }}>
        <b>{pv.problems.length ? '有问题，保存不了：' : '检查通过'}</b>{pv.problems.map(p => <p key={p} className="err">{p}</p>)}
        <p className="meta">在最近 {pv.checked_cases} 条已入库用例上：{pv.rules.map(r => `“${r.msg}” 命中 ${r.hits} 条`).join('；') || '没有自检规则'}</p>
      </div>}
      {canEdit && <div className="acts" style={{ marginTop: 12 }}>
        <input value={note} onChange={e => setNote(e.target.value)} placeholder="改动说明（必填，会记在历史里）" aria-label="改动说明" style={{ minWidth: 320, flex: 1 }} />
        <button onClick={() => void check()}>检查并预览</button>
        <button className="primary" disabled={!note.trim()} onClick={() => void save()}>保存</button>
      </div>}
      {msg && <p className="meta" role="status">{msg}</p>}
    </section>
  )
}

export default function Skills() {
  const [user, setUser] = useState<string | null | undefined>(undefined)
  const [list, setList] = useState<List | null>(null)
  const [sel, setSel] = useState<{ file: string; newFor?: string } | null>(null)
  const [err, setErr] = useState('')
  const reload = useCallback(() => { api('/api/skills').then(setList).catch(e => setErr(e.message)) }, [])
  useEffect(() => { fetch('/api/v1/auth/me').then(r => r.json()).then(j => setUser(j.user?.name ?? null)).catch(() => setUser(null)) }, [])
  useEffect(() => { if (user) reload() }, [user, reload])
  if (user === undefined) return <main className="gate">加载中…</main>
  if (user === null) return (
    <main className="gate"><h1>用例编写规范</h1><p>查看和编辑规范包需要登录。</p>
      <a className="login" href="/api/v1/auth/login?redirect=/skills">登录</a></main>)
  const own = (code: string) => list?.files.find(f => f.modules.includes(code) && !f.modules.includes('*'))
  const base = list?.files.find(f => f.modules.includes('*'))
  return (
    <main>
      <header className="top">
        <h1>用例编写规范</h1>
        <a className="link" href="/cases">用例工作台</a>
        <a className="link" href="/board">调度看板</a>
        <span className="me">{user}</span>
      </header>
      {err && <p className="err">{err}</p>}
      {list?.open_to_all && <p className="meta">还没配置管理员（CABIN_ADMINS），所有登录用户都能改规范包。</p>}
      {list && <section>
        <h2>各模块生效的规范</h2>
        <p className="meta">默认包对所有模块生效，模块包叠加在后面。通过率按审核结论统计，改规范前后对比看效果。</p>
        <div className="scroll"><table>
          <thead><tr><th>模块</th><th>生效的规范</th><th>通过率</th><th>驳回最多</th><th /></tr></thead>
          <tbody>
            {base && <tr><td><b>默认</b></td><td>{base.name} v{base.version}</td><td>—</td><td>—</td>
              <td><button onClick={() => setSel({ file: base.file })}>{base.can_edit ? '编辑' : '查看'}</button></td></tr>}
            {list.modules.map(m => {
              const f = own(m.code)
              return (
                <tr key={m.code}>
                  <td>{m.name} <span className="mono meta">{m.code}</span></td>
                  <td>{m.skill ? `${m.skill.name} v${m.skill.version}` : '—'}{f?.problems.length ? <span className="v-failed"> · 有问题</span> : null}</td>
                  <td>{rate(m.review)}</td>
                  <td className="why">{m.review?.reasons[0]?.[0] ?? '—'}</td>
                  <td>{f ? <button onClick={() => setSel({ file: f.file })}>{f.can_edit ? '编辑' : '查看'}</button>
                    : <button onClick={() => setSel({ file: `${m.name}.md`, newFor: m.code })}>新建模块规范</button>}</td>
                </tr>)
            })}
          </tbody>
        </table></div>
      </section>}
      {list && sel && <Editor key={sel.file + (sel.newFor ?? '')} list={list} file={sel.file} newFor={sel.newFor} reload={reload} />}
    </main>
  )
}
