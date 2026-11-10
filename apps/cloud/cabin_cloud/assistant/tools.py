"""cabin-tools：工具注册表。

每个工具声明：可用场景、是否需要人工确认、数据级别。确认/审核由 Hooks 与网关执行，不由模型自行判断。
file_bug / notify 不暴露给模型：只能在人工审核通过后由执行服务调用（review_gate）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from ..cases import get_cases
from ..caselib import IN_LIBRARY, get_caselib
from ..rigs import rig_profiles
from ..execsvc.service import get_service, rig_capabilities, validate_plan
from ..modelgw import get_gateway
from ..planloader import dry_run, load as load_plan
from cabin_common.schema import CaseRef, Origin, PlanStep, ExploreTask, Target

@dataclass
class ToolCtx:
    session: str
    authed: bool
    utterance: str
    user: str = "engineer"
    rig: str = ""  # 本地页面所在的台架
    emit: Callable[[str, dict], Awaitable[None]] | None = None  # 推界面消息（卡片）
    state: dict = field(default_factory=dict)  # 会话级暂存：pending_plan / pending_cancel


@dataclass
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[ToolCtx, Any], Awaitable[dict]]
    needs_confirm: bool = False
    data_label: str = "internal"

    def schema(self) -> dict:
        s = self.args.model_json_schema()
        s.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": s}


# ---------- 参数 ----------
class ReadCaseArgs(BaseModel):
    query: str = Field(description="用例编号（如 TC-DISP-012）或功能描述（如：显示设置 自动亮度）")


class PlanArgs(BaseModel):
    case_id: str = Field(description="read_case 找到的用例编号")
    rig: str = Field(description="台架编号，如 B2；用户没说时用默认台架")
    goal: str = Field("", description="一句话测试目标，可省略")


class CancelArgs(BaseModel):
    task_id: str = Field(description="要撤销的任务号")


class StatusArgs(BaseModel):
    task_id: str = Field("", description="任务号，留空查最近任务")


# ---------- 实现 ----------
async def read_case(ctx: ToolCtx, a: ReadCaseArgs) -> dict:
    repo = get_cases()
    q = a.query.strip()
    case, src = await repo.get(q)
    cases = [case] if case else (await repo.search(q))[0]
    if not cases:
        return {"found": False, "source": src, "note": "用例库中未找到匹配用例，请让用户提供用例编号"}
    c = cases[0]
    if ctx.emit:
        await ctx.emit("case.card", {"case": c.model_dump(), "source": src})
    ctx.state["last_case"] = c.id
    return {"found": True, "source": src, "case": {"id": c.id, "title": c.title, "module": c.module,
                                                   "steps": c.steps, "expected": c.expected},
            "others": [x.id + " " + x.title for x in cases[1:]]}


async def plan_task(ctx: ToolCtx, a: PlanArgs) -> dict:
    case, src = await get_cases().get(a.case_id)
    if not case:
        return {"ok": False, "error": f"用例 {a.case_id} 不存在，先调用 read_case"}
    rig = (a.rig or "").upper().replace("台架", "").strip() or ctx.rig or next(iter(rig_profiles()))
    if rig not in rig_profiles():
        return {"ok": False, "error": f"台架 {rig} 不存在"}
    tid = f"T-{time.strftime('%m%d')}-{int(time.time() * 10) % 10000:04d}"
    lc = get_caselib().get_case(case.id)
    if lc and lc.plan_status == "ready" and lc.review_status in IN_LIBRARY:
        return await _plan_replay(ctx, tid, rig, case)
    summary = ["回到桌面"] + case.steps + ["截图并采集日志", "与用例预期比对"]
    plan = ExploreTask(
        id=tid, origin=Origin(utterance=ctx.utterance, session=ctx.session, user=ctx.user,
                              model=get_gateway().chat_model),
        case_ref=CaseRef(source=case.source, case_id=case.id, expected=case.expected),
        target=Target(rig=rig, package=case.package),
        steps=[PlanStep(type="ai_explore", goal=a.goal or f"{case.title}：" + " → ".join(case.steps), max_steps=8)],
        summary=summary)
    errs = validate_plan(plan)
    if errs:
        return {"ok": False, "error": "；".join(errs)}
    ctx.state["pending_plan"] = plan
    if ctx.emit:
        await ctx.emit("confirm.request", {"kind": "plan", "plan": plan.model_dump(by_alias=True)})
    # 计划复述由系统按模板生成（confirm_gate 口径固定），不再交给模型自由发挥
    recap = (f"计划如下：在台架 {rig} 用 AI 探索执行，{'、'.join(case.steps)}，截图后和用例 {case.id} 的预期比对，"
             f"一共 {len(summary)} 步。请说“确认”开始执行。")
    return {"ok": True, "status": "待确认", "task_id": tid, "rig": rig, "case_id": case.id, "steps": summary,
            "final_reply": recap}


async def _plan_replay(ctx: ToolCtx, tid: str, rig: str, case) -> dict:
    """用例库里已有测试代码：回放 TaskPlan，不走 AI 探索。同样经过 confirm_gate。"""
    model = rig_profiles()[rig].model
    report = dry_run(case.id, rig_capabilities(rig), model)
    if not report["ok"]:
        why = "；".join(report["errors"])
        return {"ok": False, "error": f"{case.id} 的测试代码暂时不能在台架 {rig} 执行：{why}",
                "final_reply": f"{case.id} 暂时不能执行：{why[:60]}。"}
    doc, lc = load_plan(case.id, model)
    summary = [f"{s.get('action')} {_target(s)}".strip() for s in doc.get("steps") or []]
    plan = ExploreTask(
        id=tid, origin=Origin(utterance=ctx.utterance, session=ctx.session, user=ctx.user,
                              model=get_gateway().chat_model),
        case_ref=CaseRef(source=case.source, case_id=case.id, expected=case.expected),
        target=Target(rig=rig, package=case.package),
        steps=[PlanStep(type="taskplan", case_id=case.id, goal=case.title)], summary=summary)
    errs = validate_plan(plan)
    if errs:
        return {"ok": False, "error": "；".join(errs)}
    ctx.state["pending_plan"] = plan
    if ctx.emit:
        await ctx.emit("confirm.request", {"kind": "plan", "plan": plan.model_dump(by_alias=True)})
    recap = (f"计划如下：在台架 {rig} 回放 {case.id} 的测试代码（版本 {lc.plan_version}），共 {len(summary)} 步、"
             f"{len(doc.get('assertions') or [])} 个断言。请说“确认”开始执行。")
    return {"ok": True, "status": "待确认", "task_id": tid, "rig": rig, "case_id": case.id, "steps": summary,
            "final_reply": recap}


def _target(s: dict) -> str:
    sel = (s.get("target") or {}).get("selector") or {}
    return sel.get("text") or s.get("key") or (s.get("until") or {}).get("text", "")


async def cancel_task(ctx: ToolCtx, a: CancelArgs) -> dict:
    st = get_service().tasks.get(a.task_id)
    if not st:
        return {"ok": False, "error": f"任务 {a.task_id} 不存在"}
    ctx.state["pending_cancel"] = a.task_id
    if ctx.emit:
        await ctx.emit("confirm.request", {"kind": "cancel", "task_id": a.task_id})
    return {"ok": True, "status": "待确认", "final_reply": f"确定要撤销任务 {a.task_id} 吗？请说“确认”。"}


async def query_status(ctx: ToolCtx, a: StatusArgs) -> dict:
    svc = get_service()
    tasks = svc.summary()
    if a.task_id:
        tasks = [t for t in tasks if t["task_id"] == a.task_id]
    from ..store import get_store
    hist = get_store().list_tasks(5) if not tasks else []
    return {"tasks": tasks[:5], "history": hist,
            "pending_reviews": [{"id": d.id, "summary": d.summary} for d in svc.pending_reviews()]}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("read_case", "从用例库读取测试用例，取“预期结果”作为判定期望。", ReadCaseArgs, read_case),
    Tool("plan_task", "根据用例生成结构化测试计划 TaskPlan（AI 探索执行）。生成后进入待确认，必须由用户确认才会执行。",
         PlanArgs, plan_task, needs_confirm=True),
    Tool("cancel_task", "撤销一个测试任务（需用户确认）。", CancelArgs, cancel_task, needs_confirm=True),
    Tool("query_status", "查询测试任务状态、结果与待审核缺陷候选。", StatusArgs, query_status),
]}


def tools_for(authed: bool) -> list[Tool]:
    """测试工具只给登录的台架页面用户（云端用例工作台不调台架）。"""
    return list(TOOLS.values()) if authed else []
