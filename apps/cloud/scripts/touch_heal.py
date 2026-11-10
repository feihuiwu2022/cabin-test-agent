"""自愈建议：某次回放因定位失败阻塞后，用失败现场让多模态模型找新控件，给出 YAML 修改建议。

用法（apps/cloud/ 下）：uv run python scripts/touch_heal.py <证据目录> <plan.yaml> [--step 3] [--write 新plan.yaml]
不加 --write 只输出建议与差异；人工确认后加 --write 生成修改后的测试代码（用例库里的测试代码用 heal.confirm 写回 git）。
"""
import argparse
import asyncio
import json
from pathlib import Path

import yaml

from cabin_cloud import heal, planner


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence")
    ap.add_argument("plan")
    ap.add_argument("--step", type=int, default=0)
    ap.add_argument("--write", default="")
    a = ap.parse_args()
    plan = yaml.safe_load(Path(a.plan).read_text("utf8"))
    step = a.step
    if not step:
        f = Path(a.evidence) / "result.json"
        if not f.is_file():  # 云端证据目录里只有失败现场，没有 result.json：用 --step 指定步骤
            print("证据目录里没有 result.json，请用 --step 指定定位失败的步骤")
            return 1
        res = json.loads(f.read_text("utf8"))
        step = heal.failed_step(res)
        if not step:
            print("这次执行不是定位失败阻塞，不需要自愈建议")
            return 1
    s = await heal.suggest(a.evidence, plan, step)
    if not s["found"]:
        print(f"步骤 {step}：没找到对应控件（{s['reason']}）。可能不在该页面，需要人工检查或重新录制。")
        return 1
    print(f"步骤 {step} 建议改为点 {s['mark']} 号控件：{s['reason']}\n{s['diff']}")
    print(f"带编号截图：{Path(a.evidence) / f'heal-{step:02d}.png'}")
    if a.write:
        Path(a.write).write_text(planner.dump_yaml(heal.apply_to_plan(plan, s)), "utf8")
        print(f"已写出 {a.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
