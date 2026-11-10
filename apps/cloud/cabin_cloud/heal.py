"""自愈建议：回放定位全失败后，用失败现场让私有云多模态模型找出新控件，给出 YAML 修改建议。

- 台架定位失败阻塞时上传失败现场（fail-NN.png / fail-NN.ui.json），建议在云端算；
- 本次执行仍判阻塞（locator），建议不改变结论；
- 建议经测试工程师确认后才写回测试代码仓库（新提交）；拒绝的建议计入评估集。
- 只在回放结束后调用模型，回放过程本身不调模型。
"""
from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cabin_common.uimap import Decider, marks_from, marks_text, render_marks

from . import uimap

SYS_HEAL = """你是座舱测试机器人。一条自动化测试在某一步找不到要点的控件，可能是界面改版（控件 id 或文字变了、位置移动）。
截图里每个可点击控件框了红色编号。根据“原来要点的控件”和“这一步要做的事”，在当前界面里找出对应的控件。只输出 JSON：
{"action": "tap|fail", "mark": 编号, "reason": "一句话：为什么是它，或为什么找不到"}
找不到对应控件（例如当前根本不在该页面）时 action 为 fail，不要硬选。"""


@dataclass
class _El:
    """证据 ui.json 里的一行控件。"""
    text: str = ""
    desc: str = ""
    key: str = ""
    cls: str = ""
    clickable: bool = True
    checked: bool | None = None
    bounds: list[int] | None = None


def describe(target: dict) -> str:
    sel = target.get("selector") or {}
    return (sel.get("text") or target.get("ocr") or sel.get("desc") or sel.get("key") or sel.get("id")
            or target.get("image") or str(target.get("ratio") or target.get("fallback_xy") or ""))


def _fail_files(ev_dir: Path, step_id: int) -> tuple[bytes, dict]:
    png = ev_dir / f"fail-{step_id:02d}.png"
    ui = ev_dir / f"fail-{step_id:02d}.ui.json"
    if not png.exists() or not ui.exists():
        raise FileNotFoundError(f"证据目录里没有步骤 {step_id} 的失败现场（{png.name} / {ui.name}）")
    return png.read_bytes(), json.loads(ui.read_text("utf8"))


def failed_step(result: dict) -> int | None:
    """从执行结果里找定位失败的步骤。"""
    if result.get("block_reason") != "locator":
        return None
    fails = [s for s in result.get("steps") or [] if s.get("result") == "fail"]
    return int(fails[-1]["id"]) if fails else None


async def suggest(ev_dir: str | Path, plan: dict, step_id: int, *, case_step: str = "",
                  decider: Decider | None = None, session: str = "") -> dict[str, Any]:
    ev_dir = Path(ev_dir)
    step = next((s for s in plan.get("steps") or [] if s.get("id") == step_id), None)
    if not step or not step.get("target"):
        raise ValueError(f"TaskPlan 里没有带 target 的步骤 {step_id}")
    png, ui = _fail_files(ev_dir, step_id)
    els = [_El(text=e.get("text") or "", desc=e.get("desc") or "", key=e.get("key") or "", cls=e.get("cls") or "",
               clickable=e.get("clickable", True), checked=e.get("checked"), bounds=e["bounds"])
           for e in ui["elements"] if e.get("bounds")]
    from PIL import Image
    try:
        w, h = Image.open(io.BytesIO(png)).size
        marks = marks_from(els, (w, h))
        marked = render_marks(png, marks)
    except OSError as e:
        raise ValueError(f"步骤 {step_id} 的失败截图无法解析：{e}") from e
    old = step["target"]
    prompt = (f"原来要点的控件：{describe(old)}（定位写法 {json.dumps(old, ensure_ascii=False)}）\n"
              f"这一步要做的事：{case_step or step.get('note') or step.get('action')}\n当前页面：{ui.get('screen') or '—'}\n"
              f"可点击控件：\n{marks_text(marks)}")
    dec = decider or _HealDecider()
    ans = await dec.decide(prompt, marked, session)
    out: dict[str, Any] = {"step_id": step_id, "old_target": old, "reason": str(ans.get("reason") or "")[:160],
                           "at": time.time(), "status": "待确认"}
    k = ans.get("mark")
    if ans.get("action") != "tap" or not isinstance(k, int) or not 1 <= k <= len(marks):
        out.update(found=False)
    else:
        images: dict[str, dict] = {}
        c = marks[k - 1]
        new = uimap.target_from(c, images)
        if old.get("region"):
            new["region"] = old["region"]
        out.update(found=True, mark=k, control=c.model_dump(), new_target=new, images=images,
                   diff=_diff(step, new))
    (ev_dir / f"heal-{step_id:02d}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf8")
    (ev_dir / f"heal-{step_id:02d}.png").write_bytes(marked)
    return out


