// 用例闭环（一期 A）：② 用例工作台（文档 → 追溯 → 审核 → 测试代码）与 ③ 结果卡片
import { useMemo, useState } from 'react'
import { conn } from './conn'
import type { Bench, FeishuRes, State, TCase } from './state'

type Dispatch = (m: { type: string; [k: string]: unknown }) => void
type Decision = { decision: 'pass' | 'reject' | 'to_req'; reason?: string; edits?: Partial<TCase> }

const BENCH: [Bench, string][] = [['doc', '文档'], ['trace', '需求 → 用例'], ['review', '用例审核'], ['code', '测试代码']]
const PILL: Record<string, [string, string]> = { pass: ['ok', '通过'], edit: ['edit', '修改后通过'], reject: ['no', '驳回'], to_req: ['edit', '转需求方'] }
const STATUS_PILL: Record<string, string> = { 通过: 'ok', 修改后通过: 'edit', 驳回: 'no', 转需求方: 'edit', 待审核: '' }
const PLAN_TXT: Record<string, string> = { ready: '就绪', generating: '生成中', invalid: '校验未通过', unsuitable: '不适合自动化', '': '未生成' }

export type Attachment = { id: string; name: string; size: number }
export type Send = (t: string, via?: string, attachment?: Attachment) => void
export const PRD_ACCEPT = '.docx,.pdf,.md,.markdown,.txt,.html,.xlsx'
const RE_REF = /^(https?:\/\/|[A-Za-z]:[\\/]|\/|\.{1,2}[\\/])/

/** 上传 PRD 到后端暂存，返回附件引用（随对话消息发出后开始生成）。 */
export async function uploadPrd(f: File, session: string): Promise<Attachment> {
  const fd = new FormData()
  fd.append('file', f, f.name)
  const r = await fetch(`/api/prd/upload?session=${session}`, { method: 'POST', body: fd })
  const j = await r.json()
  if (!r.ok) throw new Error(j.detail || r.statusText)
  return j
}

export function CaseBench({ s, dispatch, send }: { s: State; dispatch: Dispatch; send: Send }) {
  const cf = s.cf
  const bench = cf.bench
  const [importing, setImporting] = useState(false)
  const hasPlans = Object.keys(cf.plans).length > 0
  const showImport = (!cf.doc && !hasPlans) || importing
  return (
    <div className="dut-body bench">
      <div className="tabs" role="tablist">
        {BENCH.map(([k, n]) => {
          const dis = (k === 'trace' && !cf.reqs.length) || (k === 'review' && !cf.review) || (k === 'code' && !Object.keys(cf.plans).length)
          return <button key={k} role="tab" aria-selected={!showImport && bench === k} disabled={(!cf.doc && k !== 'code') || (dis && bench !== k)} onClick={() => { setImporting(false); dispatch({ type: '_bench', bench: k }) }}>{n}</button>
        })}
        <button role="tab" aria-selected={showImport} className="tab-new" onClick={() => setImporting(true)}>＋ 导入 PRD</button>
      </div>
      {showImport ? <ImportPanel s={s} send={(t, v, a) => { send(t, v, a); setImporting(false) }} />
        : bench === 'doc' ? <DocView s={s} /> : bench === 'trace' ? <TraceView s={s} /> : bench === 'review' ? <ReviewView key={cf.batch?.batch_id} s={s} /> : <CodeView s={s} />}
    </div>
  )
}

