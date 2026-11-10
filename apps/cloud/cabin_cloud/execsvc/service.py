"""cabin-exec：调度、台架独占、任务状态机、评估、查重、审核、建单、通知。"""
from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .. import modules
from ..bus import bus
from ..config import get_settings
from ..integ import feishu
from ..integ.jira import get_jira
from ..caselib import get_caselib
from ..planloader import load as load_plan
from ..rigs import rig_health, rig_profiles
from cabin_common.results import AssertResult, PlanResult, StepResult
from cabin_common.schema import REJECT_REASONS, DefectCandidate, ExploreTask, Review, RunResult, StepRecord, TaskStatus
from ..store import get_store
from .defects import to_candidates

REDLINE_WORDS = ("刷机", "恢复出厂", "清除数据", "格式化", "wipe", "factory")
ALLOWED_STEP_TYPES = {"taskplan"}
TRIAL_ENV_BLOCKS = {"device", "timeout", "cancelled", "capability"}  # 环境问题，不说明测试代码有问题
REVIEW_DAYS = 7
EVIDENCE_SUFFIX = {".mp4", ".zip", ".tar", ".gz", ".tgz", ".txt"}  # 提单带上的文件，步骤截图和控件树不带
RESULT_GRACE_S = 900  # 云端等台架回传的额外时间：断网时台架跑完后再补传


def rig_capabilities(rig: str = "") -> set[str]:
    """台架注册时上报的能力，缺省取默认台架。"""
    return set(rig_profiles()[rig or get_settings().rig_id].capabilities)

# 演示进度轨道（与原型 12 步一致）
STAGES = ["语音下达", "读取用例", "计划确认", "AI 执行", "VLM 评估", "候选查重", "人工审核", "沙箱建单", "飞书通知", "语音播报"]


def validate_plan(plan: ExploreTask) -> list[str]:
    """ExploreTask 校验（PRD 表32）。"""
    errs = []
    rigs = rig_profiles()
    rig = rigs.get(plan.target.rig)
    if not rig:
        errs.append(f"台架 {plan.target.rig} 不在台架档案中（可用：{', '.join(rigs)}）")
    elif plan.target.package and rig.packages and plan.target.package not in rig.packages:
        errs.append(f"包名 {plan.target.package} 不在台架 {rig.id} 的被测包白名单内")
    if not any(s.type == "taskplan" for s in plan.steps):
        errs.append("只支持回放测试代码：AI 探索尚未迁移到台架 Agent")
    if not (plan.case_ref and plan.case_ref.expected):
        errs.append("任务必须有期望来源（case_ref.expected）")
    for s in plan.steps:
        if s.type not in ALLOWED_STEP_TYPES:
            errs.append(f"步骤类型 {s.type} 一期不支持")
    text = plan.model_dump_json()
    if any(w in text for w in REDLINE_WORDS):
        errs.append("计划包含红线操作（刷机 / 恢复出厂 / 清除数据）")
    return errs


@dataclass
class TaskState:
    plan: ExploreTask
    session: str
    status: TaskStatus = TaskStatus.queued
    run: RunResult | None = None
    plan_result: PlanResult | None = None  # taskplan 回放结果
    defects: list[DefectCandidate] = field(default_factory=list)
    verdict: dict | None = None
    job: asyncio.Task | None = None
    feishu: dict | None = None
    suppressed: int = 0
    started: float = field(default_factory=time.time)
    progress: int = 0
    run_id: str = ""


