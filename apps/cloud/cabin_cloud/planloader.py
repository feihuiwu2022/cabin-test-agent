"""装载与校验 TaskPlan；干跑（PF-RUN-11）：逐步校验动作、参数、音频，不操作车机。"""
from __future__ import annotations

from typing import Any

import yaml

from cabin_common.taskplan import ACTIONS, ASSERT_TYPES

from . import audioasset, imgasset, planner
from .caselib import get_caselib
from .config import get_settings
from .planvars import ResolveError, resolve
from .precommands import expand
from cabin_common.schema import TestCase


class PlanLoadError(ValueError):
    def __init__(self, msg: str, reason: str = "plan_invalid"):
        super().__init__(msg)
        self.reason = reason  # plan_invalid | not_applicable | skip


def load(case_id: str, model: str | None = None, rev: int | None = None) -> tuple[dict, TestCase]:
    """装载测试代码并按台架车型代入（设计文档 6.6）。model 缺省为本台架车型（CABIN_RIG_MODEL）。"""
    case = get_caselib().get_case(case_id, rev)
    text = planner.read_plan(case_id, rev)
    if not case or text is None:
        raise PlanLoadError(f"{case_id} 尚无测试代码")
    plan = yaml.safe_load(text)
    if not isinstance(plan, dict):
        raise PlanLoadError(f"{case_id} 的 TaskPlan 不是合法 YAML 对象")
    try:
        plan = expand(resolve(plan, model or get_settings().rig_model), model or get_settings().rig_model)
    except ResolveError as e:
        raise PlanLoadError(str(e), e.reason) from e
    return plan, case


def check(plan: dict, caps: set[str], case: TestCase | None = None) -> dict[str, Any]:
    """返回 {reason, errors, warnings}。reason 为空表示可执行。"""
    errs, warns = planner.validate(plan, case)
    missing = sorted(set(plan.get("requires") or []) - caps)
    unknown = sorted({s.get("action", "") for s in _all_steps(plan) if s.get("action") not in ACTIONS})
    no_eval = sorted({a.get("type", "") for a in plan.get("assertions") or [] if a.get("type") not in ASSERT_TYPES})
    lost = audioasset.missing(plan)
    lost_img = imgasset.missing(plan)
    if errs or lost or lost_img:
        reason = "plan_invalid"
    elif missing or unknown or no_eval:
        reason = "capability"
    else:
        reason = ""
    errors = list(errs) + ([f"测试音频未合成或缺失：{', '.join(lost)}"] if lost else []) + \
        ([f"图标模板缺失：{', '.join(lost_img)}"] if lost_img else [])
    if missing:
        errors.append(f"台架缺少能力：{', '.join(missing)}")
    if unknown:
        errors.append(f"动作库没有：{', '.join(unknown)}")
    if no_eval:
        errors.append(f"没有评估器：{', '.join(no_eval)}")
    return {"reason": reason, "errors": errors, "warnings": warns}


def _all_steps(plan: dict) -> list[dict]:
    return (plan.get("setup") or []) + (plan.get("steps") or []) + (plan.get("teardown") or [])


def dry_run(case_id: str, caps: set[str], model: str | None = None, rev: int | None = None) -> dict:
    """干跑报告：不碰设备。"""
    try:
        plan, case = load(case_id, model, rev)
    except PlanLoadError as e:
        return {"case_id": case_id, "ok": False, "reason": e.reason, "errors": [str(e)], "warnings": []}
    r = check(plan, caps, case)
    return {"case_id": case_id, "case_rev": case.case_rev, "model": plan.get("resolved_model", ""), "title": case.title,
            "plan_version": case.plan_version, "ok": not r["reason"], **r,
            "requires": plan.get("requires"), "n_steps": len(plan.get("steps") or []),
            "steps": [{"id": s.get("id"), "action": s.get("action"), "ready": s.get("action") in ACTIONS}
                      for s in plan.get("steps") or []],
            "assertions": [{"id": a.get("id"), "type": a.get("type")} for a in plan.get("assertions") or []]}
