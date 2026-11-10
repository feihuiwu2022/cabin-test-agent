// 云端用例工作台：对话导入 PRD → 需求点 → 用例 → 审核入库 → 测试代码
import { useEffect, useReducer, useRef, useState } from 'react'
import { type Attachment, CaseBench, CaseCards, PRD_ACCEPT, uploadPrd } from './CaseFlow'
import { conn } from './conn'
import { initial, reducer, type State } from './state'
import './styles.css'

const CASE_STAGES = ['导入 PRD', '需求提取', '用例生成', 'AI 自检', '用例审核', '入库通知', '测试代码', '首次试跑']
const QUICK = ['导入示例 PRD 生成用例', '导入语音用例模拟表', '采纳建议并提交审核', '查看 A01-CN 测试代码']

function Chat({ s, send }: { s: State; send: (t: string, a?: Attachment) => void }) {
  const [text, setText] = useState('')
  const [att, setAtt] = useState<Attachment | null>(null)
  const [uploading, setUploading] = useState(false)
  const [upErr, setUpErr] = useState('')
  const file = useRef<HTMLInputElement>(null)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: 'smooth' }) }, [s.chat])
  const upload = async (f: File) => {
    setUploading(true); setUpErr('')
    try {
      setAtt(await uploadPrd(f, s.session))
    } catch (e) {
      setUpErr(`上传失败：${(e as Error).message}`)
    } finally { setUploading(false) }
  }
  const submit = () => {
    if (!text.trim() && !att) return
    send(text, att ?? undefined)
    setText(''); setAtt(null)
  }
  return (
    <div className="panel c-chat">
      <h3>① 对话</h3>
      <div className="labchat" ref={ref}>
        {s.chat.length === 0 && <div className="msg sys">上传 PRD、贴链接或本机路径，也可以直接说要生成哪一章。</div>}
        {s.chat.map(i => {
          if (i.kind === 'user') return <div key={i.id} className="msg user"><div className="who">{s.user}</div>{i.text}</div>
          if (i.kind === 'bot') return <div key={i.id} className="msg"><div className="who">CabinPilot</div>{i.text}{i.src && <><br /><span className="src">来源：{i.src}</span></>}</div>
          return <div key={i.id} className="msg sys">{i.text}</div>
        })}
      </div>
      <div className="sugg" style={{ padding: '0 12px 6px' }}>{QUICK.map(q => <button key={q} onClick={() => send(q)}>{q}</button>)}</div>
      {(att || uploading || upErr) && (
        <div className="attach">{uploading ? '上传中…' : upErr ? <span style={{ color: 'var(--accent)' }}>{upErr}</span> : <>📄 {att!.name} <span className="k">{(att!.size / 1024).toFixed(0)} KB · 发送后开始生成</span></>}
          {!uploading && <button type="button" onClick={() => { setAtt(null); setUpErr('') }} aria-label="移除">✕</button>}
        </div>)}
      <form className="inbar" onSubmit={e => { e.preventDefault(); submit() }}
        onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) void upload(f) }}>
        <button type="button" className="btn" onClick={() => file.current?.click()} title="上传 PRD（.docx / .pdf / .md / .txt / .xlsx），也可拖到这里">📎 PRD</button>
        <input type="file" ref={file} accept={PRD_ACCEPT} hidden onChange={e => { const f = e.target.files?.[0]; if (f) void upload(f); e.target.value = '' }} />
        <input value={text} onChange={e => setText(e.target.value)} placeholder={att ? '可补充章节，如：只生成 §4.1' : '输入指令、PRD 路径或在线链接'} aria-label="输入指令" />
      </form>
    </div>
  )
}

function Workbench({ user }: { user: string }) {
  const [s, dispatch] = useReducer(reducer, initial)
  useEffect(() => {
    const off = conn.on(dispatch)
    conn.connect()
    return () => { off(); conn.close() }
  }, [])
  useEffect(() => { if (s.toast) { const t = setTimeout(() => dispatch({ type: '_toast', text: '' }), 3500); return () => clearTimeout(t) } }, [s.toast])
  const send = (text: string, attachment?: Attachment) => {
    const t = text.trim()
    if (t || attachment) conn.send({ type: 'user.text', text: t, via: 'text', attachment })
  }
  return (
    <div id="app">
      <header className="top">
        <div className="brand"><div><b>CabinPilot 用例工作台</b><br /><small>PRD → 用例 → 审核 → 入库 → 测试代码 → 试跑</small></div></div>
        {!s.connected && <span className="chip warn"><span className="dot" />连接中…</span>}
        <div className="ctrls">
          <span className="chip"><span className="dot" />{user}</span>
          <a className="btn" href="/board">调度看板</a>
          <a className="btn" href="/skills">用例编写规范</a>
        </div>
        <div className="rail-wrap"><div className="rail">
          {CASE_STAGES.map((n, i) => (
            <div key={n} className={i === s.caseStage ? 'cur' : i <= s.caseMaxStage ? 'done' : ''}><span className="n">{String(i + 1).padStart(2, '0')}</span>{n}</div>))}
        </div></div>
      </header>
      <main>
        <section id="lab">
          <Chat s={s} send={send} />
          <div className="panel c-dut">
            <h3>② 用例工作台 <span className="tag">{s.cf.batch ? `批次 ${s.cf.batch.batch_id}` : '还没有批次'}</span></h3>
            <CaseBench s={s} dispatch={dispatch} send={(t, _via, a) => send(t, a)} />
          </div>
          <div className="panel c-res"><h3>③ 用例与入库</h3><div className="res-body"><CaseCards s={s} feishu={s.feishu} /></div></div>
          <div className="panel c-stream">
            <h3>④ 事件流 <span className="tag">会话 {s.session}</span></h3>
            <div className="log">{s.logs.map(l => <div key={l.id} className={`l ${l.kind}`}>{l.t}  <span dangerouslySetInnerHTML={{ __html: l.text }} /></div>)}</div>
          </div>
        </section>
      </main>
      {s.toast && <div className="toast" role="alert">{s.toast}</div>}
    </div>
  )
}

export default function Cases() {
  const [user, setUser] = useState<string | null | undefined>(undefined)
  useEffect(() => {
    fetch('/api/v1/auth/me').then(r => r.json()).then(j => setUser(j.user?.name ?? null)).catch(() => setUser(null))
  }, [])
  if (user === undefined) return <div id="app">加载中…</div>
  if (user === null) {
    return (
      <div id="app" style={{ alignItems: 'center', justifyContent: 'center', gap: 12 }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>CabinPilot 用例工作台</h1>
        <p style={{ margin: 0 }}>生成和审核用例需要登录，审核记录会落到本人。</p>
        <a className="btn primary" href="/api/v1/auth/login?redirect=/cases">登录</a>
      </div>
    )
  }
  return <Workbench user={user} />
}
