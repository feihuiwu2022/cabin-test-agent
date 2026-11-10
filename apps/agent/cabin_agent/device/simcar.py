"""SimCar 适配器：用无头浏览器驱动模拟车机页面（无真实台架时的替身，接口与 ADB 一致）。"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from playwright.async_api import async_playwright

from ..config import RES_DIR
from .base import DeviceAdapter, Health, UIElement

SIMCAR_HTML = RES_DIR / "simcar" / "index.html"


class SimCarAdapter(DeviceAdapter):
    name = "simcar"
    voice_capable = True

    def __init__(self, bugs: str | None = None, ota: str = "C01-OTA-1.0.0", headless: bool = True):
        # CABIN_SIMCAR_BUGS 为空串时是无缺陷车机，用作判定反例
        self.bugs = os.getenv("CABIN_SIMCAR_BUGS", "trunc,crash") if bugs is None else bugs
        self.ota, self.headless = ota, headless
        self._pw = self._browser = self.page = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.page:
            return
        self._pw = await async_playwright().start()
        channel = os.getenv("CABIN_SIMCAR_CHANNEL", "chrome")  # 默认用本机 Chrome，免下载浏览器
        try:
            self._browser = await self._pw.chromium.launch(headless=self.headless, channel=channel)
        except Exception:
            self._browser = await self._pw.chromium.launch(headless=self.headless)
        self.page = await self._browser.new_page(viewport={"width": 1280, "height": 560}, device_scale_factor=1)
        await self.page.goto(f"{SIMCAR_HTML.as_uri()}?bugs={self.bugs}&ota={self.ota}")
        await self.page.wait_for_function("window.__simcar !== undefined")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._pw = self._browser = self.page = None

    async def _js(self, expr: str, arg: Any = None) -> Any:
        await self.start()
        async with self._lock:
            return await self.page.evaluate(expr, arg)

    async def health(self) -> Health:
        try:
            ota = await self._js("() => __simcar.getprop('ro.build.display.id')")
            model = await self._js("() => __simcar.getprop('ro.product.model')")
            bt = await self._js("() => __simcar.getprop('ro.build.type')")
            pkgs = await self._js("() => __simcar.packages()")
            return Health(online=True, ota=ota, model=model, build_type=bt, packages=pkgs, detail="SimCar 在线")
        except Exception as e:
            return Health(online=False, detail=str(e))

    async def shell(self, cmd: str) -> str:
        if cmd.startswith("getprop "):
            return await self._js("n => __simcar.getprop(n)", cmd.split(" ", 1)[1].strip())
        if cmd.startswith("dumpsys window"):
            return f"mCurrentFocus=Window{{{await self._js('() => __simcar.package()')}}}"
        m = re.fullmatch(r"settings (get|put) system screen_brightness(?: (\d+))?", cmd.strip())
        if m:  # 系统亮度 0~255，和真机 settings 命令一致
            v = await self._js("v => __simcar.brightness(v)", int(m.group(2)) if m.group(1) == "put" and m.group(2) else None)
            return "" if m.group(1) == "put" else str(v)
        m = re.fullmatch(r"cabin-hvac (on|off)(?: (\d+))?", cmd.strip())
        if m:  # SimCar 示例车型命令：设置空调前置状态
            await self._js("a => __simcar.hvacSet(a[0], a[1])", [m.group(1) == "on", int(m.group(2)) if m.group(2) else None])
            return "ok"
        m = re.match(r"(?:am start(?: -W)? -n ([\w.]+)/\S+|monkey -p ([\w.]+) )", cmd.strip())
        if m:
            return await self._js("p => __simcar.launch(p)", m.group(1) or m.group(2))
        m = re.match(r"am force-stop ([\w.]+)", cmd.strip())
        if m:
            return await self._js("p => __simcar.forceStop(p)", m.group(1))
        m = re.match(r"cmd car_service get-property-value (\d+|0x[0-9a-fA-F]+)", cmd.strip())
        if m:
            return await self._js("i => __simcar.prop(i)", int(m.group(1), 0))
        if cmd == "pm list packages":
            return "\n".join(f"package:{p}" for p in await self._js("() => __simcar.packages()"))
        return ""

    async def screenshot(self, fmt: str = "png") -> bytes:
        await self.start()
        async with self._lock:
            if fmt == "jpeg":
                return await self.page.screenshot(type="jpeg", quality=70)
            return await self.page.screenshot(type="png")

    async def dump_ui(self) -> list[UIElement]:
        raw = await self._js("() => __simcar.dumpUI()")
        return [UIElement(id=i, **{k: v for k, v in e.items() if k in UIElement.model_fields}) for i, e in enumerate(raw)]

    async def tap(self, x: int, y: int) -> None:
        await self.start()
        async with self._lock:
            await self.page.mouse.click(x, y)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        await self.start()
        async with self._lock:
            await self.page.mouse.move(x1, y1)
            await self.page.mouse.down()
            await self.page.mouse.move(x2, y2, steps=8)
            await self.page.mouse.up()

    async def key(self, name: str) -> None:
        await self._js("n => __simcar.key(n)", name)

    async def text(self, s: str) -> None:
        await self.start()
        async with self._lock:
            await self.page.keyboard.type(s)

    async def logs(self, cursor: Any = None) -> tuple[str, Any]:
        r = await self._js("c => __simcar.logs(c)", int(cursor or 0))
        return "\n".join(r["lines"]), r["next"]

    async def current_screen(self) -> str:
        return await self._js("() => __simcar.screen()")

    async def voice_wake(self, text: str) -> dict:
        return await self._js("() => __simcar.voiceWake()")

    async def voice_say(self, text: str) -> dict:
        return await self._js("t => __simcar.voiceSay(t)", text)

    async def reset(self) -> None:
        await self._js("() => __simcar.reset()")
