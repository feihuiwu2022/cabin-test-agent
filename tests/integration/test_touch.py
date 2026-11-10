"""触屏执行（台架端）：启动应用、多级定位（控件树 → OCR → 模板 → 坐标）、等开关状态、双线判定（界面 + 车辆属性）。

在 SimCar 上跑“系统设置打开空调”：正常车机通过；“只变界面”缺陷判失败；布局改版时降级到 OCR 并告警。
"""
import json

import pytest

from cabin_agent import assets
from cabin_agent.device.base import UIElement
from cabin_agent.device.simcar import SimCarAdapter
from cabin_agent.runner import vision_local
from cabin_agent.runner.actions.adb import AdbChannel, parse_prop, prop_id
from cabin_agent.runner.engine import PlanRunner
from cabin_agent.runner.locate import match_selector
from cabin_common import taskplan

SETTINGS = "com.cabin.sim.settings"
AAOS_OUT = ("CarPropertyValue{mPropertyId=0x15200510, mAreaId=0x1, mStatus=0, mTimestampNanos=1, mValue=true}\n"
            "CarPropertyValue{mPropertyId=0x15200510, mAreaId=0x4, mStatus=0, mTimestampNanos=1, mValue=false}")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import cabin_agent.runner.actions.adb as adb
    import cabin_agent.runner.engine as eng
    monkeypatch.setattr(adb, "SETTLE_S", 0.05)
    monkeypatch.setattr(eng, "RETRY_INTERVAL_S", 0)


def hvac_plan(toggle: dict | None = None, *, ui_assert=True, images=None) -> dict:
    toggle = toggle or {"selector": {"key": "hvac_power-开关"}}
    p = {"schema": taskplan.SCHEMA, "case_id": "TC-SET-901", "title": "系统设置打开空调", "requires": ["adb"],
         "timeout_s": 60, "setup": [{"action": "shell", "cmd": "cabin-hvac off"}],
         "steps": [{"id": 1, "action": "launch_app", "package": SETTINGS},
                   {"id": 2, "action": "tap", "target": {"selector": {"key": "nav-空调"}, "ocr": "空调"}},
                   {"id": 3, "action": "tap", "target": toggle},
                   {"id": 4, "action": "wait", "until": {"screen": "设置/空调"}, "timeout_s": 3},
                   {"id": 5, "action": "screenshot", "domain": "center"},
                   {"id": 6, "action": "read_prop", "prop": "HVAC_POWER_ON"}],
         "assertions": [{"id": "a2", "after_step": 6, "type": "vehicle_prop", "prop": "HVAC_POWER_ON", "expect": True},
                        {"id": "a3", "type": "rule", "rule": "no_crash"}],
         "teardown": [{"action": "close_app", "package": SETTINGS}], "on_fail": "continue"}
    if ui_assert:
        p["steps"].insert(3, {"id": 31, "action": "wait",
                              "until": {"selector": {"key": "hvac_power-开关"}, "checked": True}, "timeout_s": 3})
        p["assertions"].insert(0, {"id": "a1", "after_step": 5, "type": "ui_state",
                                   "target": {"selector": {"key": "hvac_power-开关"}}, "checked": True})
    if images:
        p["images"] = images
    return p


async def run(p, bugs="", rid="R-touch"):
    dev = SimCarAdapter(bugs=bugs)
    try:
        res = await PlanRunner(p, channels=[AdbChannel(dev)], run_id=rid).execute()
    finally:
        await dev.stop()
    return res


def by_id(res):
    return {a.assertion_id: a for a in res.asserts}


# ---------- 端到端（SimCar） ----------
async def test_settings_turn_on_hvac_passes():
    errs, warns = taskplan.validate(hvac_plan())
    assert errs == []
    res = await run(hvac_plan())
    assert res.verdict == "passed", (res.block_detail, [a.model_dump() for a in res.asserts])
    a = by_id(res)
    assert a["a1"].detail["checked"] == {"expected": True, "actual": True}
    assert a["a2"].detail["actual"] == {"1": "true", "4": "true"}
    assert [s.locator for s in res.steps if s.action == "tap"] == ["control", "control"]
    assert not any("降级" in w for w in res.warnings)
    assert res.steps[0].label == f"启动 {SETTINGS}"


async def test_ui_only_bug_fails_on_vehicle_state():
    """界面显示已开、车辆属性仍为关：双线判定判失败（显示与执行不一致）。"""
    res = await run(hvac_plan(), bugs="hvac_ui_only", rid="R-touch-bug")
    a = by_id(res)
    assert res.verdict == "failed"
    assert a["a1"].result == "pass" and a["a2"].result == "fail"
    assert "期望 true" in a["a2"].detail["reason"]


