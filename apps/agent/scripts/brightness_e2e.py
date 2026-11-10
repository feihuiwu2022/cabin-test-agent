"""TC-ADB-DISP-001 真机验证：系统设置里把屏幕亮度调到 100%。

只验操作结果：vision 判不出 255 和 250 的差别，这里不走判定，执行后用 adb 读系统值严格校验。
前置条件由本脚本设置（shell 白名单为空，TaskPlan 里下不了命令），结束后恢复设备原值。

用法（apps/agent/ 下）：uv run python scripts/brightness_e2e.py [--serial 320234976112]
不传 --serial 时用 CABIN_ADB_SERIAL，再否则取唯一在线的设备。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path

import adbutils
import yaml

from cabin_agent.config import get_settings
from cabin_agent.device.adb import AndroidAdbAdapter
from cabin_agent.runner.actions.adb import AdbChannel
from cabin_agent.runner.engine import PlanRunner
from cabin_common.schema import TestCase

SAMPLE = Path(__file__).with_name("TC-ADB-DISP-001_屏幕亮度调到100.yaml")
MAX_BRIGHTNESS = 255
START_BRIGHTNESS = 102  # 约 40%：起点不能是满亮度，否则没拖动也会通过
KEYS = ("screen_brightness_mode", "screen_auto_brightness_adj", "screen_brightness")  # 恢复时 mode 在前


def pick_serial(want: str) -> str:
    if want:
        return want
    online = [d.serial for d in adbutils.adb.device_list() if d.get_state() == "device"]
    if len(online) != 1:
        raise SystemExit(f"在线设备 {online}，请用 --serial 指定一台")
    return online[0]


async def main(serial: str) -> int:
    doc = yaml.safe_load(SAMPLE.read_text("utf8"))
    case, plan = TestCase(**doc["case"]), doc["plan"]
    plan["assertions"] = [a for a in plan["assertions"] if a["type"] != "vision"]
    dev = AndroidAdbAdapter(serial)

    async def get(key: str) -> str:
        return (await dev.shell(f"settings get system {key}")).strip()

    async def put(key: str, value: str) -> None:
        if value != "null":
            await dev.shell(f"settings put system {key} {value}")

    await dev.shell("input keyevent KEYCODE_WAKEUP")  # 自动熄屏只有 1 分钟
    power = await dev.shell("dumpsys power | grep mWakefulness=")
    keyguard = await dev.shell("dumpsys window | grep isKeyguardShowing")
    if "Awake" not in power or re.search(r"isKeyguardShowing=true", keyguard):
        print("前置条件不满足：请先亮屏解锁")
        return 2

    saved = {k: await get(k) for k in KEYS}
    print(f"设备原值：{saved}")
    try:
        await dev.shell(f"am force-stop {case.package}")  # 否则设置会回到上次的子页面
        await put("screen_brightness_mode", "0")
        await put("screen_brightness", str(START_BRIGHTNESS))
        before = int(await get("screen_brightness"))
        run_id = f"bright-{int(time.time())}"
        result = await PlanRunner(plan, channels=[AdbChannel(dev)], run_id=run_id, case=case,
                                  plan_version="sample", packages=[case.package]).execute()
        after = int(await get("screen_brightness"))
    finally:
        for k in KEYS:
            await put(k, saved[k])

    for s in result.steps:
        print(f"  步骤 {s.id} {s.result:5} {s.label}")
    for a in result.asserts:
        print(f"  断言 {a.assertion_id} [{a.type}] {a.result} conf={a.confidence} {a.detail.get('verdict') or a.detail}")
    for w in result.warnings:
        print(f"  告警：{w}")
    if result.block_reason:
        print(f"  阻塞：{result.block_reason} {result.block_detail}")
    exact = after == MAX_BRIGHTNESS and before != MAX_BRIGHTNESS
    print(f"  严格校验 screen_brightness：{before} → {after}（期望 {MAX_BRIGHTNESS}）{'通过' if exact else '不通过'}")
    print(f"证据目录：{result.evidence_dir}")
    print(f"结论：runner={result.verdict} 严格校验={'pass' if exact else 'fail'}")
    print(f"已恢复：{ {k: await get(k) for k in KEYS} }")
    return 0 if exact and result.verdict == "passed" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=get_settings().adb_serial)
    raise SystemExit(asyncio.run(main(pick_serial(ap.parse_args().serial))))
