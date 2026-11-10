// 前端状态：由网关 WebSocket 消息驱动
export type Mode = 'lab'

export interface Step {
  idx: number; action: string; label: string; thought: string; element_id: number | null
  x: number | null; y: number | null; bbox: number[] | null; screen: string; shot: string
  llm_s: number; exe_s: number; wait_s: number
}
export interface Dup { category: string; label: string; issue_key: string; issue_summary: string; suggestion: string }
export interface JiraRes { tier: string; key: string; url: string; linked_to: string; draft: Record<string, unknown>; note: string }
export interface Defect {
  id: string; task_id: string; rule: string; kind: string; title: string; summary: string; reason: string
  confidence: number; level: '高' | '中' | '低'; judged_by: string; package: string; page: string; case_id: string
  expected: string; actual_shot: string; diff_bbox: number[] | null; log_excerpt: string; ota: string; rig: string
  fingerprint: string; dup: Dup; severity: string; status: string
  review: { action: string; by: string; reason: string; severity: string } | null; jira: JiraRes | null
}
export interface CaseT { id: string; title: string; module: string; steps: string[]; expected: string }
export interface Plan { id: string; summary: string[]; steps?: { type: string }[]; target: { rig: string; package: string }; case_ref: { case_id: string; expected: string; source: string } }

export type ChatItem =
  | { kind: 'user'; id: string; text: string; via: string; pending?: boolean }
  | { kind: 'bot'; id: string; turn: number; text: string; src?: string }
  | { kind: 'sys'; id: string; text: string }
  | { kind: 'case'; id: string; c: CaseT; source: string }
  | { kind: 'plan'; id: string; plan: Plan; closed?: 'ok' | 'no' }

export interface TaskView {
  id: string; status: string; desc: string; progress: number; rig: string; ota: string
}
export interface Verdict { consistent: boolean; actual_observed?: string; diff_summary?: string; reason?: string; confidence: number }
export interface FeishuRes { tier: string; ok: boolean; title: string; fields: [string, string][]; note?: string; link?: string }

export interface State {
  connected: boolean
  session: string
  mode: Mode
  authed: boolean
  user: string
  rig: string
  device: string
  model: string
  visionModel: string
  versionLabel: string
  strictData: boolean
  llmBase: string
  stage: number
  maxStage: number
  labChat: ChatItem[]
  task: TaskView | null
  steps: Step[]
  stepTotal: number
  curStep: number | null
  judging: { expected: string; case_id: string; actual_shot: string } | null
  verdict: (Verdict & { bbox: number[] | null; actual_shot: string }) | null
  defects: Defect[]
  reasons: string[]
  feishu: FeishuRes | null
  logs: { id: number; t: string; text: string; kind: string }[]
  frame: string
  toast: string
}

export const initial: State = {
  connected: false, session: '', mode: 'lab', authed: false, user: '', rig: 'B2', device: '', model: '',
  visionModel: '', versionLabel: '', strictData: false, llmBase: '',
  stage: -1, maxStage: -1, labChat: [], task: null, steps: [], stepTotal: 0, curStep: null,
  judging: null, verdict: null, defects: [], reasons: [], feishu: null, logs: [], frame: '', toast: '',
}

let seq = 0
const uid = () => `i${++seq}`
const clock = () => new Date().toTimeString().slice(0, 8)

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Msg = { type: string; [k: string]: any }

function pushChat(s: State, item: ChatItem): State {
  return { ...s, labChat: [...s.labChat, item] }
}

function appendBot(s: State, turn: number, text: string, src: string | undefined, whole: boolean): State {
  const key = 'labChat'
  const list = s[key]
  const last = list[list.length - 1]
  // 同一回合的流式句子拼到同一个气泡；主动播报（turn<0）与整句回复单独成泡
  if (!whole && turn >= 0 && last && last.kind === 'bot' && last.turn === turn) {
    const merged = { ...last, text: last.text + text, src: src || last.src }
    return { ...s, [key]: [...list.slice(0, -1), merged] }
  }
  if (!text) {
    if (src && last && last.kind === 'bot' && last.turn === turn) return { ...s, [key]: [...list.slice(0, -1), { ...last, src }] }
    return s
  }
  return pushChat(s, { kind: 'bot', id: uid(), turn, text, src })
}