@pytest.mark.skipif(bool(vision_local.ocr_unavailable()), reason="台架未安装 OCR")
async def test_reflow_degrades_to_ocr_with_warning():
    """空调页改版（控件 id 变了）：控件树找不到，按右侧面板 OCR 找到“空调”文字点中，并告警界面可能改版。"""
    toggle = {"selector": {"key": "hvac_power"}, "ocr": "空调", "region": [0.22, 0.08, 1, 1]}
    res = await run(hvac_plan(toggle, ui_assert=False), bugs="hvac_reflow", rid="R-touch-ocr")
    assert res.verdict == "passed", (res.block_detail, [a.model_dump() for a in res.asserts])
    tap = [s for s in res.steps if s.action == "tap"][1]
    assert tap.locator == "ocr" and tap.x > 0.22 * 1280
    assert any("降级为OCR定位" in w and "界面可能改版" in w for w in res.warnings)
    loc = json.loads((res.evidence_dir and open(f"{res.evidence_dir}/manifest.json", encoding="utf8").read()) or "{}")
    assert loc["facts"]["locators"]["3"]["locator"] == "ocr"


@pytest.mark.skipif(bool(vision_local.template_unavailable()), reason="台架未安装 OpenCV")
async def test_template_locator_hits_icon():
    """图标模板：先截出开关图标存为资产，再只用模板定位点击。"""
    dev = SimCarAdapter(bugs="")
    try:
        await dev.start()
        await dev.shell(f"monkey -p {SETTINGS} 1")
        await dev.tap(*match_selector(await dev.dump_ui(), {"key": "nav-空调"}).center)
        el = match_selector(await dev.dump_ui(), {"key": "hvac_power"})
        icon = vision_local.crop(await dev.screenshot(), el.bounds)  # 整行作模板（开关本身太小、和别的开关一样）
    finally:
        await dev.stop()
    sha = assets.store_image(icon)
    p = hvac_plan({"image": "hvac_row.png"}, ui_assert=True, images={"hvac_row.png": {"sha256": sha}})
    assert taskplan.validate(p)[0] == []
    res = await run(p, rid="R-touch-tpl")
    assert res.verdict == "passed", (res.block_detail, [a.model_dump() for a in res.asserts])
    assert [s for s in res.steps if s.action == "tap"][1].locator == "image"


async def test_ratio_fallback_warns():
    toggle = {"selector": {"key": "no_such"}, "ratio": [0.9, 0.17]}
    res = await run(hvac_plan(toggle, ui_assert=False), rid="R-touch-ratio")
    tap = [s for s in res.steps if s.action == "tap"][1]
    assert tap.locator == "ratio" and tap.x == int(0.9 * 1280)
    assert any("降级为相对坐标定位" in w for w in res.warnings)


async def test_launch_unknown_package_blocks():
    p = hvac_plan()
    p["steps"][0]["package"] = "com.not.installed"
    res = await run(p, rid="R-touch-launch")
    assert res.verdict == "blocked" and res.block_reason == "launch"


# ---------- 单元 ----------
def test_parse_prop_and_ids():
    assert parse_prop(AAOS_OUT) == {"1": "true", "4": "false"}
    assert prop_id("HVAC_POWER_ON") == 0x15200510 == prop_id("0x15200510") == prop_id(str(0x15200510))


def test_selector_checked_and_priority():
    els = [UIElement(id=0, text="空调", key="com.x:id/title", bounds=[0, 0, 10, 10]),
           UIElement(id=1, key="com.x:id/ac_switch", checked=False, bounds=[20, 0, 30, 10])]
    assert match_selector(els, {"key": "com.x:id/ac_switch"}).id == 1
    assert match_selector(els, {"key": "com.x:id/ac_switch", "checked": True}) is None
    assert match_selector(els, {"text": "ac_switch"}).id == 1  # resource-id 尾段
    assert match_selector(els, {"text": "空调"}).id == 0


def test_validate_touch_plan_errors():
    p = hvac_plan()
    p["steps"][1]["target"] = {"image": "nope.png"}
    p["steps"][2]["target"] = {"ratio": [1.5, 0.2]}
    p["steps"].append({"id": 9, "action": "read_prop", "prop": "WHAT_EVER"})
    p["assertions"].append({"id": "a9", "type": "vehicle_prop", "prop": "HVAC_AC_ON", "expect": True})
    p["assertions"].append({"id": "a8", "type": "ui_state", "after_step": 5, "target": {"selector": {"key": "k"}}})
    errs, warns = taskplan.validate(p)
    text = "；".join(errs)
    assert "nope.png 不在 images" in text and "ratio 应为" in text and "未知车辆属性 WHAT_EVER" in text
    assert "没有读取 HVAC_AC_ON" in text and "checked / text / exists" in text
    assert any("只有坐标定位" in w for w in warns)


def test_drag_to_ratio_is_slider_amount_not_screen_point():
    """drag 的 to.ratio 是一个数（滑条拖多远）；屏幕比例 [x, y] 或控件才是拖到的目标。"""
    from cabin_agent.runner.actions.adb import _is_target
    assert not _is_target({"ratio": 1.0}) and not _is_target({"direction": "left"})
    assert _is_target({"ratio": [0.5, 0.5]}) and _is_target({"selector": {"text": "垃圾桶"}}) and _is_target({"ocr": "x"})
