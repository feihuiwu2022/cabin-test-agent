"""状态断言：双线判定——看屏幕（ui_state）+ 读车辆真实状态（vehicle_prop）。

ui_state：读截图步骤存下的控件树，判开关状态 / 文字 / 存在与否。
vehicle_prop：读 read_prop 步骤存下的车辆属性值（Android Automotive VHAL）。
两条都过才算通过：界面显示“已开”但车辆属性仍为关，判失败（显示与执行不一致）。
"""
from __future__ import annotations

from ..locate import match_selector
from ...device.base import UIElement
from ..context import AssertResult
from ..evidence import Evidence
from . import EvalContext, evaluator, latest_ui

TRUE = {"true", "1", "on", "yes", "开"}
FALSE = {"false", "0", "off", "no", "关"}


def _norm(v) -> object:
    s = str(v).strip().lower()
    if s in TRUE:
        return True
    if s in FALSE:
        return False
    try:
        return float(s)
    except ValueError:
        return s


@evaluator("ui_state")
async def ui_state(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid = a.get("id", "")
    ui = latest_ui(ev, a.get("after_step"))
    els = [UIElement(**{k: e.get(k) for k in ("id", "text", "desc", "key", "checked", "bounds")
                        if e.get(k) is not None}) for e in ui["elements"] if e.get("bounds")]
    sel = {k: v for k, v in ((a.get("target") or {}).get("selector") or {}).items() if k != "checked"}
    el = match_selector(els, sel)
    detail = {"selector": sel, "screen": ui["screen"], "found": el.brief() if el else ""}
    if "exists" in a and a["exists"] is False:
        return AssertResult(assertion_id=aid, type="ui_state", result="pass" if el is None else "fail", detail=detail)
    if el is None:
        return AssertResult(assertion_id=aid, type="ui_state", result="fail", detail={**detail, "error": "界面上没有该控件"})
    fails = []
    if "checked" in a:
        detail["checked"] = {"expected": bool(a["checked"]), "actual": el.checked}
        if el.checked is None:
            fails.append("控件不是开关（没有 checked 状态）")
        elif el.checked != bool(a["checked"]):
            fails.append(f"开关为{'开' if el.checked else '关'}，期望{'开' if a['checked'] else '关'}")
    if a.get("text"):
        got = f"{el.text} {el.desc}"
        detail["text"] = {"expected": a["text"], "actual": got.strip()}
        if a["text"] not in got:
            fails.append(f"文字“{got.strip()[:30]}”不含“{a['text']}”")
    if fails:
        detail["reason"] = "；".join(fails)
    return AssertResult(assertion_id=aid, type="ui_state", result="fail" if fails else "pass", detail=detail)


@evaluator("vehicle_prop")
async def vehicle_prop(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid = a.get("id", "")
    name = str(a.get("source") or a.get("prop") or "")
    got = ev.facts.get(f"prop:{name}")
    if not got:
        return AssertResult(assertion_id=aid, type="vehicle_prop", result="error",
                            detail={"error": f"没有读到车辆属性 {name}（缺少 read_prop 步骤或读取失败）"})
    vals: dict[str, str] = got["values"]
    if a.get("area") not in (None, ""):
        area = str(int(str(a["area"]), 0))
        vals = {k: v for k, v in vals.items() if k == area}
    want = _norm(a.get("expect"))
    tol = float(a.get("tol") or 0)

    def ok(v: str) -> bool:
        g = _norm(v)
        if isinstance(want, float) and isinstance(g, float):
            return abs(g - want) <= tol
        return g == want

    hits = {k: ok(v) for k, v in vals.items()}
    passed = bool(hits) and (any(hits.values()) if a.get("match") == "any" else all(hits.values()))
    detail = {"prop": got["prop"], "id": got["id"], "expected": a.get("expect"), "actual": vals,
              "match": a.get("match") or "all"}
    if not passed:
        detail["reason"] = (f"车辆属性 {got['prop']} 为 {'、'.join(f'{k}:{v}' for k, v in vals.items()) or '无'}，"
                            f"期望 {str(a.get('expect')).lower() if isinstance(a.get('expect'), bool) else a.get('expect')}")
    return AssertResult(assertion_id=aid, type="vehicle_prop", result="pass" if passed else "fail", detail=detail)
