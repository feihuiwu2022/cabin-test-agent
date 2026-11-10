// 用例工作台状态：由网关 WebSocket 消息驱动
export type ChatItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'bot'; id: string; turn: number; text: string; src?: string }
  | { kind: 'sys'; id: string; text: string }

export interface DocBlock { kind: string; text: string; level: number; section: string }
export interface Req { req_id: string; section: string; quote: string; summary: string; points?: Record<string, string>; point_source?: string; change?: string }
export interface Amb { id: string; section: string; quote: string; question: string }
export interface Check { kind: string; msg: string; suggest: string }
export interface TCase {
  case_id: string; title: string; module: string; req_id: string; prd_section: string; precondition: string
  steps: string[]; expected: string; type: string; priority: string; channel: string[]; utterance: string
  expect_intent: string; expect_screen: string; expect_tts: string; latency_max_ms: number | null; voice_profile: string
  page: string; models: string[]; batch_id: string; checks: Check[]; suggestion: string; review_status: string
  auto_status: string; plan_status: string; plan_version: string; plan_path: string
}
export interface Batch { batch_id: string; doc_name: string; doc_source: string; section: string; status: string; module: string; models: string[]; params: Record<string, unknown>; prd_id?: string; prd_version?: string; version_source?: string; prev_version?: string; diff?: Record<string, number>; inherited?: Record<string, number> }
export interface Coverage { by_req: Record<string, number>; by_type: Record<string, number>; covered: number; total_reqs: number; n_cases: number; flagged: string[]; n_voice: number }
export interface PlanRes { case_id: string; title?: string; ok: boolean; errors: string[]; warnings: string[]; plan_status?: string; plan_version?: string; plan_path?: string; generated_by?: string; n_steps?: number; n_asserts?: number; requires?: string[]; yaml?: string; auto_status?: string; trial?: Trial }
export interface Trial { verdict?: string; reason?: string; rig?: string; env?: boolean }
export interface PullRow { case_id: string; title: string; module: string; models: string[]; channel: string[]; review_status: string; auto_status: string; plan_status: string; plan_version: string | null }
export type Bench = 'doc' | 'trace' | 'review' | 'code'
export interface CaseFlow {
  doc: { name: string; source: string; section: string; blocks: DocBlock[]; n_blocks: number } | null
  batch: Batch | null
  reqs: Req[]; ambs: Amb[]; extracting: boolean
  cases: TCase[]; coverage: Coverage | null
  review: { open: boolean; reasons: string[] } | null
  lib: { counts: Record<string, number>; written: string[]; tier: string; url?: string; note?: string } | null
  bitable: { url: string; ok: boolean; note: string } | null
  pull: { model: string; cases: PullRow[] } | null
  plans: Record<string, PlanRes>
  bench: Bench
}
export const emptyFlow: CaseFlow = { doc: null, batch: null, reqs: [], ambs: [], extracting: false, cases: [], coverage: null, review: null, lib: null, bitable: null, pull: null, plans: {}, bench: 'doc' }

export interface BitableSync { ok: boolean; error?: string; rows?: number; updated?: { case_id: string; fields: string[] }[]; created?: string[]; invalid?: { record_id: string; case_id: string; errors: string[] }[]; replan?: string[]; batches_done?: string[]; at?: number }
export interface FeishuRes { tier: string; ok: boolean; title: string; fields: [string, string][]; note?: string; link?: string }

export interface State {
  connected: boolean
  session: string
  user: string
  caseStage: number
  caseMaxStage: number
  cf: CaseFlow
  bitableSync: BitableSync | null
  chat: ChatItem[]
  feishu: FeishuRes | null
  logs: { id: number; t: string; text: string; kind: string }[]
  toast: string
}

export const initial: State = {
  connected: false, session: '', user: '', caseStage: -1, caseMaxStage: -1, cf: emptyFlow, bitableSync: null, chat: [],
  feishu: null, logs: [], toast: '',
}

let seq = 0
const uid = () => `i${++seq}`
const clock = () => new Date().toTimeString().slice(0, 8)

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Msg = { type: string; [k: string]: any }