class TaskService:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskState] = {}
        self.store = get_store()
        self.remote: dict[str, tuple[TaskState, asyncio.Future]] = {}  # 执行单元 → 等待台架回传的任务

    # ---------- 事件 ----------
    async def _ev(self, st: TaskState, status: TaskStatus | None = None, desc: str | None = None,
                  progress: int | None = None, log: bool = True) -> None:
        if status:
            st.status = status
        if progress is not None:
            st.progress = progress
        self.store.save_task(st.plan.id, st.session, st.status.value, st.plan.model_dump(by_alias=True),
                             st.run.model_dump() if st.run else None)
        await bus.publish(st.session, "task.event", {
            "task_id": st.plan.id, "status": st.status.value, "desc": desc, "progress": st.progress,
            "rig": st.plan.target.rig, "ota": st.run.ota if st.run else ""}, log=log)

    async def stage(self, session: str, n: int) -> None:
        await bus.publish(session, "stage", {"stage": n, "name": STAGES[n]}, log=False)

    async def log(self, session: str, text: str, kind: str = "") -> None:
        await bus.publish(session, "log", {"text": text, "kind": kind}, log=False)

    # ---------- 提交 / 撤销 ----------
    async def submit(self, plan: ExploreTask, session: str) -> TaskState:
        errs = validate_plan(plan)
        if errs:
            raise ValueError("；".join(errs))
        st = TaskState(plan=plan, session=session)
        self.tasks[plan.id] = st
        await self._ev(st, TaskStatus.queued, "已确认 · 任务入队", 22)
        st.job = bus.spawn(self._run(st, self._run_on_rig, st))
        return st

    async def cancel(self, task_id: str, by: str = "测试工程师") -> bool:
        st = self.tasks.get(task_id)
        if not st or st.status in (TaskStatus.done, TaskStatus.cancelled):
            return False
        if st.job and not st.job.done():
            st.job.cancel()
        await self._ev(st, TaskStatus.cancelled, f"已撤销（{by}）")
        return True

    # ---------- 执行主流程 ----------
    async def _run(self, st: TaskState, work: Callable[..., Awaitable], *args) -> None:
        try:
            await work(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._on_run_error(st, e)
        finally:
            self._release_trial(st)
            self._release_retest(st)

    async def _on_run_error(self, st: TaskState, e: Exception) -> None:
        sess = st.session
        if isinstance(e, asyncio.TimeoutError):
            await self._ev(st, TaskStatus.interrupted, "执行超时，已保留已采集证据")
            await bus.say(sess, "执行超时，任务中断，证据已保留。")
            return
        await self._ev(st, TaskStatus.interrupted, f"执行中断：{type(e).__name__}: {e}")
        await self.log(sess, f"<b>error</b> {type(e).__name__}: {e}", "err")
        await bus.say(sess, "执行过程中出现异常，任务已中断，请查看事件流。")

    # ---------- 执行单元进任务池，由台架领取（A-03） ----------
    async def _run_on_rig(self, st: TaskState) -> None:
        plan, sess = st.plan, st.session
        rig = plan.target.rig
        replay = next(s for s in plan.steps if s.type == "taskplan")
        await self._ev(st, TaskStatus.preparing, f"{rig} · 健康检查", 25)
        h = rig_health(rig)
        if not h.online:
            await self._ev(st, TaskStatus.env_error, f"台架离线：{h.detail}")
            await bus.say(sess, f"台架 {rig} 离线，任务进入环境异常，需要人工处理。")
            return
        doc, case = load_plan(replay.case_id, rig_profiles()[rig].model)
        st.run = RunResult(task_id=plan.id, ota=h.ota)
        run_id = self.store.create_run(plan.id, sess, rig, replay.case_id, case.plan_version, replay.kind,
                                       int(doc.get("timeout_s") or 300), doc, case.model_dump(), owner=plan.origin.user,
                                       regression_id=replay.regression_id)
        st.run_id = run_id
        fut = self._watch(st)
        await bus.publish(sess, "device.prepare", {"rig": rig, "ota": h.ota, "model": h.model, "build_type": ""})
        await self.log(sess, f"<b>run.queue</b> {run_id} 进入任务池 · 等待台架 {rig} 领取")
        await self._ev(st, TaskStatus.queued, f"{run_id} · 等待台架 {rig} 领取", 26)
        await self._await_result(st, fut, plan.limits.max_duration_s + RESULT_GRACE_S)

    def _watch(self, st: TaskState) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.remote[st.run_id] = (st, fut)
        return fut

    async def _await_result(self, st: TaskState, fut: asyncio.Future, timeout: float) -> None:
        try:
            st.plan_result = await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.store.cancel_run(st.run_id)
            raise
        finally:
            self.remote.pop(st.run_id, None)
        await self._conclude(st)

    async def _conclude(self, st: TaskState) -> None:
        kind = st.plan.steps[0].kind
        if kind == "trial":
            await self._after_trial(st, self.store.run(st.run_id)["plan_version"])
        elif kind == "retest":
            await self._after_retest(st)
        else:
            await self._after_replay(st)

    # ---------- 云端重启：任务、执行单元、候选都在库里，接着等台架回传 ----------
    def _restore(self, task_id: str) -> TaskState | None:
        st = self.tasks.get(task_id)
        doc = self.store.get_task(task_id) if not st else None
        if doc:
            st = TaskState(plan=ExploreTask(**doc["plan"]), session=doc["session"], status=TaskStatus(doc["status"]),
                           run=RunResult(**doc["result"]) if doc["result"] else RunResult(task_id=task_id),
                           defects=[DefectCandidate(**d) for d in self.store.list_defects(task_id=task_id)])
            self.tasks[task_id] = st
        return st

    def resume(self) -> int:
        """云端启动时调用：接回没跑完的执行单元。台架照常领取和回传，云端宕机的时间不算超时。"""
        n = 0
        for run in self.store.unfinished_runs():
            st = self._restore(run["task_id"])
            if not st or run["id"] in self.remote:
                continue
            st.run_id = run["id"]
            left = run["created"] + st.plan.limits.max_duration_s + RESULT_GRACE_S - time.time()
            st.job = bus.spawn(self._run(st, self._await_result, st, self._watch(st), max(left, RESULT_GRACE_S)))
            n += 1
        alive = {t.plan.steps[0].retest_of for t in self.tasks.values() if t.job and not t.job.done()}
        for parent in {d["task_id"] for d in self.store.list_defects(status="retest")} - alive:  # 复测已经没了，直接送审
            bus.spawn(self._retest_verdict(parent, "", "blocked", []))
        return n

    async def on_run_claimed(self, run: dict) -> None:
        hit = self.remote.get(run["id"])
        if hit:
            st = hit[0]
            await self._ev(st, TaskStatus.running, f"{run['rig_id']} · {st.run.ota} · 回放测试代码", 28)
            await self.stage(st.session, 3)

    async def on_run_event(self, run: dict, e: dict) -> None:
        hit = self.remote.get(run["id"])
        if not hit:  # 任务已超时或服务重启过：结果照常入库，界面不再更新
            return
        st = hit[0]
        if e.get("type") == "step":
            await self._on_step(st, StepResult(**e["step"]), e.get("total", 0))
        elif e.get("type") == "assert":
            r = AssertResult(**e["result"]) if e.get("result") else None
            await self._on_assert(st, run["case_id"], e["phase"], e["assertion"], r, e.get("shot", ""))

    async def on_run_result(self, run: dict, res: PlanResult) -> None:
        hit = self.remote.get(run["id"])
        if hit and not hit[1].done():
            hit[1].set_result(res)
            return
        self.store.append(run["session"], "run.late_result", {"run_id": run["id"], "verdict": res.verdict})
        st = self._restore(run["task_id"])
        if st and st.status == TaskStatus.interrupted:  # 断网比宽限期还久：结果补传上来，失败不能丢
            st.run_id, st.plan_result = run["id"], res
            await self._run(st, self._conclude, st)

    async def _on_step(self, st: TaskState, sr: StepResult, total: int) -> None:
        plan, sess = st.plan, st.session
        rec = StepRecord(idx=max(sr.id, 1), action=sr.action, label=sr.label, screen=sr.screen, shot=sr.shot,
                         bbox=sr.bbox, x=sr.x, y=sr.y, exe_s=round(sr.dur_ms / 1000, 2))
        st.run.steps.append(rec)
        if sr.shot:
            st.run.final_shot = sr.shot
        await bus.publish(sess, "task.step", {"task_id": plan.id, "step": rec.model_dump(), "total": total})
        await self.log(sess, f"<b>{sr.action}</b> {sr.label} · {'ok' if sr.result == 'ok' else '失败：' + sr.error}",
                       "md" if sr.result == "ok" else "err")
        await self._ev(st, None, None, min(64, 28 + rec.idx * 6), log=False)

    async def _on_assert(self, st: TaskState, case_id: str, phase: str, a: dict, r: AssertResult | None, shot: str) -> None:
        plan, sess = st.plan, st.session
        if a.get("type") not in ("vision", "voice"):
            if r:
                await self.log(sess, f"<b>assert {a.get('id')}</b> {a.get('type')} → {r.result}",
                               "md" if r.result == "pass" else "err")
            return
        if phase == "start":
            await self._ev(st, TaskStatus.judging, "评估中：双线判定（看屏幕 + 听播报）" if a["type"] == "voice"
                           else "评估中：视觉断言 期望 vs 实际", 70)
            await self.stage(sess, 4)
            exp = a.get("expected", "")
            exp = f"屏幕：{exp.get('screen')}；播报：{exp.get('tts')}" if isinstance(exp, dict) else exp
            await bus.publish(sess, "judge.start", {"task_id": plan.id, "expected": exp,
                                                    "case_id": case_id, "actual_shot": shot}, log=False)
        elif r and r.result != "error":
            v = r.detail["verdict"]
            st.verdict = v
            await bus.publish(sess, "judge.verdict", {"task_id": plan.id, "verdict": v, "bbox": r.detail.get("bbox"),
                                                      "actual_shot": r.detail.get("actual_shot", "")})
            lines = r.detail.get("lines")
            await self.log(sess, (f"<b>voice_check</b> 看 {lines['look']} · 听 {lines['listen']} · {r.detail.get('class')}" if lines else
                                  f"<b>vision_check</b> 期望 vs 实际 → {'一致' if r.result == 'pass' else '不一致'}") +
                           f" · 置信度 {r.confidence:.2f}", "md" if r.result == "pass" else "err")
        elif r:
            await self.log(sess, f"<b>{a['type']}_check</b> 判定不可用：{r.detail.get('error')}", "err")

    def _release_trial(self, st: TaskState) -> None:
        """试跑没跑完（离线、超时、撤销）：不是测试代码的问题，退回已生成，可以再试。"""
        step = st.plan.steps[0]
        lib = get_caselib()
        c = lib.get_case(step.case_id) if step.kind == "trial" else None
        if c and c.auto_status == "试跑中":
            c.auto_status = "已生成"
            lib.save_case(c)

    async def _after_trial(self, st: TaskState, plan_version: str) -> None:
        """试跑结论回写用例。失败多半是测试代码的问题，不进缺陷审核。"""
        from .. import casewf  # casewf → rigplan → 本模块
        plan, sess, res = st.plan, st.session, st.plan_result
        lib = get_caselib()
        c = lib.get_case(res.case_id)
        if not c or c.plan_version != plan_version:  # 试跑期间测试代码又生成了一版，这次结果不算数
            await self._ev(st, TaskStatus.done, "测试代码已更新，这次试跑结果作废", 100)
            return
        env = res.verdict == "blocked" and res.block_reason in TRIAL_ENV_BLOCKS
        c.auto_status = "已验证" if res.verdict == "passed" else "已生成" if env else "试跑未通过"
        c.trial = {"verdict": res.verdict, "reason": _trial_reason(res), "rig": plan.target.rig, "plan_version": plan_version,
                   "by": plan.origin.user, "env": env, "at": time.time()}
        lib.save_case(c)
        await casewf.push_bitable(None, [c], "回写自动化状态")
        await bus.publish(sess, "plan.trial", {"case_id": c.case_id, "auto_status": c.auto_status, "trial": c.trial})
        await self.log(sess, f"<b>trial</b> {c.case_id} 首次试跑 → {c.auto_status}" + (f" · {c.trial['reason']}" if c.trial["reason"] else ""),
                       "md" if res.verdict == "passed" else "err")
        await self._ev(st, TaskStatus.done, f"首次试跑：{c.auto_status}", 100)

    async def _after_replay(self, st: TaskState) -> None:
        """结论合成后进入查重 / 审核；阻塞没有结论，不出缺陷候选。"""
        plan, sess, res = st.plan, st.session, st.plan_result
        for w in res.warnings:
            await self.log(sess, f"<b>warn</b> {w}", "ai")
        await self.log(sess, f"<b>run.end</b> {res.case_id} · 结论 {res.verdict}" +
                       (f"（{res.block_reason}：{res.block_detail}）" if res.block_reason else "") +
                       f" · 耗时 {res.duration_s}s", "md" if res.verdict == "passed" else "err")
        if res.verdict == "blocked":
            await self._ev(st, TaskStatus.interrupted, f"阻塞：{res.block_reason} · {res.block_detail}"[:120])
            await bus.say(sess, f"{res.case_id} 执行被阻塞，没有得出结论：{res.block_detail[:60]}。证据已保留。")
            return
        await bus.say(sess, "执行完成，正在汇总断言结果。")
        cands = self._candidates(st)
        if not (cands and await self._hold_for_retest(st, cands)):
            await self._triage(st, cands)

    def _candidates(self, st: TaskState) -> list[DefectCandidate]:
        plan = st.plan
        cands = to_candidates(st.plan_result, plan.id, plan.target.rig, st.run.ota, plan.target.package, st.run.final_shot,
                              rig_profiles()[plan.target.rig].model)
        for c in cands:
            self._assign(c, st)
        return cands

    # ---------- 回归失败先复测，复现了才送审 ----------
    async def _hold_for_retest(self, st: TaskState, cands: list[DefectCandidate]) -> bool:
        from .. import rigplan  # rigplan → 本模块
        plan, step, case_id = st.plan, st.plan.steps[0], st.plan_result.case_id
        pending = {(d.fingerprint, d.ota) for d in self.pending_reviews()}
        if not step.regression_id or all((c.fingerprint, c.ota) in pending or self.store.recall_review(c.fingerprint, c.ota)
                                         for c in cands):  # 已有结论或正在审的，不用再复测
            return False
        for c in cands:
            c.status = "retest"
            self.store.save_defect(c.model_dump())
        st.defects = cands
        try:
            rig = await rigplan.retest(case_id, plan.target.rig, st.run.ota, plan.origin.user, st.session, plan.id, step.regression_id)
        except (KeyError, ValueError) as e:
            await self.log(st.session, f"<b>retest</b> {e}，直接送审", "err")
            await self._retest_verdict(plan.id, "", "blocked", [])
            return True
        await self.log(st.session, f"<b>retest</b> {case_id} 失败，派到 {rig} 复测")
        await self._ev(st, TaskStatus.judging, f"{plan.target.rig} 失败，{rig} 复测中", 78)
        return True

    async def _after_retest(self, st: TaskState) -> None:
        res = st.plan_result
        cands = self._candidates(st) if res.verdict == "failed" else []
        await self._retest_verdict(st.plan.steps[0].retest_of, st.plan.target.rig, res.verdict, cands)
        await self._ev(st, TaskStatus.done, f"复测结论：{res.verdict}", 100)

    def _release_retest(self, st: TaskState) -> None:
        """复测没跑完（离线、超时、撤销）：没有结论，候选照常送审。"""
        step = st.plan.steps[0]
        parent = self._restore(step.retest_of) if step.kind == "retest" else None
        if parent and any(d.status == "retest" for d in parent.defects):
            bus.spawn(self._retest_verdict(step.retest_of, st.plan.target.rig, "blocked", []))

    async def _retest_verdict(self, parent_id: str, rig: str, verdict: str, cands: list[DefectCandidate]) -> None:
        """失败且指纹相同算复现；通过算不稳定，不送审；没跑出结论时不能丢问题，照常送审。"""
        st = self._restore(parent_id)
        held = [d for d in st.defects if d.status == "retest"] if st else []
        if not held:
            return
        seen = {c.fingerprint for c in cands}
        real = [d for d in held if verdict == "blocked" or d.fingerprint in seen]
        for d in held:
            if d in real:
                d.status = "pending"
                d.rigs = sorted({*d.rigs, rig} - {d.rig, ""})
                d.reason = d.reason if verdict != "blocked" else f"{d.reason}（未能复测）".strip()
            else:
                d.status = "flaky"
                self.store.save_defect(d.model_dump())
                self._drop_evidence(d)
        fresh = [c.model_copy(update={"task_id": parent_id}) for c in cands if c.fingerprint not in {d.fingerprint for d in held}]
        if not fresh:  # 复现的证据已经有一份
            for c in cands:
                self._drop_evidence(c)
        if not real and not fresh:
            self.store.mark_flaky(held[0].run_id)
            await self.log(st.session, f"<b>retest</b> {held[0].case_id} 未复现，记不稳定", "md")
            await self._ev(st, TaskStatus.done, "复测未复现，记不稳定", 100)
            return
        await self._triage(st, real + fresh)

    def _assign(self, c: DefectCandidate, st: TaskState) -> None:
        """待审核默认归发现人；定时任务没有发现人时，归模块全部成员。"""
        mod = modules.of_case(c.case_id)
        c.module, c.session, c.run_id, c.steps = mod.name, st.session, st.run_id, st.plan.summary
        c.finder = st.plan.origin.user if st.plan.origin.user != "engineer" else ""
        c.assignees = [c.finder] if c.finder else list(mod.members)

    async def _triage(self, st: TaskState, cands: list[DefectCandidate]) -> None:
        """缺陷候选：审核记忆 → 查重 → 待审核。探索任务与测试代码回放共用。"""
        plan, sess, run = st.plan, st.session, st.run
        await self.stage(sess, 5)
        await self._ev(st, TaskStatus.judging, "缺陷候选查重中", 76)
        jira = get_jira()
        for c in cands:
            mem = self.store.recall_review(c.fingerprint, c.ota)
            if mem and mem["action"] == "reject":
                self.store.bump_review_hit(c.fingerprint, c.ota)
                c.status = "suppressed"
                await self.log(sess, f"<b>review_memory</b> 同版本同指纹已驳回过 → 压掉只计数（{c.fingerprint}）")
                continue
            same = next((p for p in self.pending_reviews() if p.fingerprint == c.fingerprint and p.ota == c.ota), None)
            if same:  # 别的台架也报了同一问题：并成一条，这份证据不留
                same.rigs = sorted({*same.rigs, c.rig} - {same.rig})
                self.store.save_defect(same.model_dump())
                c.status = "suppressed"
                self._drop_evidence(c)
                await self.log(sess, f"<b>merge</b> {c.rig} 与 {same.rig} 是同一问题，合并到 {same.id}")
                continue
            c.dup = await jira.find_dup(c)
            await self.log(sess, f"<b>find_dup</b> {c.ota} · {c.dup.label}" + (f" → {c.dup.issue_key}" if c.dup.issue_key else ""), "md")
            if mem and mem["action"] in ("confirm", "link") and mem["issue_key"]:
                self.store.bump_review_hit(c.fingerprint, c.ota)
                c.status = "linked"
                c.jira = await jira.link_existing(c, mem["issue_key"])
                await self.log(sess, f"<b>review_memory</b> 同版本同指纹已确认过 → 自动关联 {mem['issue_key']}，复现次数 +1")
        st.suppressed = sum(c.status == "suppressed" for c in cands)
        st.defects = [c for c in cands if c.status != "suppressed"]
        for c in st.defects:
            self.store.save_defect(c.model_dump())
            await bus.publish(sess, "defect.card", {"defect": c.model_dump(), "reasons": REJECT_REASONS})
        pending = [c for c in st.defects if c.status == "pending"]
        if pending:
            await self.stage(sess, 6)
            await self._ev(st, TaskStatus.reviewing, f"{st.plan.target.rig} · {run.ota} · 执行完成 · {len(pending)} 个候选待审核", 84)
            dup = next((c for c in pending if c.dup.issue_key), None)
            await bus.say(sess, f"生成了 {len(pending)} 个缺陷候选" + ("，查重发现同类已知缺陷" if dup else "") +
                          "。是不是缺陷，需要测试工程师审核。")
        else:
            await self._finalize(st)

    # ---------- 人工审核 ----------
    async def review(self, defect_id: str, action: str, by: str = "测试工程师", reason: str = "",
                     severity: str = "", summary: str = "", link_key: str = "") -> DefectCandidate:
        st, d = self._find_defect(defect_id)
        if d.status != "pending":
            raise ValueError(f"候选 {defect_id} 已被 {d.review.by if d.review else '系统'} 处理（{d.status}）")
        if action == "reject" and reason not in REJECT_REASONS:
            raise ValueError(f"驳回必须选择原因：{'/'.join(REJECT_REASONS)}")
        d.review = Review(action=action, by=by, reason=reason, severity=severity, summary=summary)
        sess, jira = d.session, get_jira()
        if action == "confirm":  # 审核期间可能有人手工建了单
            d.dup = await jira.find_dup(d)
            if d.dup.category == "duplicate":
                action, link_key = "link", d.dup.issue_key
        await bus.publish(sess, "review.action", {"defect_id": d.id, "action": action, "by": by, "reason": reason})
        if action == "reject":
            d.status = "rejected"
            self.store.add_eval_sample(d.model_dump(), reason)
            self.store.remember_review(d.fingerprint, d.ota, "reject", "", reason, by)
            await self.log(sess, f"<b>review_gate</b> 人工驳回 · 原因={reason} · 写入评估集", "md")
        elif action == "link":
            key = link_key or d.dup.issue_key
            if not key:
                raise ValueError("没有可关联的已有单")
            d.status = "linked"
            await self.stage(sess, 7)
            d.jira = await jira.link_existing(d, key)
            self.store.remember_review(d.fingerprint, d.ota, "link", key, "", by)
            await self.log(sess, f"<b>link_issue</b> 关联已有单 {key} · 追加评论与证据", "md")
        else:
            await self.log(sess, "<b>review_gate</b> 人工确认是缺陷 · 进入建单")
            if st:
                await self._ev(st, TaskStatus.reviewing, "提单中", 88)
            await self.stage(sess, 7)
            d.jira = await jira.create_issue(d, {"id": d.task_id, "summary": d.steps}, self._evidence_files(d))
            if d.jira.tier in ("draft", "prefill-link"):  # 留在待审核，证据不动，可以重试
                note = d.jira.note
                d.jira, d.review = None, None
                raise ValueError(f"建票失败：{note}")
            d.status = "confirmed"
            self.store.remember_review(d.fingerprint, d.ota, "confirm", d.jira.key, "", by)
            await self.log(sess, f"<b>create_issue</b> {d.jira.tier} · {d.jira.key or d.jira.url or '草稿'}"
                                 + (f" · 疑似重复 {d.dup.issue_key}" if d.dup.issue_key else ""), "md")
        self.store.save_defect(d.model_dump())
        if not (d.jira and d.jira.note.startswith("附件上传失败")):
            self._drop_evidence(d)
        await bus.publish(sess, "defect.update", {"defect": d.model_dump()})
        if action == "confirm" and d.jira and d.jira.key:
            await bus.say(sess, f"已在沙箱项目创建缺陷 {_spell(d.jira.key)}。")
        elif action == "reject":
            await bus.say(sess, "已驳回，不建单。驳回原因会记入判定评估集。")
        elif action == "link":
            await bus.say(sess, f"已关联到已有缺陷 {_spell(d.jira.key)}，并追加了本次证据。")
        if st and all(c.status != "pending" for c in st.defects):
            await self._finalize(st)
        return d

    def _find_defect(self, defect_id: str) -> tuple[TaskState | None, DefectCandidate]:
        """候选以数据库为准，云端重启后还能审。"""
        doc = self.store.get_defect(defect_id)
        if not doc:
            raise KeyError(f"缺陷候选 {defect_id} 不存在")
        d = DefectCandidate(**doc)
        st = self._restore(d.task_id)
        if st:
            st.defects = [d if c.id == d.id else c for c in st.defects]
        return st, d

    def pending_reviews(self, session: str | None = None) -> list[DefectCandidate]:
        out = [DefectCandidate(**doc) for doc in self.store.list_defects(status="pending")]
        return [d for d in out if session in (None, d.session)]

    def expire_reviews(self, days: float = REVIEW_DAYS) -> int:
        """没人处理的候选 7 天后放弃，看板只记数量。"""
        n = 0
        for d in self.pending_reviews():
            if time.time() - d.created_at > days * 86400:
                d.status = "expired"
                self.store.save_defect(d.model_dump())
                self._drop_evidence(d)
                n += 1
        return n

    async def sync_jira(self) -> None:
        """同步工单状态，用于统计已关闭和未关闭。"""
        jira = get_jira()
        if not jira.rest_enabled:
            return
        for doc in self.store.list_defects(status="confirmed") + self.store.list_defects(status="linked"):
            d = DefectCandidate(**doc)
            if d.jira and d.jira.key:
                try:
                    d.jira_status = await jira.sync_status(d.jira.key)
                except Exception:  # Jira 一时连不上，下一轮再同步
                    continue
                self.store.save_defect(d.model_dump())

    def _evidence_files(self, d: DefectCandidate) -> list[Path]:
        root = get_settings().evidence_dir / d.run_id
        shot = Path(d.actual_shot).name if d.actual_shot else ""
        if not d.run_id or not root.is_dir():
            return []
        return [f for f in sorted(root.iterdir()) if f.is_file() and (f.name == shot or f.suffix in EVIDENCE_SUFFIX)]

    def _drop_evidence(self, d: DefectCandidate) -> None:
        if d.run_id:
            shutil.rmtree(get_settings().evidence_dir / d.run_id, ignore_errors=True)

    # ---------- 通知 + 播报 ----------
    async def _finalize(self, st: TaskState) -> None:
        plan, sess, run = st.plan, st.session, st.run
        if plan.steps[0].regression_id:  # 回归一轮几百条，不逐条通知，由每日汇总推送
            await self._ev(st, TaskStatus.done, "审核完成" if st.defects else "未发现缺陷", 100)
            return
        filed = [d for d in st.defects if d.status == "confirmed"]
        linked = [d for d in st.defects if d.status == "linked"]
        rejected = [d for d in st.defects if d.status == "rejected"]
        await self.stage(sess, 8)
        case_id = plan.case_ref.case_id if plan.case_ref else "-"
        if filed or linked:
            d = (filed or linked)[0]
            title = f"测试机器人发现 {len(filed) + len(linked)} 个缺陷"
            fields = [("缺陷", ", ".join(x.jira.key + ("（沙箱）" if x.jira.tier == "sandbox-local" else "")
                                         for x in filed + linked if x.jira and x.jira.key) or "草稿待建"),
                      ("类型", d.kind + " · " + d.summary), ("用例", case_id), ("台架", f"{plan.target.rig} · 安卓域"),
                      ("版本", run.ota if run else "")]
            if d.dup.issue_key:
                fields.append(("备注", f"疑似重复 {d.dup.issue_key}"))
            template = "red"
        else:
            title = f"测试完成 · 0 个缺陷"
            fields = [("用例", case_id), ("台架", f"{plan.target.rig} · 安卓域"), ("版本", run.ota if run else "")]
            if rejected:
                fields.insert(0, ("说明", f"{len(rejected)} 个候选经人工审核驳回（{rejected[0].review.reason}）"))
            if st.suppressed:
                fields.insert(0, ("说明", f"{st.suppressed} 个候选与本版本已驳回记录同指纹，已自动压掉只计数"))
            template = "green"
        from ..config import get_settings
        link = f"{get_settings().public_base_url}/?task={plan.id}"
        st.feishu = await feishu.notify(title, fields, link, template)
        await bus.publish(sess, "feishu.result", {"task_id": plan.id, **st.feishu})
        await self.log(sess, f"<b>notify</b> 飞书 · {st.feishu['tier']}" + (f" · {st.feishu.get('note')}" if st.feishu.get("note") else ""), "md")
        await self.stage(sess, 9)
        desc = f"{plan.target.rig} · {run.ota if run else ''} · " + (
            f"发现 {len(filed) + len(linked)} 个缺陷" if filed or linked else
            ("候选已驳回" if rejected else ("候选已按审核记忆压掉" if st.suppressed else "未发现缺陷")))
        await self._ev(st, TaskStatus.done, desc, 100)
        await self.log(sess, f"<b>task.done</b> {plan.id} 完成 · 缺陷 {len(filed) + len(linked)} · 驳回 {len(rejected)} · 已通知")
        fs = "飞书群已经通知" if st.feishu["tier"] != "simulated" else "飞书通知已生成（未配置群机器人，界面内模拟）"
        if filed:
            dup = f"，并标注疑似重复 {_spell(filed[0].dup.issue_key)}" if filed[0].dup.issue_key else ""
            text = f"测试完成。发现一个{filed[0].kind}缺陷，经人工确认后在沙箱提单 {_spell(filed[0].jira.key)}{dup}，{fs}。"
        elif linked:
            text = f"测试完成。缺陷已关联到已有单 {_spell(linked[0].jira.key)}，{fs}。"
        elif rejected:
            text = f"测试完成。缺陷候选被人工驳回，没有建单，{fs}。"
        elif st.suppressed:
            text = f"测试完成。发现的 {st.suppressed} 个候选与本版本已驳回的记录相同，已自动压掉只计数，{fs}。"
        else:
            text = f"测试完成。用例 {case_id} 期望与实际一致，没有发现缺陷，{fs}。"
        await bus.say(sess, text)

    def summary(self) -> list[dict]:
        out = []
        for st in sorted(self.tasks.values(), key=lambda s: -s.started):
            out.append({"task_id": st.plan.id, "status": st.status.value, "rig": st.plan.target.rig,
                        "case": st.plan.case_ref.case_id if st.plan.case_ref else "", "progress": st.progress,
                        "defects": [{"id": d.id, "summary": d.summary, "status": d.status,
                                     "jira": d.jira.key if d.jira else ""} for d in st.defects]})
        return out


def _spell(key: str) -> str:
    """播报时把单号读清楚：CTRTS-0618 → CTRTS 0618。"""
    return key.replace("-", " ")


def _trial_reason(res: PlanResult) -> str:
    fails = [f"{a.assertion_id}（{a.type}）{a.detail.get('error') or (a.detail.get('verdict') or {}).get('diff_summary') or ''}".strip()
             for a in res.asserts if a.result == "fail"]
    return "；".join(fails + ([f"{res.block_reason}：{res.block_detail}"] if res.block_reason else []))[:300]


_svc: TaskService | None = None


def get_service() -> TaskService:
    global _svc
    if _svc is None:
        _svc = TaskService()
    return _svc
