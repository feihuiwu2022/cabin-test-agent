"""Android-ADB 适配器：adb 直连，车端不部署程序。"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from typing import Any

from .base import DeviceAdapter, Health, UIElement

KEYCODES = {"back": 4, "home": 3, "enter": 66}


class AndroidAdbAdapter(DeviceAdapter):
    name = "adb"

    def __init__(self, serial: str = ""):
        self.serial = serial
        self._dev = None

    def _d(self):
        if self._dev is None:
            import adbutils
            self._dev = adbutils.adb.device(serial=self.serial or None)
        return self._dev

    async def _run(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    async def shell(self, cmd: str) -> str:
        return await self._run(lambda: self._d().shell(cmd))

    async def health(self) -> Health:
        try:
            ota = (await self.shell("getprop ro.build.display.id")).strip()
            model = (await self.shell("getprop ro.product.model")).strip()
            bt = (await self.shell("getprop ro.build.type")).strip()
            pk = [l.split(":", 1)[1] for l in (await self.shell("pm list packages")).splitlines() if ":" in l]
            df = await self.shell("df /data | tail -1")
            m = re.search(r"(\d+)%", df)
            return Health(online=True, ota=ota, model=model, build_type=bt, packages=pk,
                          storage_ok=not (m and int(m.group(1)) > 95), detail=df.strip())
        except Exception as e:
            return Health(online=False, detail=str(e))

    async def screenshot(self, fmt: str = "png") -> bytes:
        png = await self._run(lambda: self._d().shell("screencap -p", encoding=None))
        if fmt == "jpeg":
            from io import BytesIO
            try:
                from PIL import Image
                buf = BytesIO()
                Image.open(BytesIO(png)).convert("RGB").save(buf, "JPEG", quality=70)
                return buf.getvalue()
            except ImportError:
                pass
        return png

    async def dump_ui(self) -> list[UIElement]:
        xml = await self.shell("uiautomator dump /sdcard/cabin_ui.xml >/dev/null && cat /sdcard/cabin_ui.xml")
        out: list[UIElement] = []
        try:
            root = ET.fromstring(xml[xml.find("<"):])
        except ET.ParseError:
            return out
        for n in root.iter("node"):
            text, desc = n.get("text", ""), n.get("content-desc", "")
            clickable = n.get("clickable") == "true"
            if not (text or desc or clickable):
                continue
            m = re.findall(r"\d+", n.get("bounds", "[0,0][0,0]"))
            l, t, r, b = map(int, m[:4])
            checked = n.get("checked") == "true" if n.get("checkable") == "true" else None
            out.append(UIElement(id=len(out), text=text, desc=desc, key=n.get("resource-id", ""),
                                 cls=n.get("class", ""), clickable=clickable, checked=checked, bounds=[l, t, r, b]))
        return out

    async def tap(self, x: int, y: int) -> None:
        await self.shell(f"input tap {x} {y}")

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        await self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    async def key(self, name: str) -> None:
        await self.shell(f"input keyevent {KEYCODES.get(name, name)}")

    async def text(self, s: str) -> None:
        await self.shell("input text " + s.replace(" ", "%s"))

    async def logs(self, cursor: Any = None) -> tuple[str, Any]:
        # 游标为设备时间戳；首次调用只取游标，不回放历史
        now = (await self.shell("date +'%m-%d %H:%M:%S.000'")).strip()
        if cursor is None:
            return "", now
        txt = await self.shell(f"logcat -d -b main,crash -v threadtime -T '{cursor}'")
        return txt, now

    async def current_screen(self) -> str:
        out = await self.shell("dumpsys window | grep mCurrentFocus")
        m = re.search(r"\{[^ ]+ [^ ]+ ([^}]+)\}", out)
        return m.group(1) if m else out.strip()

    async def pull(self, remote: str) -> bytes:
        return await self._run(lambda: b"".join(self._d().sync.iter_content(remote)))

    async def screen_size(self) -> tuple[int, int]:
        out = await self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (1920, 720)
