"""timing：两个时间点之差与上限比较。无播报时没有终点，由 voice 断言判失败。"""
from __future__ import annotations

from ..context import AssertResult
from ..evidence import Evidence
from . import EvalContext, evaluator


@evaluator("timing")
async def timing(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid, start, end = a.get("id", ""), a.get("from", "t0"), a.get("to", "tts_start")
    if start not in ev.marks or end not in ev.marks:
        return AssertResult(assertion_id=aid, type="timing", result="skipped", detail={"missing": [start, end]})
    ms = int((ev.marks[end] - ev.marks[start]) * 1000)
    limit = int(a.get("max_ms") or 0)
    return AssertResult(assertion_id=aid, type="timing", result="pass" if ms <= limit else "fail",
                        detail={"latency_ms": ms, "max_ms": limit, "from": start, "to": end})