/* ---------- 主入口：本地文件 / 在线文档 → 生成用例 ---------- */
function ImportPanel({ s, send }: { s: State; send: Send }) {
  const [att, setAtt] = useState<Attachment | null>(null)
  const [url, setUrl] = useState('')
  const [section, setSection] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [over, setOver] = useState(false)
  const pick = async (f: File | undefined) => {
    if (!f) return
    setBusy(true); setErr(''); setUrl('')
    try { setAtt(await uploadPrd(f, s.session)) } catch (e) { setErr(`上传失败：${(e as Error).message}`) } finally { setBusy(false) }
  }
  const sec = section.trim().replace(/^§/, '')
  const scope = sec ? `的 §${sec} ` : ''
  const go = () => {
    const u = url.trim()
    if (att) send(att.name.toLowerCase().endsWith('.xlsx') ? `导入用例表：${att.name}` : `把这份 PRD ${scope}转成测试用例：${att.name}`, 'text', att)
    else if (RE_REF.test(u)) send(`把 ${u} ${scope}转成测试用例`)
    else { setErr('请上传文件，或填写以 http(s):// 开头的在线文档链接 / 本机文件路径'); return }
    setAtt(null); setUrl(''); setSection('')
  }
  const generating = s.cf.extracting || s.cf.batch?.status === '生成中'
  return (
    <div className="imp">
      <label className={`drop ${over ? 'over' : ''}`} onDragOver={e => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
        onDrop={e => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]) }}>
        <input type="file" accept={PRD_ACCEPT} hidden onChange={e => { pick(e.target.files?.[0]); e.target.value = '' }} />
        <b>{busy ? '上传中…' : att ? `📄 ${att.name}` : '点击选择或拖入 PRD 文件'}</b>
        <span className="k">{att ? `${(att.size / 1024).toFixed(0)} KB · 已就绪，点“生成测试用例”` : '支持 PRD（Word、PDF、Markdown、文本），也支持用例表（.xlsx）'}</span>
      </label>
      <div className="or">或</div>
      <input className="url" value={url} onChange={e => { setUrl(e.target.value); setAtt(null) }} onKeyDown={e => { if (e.key === 'Enter') go() }}
        placeholder="粘贴飞书多维表格、飞书云文档、Word 直链或网页，或本机文件路径" />
      <div className="row">
        <input className="sec" value={section} onChange={e => setSection(e.target.value)} placeholder="章节（可选），如 4.1" />
        <button className="btn primary" disabled={busy || generating || (!att && !url.trim())} onClick={go}>{generating ? '正在生成…' : '生成测试用例'}</button>
        <button className="btn" disabled={generating} onClick={() => send('导入示例 PRD 生成用例')}>用示例 PRD 试一下</button>
        <button className="btn" disabled={generating} onClick={() => send('导入语音用例模拟表')}>导入语音用例模拟表</button>
      </div>
      {err && <div style={{ color: 'var(--accent)', fontSize: 12.5 }}>{err}</div>}
      <div className="k" style={{ fontSize: 12 }}>也可以在左侧对话框点 📎 PRD 上传，或直接输入“把 D:\docs\xx.docx 的 §4.1 转成测试用例”。生成后依次进入：需求提取 → 用例生成与自检 → 人工审核 → 入库 → 测试代码（审核通过后自动生成）→ 首次试跑（自动派台架）。飞书云文档需配置飞书应用，并把应用加为文档协作者。</div>
    </div>
  )
}

/* ---------- 文档：标出需求点与待确认项的出处 ---------- */
function DocView({ s }: { s: State }) {
  const { doc, reqs, ambs, extracting } = s.cf
  const hit = useMemo(() => {
    const m = new Map<string, string>()
    reqs.forEach(r => m.set(r.section, `${m.get(r.section) ? m.get(r.section) + ' ' : ''}${r.req_id}`))
    return m
  }, [reqs])
  const warn = useMemo(() => new Set(ambs.map(a => a.section)), [ambs])
  if (!doc) return null
  return (
    <div className="paper">
      <div className="k" style={{ marginBottom: 6 }}>《{doc.name}》{doc.section ? ` · §${doc.section}` : ''} · {doc.source.split(':')[0]} · {doc.n_blocks} 段{extracting && <span className="aiq"> · 正在提取需求点…</span>}</div>
      {extracting && <div className="scan" />}
      {doc.blocks.map((b, i) => {
        const isHit = b.kind !== 'heading' && hit.has(b.section)
        const isWarn = b.kind !== 'heading' && warn.has(b.section)
        if (b.kind === 'heading') return <div key={i} className={`h h${Math.min(b.level, 4)}`}>{b.text}</div>
        return (
          <div key={i} className={`para ${isWarn ? 'warn' : isHit ? 'hit' : ''}`}>
            {b.kind === 'table' ? <pre>{b.text}</pre> : b.kind === 'image' ? <span className="k">[图片] {b.text}</span> : b.text}
            {isHit && <span className="tagr mono">{hit.get(b.section)}</span>}
            {isWarn && <span className="tagr warn">待确认</span>}
          </div>)
      })}
    </div>
  )
}

