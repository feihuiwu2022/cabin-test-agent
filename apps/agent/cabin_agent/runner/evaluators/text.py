"""text：控件树文本匹配。注意控件树是数据层原文，看不出屏幕上的截断，这类判定用 vision。"""
from __future__ import annotations

from ..context import AssertResult
from ..evidence import Evidence
from . import EvalContext, evaluator, latest_ui


@evaluator("text")
async def text(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid, want = a.get("id", ""), str(a.get("expected") or "")
    exact = a.get("match") == "exact"
    ui = latest_ui(ev, a.get("after_step"))
    sel = (a.get("target") or {}).get("selector") or {}
    els = [e for e in ui["elements"] if not sel or sel.get("text") == e["text"] or sel.get("key") == e["key"]]
    texts = [t for e in els for t in (e["text"], e["desc"]) if t]
    ok = any(want == t if exact else want in t for t in texts)
    return AssertResult(assertion_id=aid, type="text", result="pass" if ok else "fail",
                        detail={"expected": want, "actual": texts[:20], "screen": ui["screen"]})
