"""UI 地图：台架录制与云端确认、生成、自愈共用的数据模型与 SoM 编号。

- 录制在台架跑（要碰车机）：截图 → 控件编号 → 云端多模态模型只回编号 → 台架执行；录完上传云端待确认。
- 点击位置来自控件树或 OCR 框，可复现、可写进 YAML；回放不调模型。
- UI 地图按“应用 + 软件版本 + 车型”保存在云端。
"""
from __future__ import annotations

import hashlib
import io
import re
import time
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, Field

MAX_STEPS = 12
RE_DYNAMIC = re.compile(r"\d+([:.：]\d+)*|[℃°%]")  # 时钟、温度、百分比等会变的字不进页面签名


class UiControl(BaseModel):
    key: str = ""
    text: str = ""
    desc: str = ""
    cls: str = ""
    checkable: bool = False
    bounds: list[int]
    ratio: list[float]  # 中心点占屏幕宽高的比例
    icon_sha: str = ""  # 无文字控件的图标裁图（图像资产）
    source: Literal["control", "ocr"] = "control"

    def name(self) -> str:
        return self.text or self.desc or self.key.rsplit("/", 1)[-1] or f"@{self.ratio}"


class UiPage(BaseModel):
    sig: str
    screen: str = ""  # 前台页面 / Activity
    title: str = ""
    shot_sha: str = ""
    controls: list[UiControl] = Field(default_factory=list)


class UiEdge(BaseModel):
    src: str
    control: UiControl
    dst: str
    step: str = ""


class RecStep(BaseModel):
    n: int
    page: str
    action: Literal["tap", "back", "done", "fail"]
    mark: int | None = None
    control: UiControl | None = None
    case_step: int | None = None
    reason: str = ""
    marked_sha: str = ""  # 带编号的截图
    after_checked: bool | None = None  # 点开关后的状态（生成断言用，人工确认）


class Recording(BaseModel):
    rec_id: str
    case_id: str = ""
    rig_id: str = ""
    steps_text: list[str] = Field(default_factory=list)
    package: str
    sw_version: str = ""
    model: str = ""
    screen: list[int] = Field(default_factory=lambda: [0, 0])
    trace: list[RecStep] = Field(default_factory=list)
    pages: dict[str, UiPage] = Field(default_factory=dict)
    outcome: Literal["done", "fail", "max_steps", "error"] = "error"
    note: str = ""
    status: Literal["待确认", "已确认", "已驳回"] = "待确认"
    props: list[dict] = Field(default_factory=list)  # 人工确认时补的车辆属性断言 [{prop, expect}]
    by: str = ""
    at: float = Field(default_factory=time.time)

    def image_shas(self) -> set[str]:
        """录制引用的全部图像资产：页面截图、带编号截图、图标裁图。"""
        out = {p.shot_sha for p in self.pages.values()} | {s.marked_sha for s in self.trace}
        out |= {c.icon_sha for p in self.pages.values() for c in p.controls}
        out |= {s.control.icon_sha for s in self.trace if s.control}
        return {x for x in out if x}


class UiMap(BaseModel):
    package: str
    sw_version: str = ""
    model: str = ""
    screen: list[int] = Field(default_factory=lambda: [0, 0])
    pages: dict[str, UiPage] = Field(default_factory=dict)
    edges: list[UiEdge] = Field(default_factory=list)
    recordings: list[str] = Field(default_factory=list)
    updated: float = Field(default_factory=time.time)


class Element(Protocol):
    """控件树元素（台架 UIElement 或证据里的 ui.json 行）。"""
    text: str
    desc: str
    key: str
    cls: str
    clickable: bool
    checked: bool | None
    bounds: list[int]


class Decider(Protocol):
    """选编号的多模态模型：台架经云端接口调用，云端直接调模型网关；测试用脚本化的假模型。"""

    async def decide(self, prompt: str, image: bytes, session: str) -> dict: ...


def new_rec_id(case_id: str = "") -> str:
    return time.strftime("REC-%Y%m%d-%H%M%S-") + hashlib.sha1(f"{case_id}{time.time()}".encode()).hexdigest()[:4]


def page_sig(screen: str, els: Iterable[Element]) -> str:
    words = sorted({RE_DYNAMIC.sub("", e.key or e.text) for e in els if (e.key or e.text)} - {""})
    return hashlib.sha1((screen + "|" + "|".join(words)).encode()).hexdigest()[:10]


def _inside(inner: list[int], outer: list[int]) -> bool:
    return inner[0] >= outer[0] and inner[1] >= outer[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def marks_from(els: list[Element], size: tuple[int, int],
               ocr: Iterable[tuple[str, list[int], float]] = ()) -> list[UiControl]:
    """可点击控件编号；没文字的可点击控件借用框内的文字。控件树为空时用 OCR 文字框（台架本地算好传进来）编号。"""
    w, h = size

    def ctl(b: list[int], **kw: Any) -> UiControl:
        return UiControl(bounds=b, ratio=[round((b[0] + b[2]) / 2 / w, 4), round((b[1] + b[3]) / 2 / h, 4)], **kw)

    out: list[UiControl] = []
    texts = [e for e in els if e.text or e.desc]
    for e in els:
        l, t, r, b = e.bounds
        if not e.clickable or r - l < 4 or b - t < 4 or (r - l) * (b - t) > 0.8 * w * h:
            continue
        text = e.text or next((x.text for x in texts if x is not e and x.text and _inside(x.bounds, e.bounds)), "")
        out.append(ctl(e.bounds, key=e.key, text=text, desc=e.desc, cls=e.cls, checkable=e.checked is not None))
    if not out:
        for text, bbox, score in ocr:
            if score >= 0.6 and text.strip():
                out.append(ctl(bbox, text=text.strip(), source="ocr"))
    return out


def render_marks(png: bytes, marks: list[UiControl]) -> bytes:
    """截图上给每个可点击控件画红框和编号（Set-of-Mark）。"""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(io.BytesIO(png)).convert("RGB")
    dr = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    for i, m in enumerate(marks, 1):
        l, t, r, b = m.bounds
        dr.rectangle([l, t, r, b], outline=(255, 64, 64), width=2)
        tag = str(i)
        dr.rectangle([l, t, l + 10 * len(tag) + 6, t + 20], fill=(255, 64, 64))
        dr.text((l + 3, t + 1), tag, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def marks_text(marks: list[UiControl]) -> str:
    rows = []
    for i, m in enumerate(marks, 1):
        bits = [m.text or "（无文字）"]
        if m.desc:
            bits.append(f"说明:{m.desc}")
        if m.key:
            bits.append(f"id:{m.key.rsplit('/', 1)[-1]}")
        if m.checkable:
            bits.append("开关")
        rows.append(f"{i}. " + " · ".join(bits))
    return "\n".join(rows)


def record_prompt(steps: list[str], history: list[RecStep], marks: list[UiControl], screen: str) -> str:
    done = "\n".join(f"- 第 {h.n} 步：{h.action} {h.control.name() if h.control else ''}（{h.reason}）"
                     for h in history) or "（还没有操作）"
    return ("测试用例步骤：\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) +
            f"\n\n已执行：\n{done}\n\n当前页面：{screen or '—'}\n可点击控件：\n{marks_text(marks)}\n\n下一步做什么？")


def as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
