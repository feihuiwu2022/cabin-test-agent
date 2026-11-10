import { useEffect, useRef, useState } from 'react'
import type { Voice } from './App'
import { Bot, type BotState, Wave } from './Bot'
import { conn } from './conn'
import type { Defect, State, Step } from './state'

const DEV_W = 1280, DEV_H = 560
const QUICK = ['测一下 TC-DISP-001 屏幕调光', '测一下 TC-HVAC-001 语音开空调', '确认', '刚才的任务结果怎么样？']
const pct = (b: number[]) => ({ left: `${b[0] / DEV_W * 100}%`, top: `${b[1] / DEV_H * 100}%`, width: `${(b[2] - b[0]) / DEV_W * 100}%`, height: `${(b[3] - b[1]) / DEV_H * 100}%` })

export type Send = (t: string, via?: string) => void

export function Lab({ s, bot, voice, send, cloud }: { s: State; bot: BotState; voice: Voice; send: Send; cloud: string }) {
  const [zoom, setZoom] = useState<{ src: string; bbox?: number[] | null } | null>(null)
  return (
    <section id="lab">
      <ChatPanel s={s} bot={bot} voice={voice} send={send} />
      <DutPanel s={s} onZoom={setZoom} cloud={cloud} />
      <ResPanel s={s} onZoom={setZoom} send={send} cloud={cloud} />
      <div className="panel c-stream">
        <h3>④ 事件流 <span className="tag">会话事件日志（节选）· 会话 {s.session} · <a href={`/api/sessions/${s.session}/events`} target="_blank" rel="noreferrer">完整回放</a></span></h3>
        <div className="log">{s.logs.map(l => <div key={l.id} className={`l ${l.kind}`}>{l.t}  <span dangerouslySetInnerHTML={{ __html: l.text }} /></div>)}</div>
      </div>
      {zoom && (
        <div className="zoom" onClick={() => setZoom(null)}>
          <div><img src={zoom.src} alt="证据截图" />{zoom.bbox && <div className="hl" style={pct(zoom.bbox)} />}</div>
        </div>
      )}
    </section>
  )
}

/* ---------------- ① 机器人 + 对话 ---------------- */
function ChatPanel({ s, bot, voice, send }: { s: State; bot: BotState; voice: Voice; send: Send }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLDivElement>(null)
  const submit = () => {
    if (!text.trim()) return
    send(text, 'text')
    setText('')
  }
  useEffect(() => { ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: 'smooth' }) }, [s.labChat])
  const stateTxt = { idle: '待命', talking: '播报中…', listening: '聆听中…', thinking: '思考中…' }[bot]
  return (
    <div className="panel c-chat">
      <h3>① 机器人 + 对话 <span className="tag">只回答测试相关的问题</span></h3>
      <div className="mini">
        <Bot state={bot} />
        <div><div>CabinPilot · 台架 {s.rig}</div><div className="st">{stateTxt}</div><Wave on={voice.speaking || voice.listening} mic={voice.listening} n={14} style={{ justifyContent: 'flex-start', height: 18 }} /></div>
      </div>
      <div className="labchat" ref={ref}>
        {s.labChat.length === 0 && <div className="msg sys">{s.authed ? '说“测一下 TC-DISP-001”下达测试，或点右侧“立即运行本台架用例”。' : '登录后可以下达测试任务。'}</div>}
        {s.labChat.map(i => {
          switch (i.kind) {
            case 'user': return <div key={i.id} className="msg user"><div className="who">测试工程师{i.via === 'voice' ? ' · 语音' : ''}</div>{i.pending ? <span className="rec">{i.text}</span> : i.text}</div>
            case 'bot': return <div key={i.id} className="msg"><div className="who">机器人</div>{i.text}{i.src && <><br /><span className="src">来源：{i.src}</span></>}</div>
            case 'sys': return <div key={i.id} className="msg sys">{i.text}</div>
            case 'case': return (
              <div key={i.id} className="card"><h4>📄 用例 · {i.c.id}</h4><div className="k">{i.c.module} · 来源：{i.source}</div>
                <div>步骤：{i.c.steps.join(' → ')}</div><div><b>预期结果：</b>{i.c.expected}</div></div>)
            case 'plan': return (
              <div key={i.id} className="card"><h4>📋 计划确认 <span className="mono k">{i.plan.id}</span></h4>
                <div className="k">台架 {i.plan.target.rig} · {i.plan.steps?.[0]?.type === 'taskplan' ? '回放测试代码' : 'AI 探索执行'} · 期望来自 {i.plan.case_ref.case_id}</div>
                <ol>{i.plan.summary.map((x, k) => <li key={k}>{x}</li>)}</ol>
                {i.closed ? <div className="k">{i.closed === 'ok' ? '✓ 已确认 · 任务入队' : '✕ 已取消'}</div> : (
                  <div className="acts">
                    <button className="btn primary" onClick={() => conn.send({ type: 'confirm.response', kind: 'plan', ok: true })}>确认</button>
                    <button className="btn" onClick={() => conn.send({ type: 'confirm.response', kind: 'plan', ok: false })}>取消</button>
                    <span className="k">或说“确认”</span>
                  </div>)}
              </div>)
          }
          return null
        })}
      </div>
      <div className="sugg" style={{ padding: '0 12px 6px' }}>{QUICK.map(q => <button key={q} onClick={() => send(q)}>{q}</button>)}</div>
      <form className="inbar" onSubmit={e => { e.preventDefault(); submit() }}>
        <button type="button" className={`btn ${voice.listening ? 'on' : ''}`} onPointerDown={voice.pttDown} onPointerUp={voice.pttUp} onPointerLeave={voice.pttUp} title="按住说话">🎤</button>
        <input value={text} onChange={e => setText(e.target.value)} placeholder="输入测试指令" />
      </form>
    </div>
  )
}