/* ---------- 追溯：需求点 → 用例 ---------- */
function TraceView({ s }: { s: State }) {
  const { reqs, cases, ambs } = s.cf
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="tt">
        <thead><tr><th>需求</th><th>用例</th><th>类型</th><th>通道</th><th>{cases.some(c => c.utterance) ? '测试语句 · 预期' : '预期结果'}</th></tr></thead>
        <tbody>
          {reqs.map(r => {
            const cs = cases.filter(c => c.req_id === r.req_id)
            if (!cs.length) return <tr key={r.req_id}><td className="req">{r.req_id}<div className="k">§{r.section} {r.summary}</div></td><td colSpan={4} className="k">生成中…</td></tr>
            return cs.map((c, i) => (
              <tr key={c.case_id} className="new">
                {i === 0 && <td className="req" rowSpan={cs.length}>{r.req_id}<div className="k">§{r.section} {r.summary}</div></td>}
                <td><span className="mono">{c.case_id}</span> {c.title}{c.suggestion && <span className="aiq"> ● {c.suggestion}</span>}</td>
                <td><span className="ty">{c.type}</span></td>
                <td className="mono k">{c.channel.join('+') || '—'}</td>
                <td>{c.utterance ? <>“{c.utterance}” → {c.expect_screen}；播报“{c.expect_tts}”</> : c.expected}</td>
              </tr>))
          })}
          {ambs.map(a => <tr key={a.id} className="warnrow"><td className="req">{a.id}<div className="k">§{a.section}</div></td><td colSpan={4}>⚠ 待确认：{a.question}<span className="k">（不生成用例，转需求方）</span></td></tr>)}
        </tbody>
      </table>
    </div>
  )
}

