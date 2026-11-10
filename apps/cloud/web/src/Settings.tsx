// 设置：个人 Jira 用户名；管理员配置模型、Jira 服务账号和车型到项目的映射
import { useEffect, useState } from 'react'
import './board.css'

type Me = { name: string; jira_user: string; email: string; admin: boolean }
type Jira = { url: string; user: string; has_secret: boolean; token_auth: boolean; projects: Record<string, { project: string; component: string }>; vehicles: { code: string; name: string }[] }

type Model = { protocol: string; url: string; chat_model: string; vision_model: string; hint: string; from_env: boolean }

const put = (url: string, body: object) => fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

function Personal({ me, reload }: { me: Me; reload: () => void }) {
  const [name, setName] = useState(me.jira_user)
  const [msg, setMsg] = useState('')
  const save = async () => {
    const r = await put('/api/settings/me', { jira_user: name.trim() })
    setMsg(r.ok ? '已保存' : `保存失败：HTTP ${r.status}`)
    reload()
  }
  return (
    <section>
      <h2>我的 Jira 用户名</h2>
      <p className="meta">建票时报告人设成你。系统会先按飞书邮箱自动关联，查不到时才需要填。不用填密码。</p>
      <div className="acts">
        <input value={name} onChange={e => setName(e.target.value)} placeholder="Jira 用户名" aria-label="Jira 用户名" />
        <button className="primary" onClick={save}>保存</button>
        {msg && <span className="meta">{msg}</span>}
      </div>
    </section>
  )
}

/* 判定和云端对话共用这一份；回放不调模型 */
function ModelAdmin() {
  const [m, setM] = useState<Model | null>(null)
  const [secret, setSecret] = useState('')
  const [msg, setMsg] = useState('')
  const load = () => fetch('/api/settings/model').then(r => r.json()).then(setM).catch(() => setMsg('读取失败'))
  useEffect(() => { void load() }, [])
  if (!m) return <p className="meta">{msg || '加载中…'}</p>
  const save = async () => {
    const r = await put('/api/settings/model', { ...m, secret })
    if (r.ok) { setSecret(''); setMsg('已保存，立即生效'); void load() } else setMsg((await r.json()).detail ?? `保存失败：HTTP ${r.status}`)
  }
  const test = async () => {
    setMsg('测试中…')
    const r = await fetch('/api/settings/model/test', { method: 'POST' })
    const b = await r.json()
    setMsg(r.ok ? `对话和看图都正常，耗时 ${b.latency_s} 秒` : b.detail)
  }
  return (
    <section>
      <h2>模型</h2>
      <p className="meta">判定、用例生成和云端对话用这里的模型。{m.from_env && '当前用的是环境变量里的配置。'}</p>
      <div className="form">
        <label>接口类型 <select value={m.protocol} onChange={e => setM({ ...m, protocol: e.target.value })}><option value="anthropic">Anthropic 兼容</option><option value="openai">OpenAI 兼容</option></select></label>
        <label>地址 <input value={m.url} onChange={e => setM({ ...m, url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
        <label>对话模型 <input value={m.chat_model} onChange={e => setM({ ...m, chat_model: e.target.value })} /></label>
        <label>视觉模型 <input value={m.vision_model} onChange={e => setM({ ...m, vision_model: e.target.value })} /></label>
        <label>Token <input type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} placeholder={m.hint ? `已保存 ${m.hint}，不改就留空` : '换地址时必填'} /></label>
      </div>
      <div className="acts"><button className="primary" onClick={save}>保存</button><button onClick={test}>测试连接</button>{msg && <span className="meta">{msg}</span>}</div>
    </section>
  )
}

function JiraAdmin() {
  const [j, setJ] = useState<Jira | null>(null)
  const [secret, setSecret] = useState('')
  const [msg, setMsg] = useState('')
  useEffect(() => { fetch('/api/settings/jira').then(r => r.json()).then(setJ).catch(() => setMsg('读取失败')) }, [])
  if (!j) return <p className="meta">{msg || '加载中…'}</p>
  const setProject = (code: string, k: 'project' | 'component', v: string) => {
    const old = j.projects[code] ?? { project: '', component: '' }
    setJ({ ...j, projects: { ...j.projects, [code]: { ...old, [k]: v } } })
  }
  const save = async () => {
    const r = await put('/api/settings/jira', { url: j.url, user: j.user, secret, token_auth: j.token_auth, projects: j.projects })
    if (r.ok) { setSecret(''); setJ({ ...j, has_secret: j.has_secret || !!secret }); setMsg('已保存') } else setMsg((await r.json()).detail ?? `保存失败：HTTP ${r.status}`)
  }
  const test = async () => {
    const r = await fetch('/api/settings/jira/test', { method: 'POST' })
    const b = await r.json()
    setMsg(r.ok ? `连接正常：${b.user}` : b.detail)
  }
  return (
    <section>
      <h2>Jira 公共服务账号</h2>
      <div className="form">
        <label>地址 <input value={j.url} onChange={e => setJ({ ...j, url: e.target.value })} placeholder="https://jira.example.com" /></label>
        <label>账号 <input value={j.user} onChange={e => setJ({ ...j, user: e.target.value })} /></label>
        <label>{j.token_auth ? 'Token' : '密码'} <input type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)} placeholder={j.has_secret ? '已保存，不改就留空' : ''} /></label>
        <label className="check"><input type="checkbox" checked={j.token_auth} onChange={e => setJ({ ...j, token_auth: e.target.checked })} /> 用 Token 登录</label>
      </div>
      <h2>车型对应的 Jira 项目</h2>
      <div className="scroll"><table>
        <thead><tr><th>车型</th><th>项目</th><th>模块（可选）</th></tr></thead>
        <tbody>{j.vehicles.map(v => (
          <tr key={v.code}><td>{v.name}</td>
            <td><input value={j.projects[v.code]?.project ?? ''} onChange={e => setProject(v.code, 'project', e.target.value)} aria-label={`${v.name} 项目`} /></td>
            <td><input value={j.projects[v.code]?.component ?? ''} onChange={e => setProject(v.code, 'component', e.target.value)} aria-label={`${v.name} 模块`} /></td></tr>))}
        </tbody>
      </table></div>
      <div className="acts"><button className="primary" onClick={save}>保存</button><button onClick={test}>测试连接</button>{msg && <span className="meta">{msg}</span>}</div>
    </section>
  )
}

export default function Settings() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  const load = () => fetch('/api/settings/me').then(r => (r.ok ? r.json() : null)).then(setMe).catch(() => setMe(null))
  useEffect(() => { void load() }, [])
  if (me === undefined) return <main className="gate">加载中…</main>
  if (me === null) return <main className="gate"><h1>设置</h1><a className="login" href="/api/v1/auth/login?redirect=/settings">登录</a></main>
  return (
    <main>
      <header className="top"><h1>设置</h1><span className="sub">{me.name}</span><a className="link" href="/board">调度看板</a><a className="link" href="/cases">用例工作台</a></header>
      <Personal me={me} reload={() => void load()} />
      {me.admin ? <><ModelAdmin /><JiraAdmin /></> : <p className="meta" style={{ marginTop: 16 }}>模型和 Jira 公共配置由管理员维护。</p>}
    </main>
  )
}
