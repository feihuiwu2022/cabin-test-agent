"""value：系统读到的值与期望比较。视觉判定分不出 255 和 250，能读到的值都走这里。"""
from __future__ import annotations

from cabin_common.taskplan import compare

from ..context import AssertResult
from ..evidence import Evidence
from . import EvalContext, evaluator


@evaluator("value")
async def value(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid, key, op, want = a.get("id", ""), a.get("key", ""), a.get("op", "eq"), a.get("expected")
    if key not in ev.facts:
        return AssertResult(assertion_id=aid, type="value", result="error", detail={"error": f"没有读到 {key}"})
    got = ev.facts[key]
    return AssertResult(assertion_id=aid, type="value", result="pass" if compare(op, got, want) else "fail",
                        detail={"key": key, "op": op, "expected": want, "actual": got})
