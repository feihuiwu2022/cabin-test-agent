// 静态演示：把 WebSocket 和 fetch 换成回放。录像来自 SimCar 模拟车机 + 三台台架上的一次真实运行（demo-tools/）。
type Ev = { t: number; m: Record<string, unknown> }
type CasesRec = { segs: Record<string, Ev[]>; http: Record<string, string> }
type BoardRec = { snaps: { t: number; j: string }[]; http: Record<string, string> }

const GAP_MAX = 1500
const PAGE = import.meta.env.VITE_DEMO_PAGE as string
let sock: FakeSocket | null = null
let turn = 100
let done = { import: false, submit: false }
const timers: number[] = []

function emit(m: Record<string, unknown>) { sock?.onmessage?.({ data: JSON.stringify(m) } as MessageEvent) }

function play(evs: Ev[], patch: (m: Record<string, unknown>) => Record<string, unknown> = m => m) {
  let at = 0
  let prev = evs[0]?.t ?? 0
  for (const e of evs) {
    at += Math.min(e.t - prev, GAP_MAX)
    prev = e.t
    const m = patch({ ...e.m })
    timers.push(window.setTimeout(() => emit(m), at))
  }
}

function say(text: string, answer: string) {
  const t = ++turn
  emit({ type: 'user.echo', turn: t, text, via: 'text' })
  timers.push(window.setTimeout(() => emit({ type: 'reply.text', turn: t, text: answer, final: true, src: '', whole: true }), 400))
}

let cases: CasesRec
function onCasesText(text: string) {
  if (/示例\s*PRD|示例需求|生成用例/.test(text) && !done.import) {
    done.import = true
    play(cases.segs.import, m => (m.type === 'user.echo' ? { ...m, text } : m))
    return
  }
  if (/采纳建议|提交审核/.test(text) && done.import && !done.submit) {
    done.submit = true
    play(cases.segs.submit, m => (m.type === 'user.echo' ? { ...m, text } : m))
    return
  }
  say(text, done.import
    ? (done.submit ? '这一批已经入库并生成了测试代码，可以在“测试代码”页看每条的 YAML 和试跑结果。' : '可以在中间逐条审核，或说“采纳建议并提交审核”。')
    : '静态演示只录了示例 PRD：点“用示例 PRD 试一下”，或说“导入示例 PRD 生成用例”。')
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
    if (m.type === 'session.open') play(cases.segs.open)
    else if (m.type === 'user.text') onCasesText(String(m.text || '').trim())
    else if (m.type === 'case.review.submit' && !done.submit) { done.submit = true; play(cases.segs.submit.filter(e => e.m.type !== 'user.echo')) }
    else if (m.type !== 'tts.set') emit({ type: 'error', text: '静态演示不支持这个操作' })
  }
  close() { this.readyState = 3 }
}

function json(body: unknown, status = 200) {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

let board: BoardRec
let t0 = 0
function boardNow(): string {
  if (!t0) return board.snaps[0].j
  const el = Date.now() - t0
  let j = board.snaps[0].j
  for (const s of board.snaps) { if (s.t <= el) j = s.j; else break }
  return j
}

const LINKS: Record<string, string> = { '/cases': '../demo-cases/', '/board': '../demo-board/' }

export async function install() {
  const real = window.fetch.bind(window)
  const rec = await (await real('rec.json')).json()
  if (PAGE === 'board') { board = rec; window.setTimeout(() => { t0 = Date.now() }, 1500) } else cases = rec
  const http: Record<string, string> = rec.http
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
    const method = (init?.method || 'GET').toUpperCase()
    const path = url.pathname.replace(/^.*?(\/api\/)/, '$1') + url.search
    if (!path.startsWith('/api/')) return real(input, init)
    if (path.startsWith('/api/v1/auth/me')) return json(http['/api/v1/auth/me'] ?? http['GET /api/v1/auth/me'] ?? { user: { name: '访客' } })
    if (PAGE === 'board') {
      if (path === '/api/board') return json(boardNow())
      if (/\/api\/board\/rigs\/[^/]+\/run$/.test(path) && method === 'POST') { t0 = Date.now(); return json({ ok: true }) }
      if (method === 'GET' && http[path]) return json(http[path])
    } else if (method === 'GET' && http[`GET ${path}`]) return json(http[`GET ${path}`])
    if (method !== 'GET') return json({ detail: '静态演示不能修改', message: '静态演示不能修改' }, 403)
    return json({ detail: '静态演示没有这个接口' }, 404)
  }
  ;(window as unknown as { WebSocket: unknown }).WebSocket = FakeSocket
  document.addEventListener('click', e => {
    const a = (e.target as HTMLElement).closest('a')
    const href = a?.getAttribute('href') ?? ''
    if (!a || !href.startsWith('/')) return
    e.preventDefault()
    const to = LINKS[href.split('?')[0]]
    if (to) location.href = to
    else alert('静态演示只包含：测试执行、用例生成、多台架看板三个页面。')
  }, true)
  const css = document.createElement('style')
  css.textContent = '.demo-bar{position:fixed;left:50%;bottom:10px;transform:translateX(-50%);z-index:50;display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;justify-content:center;max-width:calc(100% - 32px);padding:6px 14px;border-radius:999px;font:12px/1.5 system-ui,sans-serif;background:#5B4BE6;color:#fff;box-shadow:0 4px 16px rgba(0,0,0,.25)}.demo-bar a{color:#fff;text-decoration:underline}'
  document.head.appendChild(css)
  const bar = document.createElement('div')
  bar.className = 'demo-bar'
  bar.innerHTML = (PAGE === 'board' ? '静态演示：回放三台台架同时跑用例集（B3 注入了缺陷）。<a href="#" id="replay">从头回放</a>' : '静态演示：回放一次真实的用例生成。') +
    '<a href="../demo/">测试执行</a><a href="../demo-cases/">用例生成</a><a href="../demo-board/">多台架看板</a><a href="../">介绍页</a>'
  document.body.prepend(bar)
  bar.querySelector('#replay')?.addEventListener('click', e => { e.preventDefault(); t0 = Date.now() })
}