function appendBot(s: State, turn: number, text: string, src: string | undefined, whole: boolean): State {
  const last = s.chat[s.chat.length - 1]
  // 同一回合的流式句子拼到同一个气泡；主动播报（turn<0）与整句回复单独成泡
  if (!whole && turn >= 0 && last && last.kind === 'bot' && last.turn === turn) {
    return { ...s, chat: [...s.chat.slice(0, -1), { ...last, text: last.text + text, src: src || last.src }] }
  }
  if (!text) return s
  return { ...s, chat: [...s.chat, { kind: 'bot', id: uid(), turn, text, src }] }
}

export function reducer(s: State, m: Msg): State {
  switch (m.type) {
    case '_connected': return { ...s, connected: m.on }
    case '_toast': return { ...s, toast: m.text }
    case '_bench': return { ...s, cf: { ...s.cf, bench: m.bench } }
    case 'session.state': return { ...s, session: m.session, user: m.user ?? '' }
    case 'user.echo': return { ...s, chat: [...s.chat, { kind: 'user', id: uid(), text: m.text }] }
    case 'reply.text': return appendBot(s, m.turn, m.text, m.src, !!m.whole || m.turn < 0)
    case 'stage': return m.flow === 'case' ? { ...s, caseStage: m.stage, caseMaxStage: Math.max(s.caseMaxStage, m.stage) } : s
    case 'casegen.doc': return { ...s, caseMaxStage: Math.max(s.caseMaxStage, 0), cf: { ...emptyFlow, doc: m as unknown as CaseFlow['doc'], extracting: true, bench: 'doc' } }
    case 'casegen.batch': return { ...s, cf: { ...s.cf, batch: m.batch } }
    case 'casegen.reqs': return { ...s, cf: { ...s.cf, reqs: m.requirements, ambs: m.ambiguities, extracting: false, bench: 'trace', batch: s.cf.batch && { ...s.cf.batch, module: m.module, models: m.models, prd_id: m.prd_id, prd_version: m.version, version_source: m.version_source, prev_version: m.prev_version, diff: m.diff } } }
    case 'casegen.case': return { ...s, cf: { ...s.cf, cases: [...s.cf.cases.filter(c => c.case_id !== m.case.case_id), m.case] } }
    case 'casegen.done': return { ...s, cf: { ...s.cf, batch: m.batch, coverage: m.coverage } }
    case 'casereview.open': return { ...s, cf: { ...s.cf, batch: m.batch, cases: m.cases, coverage: m.coverage, extracting: false, review: { open: true, reasons: m.reasons }, bitable: m.bitable ?? null, bench: 'review' } }
    case 'casereview.closed': return { ...s, cf: { ...s.cf, review: s.cf.review && { ...s.cf.review, open: false } } }
    case 'caselib.result': return { ...s, cf: { ...s.cf, cases: m.cases, lib: { counts: m.counts, written: m.written, tier: m.tier, url: m.url, note: m.note }, review: s.cf.review && { ...s.cf.review, open: false } } }
    case 'caselib.sync': return { ...s, bitableSync: m as unknown as BitableSync }
    case 'casepull.result': return { ...s, cf: { ...s.cf, pull: { model: m.model, cases: m.cases } } }
    case 'plan.result': return { ...s, cf: { ...s.cf, plans: { ...s.cf.plans, [m.case_id]: m as unknown as PlanRes }, bench: 'code' } }
    case 'plan.trial': {
      const p = s.cf.plans[m.case_id]
      const pull = s.cf.pull && { ...s.cf.pull, cases: s.cf.pull.cases.map(r => (r.case_id === m.case_id ? { ...r, auto_status: m.auto_status } : r)) }
      return { ...s, cf: { ...s.cf, pull, plans: p ? { ...s.cf.plans, [m.case_id]: { ...p, auto_status: m.auto_status, trial: m.trial } } : s.cf.plans } }
    }
    case 'feishu.result': return { ...s, feishu: m as unknown as FeishuRes }
    case 'log': return { ...s, logs: [{ id: seq++, t: clock(), text: m.text, kind: m.kind }, ...s.logs].slice(0, 300) }
    case 'error': return { ...s, toast: m.text }
    default: return s
  }
}
