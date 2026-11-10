"""装载前校验：云端下发前校验过一次，台架按本机动作库、评估器与音频缓存再校验一次。"""
from __future__ import annotations

from typing import Any

from cabin_common.schema import TestCase
from cabin_common.taskplan import validate

from .. import assets
from .actions import registered as actions
from .evaluators import registered as evaluators


def _all_steps(plan: dict) -> list[dict]:
    return (plan.get("setup") or []) + (plan.get("steps") or []) + (plan.get("teardown") or [])


def check(plan: dict, caps: set[str], case: TestCase | None = None) -> dict[str, Any]:
    """返回 {reason, errors, warnings}。reason 为空表示可执行。"""
    errs, warns = validate(plan, case)
    acts, evals = actions(), evaluators()
    missing = sorted(set(plan.get("requires") or []) - caps)
    unknown = sorted({s.get("action", "") for s in _all_steps(plan) if s.get("action") not in acts})
    no_eval = sorted({a.get("type", "") for a in plan.get("assertions") or [] if a.get("type") not in evals})
    lost = assets.missing(plan)
    lost_img = assets.missing_images(plan)
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
        errors.append(f"本机动作库未实现：{', '.join(unknown)}")
    if no_eval:
        errors.append(f"本机没有评估器：{', '.join(no_eval)}")
    return {"reason": reason, "errors": errors, "warnings": warns}
