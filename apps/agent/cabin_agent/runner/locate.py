"""多级定位（台架端 PRD 触屏执行 4.1）：控件树 → OCR → 图标模板 → 相对坐标 → 绝对坐标。

target 写法（任选几项，按上面的顺序尝试，先命中先用）：
  selector: {key: "com.x:id/ac_switch", text: "空调", desc: "…"}   控件树：resource-id 精确 > 文字
  ocr: "空调"                                                     截图 OCR 找文字
  image: "icon_hvac.png"                                          plan.images 里的图标模板
  ratio: [0.12, 0.40]                                             屏幕宽高比例
  fallback_xy: [150, 220]                                         绝对像素（旧写法，最后兜底）
  region: [0, 0, 0.3, 1]                                          OCR / 模板只在这块区域找（比例）
回放不调大模型；命中层级写进步骤结果，降到 TaskPlan 首选层之后的层会告警（界面可能改版）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..device.base import DeviceAdapter, UIElement
from . import vision_local
from .context import LocatorError

SLIDER_CLS = re.compile(r"SeekBar|Slider", re.I)
LEVELS = ("control", "ocr", "image", "ratio", "xy")
LEVEL_CN = {"control": "控件树", "ocr": "OCR", "image": "图标模板", "ratio": "相对坐标", "xy": "绝对坐标"}
_FIELD = {"control": "selector", "ocr": "ocr", "image": "image", "ratio": "ratio", "xy": "fallback_xy"}


@dataclass
class Hit:
    x: int
    y: int
    level: str
    el: UIElement | None = None
    bbox: list[int] | None = None
    score: float = 1.0
    tried: list[str] = field(default_factory=list)  # 没命中的层及原因

    @property
    def degraded(self) -> bool:
        return bool(self.tried)


def declared(target: dict) -> list[str]:
    return [lv for lv in LEVELS if target.get(_FIELD[lv]) not in (None, "", {}, [])]


def match_selector(els: list[UIElement], sel: dict, slider: bool = False) -> UIElement | None:
    """控件树匹配：resource-id 精确 > 文字相等 > id 尾段 > 文字包含 > 说明包含；带 checked 时还要状态一致。
    slider：目标是滑条。滑条本身常没有文字，名字在说明里，同名标题不能抢先命中。"""
    key, text, desc = sel.get("key", "") or sel.get("id", ""), sel.get("text", ""), sel.get("desc", "")
    want_checked = sel.get("checked")

    def ok(e: UIElement) -> bool:
        return want_checked is None or e.checked == bool(want_checked)

    if key:
        hit = next((e for e in els if e.key == key and ok(e)), None)
        if hit:
            return hit
    if text and slider:
        hit = next((e for e in els if SLIDER_CLS.search(e.cls or "") and text in f"{e.text}{e.desc}"), None)
        if hit:
            return hit
    if text:
        for m in (lambda e: e.text == text, lambda e: e.key == text or e.key.endswith(f"/{text}") or e.key.endswith(f"-{text}"),
                  lambda e: text in (e.text or ""), lambda e: text in (e.desc or "")):
            hit = next((e for e in els if m(e) and ok(e)), None)
            if hit:
                return hit
    if desc:
        return next((e for e in els if desc in (e.desc or "") and ok(e)), None)
    return None


def _region(target: dict, w: int, h: int) -> list[int] | None:
    r = target.get("region")
    if not r or len(r) != 4:
        return None
    if all(0 <= float(v) <= 1 for v in r):
        return [int(float(r[0]) * w), int(float(r[1]) * h), int(float(r[2]) * w), int(float(r[3]) * h)]
    return [int(v) for v in r]


async def locate(dev: DeviceAdapter, target: dict, images: dict[str, bytes] | None = None, slider: bool = False) -> Hit:
    tried: list[str] = []
    png: bytes | None = None
    size: tuple[int, int] | None = None

    async def shot() -> bytes:
        nonlocal png
        if png is None:
            png = await dev.screenshot("png")
        return png

    async def wh() -> tuple[int, int]:
        nonlocal size
        if size is None:
            size = await dev.screen_size()
        return size

    for lv in declared(target):
        v = target[_FIELD[lv]]
        if lv == "control":
            el = match_selector(await dev.dump_ui(), v, slider)
            if el:
                return Hit(*el.center, level=lv, el=el, bbox=el.bounds, tried=tried)
            tried.append(f"控件树未找到 {v}")
        elif lv == "ocr":
            why = vision_local.ocr_unavailable()
            if why:
                tried.append(f"OCR 不可用（{why}）")
                continue
            f = vision_local.ocr_find(await shot(), str(v), _region(target, *(await wh())))
            if f:
                return Hit(*f.center, level=lv, bbox=f.bbox, score=f.score, tried=tried)
            tried.append(f"OCR 未找到“{v}”")
        elif lv == "image":
            why = vision_local.template_unavailable()
            data = (images or {}).get(str(v))
            if why or data is None:
                tried.append(f"图标模板不可用（{why or f'plan.images 没有 {v}'}）")
                continue
            f = vision_local.template_find(await shot(), data, float(target.get("threshold") or 0.85),
                                           _region(target, *(await wh())))
            if f:
                return Hit(*f.center, level=lv, bbox=f.bbox, score=f.score, tried=tried)
            tried.append(f"图标模板未匹配 {v}")
        elif lv == "ratio":
            w, h = await wh()
            return Hit(int(float(v[0]) * w), int(float(v[1]) * h), level=lv, tried=tried)
        elif lv == "xy":
            return Hit(int(v[0]), int(v[1]), level=lv, tried=tried)
    raise LocatorError("；".join(tried) or f"target 没有可用的定位方式：{target}")


def describe(target: dict) -> str:
    sel = target.get("selector") or {}
    return (sel.get("text") or target.get("ocr") or sel.get("desc") or sel.get("key") or sel.get("id")
            or target.get("image") or str(target.get("ratio") or target.get("fallback_xy") or ""))


def hit_info(h: Hit) -> dict[str, Any]:
    return {"locator": h.level, "locator_cn": LEVEL_CN[h.level], "score": round(h.score, 3), "tried": h.tried}
