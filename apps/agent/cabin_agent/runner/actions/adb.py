"""ADB 通道：包装 DeviceAdapter（真实 ADB 与 SimCar 同一接口）。定位见 runner/locate.py（控件树 → OCR → 模板 → 坐标）。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from cabin_common.taskplan import VEHICLE_PROPS

from ...device.base import DeviceAdapter, UIElement
from .. import locate as loc
from ..context import ActionError, RunContext, StepResult
from . import action

SETTLE_S = 0.6
LAUNCH_TIMEOUT_S = 8
RE_PROP_VALUE = re.compile(r"mAreaId=(0x[0-9a-fA-F]+|\d+).*?mValue=([^,}\s]+)", re.S)


class AdbChannel:
    name = "adb"
    capabilities = {"adb"}

    def __init__(self, dev: DeviceAdapter):
        self.dev = dev
        self._cursor: Any = None

    async def start(self) -> None:
        await self.dev.start()
        _, self._cursor = await self.dev.logs(None)  # 只看本次执行的日志增量

    async def stop(self) -> None:
        """设备由台架管理，通道不负责关闭。"""

    async def after_step(self, ctx: RunContext, sr: StepResult, trace: bool) -> None:
        """每步结束：收日志增量；trace 时留一张轨迹截图给界面。"""
        text, self._cursor = await self.dev.logs(self._cursor)
        ctx.evidence.add_log(text or "")
        sr.screen = await self.dev.current_screen()
        if trace and not sr.shot:
            f = ctx.evidence.add(f"trace-{sr.id:02d}.png", "trace", await self.dev.screenshot("png"), step=sr.id)
            sr.shot = ctx.evidence.url(f)

    async def on_failure(self, ctx: RunContext, step: int) -> None:
        """失败现场：截图 + 控件树（PF-EVD-01）。"""
        await self.snapshot(ctx, step, prefix="fail")

    async def has_text(self, want: str) -> bool:
        return any(want in (e.text or "") or want in (e.desc or "") for e in await self.dev.dump_ui())

    async def matches(self, until: dict) -> bool:
        """等待条件：text 出现 / gone 消失 / selector（可带 checked）命中 / screen 前台包含。"""
        if until.get("screen"):  # 前台页面或包名
            return until["screen"] in (await self.dev.current_screen() or "") or \
                until["screen"] in (await self.dev.shell("dumpsys window | grep mCurrentFocus") or "")
        sel = until.get("selector") or {k: until[k] for k in ("key", "text", "desc") if until.get(k)}
        if "checked" in until:
            sel = {**sel, "checked": until["checked"]}
        if until.get("gone"):
            return not await self.has_text(until["gone"])
        if sel.get("key") or sel.get("desc") or "checked" in sel:
            return loc.match_selector(await self.dev.dump_ui(), sel) is not None
        return await self.has_text(sel.get("text", ""))

    async def locate(self, ctx: RunContext, target: dict, slider: bool = False) -> loc.Hit:
        return await loc.locate(self.dev, target, ctx.images, slider)

    async def snapshot(self, ctx: RunContext, step: int, prefix: str = "step") -> str:
        """截图 + 控件树 + 页面一起入证据，评估器不用再碰设备。"""
        png = await self.dev.screenshot("png")
        f = ctx.evidence.add(f"{prefix}-{step:02d}.png", "screenshot", png, step=step)
        els = await self.dev.dump_ui()
        screen = await self.dev.current_screen()
        doc = {"screen": screen, "elements": [{"id": e.id, "brief": e.brief(), "bounds": e.bounds, "text": e.text,
                                               "desc": e.desc, "key": e.key, "checked": e.checked, "cls": e.cls,
                                               "clickable": e.clickable, "truncated": e.truncated} for e in els]}
        ctx.evidence.add(f"{prefix}-{step:02d}.ui.json", "ui", json.dumps(doc, ensure_ascii=False).encode("utf8"),
                         step=step)
        return ctx.evidence.url(f)


async def _settle() -> None:
    await asyncio.sleep(SETTLE_S)


def _name(target: dict) -> str:
    return loc.describe(target)


def _note_hit(ctx: RunContext, step: dict, h: loc.Hit) -> None:
    """降级定位告警：没用上 TaskPlan 首选的定位层，说明界面可能改版。"""
    ctx.evidence.fact("locators", {**ctx.evidence.facts.get("locators", {}), str(step.get("id", "")): loc.hit_info(h)})
    if h.degraded:
        ctx.warnings.append(f"步骤 {step.get('id')}“{_name(step['target'])}”降级为{loc.LEVEL_CN[h.level]}定位"
                            f"（{'；'.join(h.tried)}），界面可能改版，建议更新测试代码")


@action("tap", channel="adb")
async def tap(ctx: RunContext, step: dict) -> dict:
    h = await ctx.adb.locate(ctx, step["target"])
    _note_hit(ctx, step, h)
    await ctx.adb.dev.tap(h.x, h.y)
    await _settle()
    return {"label": f"点击“{_name(step['target'])}”", "x": h.x, "y": h.y, "bbox": h.bbox, **loc.hit_info(h)}


def _end_point(el: UIElement | None, x: int, y: int, to: dict) -> tuple[int, int]:
    l, t, r, b = el.bounds if el else (x - 200, y - 200, x + 200, y + 200)
    if "ratio" in to:
        return int(l + float(to["ratio"]) * (r - l)), y
    dist = max(r - l, b - t, 200)
    dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[to.get("direction", "left")]
    return x + dx * dist, y + dy * dist


def _is_target(to: dict) -> bool:
    """to 是另一个控件 / 坐标（拖到那里），还是滑条比例 {ratio: 0~1} / 方向（拖多远）。
    定位层的 ratio 是屏幕比例 [x, y]，滑条的 ratio 是一个数，不能混。"""
    return any(to.get(k) for k in ("selector", "ocr", "image", "fallback_xy")) or isinstance(to.get("ratio"), list)


async def _drag(ctx: RunContext, step: dict, ms: int) -> dict:
    to = step["to"]
    h = await ctx.adb.locate(ctx, step["target"], slider=isinstance(to.get("ratio"), (int, float)))
    _note_hit(ctx, step, h)
    x, y, el = h.x, h.y, h.el
    if _is_target(to):
        h2 = await ctx.adb.locate(ctx, to)
        x2, y2 = h2.x, h2.y
    else:
        try:
            x2, y2 = _end_point(el, x, y, to)
        except KeyError as e:
            raise ActionError("plan_invalid", f"to.direction 不支持 {e}") from e
    await ctx.adb.dev.swipe(x, y, x2, y2, ms)
    await _settle()
    return {"label": f"{step['action']} “{_name(step['target'])}”", "x": x, "y": y, "bbox": h.bbox, **loc.hit_info(h)}


@action("swipe", channel="adb")
async def swipe(ctx: RunContext, step: dict) -> dict:
    return await _drag(ctx, step, 300)


@action("drag", channel="adb")
async def drag(ctx: RunContext, step: dict) -> dict:
    return await _drag(ctx, step, 800)


@action("key", channel="adb")
async def key(ctx: RunContext, step: dict) -> dict:
    await ctx.adb.dev.key(step["key"])
    await _settle()
    return {"label": {"home": "回到桌面", "back": "返回"}.get(step["key"], f"按键 {step['key']}")}


@action("input_text", channel="adb")
async def input_text(ctx: RunContext, step: dict) -> dict:
    await ctx.adb.dev.text(step["text"])
    await _settle()
    return {"label": f"输入“{step['text']}”"}


@action("screenshot", channel="adb")
async def screenshot(ctx: RunContext, step: dict) -> dict:
    url = await ctx.adb.snapshot(ctx, int(step.get("id") or 0))
    return {"label": "截图", "shot": url}


@action("read_value", channel="adb")
async def read_value(ctx: RunContext, step: dict) -> dict:
    """读系统值存为事实，由 value 断言判定。来源只允许 settings 和 getprop，静态校验已限定写法。"""
    kind, _, path = step["read"].partition(":")
    cmd = "settings get " + path.replace("/", " ", 1) if kind == "settings" else f"getprop {path}"
    got = (await ctx.adb.dev.shell(cmd)).strip()
    ctx.evidence.fact(step["save_as"], got)
    return {"label": f"读取 {path} = {got}"}


@action("shell", channel="adb")
async def shell(ctx: RunContext, step: dict) -> dict:
    cmd = step.get("cmd")
    if not cmd:  # 车型命令库未就绪：不阻塞，告警由静态校验给出
        note = step.get("note") or step.get("cmd_ref") or ""
        ctx.evidence.fact("setup_unmet", [*ctx.evidence.facts.get("setup_unmet", []), note])
        return {"label": f"前置条件（未自动设置）：{note[:24]}"}
    out = await ctx.adb.dev.shell(cmd)
    return {"label": f"shell {cmd[:24]}", "output": out[:500]}


# ---------- 应用启停（起点固定：测试从被测应用首页开始） ----------
@action("launch_app", channel="adb")
async def launch_app(ctx: RunContext, step: dict) -> dict:
    pkg = step["package"]
    if step.get("activity"):
        act = step["activity"] if "/" in step["activity"] else f"{pkg}/{step['activity']}"
        cmd = f"am start -W -n {act}"
    elif step.get("intent"):
        cmd = f"am start -W -a {step['intent']} -p {pkg}"
    else:
        cmd = f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"
    out = await ctx.adb.dev.shell(cmd)
    if re.search(r"Error|No activities found|does not exist|aborted", out or "", re.I):
        raise ActionError("launch", f"启动 {pkg} 失败：{(out or '').strip()[:120]}")
    end = time.time() + float(step.get("timeout_s") or LAUNCH_TIMEOUT_S)
    while time.time() < end:
        if pkg in (await ctx.adb.dev.shell("dumpsys window | grep mCurrentFocus") or ""):
            await _settle()
            return {"label": f"启动 {pkg}"}
        await asyncio.sleep(0.3)
    raise ActionError("launch", f"{pkg} 启动后 {step.get('timeout_s') or LAUNCH_TIMEOUT_S} 秒内没有到前台")


@action("close_app", channel="adb")
async def close_app(ctx: RunContext, step: dict) -> dict:
    await ctx.adb.dev.shell(f"am force-stop {step['package']}")
    await _settle()
    return {"label": f"关闭 {step['package']}"}


# ---------- 车辆属性（Android Automotive VHAL，双线判定的“读状态”一线） ----------
def prop_id(name: Any) -> int:
    s = str(name).strip()
    if s.upper() in VEHICLE_PROPS:
        return VEHICLE_PROPS[s.upper()]
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        raise ActionError("plan_invalid", f"未知车辆属性 {name}（可用：{', '.join(VEHICLE_PROPS)} 或数字 ID）") from None


def parse_prop(out: str) -> dict[str, str]:
    """cmd car_service get-property-value 输出 → {区域: 值}。"""
    return {(str(int(a, 16)) if a.lower().startswith("0x") else a): v for a, v in RE_PROP_VALUE.findall(out or "")}


@action("read_prop", channel="adb")
async def read_prop(ctx: RunContext, step: dict) -> dict:
    pid = prop_id(step["prop"])
    area = step.get("area")
    cmd = f"cmd car_service get-property-value {pid}" + (f" {int(str(area), 0)}" if area not in (None, "") else "")
    if step.get("delay_s"):  # 车辆状态比界面晚生效时等一下
        await asyncio.sleep(float(step["delay_s"]))
    out = await ctx.adb.dev.shell(cmd)
    vals = parse_prop(out)
    if not vals:
        raise ActionError("capability", f"读不到车辆属性 {step['prop']}（需要 Android Automotive 车机）：{(out or '').strip()[:120]}")
    name = str(step.get("save_as") or step["prop"])
    ctx.evidence.fact(f"prop:{name}", {"prop": str(step["prop"]), "id": hex(pid), "values": vals, "raw": out[:2000]})
    return {"label": f"读车辆属性 {step['prop']} = {'、'.join(sorted(set(vals.values())))}"}