def load_suggestion(ev_dir: str | Path, step_id: int) -> dict | None:
    f = Path(ev_dir) / f"heal-{step_id:02d}.json"
    return json.loads(f.read_text("utf8")) if f.is_file() else None


def mark(ev_dir: str | Path, step_id: int, status: str, by: str) -> None:
    """记下建议的处理结果，避免同一条建议被重复写回。"""
    f = Path(ev_dir) / f"heal-{step_id:02d}.json"
    sug = json.loads(f.read_text("utf8"))
    sug.update(status=status, by=by, decided_at=time.time())
    f.write_text(json.dumps(sug, ensure_ascii=False, indent=1), "utf8")


def _diff(step: dict, new: dict) -> str:
    a = yaml.safe_dump({"target": step["target"]}, allow_unicode=True, sort_keys=False).splitlines()
    b = yaml.safe_dump({"target": new}, allow_unicode=True, sort_keys=False).splitlines()
    import difflib
    return "\n".join(difflib.unified_diff(a, b, f"步骤 {step['id']}（原）", f"步骤 {step['id']}（建议）", lineterm=""))


def apply_to_plan(plan: dict, suggestion: dict) -> dict:
    """把确认的建议写进 TaskPlan（返回新对象）；图标模板一并加入 images。"""
    if not suggestion.get("found"):
        raise ValueError("这条建议没有找到新控件，不能应用")
    p = json.loads(json.dumps(plan))
    for s in p.get("steps") or []:
        if s.get("id") == suggestion["step_id"]:
            s["target"] = suggestion["new_target"]
            break
    else:
        raise ValueError(f"TaskPlan 里没有步骤 {suggestion['step_id']}")
    if suggestion.get("images"):
        p.setdefault("images", {}).update(suggestion["images"])
    meta = p.setdefault("meta", {})
    meta["healed"] = [*(meta.get("healed") or []),
                      {"step_id": suggestion["step_id"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}]
    return p


async def confirm(case_id: str, suggestion: dict, by: str, ok: bool = True) -> dict:
    """人工确认：接受则写回测试代码仓库（新提交）；拒绝则计入评估集。"""
    from . import planner
    from .caselib import get_caselib
    from .store import get_store
    lib = get_caselib()
    c = lib.get_case(case_id)
    if not c:
        raise KeyError(f"用例 {case_id} 不存在")
    if not ok:
        get_store().x("INSERT INTO eval_set(defect_id, rule, reason, doc, at) VALUES(?,?,?,?,?)",
                      (case_id, "self-heal", suggestion.get("reason", "建议被拒绝"),
                       json.dumps(suggestion, ensure_ascii=False, default=str), time.time()))
        return {"case_id": case_id, "applied": False}
    text = planner.read_plan(case_id)
    if text is None:
        raise ValueError(f"{case_id} 没有测试代码")
    plan = apply_to_plan(yaml.safe_load(text), suggestion)
    version = await planner.save_plan(c, plan)
    c.plan_version = version
    lib.save_rev(c)
    get_store().append("system", "plan.healed", {"case_id": case_id, "step_id": suggestion["step_id"], "by": by,
                                                 "plan_version": version})
    return {"case_id": case_id, "applied": True, "plan_version": version}


class _HealDecider:
    async def decide(self, prompt: str, image: bytes, session: str) -> dict:
        from .modelgw import get_gateway
        return await get_gateway().vision_json(system=SYS_HEAL, prompt=prompt, images=[image], label="sensitive",
                                               source="heal.suggest", session=session, max_tokens=400)