/* ---------------- ② 车机实时投屏 + AI 交互轨迹 ---------------- */
function LiveScreen({ step, scanning }: { step: Step | null; scanning: boolean }) {
  const [jpg, setJpg] = useState(conn.lastFrame)
  useEffect(() => conn.onFrame(setJpg), [])
  return (
    <div className="screen">
      {jpg ? <img src={`data:image/jpeg;base64,${jpg}`} alt="车机实时画面" /> : <div className="empty" style={{ padding: 20 }}>等待投屏…</div>}
      <span className="live"><i />实时</span>
      {step?.bbox && <div className="hl" style={pct(step.bbox)} />}
      {step?.x != null && step.y != null && <div className="aicur" style={{ left: `${step.x / DEV_W * 100}%`, top: `${step.y / DEV_H * 100}%` }} />}
      {scanning && <div className="scan" />}
    </div>
  )
}

function DutPanel({ s, onZoom, cloud }: { s: State; onZoom: (z: { src: string; bbox?: number[] | null }) => void; cloud: string }) {
  const [sel, setSel] = useState<number | null>(null)
  useEffect(() => setSel(null), [s.steps.length])
  const idx = sel ?? s.curStep
  const step = idx != null ? s.steps[idx] : null
  const running = s.task?.status === '执行中'
  const W = (v: number) => `${Math.min(100, v / 8 * 100)}%`
  return (
    <div className="panel c-dut">
      <h3>② 车机实时投屏 + AI 交互轨迹 <span className="tag">台架 {s.rig} · 安卓域 · {s.device === 'simcar' ? 'SimCar' : 'ADB'}{s.task?.ota ? ` · ${s.task.ota}` : ''}{cloud && <> · <a href={`${cloud}/cases`} target="_blank" rel="noreferrer" style={{ color: 'var(--model)' }}>用例工作台</a></>}</span></h3>
      <div className="dut-body">
        <LiveScreen step={running || s.task?.status === '判定中' ? step : null} scanning={!!s.judging && !s.verdict} />
        <div className="track">
          {s.steps.length === 0 ? <span className="empty">还没有执行任务。点右侧“立即运行本台架用例”，或在左边说“测一下 TC-DISP-001”；执行开始后这里按步骤显示截图与动作。</span> : s.steps.map((t, i) => (
            <button key={i} className={`tk ${i === idx ? 'cur-step' : ''}`} onClick={() => setSel(i)} onDoubleClick={() => t.shot && onZoom({ src: t.shot, bbox: t.bbox })}>
              {t.shot ? <img src={t.shot} alt={t.label} /> : <div style={{ height: 40 }} />}
              <span className="act">{t.action}</span> {t.idx}/{Math.max(s.stepTotal, s.steps.length)}
              <span className="lb">{t.label}</span>
            </button>))}
        </div>
        <div className="detail">
          {step ? <>
            <b>步骤 {step.idx}：{step.label}</b>
            <div className="th">页面：{step.screen || '—'}{step.thought ? ` · 模型：${step.thought}` : ''}</div>
            <div className="bars">
              <span>LLM</span><span className="b" style={{ width: W(step.llm_s), background: 'var(--model)' }} /><span className="mono">{step.llm_s.toFixed(1)}s</span>
              <span>执行</span><span className="b" style={{ width: W(step.exe_s), background: 'var(--ai)' }} /><span className="mono">{step.exe_s.toFixed(1)}s</span>
              <span>等待</span><span className="b" style={{ width: W(step.wait_s), background: 'var(--faint)' }} /><span className="mono">{step.wait_s.toFixed(1)}s</span>
            </div>
          </> : <span className="empty">步骤详情：动作、目标与各环节耗时（点击轨迹查看，双击放大截图）。</span>}
        </div>
      </div>
    </div>
  )
}

