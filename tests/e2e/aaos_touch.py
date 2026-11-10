"""AAOS 实测：私有云多模态模型 + Android Automotive 模拟器 / 真车机，跑一遍 UI 地图录制、回放和自愈。

在一台同时有云端代码、台架代码和车机的开发机上跑（同一进程里调云端的模型网关与台架的执行器，省去部署与注册）：

  # 1. 模型：在云端设置页（/settings → 模型）或 .env 配私有云多模态模型（CABIN_LLM_BASE_URL / CABIN_VISION_MODEL / Token）
  # 2. 车机：启动 AAOS 模拟器（需要 KVM / HAXM / Apple 芯片），adb devices 能看到；可选 uv sync --extra vision 装 OCR
  # 3. 跑（仓库根目录）：
  uv run python tests/e2e/aaos_touch.py --serial emulator-5554 \\
      --package com.android.car.settings --steps "打开设置;点击“显示”;打开“自适应亮度”" --out e2e-aaos

步骤：预检（模型对话 + 看图、车机是否 Android Automotive、OCR、车辆属性）→ 录制（模型选编号）→ 按录制生成测试代码并回放
→ 把一个控件 id 改成不存在（模拟界面改版）→ 回放应判定位失败阻塞 → 模型出自愈建议 → 写回后再回放应通过。
结果写到 --out：report.md（结论、模型耗时、每步）、录制回看页 recording.html、两份测试代码与证据目录路径。

--simcar --fake-model：用 SimCar 和脚本化假模型把这个脚本自身跑通（不验证模型与车机，CI / 无车机时自检用）。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Timed:
    """包一层选编号的模型，记录每次耗时与回答。"""

    def __init__(self, inner):
        self.inner, self.calls = inner, []

    async def decide(self, prompt: str, image: bytes, session: str) -> dict:
        t0 = time.time()
        ans = await self.inner.decide(prompt, image, session)
        self.calls.append({"latency_s": round(time.time() - t0, 2), "answer": ans})
        return ans


class FakeVlm:
    """--fake-model：按清单里的控件 id 依次选（SimCar 的设置 → 空调 → 开关）。"""

    def __init__(self, wants: list[str]):
        self.wants = list(wants)

    async def decide(self, prompt: str, image: bytes, session: str) -> dict:
        if not self.wants:
            return {"action": "done", "reason": "步骤已完成"}
        want = self.wants.pop(0)
        m = re.search(rf"^(\d+)\. .*id:{re.escape(want)}(?:\s|·|$)", prompt, re.M)
        return {"action": "tap", "mark": int(m.group(1)), "step": 1, "reason": want} if m else {"action": "fail", "reason": want}


async def preflight(dev, fake_model: bool, lines: list[str]) -> bool:
    from cabin_agent.runner import vision_local
    from cabin_agent.runner.actions.adb import parse_prop
    ok = True
    h = await dev.health()
    lines.append(f"- 车机：{'在线' if h.online else '离线'} · {h.model or '—'} · OTA {h.ota or '—'}")
    ok &= h.online
    auto = "android.hardware.type.automotive" in (await dev.shell("pm list features") or "")
    lines.append(f"- Android Automotive：{'是' if auto else '否（车辆属性断言会跳过）'}")
    prop = parse_prop(await dev.shell("cmd car_service get-property-value 354419984") or "")  # HVAC_POWER_ON
    lines.append(f"- 车辆属性 HVAC_POWER_ON：{prop or '读不到'}")
    lines.append(f"- 本地 OCR：{vision_local.ocr_unavailable() or '可用'} · 图标模板：{vision_local.template_unavailable() or '可用'}")
    if fake_model:
        lines.append("- 模型：脚本化假模型（--fake-model，不验证模型）")
        return ok
    from cabin_cloud.modelgw import get_gateway
    from cabin_cloud.settings_api import PIXEL
    gw, t0 = get_gateway(), time.time()
    try:
        out = await gw.vision_json(system="只输出 JSON", prompt='看图后回复 {"ok": true}', images=[PIXEL], label="public",
                                   source="e2e.preflight", max_tokens=64)
        lines.append(f"- 视觉模型 {gw.vision_model} @ {gw.base_url}：通，{round(time.time() - t0, 2)} 秒 · {out}")
    except Exception as e:
        lines.append(f"- 视觉模型 {gw.vision_model} @ {gw.base_url}：**不通** {type(e).__name__}: {str(e)[:200]}")
        ok = False
    return ok


def _break_first_id(plan: dict) -> tuple[dict, int, str]:
    """模拟界面改版：第一个按 id 定位的点击，只留一个不存在的 id（去掉 OCR / 坐标兜底），回放应定位失败。"""
    p = copy.deepcopy(plan)
    for s in p["steps"]:
        key = ((s.get("target") or {}).get("selector") or {}).get("key")
        if s.get("action") == "tap" and key:
            s["target"] = {"selector": {"key": key + "_renamed_by_e2e"}}
            return p, s["id"], key
    raise SystemExit("录制里没有按控件 id 定位的点击，没法模拟改版；换一个用例步骤再试")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="")
    ap.add_argument("--package", default="com.android.car.settings")
    ap.add_argument("--steps", default="", help="用例步骤，分号分隔")
    ap.add_argument("--case", default="TC-E2E-AAOS-001")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--settle", type=float, default=1.2, help="每步操作后等界面稳定的秒数")
    ap.add_argument("--out", default="e2e-aaos")
    ap.add_argument("--simcar", action="store_true")
    ap.add_argument("--fake-model", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    import yaml
    from cabin_agent import assets
    from cabin_agent.runner.actions.adb import AdbChannel
    from cabin_agent.runner.engine import PlanRunner
    from cabin_cloud import heal, planner, uimap
    from cabin_common import taskplan
    from cabin_common.uimap import MAX_STEPS  # noqa: F401
    from cabin_agent import uimap_rec

    if a.simcar:
        from cabin_agent.device.simcar import SimCarAdapter
        dev_factory = lambda bugs="": SimCarAdapter(bugs=bugs)  # noqa: E731
        a.package = "com.cabin.sim.settings"
        a.steps = a.steps or "打开设置;点击左侧“空调”;打开空调开关"
    else:
        from cabin_agent.device.adb import AndroidAdbAdapter
        dev_factory = lambda bugs="": AndroidAdbAdapter(a.serial)  # noqa: E731
    steps = [x.strip() for x in a.steps.replace("；", ";").split(";") if x.strip()]
    if not steps:
        ap.error("需要 --steps")
    rec_vlm = Timed(FakeVlm(["nav-空调", "hvac_power-开关"]) if a.fake_model else uimap.VlmDecider())

    lines = [f"# AAOS 触屏实测 · {time.strftime('%Y-%m-%d %H:%M')}", "", f"应用 `{a.package}` · 步骤：{'；'.join(steps)}", "",
             "## 预检"]
    verdict: dict[str, str] = {}

    async def with_dev(fn, bugs=""):
        d = dev_factory(bugs)
        try:
            await d.start()
            return await fn(d)
        finally:
            await d.stop()

    ok = await with_dev(lambda d: preflight(d, a.fake_model, lines))
    if not ok:
        lines.append("\n**预检不通过，停止。**")
        (out / "report.md").write_text("\n".join(lines), "utf8")
        print("\n".join(lines))
        return 2

    # ---------- 录制 ----------
    async def rec_fn(d):
        sw = (await d.health()).ota
        return await uimap_rec.record(d, steps, a.package, rec_vlm, case_id=a.case, max_steps=a.max_steps,
                                      sw_version=sw, model="A01-CN", rig_id="e2e", settle_s=a.settle)
    t0 = time.time()
    r = await with_dev(rec_fn)
    verdict["录制"] = r.outcome
    lines += ["", "## 录制", f"- 结果 **{r.outcome}** {r.note} · {len(r.trace)} 步 · {round(time.time() - t0, 1)} 秒 · "
              f"模型 {len(rec_vlm.calls)} 次，平均 {round(sum(c['latency_s'] for c in rec_vlm.calls) / max(len(rec_vlm.calls), 1), 2)} 秒"]
    for st in r.trace:
        lines.append(f"  {st.n}. {st.action} {st.control.name() if st.control else ''}"
                     f"{' · id ' + st.control.key if st.control and st.control.key else ''} ← {st.reason}")
    uimap.accept_upload(r, {sha: assets.read_image(sha) for sha in r.image_shas()}, "e2e")
    uimap.confirm(r.rec_id, "e2e")
    (out / "recording.html").write_text(uimap.report_html(uimap.load_recording(r.rec_id)), "utf8")
    if r.outcome != "done":
        lines.append("\n**录制没走完，停止。** 看 recording.html 里模型每一步的选择。")
        (out / "report.md").write_text("\n".join(lines), "utf8")
        print("\n".join(lines))
        return 1

    # ---------- 回放 ----------
    plan = {"schema": taskplan.SCHEMA, "case_id": a.case, "title": "；".join(steps)[:40], "requires": ["adb"],
            "timeout_s": 180, **uimap.plan_from_recording(r), "on_fail": "continue"}
    errs, warns = taskplan.validate(plan)
    (out / "plan.yaml").write_text(planner.dump_yaml(plan), "utf8")
    if errs:
        lines.append(f"\n**测试代码校验不通过**：{'；'.join(errs)}")
        (out / "report.md").write_text("\n".join(lines), "utf8")
        return 1

    async def replay(p, rid):
        return await with_dev(lambda d: PlanRunner(p, channels=[AdbChannel(d)], run_id=rid).execute())
    res = await replay(plan, f"E2E-{time.strftime('%H%M%S')}-1")
    verdict["回放"] = res.verdict
    lines += ["", "## 按录制回放", f"- 结论 **{res.verdict}** {res.block_reason} {res.block_detail} · {res.duration_s} 秒 · 证据 {res.evidence_dir}"]
    lines += [f"  - 步骤 {s.id} {s.result} {s.label} · {s.locator or '—'}" for s in res.steps]
    lines += [f"  - 断言 {x.assertion_id} {x.type} {x.result} {x.detail.get('reason') or ''}" for x in res.asserts]

    # ---------- 改版 → 阻塞 → 自愈 → 回放 ----------
    broken, sid, key = _break_first_id(plan)
    # 假模型：界面上控件其实没变，应选回原来的 id；真模型自己看图选
    heal_vlm = Timed(FakeVlm([key]) if a.fake_model else heal._HealDecider())
    what = next((r.steps_text[t.case_step - 1] for t in r.trace if t.control and t.control.key == key
                 and t.case_step and t.case_step <= len(r.steps_text)), "")
    res2 = await replay(broken, f"E2E-{time.strftime('%H%M%S')}-2")
    verdict["改版后回放"] = f"{res2.verdict}/{res2.block_reason}"
    lines += ["", "## 模拟改版后的自愈", f"- 第 {sid} 步的 id `{key}` 改成不存在 → 回放 **{res2.verdict}** {res2.block_reason}"]
    if res2.block_reason == "locator":
        t0 = time.time()
        sug = await heal.suggest(res2.evidence_dir, broken, heal.failed_step(res2.model_dump()) or sid, case_step=what,
                                 decider=heal_vlm)
        lines.append(f"- 自愈建议：{'找到' if sug['found'] else '没找到'} · {sug['reason']} · {round(time.time() - t0, 2)} 秒")
        if sug["found"]:
            lines.append(f"- 新定位：`{json.dumps(sug['new_target'], ensure_ascii=False)}`（原 id `{key}`）")
            healed = heal.apply_to_plan(broken, sug)
            (out / "plan.healed.yaml").write_text(planner.dump_yaml(healed), "utf8")
            res3 = await replay(healed, f"E2E-{time.strftime('%H%M%S')}-3")
            verdict["自愈后回放"] = res3.verdict
            lines.append(f"- 写回后回放 **{res3.verdict}** {res3.block_reason} · 证据 {res3.evidence_dir}")
        else:
            verdict["自愈"] = "没找到"
    lines += ["", "## 结论", *(f"- {k}：{v}" for k, v in verdict.items())]
    good = verdict.get("录制") == "done" and verdict.get("回放") == "passed" and verdict.get("自愈后回放") == "passed"
    lines.append(f"\n**{'全部通过' if good else '有未通过的环节'}**")
    (out / "report.md").write_text("\n".join(lines), "utf8")
    print("\n".join(lines))
    print(f"\n报告：{out / 'report.md'} · 录制回看：{out / 'recording.html'}")
    return 0 if good else 1


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT / "tests" / "integration")]
    sys.exit(asyncio.run(main()))
