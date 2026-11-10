// 静态演示：把 WebSocket 和 fetch 换成回放。录像来自 SimCar 模拟车机上的一次真实运行（scripts/record_demo.js）。
type Ev = { t: number; m: Record<string, unknown> }
type Rec = { segs: Record<string, Ev[]>; http: Record<string, string> }

const GAP_MAX = 1200 // 回放时两条消息之间最多等这么久（毫秒）
let rec: Rec
let sock: FakeSocket | null = null
let turn = 100
const timers: number[] = []

const OFF_TOPIC = '我只负责座舱测试。可以说“测一下 TC-DISP-001”，或点右侧“立即运行本台架用例”。'
let ran = false

function emit(m: Record<string, unknown>) {
  sock?.onmessage?.({ data: JSON.stringify(m) } as MessageEvent)
}

function play(evs: Ev[], patch: (m: Record<string, unknown>) => Record<string, unknown> | null = m => m) {
  let at = 0
  let prev = evs[0]?.t ?? 0
  for (const e of evs) {
    at += Math.min(e.t - prev, GAP_MAX)
    prev = e.t
    const m = patch({ ...e.m })
    if (m) timers.push(window.setTimeout(() => emit(m), at))
  }
}

function say(text: string, answer: string) {
  const t = ++turn
  emit({ type: 'user.echo', turn: t, text, via: 'text' })
  const parts = answer.match(/[^。！？]+[。！？]?/g) ?? [answer]
  parts.forEach((p, i) => timers.push(window.setTimeout(() => emit({ type: 'reply.text', turn: t, text: p, final: false }), 250 + i * 350)))
  timers.push(window.setTimeout(() => emit({ type: 'reply.text', turn: t, text: '', final: true, src: '' }), 300 + parts.length * 350))
}

function runCase(which: 'all' | 'TC-DISP-001' | 'TC-HVAC-001') {
  ran = true
  play(which === 'all' ? rec.segs.run : rec.segs[which])
}

function onUserText(text: string) {
  const m = text.match(/TC-(DISP|HVAC)-001/i) ?? (/亮度|调光/.test(text) ? [, 'DISP'] : /空调/.test(text) ? [, 'HVAC'] : null)
  if (m && /测|跑|运行|执行/.test(text)) {
    const id = `TC-${String(m[1]).toUpperCase()}-001` as 'TC-DISP-001'
    say(text, `收到，在台架 B1 上回放 ${id} 的测试代码。`)
    timers.push(window.setTimeout(() => runCase(id), 900))
    return
  }
  if (/回归|全部|所有用例/.test(text)) {
    say(text, '好的，台架 B1 的用例集一共两条，开始执行。')
    timers.push(window.setTimeout(() => runCase('all'), 900))
    return
  }
  if (/结果|怎么样|通过|缺陷/.test(text)) {
    say(text, ran ? '刚才执行的用例都通过了，没有发现缺陷：亮度读到 255，空调打开且播报正确。' : '还没有执行任务。点右侧“立即运行本台架用例”试试。')
    return
  }
  if (/^(确认|确定|好的?|取消)/.test(text)) {
    say(text, '现在没有等待确认的计划。')
    return
  }
  say(text, OFF_TOPIC)
}

class FakeSocket {
  static OPEN = 1
  readyState = 1
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  constructor() {
    sock = this // eslint-disable-line @typescript-eslint/no-this-alias
    window.setTimeout(() => this.onopen?.(new Event('open')), 50)
  }
  send(raw: string) {
    const m = JSON.parse(raw)
    if (m.type === 'session.open') play(rec.segs.open)
    else if (m.type === 'user.text') onUserText(String(m.text || '').trim())
  }
  close() { this.readyState = 3 }
}

function json(body: unknown, status = 200) {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

export async function install() {
  const real = window.fetch.bind(window)
  rec = await (await real('rec.json')).json()
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
    const method = (init?.method || 'GET').toUpperCase()
    const path = url.pathname.replace(/^.*?(\/(local|api|auth)\/)/, '$1') + url.search
    if (!/^\/(local|api|auth)\//.test(path)) return real(input, init)
    if (/\/api\/board\/rigs\/[^/]+\/run$/.test(path) && method === 'POST') {
      window.setTimeout(() => runCase('all'), 300)
      return json(rec.http['POST /api/board/rigs/B1/run'] ?? { ok: true })
    }
    if (path.startsWith('/api/demo/reset')) { timers.forEach(clearTimeout); window.setTimeout(() => location.reload(), 100); return json({ ok: true }) }
    if (path.startsWith('/api/asr')) return json({ text: '', raw: '' })
    const key = `${method} ${path}`
    if (key in rec.http) return json(rec.http[key])
    if (method !== 'GET') return json({ message: '静态演示不能修改' }, 403)
    return json({ message: '静态演示没有这个接口' }, 404)
  }
  ;(window as unknown as { WebSocket: unknown }).WebSocket = FakeSocket
  const bar = document.createElement('div')
  bar.className = 'demo-bar'
  bar.innerHTML = '静态演示：回放 SimCar 模拟车机上的一次真实运行，不连服务器。' +
    '<a href="../demo/">测试执行</a><a href="../demo-cases/">用例生成</a><a href="../demo-board/">多台架看板</a><a href="../">介绍页</a>'
  document.body.prepend(bar)
}
