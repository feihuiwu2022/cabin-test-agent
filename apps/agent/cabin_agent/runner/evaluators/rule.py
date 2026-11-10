"""rule：本地规则判定。"""
from __future__ import annotations

from cabin_common.crash import RULES, crash_frames
from ..context import AssertResult
from ..evidence import Evidence
from . import EvalContext, evaluator


@evaluator("rule")
async def rule(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid = a.get("id", "")
    if a.get("rule") != "no_crash":
        return AssertResult(assertion_id=aid, type="rule", result="error", detail={"error": f"未知规则 {a.get('rule')}"})
    for rid, kind, rx in RULES:
        for m in rx.finditer(ev.log_text):
            pkg = m.group("pkg")
            if ctx.packages and pkg not in ctx.packages:
                continue
            seg = ev.log_text[m.start(): m.start() + 1500]
            return AssertResult(assertion_id=aid, type="rule", result="fail",
                                detail={"rule": rid, "kind": kind, "package": pkg, "log_excerpt": seg,
                                        "frames": crash_frames(seg)})
    return AssertResult(assertion_id=aid, type="rule", result="pass")
