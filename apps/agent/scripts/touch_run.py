"""在接着的车机 / 模拟器上直接跑一份 TaskPlan YAML（不走用例库，调试测试代码用；回放不调大模型）。

用法（apps/agent/ 下）：uv run python scripts/touch_run.py plan.yaml [--serial emulator-5554]
       uv run python scripts/touch_run.py plan.yaml --simcar        # 在 SimCar 模拟车机上跑
含车型参数 ${…} 的测试代码先从云端按车型取已代入的版本（台架领取任务时拿到的 plan_inline 就是代入后的）。
结果：结论、每步命中的定位层、断言明细、告警；证据目录里有截图、控件树、日志。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

import yaml

from cabin_agent.runner.actions.adb import AdbChannel
from cabin_agent.runner.engine import PlanRunner
from cabin_agent.runner.locate import LEVEL_CN
from cabin_common import taskplan


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--serial", default="")
    ap.add_argument("--simcar", action="store_true")
    ap.add_argument("--bugs", default="", help="SimCar 注入缺陷，如 hvac_ui_only")
    a = ap.parse_args()
    plan = yaml.safe_load(Path(a.plan).read_text("utf8"))
    plan.setdefault("schema", taskplan.SCHEMA)
    errs, warns = taskplan.validate(plan)
    for w in warns:
        print(f"告警：{w}")
    if errs:
        print("测试代码校验未通过：\n  " + "\n  ".join(errs))
        return 2
    if a.simcar:
        from cabin_agent.device.simcar import SimCarAdapter
        dev = SimCarAdapter(bugs=a.bugs)
    else:
        from cabin_agent.device.adb import AndroidAdbAdapter
        dev = AndroidAdbAdapter(a.serial)
    try:
        res = await PlanRunner(plan, channels=[AdbChannel(dev)], run_id=time.strftime("T%m%d-%H%M%S")).execute()
    finally:
        if a.simcar:
            await dev.stop()
    print(f"\n结论：{res.verdict}" + (f"（{res.block_reason}：{res.block_detail}）" if res.block_reason else "") +
          f" · {res.duration_s}s")
    for s in res.steps:
        how = f" · {LEVEL_CN.get(s.locator, s.locator)}" if s.locator else ""
        print(f"  步骤 {s.id:<2} {s.result:<4} {s.label}{how}" + (f" · {s.error}" if s.error else ""))
    for r in res.asserts:
        print(f"  断言 {r.assertion_id:<3} {r.type:<12} {r.result:<7} {r.detail.get('reason') or ''}")
    for w in res.warnings:
        print(f"  告警：{w}")
    print(f"证据：{res.evidence_dir}")
    if res.block_reason == "locator":
        print(f"定位失败：把证据目录里的 fail-*.png / fail-*.ui.json 拷到云端，执行 apps/cloud/scripts/touch_heal.py 获取修改建议")
    return 0 if res.verdict == "passed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