/* ---------- 审核：写入用例库前的人工门 ---------- */
function ReviewView({ s }: { s: State }) {
  const { cases, review, batch, reqs } = s.cf
  const open = !!review?.open
  const reasons = review?.reasons ?? []
  const [dec, setDec] = useState<Record<string, Decision>>({})
  const [sel, setSel0] = useState(() => (cases.find(x => x.suggestion) ?? cases[0])?.case_id ?? '')
  const [draft, setDraft] = useState<Partial<TCase>>({})
  const [reason, setReason] = useState(reasons[0] ?? '')
  const setSel = (id: string) => { setSel0(id); setDraft({}); setReason(reasons[0] ?? '') }
  const c = cases.find(x => x.case_id === sel) ?? cases[0]
  if (!c) return <span className="empty">没有待审核的用例。</span>
  const done = Object.keys(dec).length
  const decide = (d: Decision) => {
    const nd = { ...dec, [c.case_id]: d }
    setDec(nd)
    const next = cases.find(x => !nd[x.case_id])
    if (next) setSel(next.case_id)
  }
  const edited = Object.keys(draft).length > 0
  const val = <K extends keyof TCase>(k: K) => (draft[k] ?? c[k]) as TCase[K]
  const set = (k: keyof TCase, v: unknown) => setDraft(d => ({ ...d, [k]: v }))
  const pill = (x: TCase) => {
    if (!open) return STATUS_PILL[x.review_status] !== undefined ? [STATUS_PILL[x.review_status], x.review_status] : ['', x.review_status]
    const d = dec[x.case_id]
    if (!d) return ['', '待审核']
    return d.decision === 'pass' && d.edits && Object.keys(d.edits).length ? PILL.edit : PILL[d.decision]
  }
  const adopt = () => {
    if (c.suggestion === '驳回') decide({ decision: 'reject', reason: c.checks.some(k => k.kind === '重复') ? '与已有用例重复' : '预期不可判定' })
  }
  const submit = () => conn.send({
    type: 'case.review.submit', batch_id: batch?.batch_id,
    decisions: cases.map(x => ({ case_id: x.case_id, decision: dec[x.case_id]?.decision ?? 'pass', reason: dec[x.case_id]?.reason ?? '', edits: dec[x.case_id]?.edits ?? {} })),
  })
  const restPass = () => { const nd = { ...dec }; cases.forEach(x => { if (!nd[x.case_id] && !x.suggestion) nd[x.case_id] = { decision: 'pass' } }); setDec(nd); const n = cases.find(x => !nd[x.case_id]); if (n) setSel(n.case_id) }
  const req = reqs.find(r => r.req_id === c.req_id)
  const d = dec[c.case_id]
  const ro = !open || !!d
  return (
    <div className="erv">
      <div className="erv-head">
        <span>批次 <span className="mono">{batch?.batch_id}</span> · {batch?.module} · {cases.length} 条</span>
        <span className="mono k">{open ? `已审 ${done} / ${cases.length}` : '已提交'}</span>
        <span style={{ flex: 1 }} />
        {s.cf.bitable?.ok && <a className="btn" href={s.cf.bitable.url} target="_blank" rel="noreferrer" title="审核状态列改完后点“从多维表格同步”">在飞书多维表格中审核 ↗</a>}
        {open && <button className="btn" onClick={restPass}>无意见的全部通过</button>}
        {open && <button className="btn primary" disabled={done < cases.length} onClick={submit} title={done < cases.length ? '还有用例未审核' : ''}>提交审核结果</button>}
      </div>
      <div className="erv-body">
        <div className="erv-list">
          {cases.map(x => {
            const [cls, txt] = pill(x)
            return (
              <button key={x.case_id} className={`erv-it ${x.case_id === c.case_id ? 'sel' : ''}`} onClick={() => setSel(x.case_id)}>
                <div><span className="mono">{x.case_id}</span> {x.title}</div>
                <div className="m"><span className={`pill ${cls}`}>{txt}</span><span>{x.req_id}</span><span>{x.type}</span>{x.channel.includes('audio') && <span>语音</span>}{x.suggestion && open && !dec[x.case_id] && <span className="aiq">● AI 建议{x.suggestion}</span>}</div>
              </button>)
          })}
        </div>
        <div className="erv-d">
          {c.checks.length > 0 && (
            <div className="erv-ai">
              <div>{c.checks.map((k, i) => <div key={i}>● {k.kind}：{k.msg} → 建议{k.suggest}</div>)}</div>
              {open && !d && c.suggestion === '驳回' && <button className="btn" onClick={adopt}>采纳建议</button>}
            </div>)}
          <h5><span className="mono">{c.case_id}</span> {ro ? c.title : <input value={val('title')} onChange={e => set('title', e.target.value)} />}</h5>
          <div className="erv-f">
            <Field k="前置条件" v={val('precondition')} ro={ro} onChange={v => set('precondition', v)} changed={'precondition' in draft} />
            {c.channel.includes('audio') && <>
              <Field k="测试语句" v={val('utterance')} ro={ro} onChange={v => set('utterance', v)} changed={'utterance' in draft} />
              <Field k="期望屏幕" v={val('expect_screen')} ro={ro} onChange={v => set('expect_screen', v)} changed={'expect_screen' in draft} />
              <Field k="期望播报" v={val('expect_tts')} ro={ro} onChange={v => set('expect_tts', v)} changed={'expect_tts' in draft} />
              <span className="k">时延 · 音色</span><span>≤ {c.latency_max_ms ?? 1500} ms · {c.voice_profile || 'female_a'}{c.expect_intent ? ` · 意图 ${c.expect_intent}` : ''}</span>
            </>}
            <Field k="测试步骤" v={(val('steps') as string[]).join('\n')} ro={ro} multi onChange={v => set('steps', v.split('\n').map(x => x.trim()).filter(Boolean))} changed={'steps' in draft} />
            <Field k="预期结果" v={val('expected')} ro={ro} multi onChange={v => set('expected', v)} changed={'expected' in draft} />
            <span className="k">类型 · 通道</span><span>{c.type} · {c.priority} · {c.channel.join(' + ') || '未判断'} · 车型 {c.models.join('/') || '通用'}</span>
          </div>
          {req && <div className="erv-src">PRD §{req.section}：{req.quote}</div>}
          {open && (
            <div className="erv-acts">
              {!d ? <>
                <button className="btn primary" onClick={() => decide({ decision: 'pass', edits: edited ? draft : undefined })}>{edited ? '修改后通过' : '通过'}</button>
                <select value={reason} onChange={e => setReason(e.target.value)} title="驳回原因（写入评估集）">{reasons.map(r => <option key={r}>{r}</option>)}</select>
                <button className="btn" onClick={() => decide({ decision: 'reject', reason })}>驳回</button>
                <button className="btn" onClick={() => decide({ decision: 'to_req', reason: '转需求方确认' })}>转需求方</button>
                {edited && <button className="btn" onClick={() => setDraft({})}>撤销修改</button>}
              </> : <>
                <span className="k">{PILL[d.decision][1]}{d.reason ? `（${d.reason}）` : ''}{d.edits && Object.keys(d.edits).length ? ` · 改了 ${Object.keys(d.edits).length} 个字段` : ''}</span>
                <button className="btn" onClick={() => { const nd = { ...dec }; delete nd[c.case_id]; setDec(nd) }}>撤回</button>
              </>}
            </div>)}
        </div>
      </div>
    </div>
  )
}

