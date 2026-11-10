"""UI 地图的云端部分：选编号、收录制、回看确认、并入地图、生成测试代码。

录制本身在台架跑（要碰车机）；云端持有模型凭证，只在这里调多模态模型。录制经人工确认后，才用于生成测试代码，
回放不调模型。UI 地图按“应用 + 软件版本 + 车型”保存。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from cabin_common.uimap import (MAX_STEPS, Decider, RecStep, Recording, UiControl, UiEdge, UiMap,  # noqa: F401
                               UiPage, marks_from, marks_text, render_marks)

from . import imgasset
from .config import get_settings


# ---------------------------------------------------------------- 存储
def maps_dir() -> Path:
    d = get_settings().data_dir / "uimaps"
    (d / "recordings").mkdir(parents=True, exist_ok=True)
    return d


def _safe(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s or "_")


def map_path(package: str, sw: str, model: str) -> Path:
    return maps_dir() / _safe(package) / f"{_safe(sw or 'any')}__{_safe(model or 'any')}.json"


def load_map(package: str, sw: str = "", model: str = "") -> UiMap | None:
    """精确版本优先；没有就取该应用该车型最近更新的一份（软件小版本间多数页面不变）。"""
    p = map_path(package, sw, model)
    if p.exists():
        return UiMap.model_validate_json(p.read_text("utf8"))
    cands = sorted((maps_dir() / _safe(package)).glob(f"*__{_safe(model or 'any')}.json"), key=lambda x: x.stat().st_mtime)
    if not cands:
        cands = sorted((maps_dir() / _safe(package)).glob("*.json"), key=lambda x: x.stat().st_mtime)
    return UiMap.model_validate_json(cands[-1].read_text("utf8")) if cands else None


def save_map(m: UiMap) -> Path:
    p = map_path(m.package, m.sw_version, m.model)
    p.parent.mkdir(parents=True, exist_ok=True)
    m.updated = time.time()
    p.write_text(m.model_dump_json(indent=1), "utf8")
    return p


def rec_path(rec_id: str) -> Path:
    return maps_dir() / "recordings" / f"{_safe(rec_id)}.json"


def save_recording(r: Recording) -> Path:
    p = rec_path(r.rec_id)
    p.write_text(r.model_dump_json(indent=1), "utf8")
    return p


def load_recording(rec_id: str) -> Recording | None:
    p = rec_path(rec_id)
    return Recording.model_validate_json(p.read_text("utf8")) if p.exists() else None


def recordings_for(case_id: str, status: str = "已确认") -> list[Recording]:
    out = []
    for p in (maps_dir() / "recordings").glob("*.json"):
        r = Recording.model_validate_json(p.read_text("utf8"))
        if r.case_id == case_id and (not status or r.status == status):
            out.append(r)
    return sorted(out, key=lambda r: r.at)


def confirm(rec_id: str, by: str, ok: bool = True, props: list[dict] | None = None) -> Recording:
    """人工确认录制：确认后并入 UI 地图，可用于生成测试代码；驳回的不并入。"""
    r = load_recording(rec_id)
    if not r:
        raise KeyError(f"录制 {rec_id} 不存在")
    r.status, r.by = ("已确认" if ok else "已驳回"), by
    if props is not None:
        r.props = props
    save_recording(r)
    if ok:
        merge(r)
    return r


def merge(r: Recording) -> UiMap:
    m = load_map(r.package, r.sw_version, r.model)
    if not m or (m.sw_version, m.model) != (r.sw_version, r.model):
        m = UiMap(package=r.package, sw_version=r.sw_version, model=r.model, screen=r.screen)
    for sig, pg in r.pages.items():
        m.pages[sig] = pg
    have = {(e.src, e.control.key, e.control.text, e.dst) for e in m.edges}
    nxt = {s.n: s for s in r.trace}
    for s in r.trace:
        if s.action == "tap" and s.control and (s.n + 1) in nxt:
            e = UiEdge(src=s.page, control=s.control, dst=nxt[s.n + 1].page,
                       step=r.steps_text[s.case_step - 1] if s.case_step and s.case_step <= len(r.steps_text) else "")
            if (e.src, e.control.key, e.control.text, e.dst) not in have:
                m.edges.append(e)
    if r.rec_id not in m.recordings:
        m.recordings.append(r.rec_id)
    save_map(m)
    return m


# ---------------------------------------------------------------- 选编号
SYS_SOM = """你是座舱测试机器人，在车机上按测试用例步骤操作。截图里每个可点击控件框了红色编号，下面也列出了编号对应的控件。
每次只决定下一步，只输出一个 JSON：
{"action": "tap|back|done|fail", "mark": 编号（tap 时必填）, "step": 正在执行的用例步骤序号, "reason": "一句话"}
- tap：点编号对应的控件；back：返回上一页；done：用例步骤已全部完成；fail：当前界面无法完成（说明原因）。
- 只能选列出的编号，不要编造坐标。
- 开关类步骤（打开 / 关闭某开关）：点开关本身或它所在的行；开关已经是要求的状态时不要点，直接视为该步完成。"""


class VlmDecider:
    """私有云多模态模型（经模型网关；截图按内部敏感数据标记）。台架录制时经云端接口调用。"""

    async def decide(self, prompt: str, image: bytes, session: str) -> dict:
        from .modelgw import get_gateway
        return await get_gateway().vision_json(system=SYS_SOM, prompt=prompt, images=[image], label="sensitive",
                                               source="uimap.record", session=session, max_tokens=600)



def accept_upload(rec: Recording, images: dict[str, bytes], rig_id: str) -> Recording:
    """收台架上传的录制：引用的截图要齐、哈希要对；状态一律重置为待确认。"""
    for sha, data in images.items():
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"截图 {sha[:12]} 哈希不符")
    lost = sorted(sha for sha in rec.image_shas() if sha not in images and not imgasset.exists(sha))
    if lost:
        raise ValueError(f"录制缺少截图：{', '.join(x[:12] for x in lost)}")
    if not re.fullmatch(r"REC-[\w-]+", rec.rec_id) or len(rec.trace) > MAX_STEPS * 2:
        raise ValueError("录制编号或步数不合法")
    for sha, data in images.items():
        imgasset.store(data)
    rec = rec.model_copy(update={"rig_id": rig_id, "status": "待确认", "by": "", "props": [], "at": time.time()})
    save_recording(rec)
    return rec


def list_recordings(status: str = "") -> list[dict]:
    out = []
    for p in sorted((maps_dir() / "recordings").glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        r = Recording.model_validate_json(p.read_text("utf8"))
        if not status or r.status == status:
            out.append({"rec_id": r.rec_id, "case_id": r.case_id, "package": r.package, "rig_id": r.rig_id,
                        "outcome": r.outcome, "status": r.status, "steps": len(r.trace), "at": r.at})
    return out


# ---------------------------------------------------------------- 生成测试代码
def target_from(c: UiControl, images: dict[str, dict]) -> dict:
    """按控件信息补全多级定位：id → 文字 → OCR → 图标 → 相对坐标（总是写，兜底）。"""
    t: dict[str, Any] = {}
    sel = {k: v for k, v in (("key", c.key), ("text", c.text)) if v}
    if sel and c.source == "control":
        t["selector"] = sel
    if c.text:
        t["ocr"] = c.text
    if c.icon_sha:
        name = f"icon_{c.icon_sha[:10]}.png"
        images[name] = {"sha256": c.icon_sha}
        t["image"] = name
    t["ratio"] = c.ratio
    return t


def plan_from_recording(r: Recording) -> dict:
    """确认过的录制 → TaskPlan 主体（setup 以外）：launch_app + 各步点击；点了开关就等状态、截图、加界面断言。"""
    if r.outcome != "done":
        raise ValueError(f"录制 {r.rec_id} 没有走完（{r.outcome}：{r.note}），不能生成测试代码")
    images: dict[str, dict] = {}
    steps: list[dict] = [{"action": "launch_app", "package": r.package}]
    asserts: list[dict] = []
    last_toggle: tuple[dict, bool] | None = None
    for s in r.trace:
        if s.action == "back":
            steps.append({"action": "key", "key": "back"})
        elif s.action == "tap" and s.control:
            steps.append({"action": "tap", "target": target_from(s.control, images)})
            if s.control.checkable and s.after_checked is not None and s.control.key:
                last_toggle = ({"key": s.control.key}, s.after_checked)
    if last_toggle:
        sel, on = last_toggle
        steps.append({"action": "wait", "until": {"selector": sel, "checked": on}, "timeout_s": 3})
    steps.append({"action": "screenshot", "domain": "center"})
    for i, s in enumerate(steps, 1):
        steps[i - 1] = {"id": i, **s}
    shot = len(steps)
    if last_toggle:
        asserts.append({"after_step": shot, "type": "ui_state", "target": {"selector": last_toggle[0]},
                        "checked": last_toggle[1]})
    for p in r.props:
        steps.append({"id": len(steps) + 1, "action": "read_prop", "prop": p["prop"], **({"delay_s": p["delay_s"]} if p.get("delay_s") else {})})
        asserts.append({"after_step": len(steps), "type": "vehicle_prop", "prop": p["prop"], "expect": p["expect"]})
    asserts.append({"type": "rule", "rule": "no_crash"})
    body: dict[str, Any] = {"steps": steps, "assertions": [{"id": f"a{i}", **a} for i, a in enumerate(asserts, 1)],
                            "teardown": [{"action": "close_app", "package": r.package}],
                            "meta": {"uimap_recording": r.rec_id, "sw_version": r.sw_version}}
    if images:
        body["images"] = images
    return body


def map_hint(m: UiMap, limit: int = 120) -> str:
    """给生成测试代码的模型看的控件清单：只能引用这里的控件。"""
    rows = []
    for pg in m.pages.values():
        names = [c.name() + ("（开关）" if c.checkable else "") for c in pg.controls]
        rows.append(f"页面「{pg.title or pg.screen}」：" + "、".join(names[:30]))
    return "\n".join(rows)[: limit * 40]


def enrich(steps: list[dict], m: UiMap) -> tuple[list[dict], list[str], dict[str, dict]]:
    """把模型写的按文字定位补全为多级定位；引用了地图里没有的控件时返回告警。"""
    by_text: dict[str, UiControl] = {}
    for pg in m.pages.values():
        for c in pg.controls:
            for k in (c.text, c.key, c.key.rsplit("/", 1)[-1] if c.key else ""):
                if k:
                    by_text.setdefault(k, c)
    warns: list[str] = []
    images: dict[str, dict] = {}
    for s in steps:
        tg = s.get("target") or {}
        sel = tg.get("selector") or {}
        name = sel.get("key") or sel.get("text") or tg.get("ocr")
        if s.get("action") not in ("tap", "swipe", "drag") or not name:
            continue
        c = by_text.get(name)
        if not c:
            warns.append(f"步骤 {s.get('id')}“{name}”不在 UI 地图里（地图可能过期，或模型编造了控件）")
            continue
        full = target_from(c, images)
        s["target"] = {**full, **{k: v for k, v in tg.items() if k == "region"}}
    return steps, warns, images


def report_html(r: Recording) -> str:
    """录制回看页：每步带编号截图、模型选择与理由，供人工确认。"""
    import base64
    from html import escape
    rows = []
    for s in r.trace:
        img = ""
        if s.marked_sha:
            try:
                img = f'<img src="data:image/png;base64,{base64.b64encode(imgasset.read_verified(s.marked_sha)).decode()}">'
            except imgasset.ImageAssetError:
                img = "（截图缺失）"
        what = f"点 {s.mark} 号：{escape(s.control.name())}" if s.action == "tap" and s.control else escape(s.action)
        chk = "" if s.after_checked is None else f"；点后开关为{'开' if s.after_checked else '关'}"
        rows.append(f"<section><h3>第 {s.n} 步 · {what}</h3><p>用例步骤 {s.case_step or '—'} · {escape(s.reason)}{chk}</p>{img}</section>")
    steps = "".join(f"<li>{escape(x)}</li>" for x in r.steps_text)
    return f"""<!doctype html><meta charset="utf-8"><title>录制 {r.rec_id}</title>
<style>body{{font-family:sans-serif;max-width:1100px;margin:24px auto;padding:0 16px}}img{{max-width:100%;border:1px solid #ccc}}
section{{margin:18px 0;padding-bottom:12px;border-bottom:1px solid #eee}}</style>
<h1>UI 地图录制 {r.rec_id}</h1><p>用例 {escape(r.case_id)} · 应用 {escape(r.package)} · 软件 {escape(r.sw_version or '—')} · 车型 {escape(r.model or '—')}
 · 结果 <b>{r.outcome}</b> {escape(r.note)} · 状态 {r.status}</p><ol>{steps}</ol>{''.join(rows)}"""
