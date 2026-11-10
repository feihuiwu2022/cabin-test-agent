"""触屏执行的车机能力探查：在真车机 / AAOS 模拟器上先跑这个，看各级定位和车辆属性能不能用。

用法（apps/agent/ 下）：uv run python scripts/touch_probe.py [--serial emulator-5554] [--find 空调] [--out probe]
输出：前台页面、是否 Android Automotive、控件树（前 40 个）、OCR 识别文字、HVAC 车辆属性，以及截图 / 控件树文件。
"""
import argparse
import asyncio
import json
from pathlib import Path

from cabin_agent.device.adb import AndroidAdbAdapter
from cabin_agent.runner import vision_local
from cabin_agent.runner.actions.adb import parse_prop
from cabin_agent.runner.locate import match_selector
from cabin_common.taskplan import VEHICLE_PROPS


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="")
    ap.add_argument("--find", default="", help="要找的控件文字，分别用控件树和 OCR 找一次")
    ap.add_argument("--out", default="probe")
    a = ap.parse_args()
    dev = AndroidAdbAdapter(a.serial)
    h = await dev.health()
    if not h.online:
        print(f"连不上设备：{h.detail}（adb devices 看一下，模拟器用 --serial emulator-5554）")
        return 1
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    auto = "android.hardware.type.automotive" in await dev.shell("pm list features")
    w, hgt = await dev.screen_size()
    print(f"设备：{h.model} · {h.ota} · {h.build_type} · 屏幕 {w}x{hgt} · {'Android Automotive' if auto else '非 Automotive（读不到车辆属性）'}")
    print(f"前台：{(await dev.shell('dumpsys window | grep mCurrentFocus')).strip()}")

    png = await dev.screenshot()
    (out / "screen.png").write_bytes(png)
    els = await dev.dump_ui()
    (out / "ui.json").write_text(json.dumps([e.model_dump() for e in els], ensure_ascii=False, indent=1), "utf8")
    print(f"\n控件树：{len(els)} 个（全部见 {out / 'ui.json'}）")
    for e in els[:40]:
        print("  " + e.brief() + (f"  id={e.key}" if e.key else ""))

    why = vision_local.ocr_unavailable()
    if why:
        print(f"\nOCR 不可用：{why}（uv sync --extra vision）")
    else:
        lines = vision_local.get_ocr().read(png)
        print(f"\nOCR：{len(lines)} 行")
        for text, bbox, score in lines[:40]:
            print(f"  {text}  @{bbox}  {score:.2f}")
    print(f"图标模板：{vision_local.template_unavailable() or '可用'}")

    if a.find:
        el = match_selector(els, {"text": a.find})
        f = None if why else vision_local.ocr_find(png, a.find)
        print(f"\n找“{a.find}”：控件树 {el.brief() if el else '未找到'}；OCR {f.bbox if f else '未找到'}")

    if auto:
        print("\n车辆属性：")
        for name in ("HVAC_POWER_ON", "HVAC_AC_ON", "HVAC_AUTO_ON", "HVAC_TEMPERATURE_SET", "HVAC_FAN_SPEED"):
            raw = await dev.shell(f"cmd car_service get-property-value {VEHICLE_PROPS[name]}")
            vals = parse_prop(raw)
            print(f"  {name:<22} {vals or (raw.strip().splitlines() or ['（无输出）'])[0][:80]}")
    print(f"\n截图与控件树已存到 {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
