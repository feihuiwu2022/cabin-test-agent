// WebSocket 连接（自动重连）。会话编号存在 sessionStorage，刷新后接回同一个会话，生成进度不丢
import type { Msg } from './state'

type Listener = (m: Msg) => void

export class Conn {
  ws: WebSocket | null = null
  private listeners: Listener[] = []
  private sid = sessionStorage.getItem('cabin-session') || ''
  private closed = false

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const q = this.sid ? `?session=${this.sid}` : ''
    const ws = new WebSocket(`${proto}://${location.host}/ws${q}`)
    this.ws = ws
    ws.onopen = () => {
      this.emit({ type: '_connected', on: true })
      this.send({ type: 'session.open', terminal: 'web', tts: false })
    }
    ws.onmessage = e => {
      const m = JSON.parse(e.data) as Msg
      if (m.type === 'session.state') { this.sid = m.session; sessionStorage.setItem('cabin-session', m.session) }
      this.emit(m)
    }
    ws.onclose = () => {
      this.emit({ type: '_connected', on: false })
      if (!this.closed) setTimeout(() => this.connect(), 1500)
    }
  }

  close() { this.closed = true; this.ws?.close() }
  send(m: Msg) { if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(m)) }
  on(l: Listener) { this.listeners.push(l); return () => { this.listeners = this.listeners.filter(x => x !== l) } }
  private emit(m: Msg) { this.listeners.forEach(l => l(m)) }
}

export const conn = new Conn()
