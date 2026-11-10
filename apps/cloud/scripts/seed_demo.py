"""公开演示的种子数据：三条可直接运行的用例（系统设置调光、语音打开空调、语音调到 24 度）。

演示容器启动时运行一次（deploy/demo-start.sh），已有就跳过。语音用例的测试音频用模型密钥合成；
没有密钥时仍写入用例，运行时会提示合成失败。
用法：uv run python apps/cloud/scripts/seed_demo.py
"""
from __future__ import annotations

import asyncio
import os
import time

from cabin_cloud import planner
from cabin_cloud.caselib import get_caselib
from cabin_cloud.store import get_store
from cabin_common.schema import TestCase

BRIGHT = TestCase(
    case_id="TC-DISP-001", title="系统设置里把屏幕亮度调到最亮", module="设置/显示", page="设置/显示",
    channel=["adb"], precondition="车机处于桌面，当前屏幕亮度约 40%",
    steps=["打开“设置”", "点击“显示”", "把亮度滑块拖到最右端", "读取系统亮度值"],
    expected="亮度滑块到最右端，系统亮度为 255（100%），页面无报错、无闪退",
    type="正向", priority="P1", review_status="通过", auto_status="已验证", plan_status="ready")

HVAC = TestCase(
    case_id="TC-HVAC-001", title="语音打开空调", module="语音助手/空调", channel=["audio", "adb"],
    precondition="空调已关闭", steps=["说唤醒词“你好千禾”", "说“打开空调”"], utterance="打开空调",
    expected="说“打开空调”后空调打开：屏幕显示空调开，播报“空调已打开”",
    expect_screen="空调开关显示为开", expect_tts="空调已打开", type="正向", priority="P0",
    review_status="通过", auto_status="已验证", plan_status="ready")

HVAC24 = TestCase(
    case_id="TC-HVAC-002", title="语音把空调调到 24 度", module="语音助手/空调", channel=["audio", "adb"],
    precondition="空调已开启，当前 22℃", steps=["说唤醒词“你好千禾”", "说“空调调到 24 度”"], utterance="空调调到 24 度",
    expected="说“空调调到 24 度”后温度设为 24℃：屏幕显示 24℃，播报“已为您调到 24 度”",
    expect_screen="空调温度显示 24℃", expect_tts="已为您调到 24 度", type="正向", priority="P0",
    review_status="通过", auto_status="已验证", plan_status="ready")

BRIGHT_PLAN = {
    "schema": planner.SCHEMA, "case_id": BRIGHT.case_id, "title": BRIGHT.title, "requires": ["adb"], "timeout_s": 120,
    "setup": [{"action": "shell", "cmd": "settings put system screen_brightness 102", "note": BRIGHT.precondition},
              {"action": "key", "key": "home"}],
    "steps": [
        {"id": 1, "action": "tap", "target": {"selector": {"text": "设置"}}},
        {"id": 2, "action": "tap", "target": {"selector": {"text": "显示"}}},
        {"id": 3, "action": "screenshot"},
        {"id": 4, "action": "drag", "target": {"selector": {"key": "亮度滑块"}}, "to": {"ratio": 1.0}},
        {"id": 5, "action": "screenshot"},
        {"id": 6, "action": "read_value", "read": "settings:system/screen_brightness", "save_as": "brightness"},
    ],
    "assertions": [
        {"id": "a1", "after_step": 5, "type": "vision",
         "expected": "第二张截图里亮度滑块已到最右端（满格），页面仍是显示设置页，没有报错弹窗"},
        {"id": "a2", "type": "rule", "rule": "no_crash"},
        {"id": "a3", "type": "value", "key": "brightness", "op": "eq", "expected": 255},
    ],
    "teardown": [{"action": "key", "key": "home"}],
    "on_fail": "continue",
    "meta": {"generated_by": "demo-seed", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
}


async def main() -> None:
    lib = get_caselib()
    for c in (BRIGHT, HVAC, HVAC24):
        if lib.get_case(c.case_id):
            print(f"{c.case_id} 已存在，跳过")
            continue
        c.plan_path = planner.plan_rel_path(c)
        lib.save_case(c)
        if c is BRIGHT:
            plan = {**BRIGHT_PLAN, "models": planner.plan_models(c)}
        else:
            try:
                plan, _ = await planner.build_plan(c)
            except Exception as e:  # 没有模型密钥：合成不了测试音频，先写不带音频哈希的版本
                print(f"{c.case_id} 测试音频合成失败（{e}），先写入用例")
                plan = {"schema": planner.SCHEMA, "case_id": c.case_id, "title": c.title, "requires": ["audio", "adb"],
                        "timeout_s": 120, "models": planner.plan_models(c), **planner.voice_plan(c), "on_fail": "continue"}
        await planner.save_plan(c, plan)
        print(f"{c.case_id} 已写入")
    # 演示台架的用例集：页面上“立即运行本台架用例”直接跑这两条（不经过对话，省模型调用）
    rigs = os.getenv("CABIN_DEMO_RIGS") or os.getenv("CABIN_RIG_ID", "B1")  # 多台架演示：B1,B2,B3
    for rig in [r.strip() for r in rigs.split(",") if r.strip()]:
        if not get_store().rig_caseset(rig):
            get_store().save_rig_caseset(rig, [BRIGHT.case_id, HVAC.case_id, HVAC24.case_id], "demo-seed")


if __name__ == "__main__":
    asyncio.run(main())