function Field({ k, v, ro, multi, changed, onChange }: { k: string; v: string; ro: boolean; multi?: boolean; changed?: boolean; onChange: (v: string) => void }) {
  return <>
    <span className="k">{k}</span>
    {ro ? <span style={{ whiteSpace: 'pre-wrap' }}>{v || <span className="k">—</span>}</span>
      : multi ? <textarea className={changed ? 'chg' : ''} rows={Math.min(5, Math.max(2, v.split('\n').length))} value={v} onChange={e => onChange(e.target.value)} />
        : <input className={changed ? 'chg' : ''} value={v} onChange={e => onChange(e.target.value)} />}
  </>
}

/* ---------- 测试代码：TaskPlan + 静态校验 ---------- */
function CodeView({ s }: { s: State }) {
  const plans = Object.values(s.cf.plans)
  const [sel, setSel] = useState('')
  const p = plans.find(x => x.case_id === sel) ?? plans[0]
  if (!p) return <span className="empty">用例审核通过后自动生成测试代码，结果显示在这里。</span>
  return (
    <div className="erv">
      <div className="erv-head"><span>测试代码 · cabin/taskplan/v1 · 存 git（data/plans）</span><span className="mono k">{plans.filter(x => x.ok).length} / {plans.length} 通过静态校验</span></div>
      <div className="erv-body">
        <div className="erv-list">
          {plans.map(x => (
            <button key={x.case_id} className={`erv-it ${x.case_id === p.case_id ? 'sel' : ''}`} onClick={() => setSel(x.case_id)}>
              <div><span className="mono">{x.case_id}</span> {x.title}</div>
              <div className="m"><span className={`pill ${x.ok ? 'ok' : 'no'}`}>{x.ok ? '校验通过' : '校验未通过'}</span>{x.auto_status === '已验证' && <span className="pill ok">已验证</span>}<span className="mono">{x.plan_version}</span></div>
            </button>))}
        </div>
        <div className="erv-d">
          <h5><span className="mono">{p.case_id}</span> {p.title}</h5>
          <div className="k">生成方式：{p.generated_by === 'voice-template' ? '语音时序模板（不调用模型）' : p.generated_by === 'model' ? '模型翻译步骤 → 动作库' : p.generated_by === 'rule-fallback' ? '规则兜底' : p.generated_by} · {p.n_steps} 步 · {p.n_asserts} 个断言 · 能力 {p.requires?.join(' + ')} · <span className="mono">{p.plan_path}@{p.plan_version}</span></div>
          {p.errors.length > 0 && <div className="vlist bad">{p.errors.map((e, i) => <div key={i}>✕ {e}</div>)}</div>}
          {p.warnings.length > 0 && <div className="vlist">{p.warnings.map((e, i) => <div key={i}>⚠ {e}</div>)}</div>}
          {p.ok && <div className="vlist okl">✓ 格式、动作库、通道一致性、断言判定方式校验通过</div>}
          <pre className="code">{p.yaml}</pre>
          <div className="erv-acts">
            <a className="btn" href={`/api/v1/plans/${p.case_id}`} target="_blank" rel="noreferrer">下载 YAML ↗</a>
            {p.ok && p.auto_status !== '已验证' && <button className="btn primary" disabled={p.auto_status === '试跑中'} onClick={() => conn.send({ type: 'plan.trial', case_id: p.case_id })}>{p.auto_status === '试跑中' ? `试跑中${p.trial?.rig ? `（台架 ${p.trial.rig}）` : ''}` : p.auto_status === '试跑未通过' ? '重新试跑' : '首次试跑'}</button>}
            {p.auto_status === '已验证' && <span className="k">✓ 首次试跑通过</span>}
            {p.auto_status === '试跑未通过' && <span className="k" style={{ color: 'var(--accent)' }}>试跑未通过：{p.trial?.reason}</span>}
            {p.trial?.env && <span className="k">上次试跑没跑完：{p.trial.reason}</span>}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ---------- ③ 结果卡片 ---------- */
export function CaseCards({ s, feishu }: { s: State; feishu: FeishuRes | null }) {
  const { doc, batch, reqs, ambs, coverage, lib, pull } = s.cf
  if (!doc && !pull && !s.bitableSync) return <div className="card"><span className="empty">需求点、用例覆盖、入库结果与测试代码 / 试跑状态会依次显示在这里。</span></div>
  const max = Math.max(3, ...Object.values(coverage?.by_req ?? {}))
  return <>
    {doc && <div className="card">
      <h4>📄 PRD <span className="k" style={{ fontWeight: 400 }}>· {doc.source.split(':')[0]}</span></h4>
      <div>《{doc.name}》{doc.section && ` §${doc.section}`}</div>
      {batch && <div className="k">批次 <span className="mono">{batch.batch_id}</span> · {batch.status}{batch.models?.length ? ` · 车型 ${batch.models.join('/')}` : ''}</div>}
      {batch?.prd_version && <div className="k">版本 <span className="mono">{batch.prd_id}@{batch.prd_version}</span>{batch.version_source === '测试预填' ? '（测试预填，待产品部补正）' : ''}
        {batch.prev_version && batch.diff ? ` · 对比 ${batch.prev_version}：新增 ${batch.diff['新增'] ?? 0} · 修改 ${batch.diff['修改'] ?? 0} · 未改 ${batch.diff['未改'] ?? 0} · 删除 ${batch.diff['删除'] ?? 0}` : ''}</div>}
    </div>}
    {(reqs.length > 0 || ambs.length > 0) && (
      <div className="card">
        <h4>需求点 <span className="k" style={{ fontWeight: 400 }}>· 自 PRD 提取 {reqs.length} 条</span></h4>
        {reqs.map(r => {
          const no = Object.entries(r.points ?? {}).filter(([, v]) => v !== '●').map(([k]) => k)
          return <div key={r.req_id} className="rq"><b className="mono">{r.req_id}</b> {r.summary}<span className="k"> §{r.section}</span>
            {r.change && r.change !== '新增' && batch?.prev_version ? <span className="k"> · {r.change}</span> : null}
            {no.length ? <span className="k"> · 不适用 {no.join('/')}（{r.point_source}）</span> : null}</div>
        })}
        {ambs.map(a => <div key={a.id} className="rq warn">⚠ 待确认：{a.question}<span className="k"> §{a.section}</span></div>)}
      </div>)}
    {coverage && (
      <div className="card">
        <h4>用例覆盖</h4>
        <div className="bars" style={{ gridTemplateColumns: '62px 1fr 40px' }}>
          {Object.entries(coverage.by_req).map(([r, n]) => <FragmentBar key={r} r={r} n={n} max={max} />)}
        </div>
        <div className="k" style={{ marginTop: 6 }}>共 {coverage.n_cases} 条 · 正向 {coverage.by_type['正向']} · 边界 {coverage.by_type['边界']} · 异常 {coverage.by_type['异常']} · {coverage.covered} / {coverage.total_reqs} 需求已覆盖{coverage.n_voice ? ` · 语音 ${coverage.n_voice} 条` : ''}{coverage.flagged.length ? ` · 自检有意见 ${coverage.flagged.length} 条` : ''}</div>
      </div>)}
    {lib && (
      <div className="card">
        <h4>✓ 已写入用例库 <span className="k" style={{ fontWeight: 400 }}>{lib.tier === 'bitable' ? '· 飞书多维表格' : `· 本地库（${lib.note || '飞书多维表格未配置'}）`}</span></h4>
        {lib.url && <div><a href={lib.url} target="_blank" rel="noreferrer" style={{ color: 'var(--model)' }}>打开用例库多维表格 ↗</a></div>}
        <div className="kv">
          <span className="k">写入</span><span>{lib.written.length} 条（通过 {lib.counts.pass} · 修改 {lib.counts.edit}）</span>
          <span className="k">驳回</span><span>{lib.counts.reject} 条 · 计入评估集</span>
          {lib.counts.to_req > 0 && <><span className="k">转需求方</span><span>{lib.counts.to_req} 条</span></>}
          {ambs.length > 0 && <><span className="k">待确认</span><span>{ambs.length} 条 · 已转需求方</span></>}
        </div>
      </div>)}
    {feishu && (
      <div className="card fs">
        <h4>飞书群通知 {feishu.tier === 'simulated' ? '（模拟卡片）' : '· 已推送'}</h4>
        <div className="kv"><span className="k">标题</span><span>{feishu.title}</span>{feishu.fields.map(([k, v]) => <FragmentKV key={k} k={k} v={v} />)}</div>
      </div>)}
    <SyncCard s={s} />
    {pull && (
      <div className="card">
        <h4>测试代码与首次试跑 <span className="k" style={{ fontWeight: 400 }}>· {pull.model === '本批' ? '本批审核通过的用例' : `车型 ${pull.model}`}</span></h4>
        {pull.cases.map(r => (
          <div key={r.case_id} className="prow">
            <span className="mono">{r.case_id}</span><span className="t">{r.title}</span>
            <span className={`pill ${r.plan_status === 'ready' ? 'ok' : r.plan_status === 'invalid' ? 'no' : ''}`}>{PLAN_TXT[r.plan_status] ?? r.plan_status}</span>
            {r.auto_status === '已验证' && <span className="pill ok">已验证</span>}
            {r.plan_status === 'ready' && r.auto_status === '试跑中' && <span className="pill edit">试跑中</span>}
            {r.plan_status === 'ready' && !['已验证', '试跑中'].includes(r.auto_status) && <button className="btn" onClick={() => conn.send({ type: 'plan.trial', case_id: r.case_id })}>{r.auto_status === '试跑未通过' ? '重新试跑' : '首次试跑'}</button>}
          </div>))}
        <div className="k" style={{ marginTop: 6 }}>没有能跑的台架时留“待试跑”，台架上线后自动补派。台架在本地页面认领 / 拉取已验证的用例。</div>
      </div>)}
  </>
}

/* 多维表格 → CabinPilot 同步（以表格为准；默认每 5 分钟自动一次，也可手动） */
function SyncCard({ s }: { s: State }) {
  const r = s.bitableSync
  const [busy, setBusy] = useState(false)
  if (!s.cf.lib && !s.cf.bitable && !r) return null
  const sync = () => { setBusy(true); conn.send({ type: 'caselib.sync' }); setTimeout(() => setBusy(false), 1500) }
  return (
    <div className="card">
      <h4>飞书多维表格同步 <button className="btn" style={{ marginLeft: 'auto', padding: '2px 8px' }} disabled={busy} onClick={sync}>{busy ? '同步中…' : '从多维表格同步'}</button></h4>
      {!r ? <div className="k">表格里改了用例或审核状态后点同步；后台每 5 分钟自动同步一次。</div>
        : !r.ok ? <div style={{ color: 'var(--accent)' }}>{r.error}</div>
          : <>
            <div className="k">读 {r.rows} 行 · 更新 {r.updated?.length ?? 0} · 新增 {r.created?.length ?? 0} · 测试代码需重新生成 {r.replan?.length ?? 0}{r.batches_done?.length ? ` · 批次审完 ${r.batches_done.join('、')}` : ''}</div>
            {r.updated?.slice(0, 6).map(u => <div key={u.case_id} className="k"><span className="mono">{u.case_id}</span> 改了 {u.fields.join('、')}</div>)}
            {!!r.invalid?.length && <div style={{ color: 'var(--ai)', marginTop: 4 }}>校验不通过 {r.invalid.length} 行（不进入用例库）：
              {r.invalid.slice(0, 5).map(x => <div key={x.record_id} className="k">{x.case_id || x.record_id}：{x.errors.join('；')}</div>)}</div>}
          </>}
    </div>
  )
}

function FragmentBar({ r, n, max }: { r: string; n: number; max: number }) {
  return <><span className="mono">{r}</span><span className="b" style={{ width: `${Math.max(2, n / max * 100)}%`, background: 'var(--model)' }} /><span className="mono">{n} 条</span></>
}
function FragmentKV({ k, v }: { k: string; v: string }) {
  return <><span className="k">{k}</span><span>{v}</span></>
}
