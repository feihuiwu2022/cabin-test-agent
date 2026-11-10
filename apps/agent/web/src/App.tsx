import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { Mic, Player, stripWake } from './audio'
import type { BotState } from './Bot'
import { conn } from './conn'
import { Lab } from './Lab'
import { initial, reducer } from './state'

const STAGES = ['下达任务', '读取用例', '计划确认', 'AI 执行', 'VLM 评估', '候选查重', '人工审核', '沙箱建单', '飞书通知', '语音播报']
const WAKE_WINDOW_MS = 30000

export default function App() {
  const [s, dispatch] = useReducer(reducer, initial)
  const [speaking, setSpeaking] = useState(false)
  const [listening, setListening] = useState(false)
  const [thinking, setThinking] = useState(false)
  const [handsfree, setHandsfree] = useState(false)
  const [wakeReq, setWakeReq] = useState(true)
  const [ttsOn, setTtsOn] = useState(true)
  const [cloud, setCloud] = useState('')
  const player = useRef(new Player())
  const mic = useRef<Mic | null>(null)
  const minTurn = useRef(0)
  const wakeUntil = useRef(0)
  const pendingId = useRef<string | null>(null)
  const sessionRef = useRef('')
  sessionRef.current = s.session
  const st = useRef({ handsfree, wakeReq })
  st.current = { handsfree, wakeReq }

  useEffect(() => { fetch('/local/status').then(r => r.json()).then(j => setCloud(j.board_url)).catch(() => setCloud('')) }, [])
  useEffect(() => { if (s.toast) { const t = setTimeout(() => dispatch({ type: '_toast', text: '' }), 3500); return () => clearTimeout(t) } }, [s.toast])

  // WebSocket
  useEffect(() => {
    player.current.onSpeaking = on => {
      setSpeaking(on)
      // 唤醒窗口从机器人播报结束开始计时（播报本身可能比窗口还长）
      if (!on && Date.now() < wakeUntil.current + 60000) wakeUntil.current = Date.now() + WAKE_WINDOW_MS
    }
    const off = conn.on(m => {
      if (m.type === 'reply.audio') {
        if (m.turn < 0 || m.turn >= minTurn.current) player.current.enqueue(m.mp3)
        return
      }
      if (m.type === 'user.echo') { minTurn.current = m.turn; setThinking(true) }
      if (m.type === 'reply.text' && (m.text || m.final)) setThinking(false)
      dispatch(m)
    })
    conn.connect()
    return () => { off(); conn.close() }
  }, [])

  const sendText = useCallback((text: string, via = 'text') => {
    const t = text.trim()
    if (!t) return
    player.current.ensure()
    if (player.current.speaking) { player.current.stop(); conn.send({ type: 'barge_in', reason: 'new_input' }) }
    conn.send({ type: 'user.text', text: t, via })
  }, [])

  // 麦克风
  const ensureMic = useCallback(async () => {
    if (mic.current?.active) return mic.current
    const m = new Mic({
      onSpeechStart: () => {
        setListening(true)
        if (player.current.speaking) { // 打断：检测到人声立即停播
          player.current.stop()
          conn.send({ type: 'barge_in', reason: 'voice' })
        }
        const id = `p${Date.now()}`
        pendingId.current = id
        dispatch({ type: '_userPending', id })
      },
      onDiscard: () => {
        setListening(false)
        if (pendingId.current) dispatch({ type: '_userDrop', id: pendingId.current })
        pendingId.current = null
      },
      onUtterance: async wav => {
        setListening(false)
        const id = pendingId.current
        pendingId.current = null
        const fd = new FormData()
        fd.append('file', wav, 'speech.wav')
        let text = ''
        try { text = ((await (await fetch(`/api/asr?session=${sessionRef.current}`, { method: 'POST', body: fd })).json()).text || '').trim() } catch { /* noop */ }
        text = text.replace(/[。．.]$/, '')
        const drop = () => id && dispatch({ type: '_userDrop', id })
        if (!text) return drop()
        // 唤醒词：免提监听且需要唤醒时，只响应“你好小禾 …”开头或唤醒窗口内的话
        if (st.current.handsfree && st.current.wakeReq && mic.current?.continuous) {
          const { woke, rest } = stripWake(text)
          if (!woke && Date.now() > wakeUntil.current && !player.current.speaking) {
            // 未唤醒：不静默丢弃，告诉用户听到了什么、该怎么说
            if (id) dispatch({ type: '_userDrop', id })
            dispatch({ type: '_hint', text: `听到“${text.slice(0, 20)}”，但未唤醒。请以“你好小禾”开头说话，或关闭唤醒词。` })
            return
          }
          wakeUntil.current = Date.now() + WAKE_WINDOW_MS
          if (woke) text = rest || '你好'
        }
        sendText(text, 'voice')
      },
    })
    m.ttsPlaying = () => player.current.speaking
    await m.start()
    mic.current = m
    return m
  }, [sendText])

  const toggleHandsfree = async () => {
    player.current.ensure()
    if (handsfree) { if (mic.current) mic.current.continuous = false; setHandsfree(false); return }
    try { const m = await ensureMic(); m.continuous = true; setHandsfree(true) } catch { dispatch({ type: '_toast', text: '无法打开麦克风（需允许麦克风权限）' }) }
  }
  const pttDown = async () => {
    player.current.ensure()
    try { (await ensureMic()).pttDown() } catch { dispatch({ type: '_toast', text: '无法打开麦克风（需允许麦克风权限）' }) }
  }
  const pttUp = () => mic.current?.pttUp()

  const toggleTts = () => {
    const on = !ttsOn
    setTtsOn(on)
    player.current.muted = !on
    if (!on) player.current.stop()
    conn.send({ type: 'tts.set', on })
  }
  const reset = async () => {
    player.current.stop()
    await fetch('/api/demo/reset', { method: 'POST' })
    dispatch({ type: '_reset' })
  }

  const botState: BotState = listening ? 'listening' : speaking ? 'talking' : thinking ? 'thinking' : 'idle'
  const voice = { handsfree, wakeReq, listening, speaking, toggleHandsfree, setWakeReq, pttDown, pttUp }

  return (
    <div id="app">
      <header className="top">
        <div className="brand">
          <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><rect x="3" y="6" width="22" height="17" rx="7" fill="none" stroke="currentColor" strokeWidth="2" /><rect x="9" y="12" width="3" height="5" rx="1.5" fill="currentColor" /><rect x="16" y="12" width="3" height="5" rx="1.5" fill="currentColor" /><line x1="14" y1="6" x2="14" y2="2" stroke="currentColor" strokeWidth="2" /></svg>
          <div><b>座舱智能测试机器人 CabinPilot</b><br /><small title={`数据合规：${s.strictData ? '严格模式（internal/sensitive 不走外网）' : '开发模式（internal/sensitive 走外网，审计标 DEV-ONLY）'}`}>{s.versionLabel || '—'} · 台架 {s.rig} · {s.device === 'simcar' ? 'SimCar 模拟车机' : 'ADB'}</small></div>
        </div>
        <span className={`chip ${s.authed ? 'lab' : 'warn'}`}><span className="dot" />{s.authed ? '测试模式' : '测试模式 · 登录后可下达任务'}</span>
        {!s.connected && <span className="chip warn"><span className="dot" />连接中…</span>}
        <div className="ctrls">
          {s.user
            ? <span className="chip"><span className="dot" />{s.user}</span>
            : <a className="btn" href="/auth/login">登录</a>}
          {cloud && <a className="btn primary" href={`${cloud}/cases`} target="_blank" rel="noreferrer" title="用例生成和审核在云端">📄 用例工作台</a>}
          <button className={`btn ${handsfree ? 'on' : ''}`} onClick={toggleHandsfree} title="浏览器端 VAD 自动切句；播报中说话可打断">{handsfree ? '🎙 免提监听中' : '🎙 免提监听'}</button>
          {handsfree && <button className={`btn ${wakeReq ? 'on' : ''}`} onClick={() => setWakeReq(!wakeReq)} title="开启后需以“你好小禾”或“hello xiaohe”开头唤醒，30 秒内连续对话免唤醒">{wakeReq ? '唤醒词：你好小禾' : '唤醒词：关'}</button>}
          <button className="btn" onClick={toggleTts} aria-pressed={!ttsOn}>{ttsOn ? '🔊 播报开' : '🔇 播报关'}</button>
          <button className="btn" onClick={reset}>重置演示</button>
        </div>
        <div className="rail-wrap"><div className="rail">
          {STAGES.map((n, i) => (
            <div key={n} className={i === s.stage ? 'cur' : i <= s.maxStage ? 'done' : ''}><span className="n">{String(i + 1).padStart(2, '0')}</span>{n}</div>))}
        </div></div>
      </header>
      <main>
        <Lab s={s} bot={botState} voice={voice} send={sendText} cloud={cloud} />
      </main>
      <footer className="note">CabinPilot · 模型经模型网关调用并审计 · ASR 本地离线 · Jira {`/`} 飞书未配置时自动降级为本地沙箱 {`/`} 界面内模拟卡片</footer>
      {s.toast && <div className="toast">{s.toast}</div>}
    </div>
  )
}

export interface Voice {
  handsfree: boolean; wakeReq: boolean; listening: boolean; speaking: boolean
  toggleHandsfree: () => void; setWakeReq: (b: boolean) => void; pttDown: () => void; pttUp: () => void
}