export function reducer(s: State, m: Msg): State {
  switch (m.type) {
    case '_connected': return { ...s, connected: m.on }
    case '_toast': return { ...s, toast: m.text }
    case '_hint': return pushChat(s, { kind: 'sys', id: uid(), text: m.text })
    case '_userPending': return pushChat(s, { kind: 'user', id: m.id, text: '● 识别中…', via: 'voice', pending: true })
    case '_userDrop': {
      const f = (l: ChatItem[]) => l.filter(i => i.id !== m.id)
      return { ...s, labChat: f(s.labChat) }
    }
    case '_reset': return { ...initial, connected: s.connected, session: s.session, mode: s.mode, authed: s.authed, user: s.user, frame: s.frame, rig: s.rig, device: s.device, model: s.model, visionModel: s.visionModel, versionLabel: s.versionLabel, strictData: s.strictData, llmBase: s.llmBase }
    case 'session.state': {
      let n: State = {
        ...s,
        session: m.session, mode: m.mode, authed: m.authed, user: m.user ?? '',
        rig: m.rig, device: m.device, model: m.model,
        visionModel: m.vision_model ?? s.visionModel,
        versionLabel: m.version_label ?? s.versionLabel,
        strictData: m.strict_data ?? s.strictData,
        llmBase: m.llm_base ?? s.llmBase,
      }
      return n
    }
    case 'user.echo': {
      // 替换"识别中"占位或去重（本地已显示的文本）
      const key = 'labChat'
      const list = s[key]
      const pi = list.findIndex(i => i.kind === 'user' && i.pending)
      const item: ChatItem = { kind: 'user', id: uid(), text: m.text, via: m.via }
      if (pi >= 0) return { ...s, [key]: [...list.slice(0, pi), item, ...list.slice(pi + 1)] }
      return pushChat(s, item)
    }
    case 'reply.text': return appendBot(s, m.turn, m.text, m.src, !!m.whole || m.turn < 0)
    case 'stage': return { ...s, stage: m.stage, maxStage: Math.max(s.maxStage, m.stage) }
    case 'case.card': return pushChat(s, { kind: 'case', id: uid(), c: m.case, source: m.source })
    case 'confirm.request':
      if (m.kind === 'plan') return pushChat(s, { kind: 'plan', id: uid(), plan: m.plan })
      return pushChat(s, { kind: 'sys', id: uid(), text: `撤销任务 ${m.task_id}？请说“确认”` })
    case 'confirm.closed': {
      const labChat = s.labChat.map(i => (i.kind === 'plan' && i.plan.id === m.task_id ? { ...i, closed: (m.ok ? 'ok' : 'no') as 'ok' | 'no' } : i))
      return { ...s, labChat }
    }
    case 'task.event': {
      const fresh = !s.task || s.task.id !== m.task_id
      const task: TaskView = { id: m.task_id, status: m.status, desc: m.desc ?? (fresh ? '' : s.task!.desc), progress: m.progress, rig: m.rig, ota: m.ota || (fresh ? '' : s.task!.ota) }
      if (fresh) return { ...s, task, steps: [], curStep: null, judging: null, verdict: null, defects: [], feishu: null }
      return { ...s, task }
    }
    case 'task.step': return { ...s, steps: [...s.steps, m.step], stepTotal: m.total, curStep: s.steps.length }
    case 'judge.start': return { ...s, judging: { expected: m.expected, case_id: m.case_id, actual_shot: m.actual_shot } }
    case 'judge.verdict': return { ...s, verdict: { ...m.verdict, bbox: m.bbox, actual_shot: m.actual_shot } }
    case 'defect.card': return { ...s, defects: [...s.defects.filter(d => d.id !== m.defect.id), m.defect], reasons: m.reasons }
    case 'defect.update': return { ...s, defects: s.defects.map(d => (d.id === m.defect.id ? m.defect : d)) }
    case 'feishu.result': return { ...s, feishu: m as unknown as FeishuRes }
    case 'log': return { ...s, logs: [{ id: seq++, t: clock(), text: m.text, kind: m.kind }, ...s.logs].slice(0, 300) }
    case 'screen.frame': return s.frame === m.jpg ? s : { ...s, frame: m.jpg }
    case 'error': return { ...s, toast: m.text }
    default: return s
  }
}