/* ---------------- ③ 任务与结果 ---------------- */
type RigStatus = { model: string; hardware: string[]; state: string; model_warning: string }
const HARDWARE: [string, string][] = [['audio', '喇叭 + 麦克风'], ['can', 'CAN 工具'], ['camera', '摄像头']]

/* 本台架配置：车型和外设决定云端派什么任务，空闲时才能改 */
function RigSetup({ s, session }: { s: State; session: string }) {
  const [st, setSt] = useState<RigStatus | null>(null)
  const [cars, setCars] = useState<{ code: string; name: string }[]>([])
  const [model, setModel] = useState('')
  const [hw, setHw] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  const load = () => fetch('/local/status').then(r => r.json()).then((j: RigStatus) => { setSt(j); setModel(j.model); setHw(j.hardware) })
  useEffect(() => {
    load().catch(() => setSt(null))
    fetch('/api/v1/vehicles').then(r => r.json()).then(j => setCars(j.data)).catch(() => setCars([]))
  }, [])
  const save = async () => {
    const r = await fetch('/local/profile', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, hardware: hw }) })
    setMsg(r.ok ? '已保存，云端按新配置派发任务' : (await r.json()).message)
    if (r.ok) await load()
  }
  const run = async () => {
    const r = await fetch(`/api/board/rigs/${s.rig}/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session }) })
    const j = await r.json()
    if (!r.ok) { setMsg(j.detail ?? `运行失败：HTTP ${r.status}`); return }
    setMsg(`已派发 ${j.queued.length} 条` + (j.skipped.length ? `，跳过 ${j.skipped.length} 条` : ''))
  }
  if (!st) return null
  const dirty = model !== st.model || [...hw].sort().join() !== [...st.hardware].sort().join()
  return (
    <div className="card">
      <h4>本台架 {s.rig}</h4>
      <label className="k">车型 <select value={model} onChange={e => setModel(e.target.value)} disabled={st.state !== 'idle'}>
        {cars.map(c => <option key={c.code} value={c.code}>{c.name}</option>)}
      </select></label>
      <div className="k">
        {HARDWARE.map(([k, n]) => (
          <label key={k} style={{ marginRight: 10 }}><input type="checkbox" checked={hw.includes(k)} disabled={st.state !== 'idle'}
            onChange={e => setHw(e.target.checked ? [...hw, k] : hw.filter(x => x !== k))} /> {n}</label>))}
      </div>
      {st.model_warning && <div className="k" role="alert" style={{ color: 'var(--accent)' }}>⚠ {st.model_warning}</div>}
      <div className="acts">
        <button className="btn" disabled={!dirty || st.state !== 'idle'} onClick={save}>保存配置</button>
        <button className="btn primary" disabled={st.state !== 'idle'} onClick={run}>立即运行本台架用例</button>
      </div>
      {msg && <div className="k">{msg}</div>}
    </div>
  )
}

type RunCase = { case_id: string; title: string; type: string; priority: string; channel: string[]; utterance: string }

/* 可执行用例：点运行等于对话下达，仍要确认计划 */
function RunnableCases({ s, send }: { s: State; send: Send }) {
  const [cases, setCases] = useState<RunCase[] | null>(null)
  const [q, setQ] = useState('')
  const status = s.task?.status
  useEffect(() => {
    fetch(`/api/runnable?rig=${s.rig}`).then(r => r.json()).then(j => setCases(j.cases)).catch(() => setCases([]))
  }, [status, s.rig])
  const busy = !!status && !['已完成', '已取消', '执行中断', '环境异常'].includes(status)
  const list = (cases ?? []).filter(c => !q || `${c.case_id}${c.title}${c.utterance}`.includes(q))
  return (
    <div className="card">
      <h4>可执行用例 <span className="k" style={{ fontWeight: 400 }}>{cases ? `· ${cases.length} 条` : '· 加载中'}</span></h4>
      {cases?.length === 0 ? <div className="k">用例库里还没有可执行的用例，请先到云端用例工作台生成并审核。</div> : <>
        <input className="runq" value={q} onChange={e => setQ(e.target.value)} placeholder="搜索编号、标题或测试语句" />
        <div className="runlist">
          {list.map(c => (
            <div key={c.case_id} className="prow run">
              <span className="mono">{c.case_id}</span><span className="t" title={c.utterance || c.title}>{c.title}</span>
              <span className="k">{c.channel.includes('audio') ? '语音' : '触屏'}</span>
              <button className="btn" disabled={busy} onClick={() => send(`在 ${s.rig} 台架测一下 ${c.case_id}`)}>运行</button>
            </div>))}
        </div>
      </>}
    </div>
  )
}

function ResPanel({ s, onZoom, send, cloud }: { s: State; onZoom: (z: { src: string; bbox?: number[] | null }) => void; send: Send; cloud: string }) {
  const t = s.task
  const cls = !t ? '' : t.status === '已完成' ? 'ok' : ['执行中断', '环境异常', '已取消'].includes(t.status) ? 'bad' : 'run'
  const v = s.verdict
  return (
    <div className="panel c-res">
      <h3>③ 任务与结果</h3>
      <div className="res-body">
        {s.authed && <RigSetup s={s} session={s.session} />}
        {s.authed && <RunnableCases s={s} send={send} />}
        <div className="card">
          <h4>任务 <span className="mono">{t?.id ?? '—'}</span> <span className={`status ${cls}`}>{t?.status ?? '未开始'}</span></h4>
          <div className="k">{t?.desc || '等待语音下达任务'}</div>
          <div className="prog"><i style={{ width: `${t?.progress ?? 0}%` }} /></div>
        </div>
        {s.judging && (
          <div className="card">
            <h4>期望 vs 实际 <span className="k" style={{ fontWeight: 400 }}>· VLM 评估器</span></h4>
            <div className="vs">
              <div className="side"><div>期望（用例 {s.judging.case_id}）</div><div className="scr">{s.judging.expected}</div></div>
              <div className="side"><div>实际（车机截图）</div>
                <div className="shot" onClick={() => onZoom({ src: s.judging!.actual_shot, bbox: v?.bbox })}>
                  <img src={s.judging.actual_shot} alt="实际截图" />
                  {v?.bbox && <div className="hl" style={pct(v.bbox)} />}
                  {!v && <div className="scan" />}
                </div>
              </div>
            </div>
            <div className="verdict">{!v ? <span className="k">VLM 比对中…</span> : v.consistent
              ? <><span className="ok">✓ 一致</span><span className={`lvl mid`}>置信度 {v.confidence.toFixed(2)}</span></>
              : <><span className="x">✕ 不一致</span><span>{v.diff_summary}</span><span className="lvl">置信度 {v.confidence.toFixed(2)}</span></>}
            </div>
            {v && <div className="k" style={{ marginTop: 4 }}>实际可见：{v.actual_observed}<br />理由：{v.reason}</div>}
          </div>
        )}
        {s.defects.map(d => <DefectCard key={d.id} d={d} cloud={cloud} onZoom={onZoom} />)}
        {s.feishu && (
          <div className="card fs">
            <h4>飞书群通知 {s.feishu.tier === 'simulated' ? '（模拟卡片）' : s.feishu.tier === 'card' ? '· 已推送卡片' : '· 已推送文本'}</h4>
            <div className="kv"><span className="k">标题</span><span>{s.feishu.title}</span>{s.feishu.fields.map(([k, val]) => <FragmentKV key={k} k={k} v={val} />)}</div>
            {s.feishu.note && <div className="k" style={{ marginTop: 4 }}>{s.feishu.note}</div>}
          </div>
        )}
      </div>
    </div>
  )
}

function FragmentKV({ k, v }: { k: string; v: string }) {
  return <><span className="k">{k}</span><span>{v}</span></>
}

function DefectCard({ d, cloud, onZoom }: { d: Defect; cloud: string; onZoom: (z: { src: string; bbox?: number[] | null }) => void }) {
  const lvlCls = d.level === '高' ? '' : d.level === '中' ? 'mid' : 'low'
  const title = d.status === 'pending' ? '缺陷审核（待人工确认）' : d.status === 'confirmed' ? '缺陷审核：已确认' : d.status === 'rejected' ? '缺陷审核：已驳回' : '缺陷审核：已关联已有单'
  return (
    <>
      <div className="card">
        <h4>{title}</h4>
        <div>● {d.kind} · {d.summary} <span className={`lvl ${lvlCls}`}>{d.judged_by} · 置信度 {d.confidence.toFixed(2)}</span></div>
        <div className="k">规则 {d.rule} · {d.page || d.package} · 指纹 <span className="mono">{d.fingerprint}</span></div>
        <div className="k">证据：{d.actual_shot ? <a href="#" onClick={e => { e.preventDefault(); onZoom({ src: d.actual_shot, bbox: d.diff_bbox }) }} style={{ color: 'var(--model)' }}>实际截图</a> : null}{d.log_excerpt ? ' · logcat 片段' : ''} · 用例 {d.case_id || '—'}</div>
        {d.log_excerpt && <pre className="logx">{d.log_excerpt.slice(0, 900)}</pre>}
        <div className="k" style={{ marginTop: 4 }}>查重：{d.dup.label}{d.dup.issue_key && <> · <span className="mono">{d.dup.issue_key}</span></>}</div>
        {d.status === 'pending' ? (
          <div className="k" style={{ marginTop: 6 }}>待审核，审核和建票在 <a href={`${cloud}/board`} target="_blank" rel="noreferrer" style={{ color: 'var(--model)' }}>云端看板</a></div>
        ) : d.review && (
          <div className="k" style={{ marginTop: 6 }}>审核人：{d.review.by} · 结论：{d.review.action === 'confirm' ? `是缺陷 · 严重度 ${d.review.severity || d.severity}` : d.review.action === 'reject' ? `驳回（${d.review.reason}）· 不建单 · 计入评估集` : '关联已有单'}</div>
        )}
      </div>
      {d.jira && (
        <div className="card jira">
          <h4>{d.jira.linked_to ? `✓ 已关联 ${d.jira.linked_to}` : d.jira.key ? <>✓ 已创建 <span className="mono">{d.jira.key}</span></> : 'Jira 草稿'} <span className="k" style={{ fontWeight: 400 }}>{d.jira.tier === 'sandbox-local' ? '（本地沙箱）' : d.jira.tier === 'rest' ? '（沙箱 REST）' : d.jira.tier === 'prefill-link' ? '（预填链接）' : '（草稿，可复制）'}</span></h4>
          {!d.jira.linked_to && <div className="kv">
            <span className="k">摘要</span><span>{String(d.jira.draft.summary ?? '')}</span>
            <span className="k">发现版本</span><span className="mono">{String(d.jira.draft.found_version ?? '')}</span>
            <span className="k">严重度</span><span>{String(d.jira.draft.severity ?? '')}</span>
            {d.dup.issue_key && <><span className="k">疑似重复</span><span className="mono">{d.dup.issue_key}</span></>}
            <span className="k">附件</span><span>{(d.jira.draft.attachments as string[] | undefined)?.length ?? 0} 项（截图、logcat）</span>
            <span className="k">用例</span><span className="mono">{d.case_id}</span>
          </div>}
          {d.jira.url && <div style={{ marginTop: 4 }}><a href={d.jira.url} target="_blank" rel="noreferrer">打开 {d.jira.key || '建单链接'}</a></div>}
          {d.jira.tier === 'draft' && <button className="btn" style={{ marginTop: 6 }} onClick={() => navigator.clipboard.writeText(JSON.stringify(d.jira!.draft, null, 1))}>复制草稿</button>}
          {d.jira.note && <div className="k">{d.jira.note}</div>}
        </div>
      )}
    </>
  )
}
