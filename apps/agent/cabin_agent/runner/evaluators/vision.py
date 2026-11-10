"""vision：期望 vs 截图，经 JudgeClient（A-10）判定。"""
from __future__ import annotations

from ..context import AssertResult
from ..evidence import Evidence
from cabin_common.judging import VisionReq
from . import EvalContext, evaluator, latest_ui

MAX_IMAGES = 6


@evaluator("vision")
async def vision(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid, step = a.get("id", ""), a.get("after_step")
    shot = ev.find("screenshot", step)
    if not shot:
        return AssertResult(assertion_id=aid, type="vision", result="error", detail={"error": "没有可判定的截图"})
    if ctx.judge is None:
        return AssertResult(assertion_id=aid, type="vision", result="error", detail={"error": "未配置判定服务"})
    ui = latest_ui(ev, step)
    try:
        v = await ctx.judge.vision(VisionReq(
            case_id=ctx.case_id, expected=str(a.get("expected", "")), screen=ui["screen"],
            images=[ev.read(f) for f in ev.find_all("screenshot", step)[-MAX_IMAGES:]],
            elements="\n".join(e["brief"] for e in ui["elements"]), region=a.get("region"), session=ctx.session))
    except Exception as e:  # 判定不可用不等于被测功能失败：结论 blocked，证据留着可重判
        return AssertResult(assertion_id=aid, type="vision", result="error",
                            detail={"error": f"{type(e).__name__}: {e}", "actual_shot": ev.url(shot)})
    eid = v.get("element_id")
    bbox = next((e["bounds"] for e in ui["elements"] if e["id"] == eid), None) if eid is not None else None
    return AssertResult(assertion_id=aid, type="vision", confidence=float(v.get("confidence") or 0),
                        result="pass" if v.get("consistent", True) else "fail",
                        detail={"verdict": v, "bbox": bbox, "actual_shot": ev.url(shot), "screen": ui["screen"],
                                "expected": a.get("expected", "")})
