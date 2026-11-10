"""UI 地图录制与自愈，按云端 / 台架分工：

- 录制在台架跑（SimCar），每步经云端选编号，录完上传云端，云端确认后并入 UI 地图、生成测试代码；
- 定位失败阻塞时台架只上传失败现场，云端出自愈建议，确认后写回测试代码。
用脚本化的假模型代替私有云多模态模型：按编号清单里的控件 id 选编号。
"""
import json
import re

import httpx
import pytest
import yaml

from cabin_agent import assets, uimap_rec
from cabin_agent.client import CloudClient, CloudRejected
from cabin_agent.device.simcar import SimCarAdapter
from cabin_agent.runner.actions.adb import AdbChannel
from cabin_agent.runner.engine import PlanRunner
from cabin_cloud import heal, planner, uimap
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc.service import get_service
from cabin_cloud.main import app
from cabin_common import taskplan
from cabin_common import uimap as um
from cabin_common.schema import TestCase
from test_rigagent import CODE, URL, agent, case_with_plan, cloud, submit  # noqa: F401

SETTINGS = "com.cabin.sim.settings"
STEPS = ["打开设置", "点击左侧“空调”", "打开空调开关"]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import cabin_agent.runner.actions.adb as adb
    import cabin_agent.runner.engine as eng
    monkeypatch.setattr(adb, "SETTLE_S", 0.05)
    monkeypatch.setattr(eng, "RETRY_INTERVAL_S", 0)


class ScriptedVlm:
    """按顺序找含指定 id 的编号；找完了回 done。记录收到的提示与图片。"""

    def __init__(self, wants: list[str]):
        self.wants, self.calls = list(wants), []

    async def decide(self, prompt: str, image: bytes, session: str) -> dict:
        self.calls.append((prompt, image, session))
        if not self.wants:
            return {"action": "done", "reason": "步骤已完成"}
        want = self.wants.pop(0)
        m = re.search(rf"^(\d+)\. .*id:{re.escape(want)}(?:\s|·|$)", prompt, re.M)
        if not m:
            return {"action": "fail", "reason": f"没找到 {want}"}
        return {"action": "tap", "mark": int(m.group(1)), "step": len(self.calls) + 1, "reason": f"点 {want}"}


async def rig_client() -> CloudClient:
    c = CloudClient(URL, "", transport=httpx.ASGITransport(app=app))
    c.token = (await c.call("POST", "/api/v1/rigs/register",
                            json={"rig_id": "B2", "model": "A01-CN", "register_code": CODE}))["rig_token"]
    return c


async def record_hvac(decider, case_id="TC-SET-950"):
    dev = SimCarAdapter(bugs="")
    try:
        await dev.shell("cabin-hvac off")
        return await uimap_rec.record(dev, STEPS, SETTINGS, decider, case_id=case_id, sw_version="C01-OTA-1.0.0",
                                      model="A01-CN", rig_id="B2", settle_s=0.2)
    finally:
        await dev.stop()


async def run(plan, bugs="", rid="R-uimap"):
    dev = SimCarAdapter(bugs=bugs)
    try:
        return await PlanRunner(plan, channels=[AdbChannel(dev)], run_id=rid).execute()
    finally:
        await dev.stop()


def full(body, case_id="TC-SET-950"):
    return {"schema": taskplan.SCHEMA, "case_id": case_id, "title": "系统设置打开空调", "requires": ["adb"],
            "timeout_s": 60, "setup": [{"action": "shell", "cmd": "cabin-hvac off"}], **body, "on_fail": "continue"}


# ---------- 录制：台架 → 云端 ----------
async def test_record_over_cloud_then_confirm_generate_replay(cloud, monkeypatch):  # noqa: F811
    vlm = ScriptedVlm(["nav-空调", "hvac_power-开关"])
    monkeypatch.setattr(uimap, "VlmDecider", lambda: vlm)
    c = await rig_client()
    try:
        r = await record_hvac(uimap_rec.CloudDecider(c))  # 选编号经云端
        assert r.outcome == "done", r.note
        assert vlm.calls[0][2] == f"rig:B2:{r.rec_id}" and vlm.calls[0][1][:4] == b"\x89PNG"
        assert "打开空调开关" in vlm.calls[0][0] and "id:nav-空调" in vlm.calls[0][0]
        taps = [s for s in r.trace if s.action == "tap"]
        assert [s.control.key for s in taps] == ["nav-空调", "hvac_power-开关"]
        assert taps[-1].after_checked is True and taps[-1].control.checkable
        up = await uimap_rec.upload(c, r)  # 上传云端
    finally:
        await c.close()
    assert up["status"] == "待确认" and up["review_url"].endswith(r.rec_id)
    got = uimap.load_recording(r.rec_id)
    assert got.rig_id == "B2" and got.image_shas() == r.image_shas() and "空调" in uimap.report_html(got)

    # 没确认的录制不能用于生成；确认时补车辆属性断言，并入 UI 地图
    assert uimap.recordings_for(r.case_id) == []
    uimap.confirm(r.rec_id, "张工", props=[{"prop": "HVAC_POWER_ON", "expect": "true"}])
    m = uimap.load_map(SETTINGS, "C01-OTA-1.0.0", "A01-CN")
    assert r.rec_id in m.recordings and m.edges
    plan = full(uimap.plan_from_recording(uimap.recordings_for(r.case_id)[-1]))
    assert taskplan.validate(plan)[0] == []
    tap2 = [s for s in plan["steps"] if s["action"] == "tap"][1]["target"]
    assert tap2["selector"] == {"key": "hvac_power-开关"} and tap2["ratio"]  # 多级定位：id + 坐标兜底
    assert [a["type"] for a in plan["assertions"]] == ["ui_state", "vehicle_prop", "rule"]
    assert (await run(plan, rid="R-uimap-ok")).verdict == "passed"
    assert (await run(plan, bugs="hvac_ui_only", rid="R-uimap-bug")).verdict == "failed"


async def test_rig_endpoints_need_token_and_check_upload(cloud, monkeypatch):  # noqa: F811
    anon = CloudClient(URL, "", transport=httpx.ASGITransport(app=app))
    with pytest.raises(CloudRejected):
        await anon.call("POST", "/api/v1/uimap/decide", data={"meta": json.dumps({"prompt": "x"})},
                        files=[("image", ("a.png", b"x", "image/png"))])
    r = await anon.http.get("/api/uimap/recordings")  # 回看 / 确认要登录
    assert r.status_code == 401
    await anon.close()
    c = await rig_client()
    try:
        png = assets.read_image(assets.store_image(b"\x89PNG fake"))
        sha = assets.store_image(png)
        rec = um.Recording(rec_id="REC-20261109-000000-abcd", package=SETTINGS,
                           trace=[um.RecStep(n=1, page="p", action="done", marked_sha="0" * 64)])
        with pytest.raises(CloudRejected, match="缺少截图"):  # 引用的截图没传
            await c.call("POST", "/api/v1/uimap/recordings", data={"meta": rec.model_dump_json()})
        with pytest.raises(CloudRejected, match="哈希不符"):
            await c.call("POST", "/api/v1/uimap/recordings", data={"meta": rec.model_dump_json()},
                         files=[("images", (f"{'0' * 64}.png", png, "image/png"))])
        rec.trace[0].marked_sha = sha
        rec.status, rec.by = "已确认", "台架自己"  # 台架不能替人确认
        got = await c.call("POST", "/api/v1/uimap/recordings", data={"meta": rec.model_dump_json()},
                           files=[("images", (f"{sha}.png", png, "image/png"))])
        assert got["status"] == "待确认" and uimap.load_recording(rec.rec_id).by == ""
    finally:
        await c.close()


# ---------- 云端生成测试代码 ----------
async def test_build_plan_uses_confirmed_recording_and_map_hint(monkeypatch):
    lib = get_caselib()
    c = TestCase(case_id=lib.next_id("SETR"), title="设置打开空调", channel=["adb"], precondition="空调已关闭",
                 steps=STEPS, expected="空调开关为开", models=["A01-CN"])
    lib.save_case(c)
    r = await record_hvac(ScriptedVlm(["nav-空调", "hvac_power-开关"]), c.case_id)
    uimap.accept_upload(r, {sha: assets.read_image(sha) for sha in r.image_shas()}, "B2")
    uimap.confirm(r.rec_id, "张工")
    plan, how = await planner.build_plan(c)
    assert how == "uimap-recording" and plan["meta"]["uimap_recording"] == r.rec_id
    assert planner.validate(plan, c)[0] == []

    # 没有录制的用例：模型生成时附上 UI 地图控件清单，生成后按地图补全多级定位；地图里没有的控件告警
    seen = {}

    class Gw:
        async def complete(self, **kw):
            seen.update(kw)
            return json.dumps({"steps": [{"id": 1, "action": "launch_app", "package": SETTINGS},
                                         {"id": 2, "action": "tap", "target": {"selector": {"text": "空调"}}},
                                         {"id": 3, "action": "tap", "target": {"selector": {"text": "不存在的按钮"}}},
                                         {"id": 4, "action": "screenshot"}]}, ensure_ascii=False)

    monkeypatch.setattr(planner, "get_gateway", lambda: Gw())
    c2 = TestCase(case_id=lib.next_id("SETR"), title="设置打开空调（无录制）", channel=["adb"], precondition="p",
                  steps=STEPS, expected="空调开关为开", models=["A01-CN"])
    body, how = await planner.ui_plan(c2)
    assert how == "model+uimap" and "UI 地图" in seen["prompt"] and "设置=com.cabin.sim.settings" in seen["system"]
    t2 = body["steps"][1]["target"]
    assert t2["selector"]["key"] and t2["ocr"] == "空调" and t2["ratio"]
    _, warns = planner.validate({"schema": planner.SCHEMA, "case_id": c2.case_id, "requires": ["adb"], **body}, c2)
    assert any("不存在的按钮" in w and "不在 UI 地图里" in w for w in warns)


async def test_ui_plan_merges_model_assertions(monkeypatch):
    class Gw:
        async def complete(self, **kw):
            return json.dumps({"steps": [
                {"id": 1, "action": "launch_app", "package": SETTINGS},
                {"id": 2, "action": "tap", "target": {"selector": {"text": "空调"}, "ocr": "空调"}},
                {"id": 3, "action": "wait", "until": {"selector": {"text": "空调"}, "checked": True}},
                {"id": 7, "action": "screenshot"}, {"id": 8, "action": "read_prop", "prop": "HVAC_POWER_ON"},
                {"id": 9, "action": "read_value", "read": "settings:system/x", "save_as": "x"}],
                "assertions": [{"type": "ui_state", "after_step": 7, "target": {"selector": {"text": "空调"}}, "checked": True},
                               {"type": "vehicle_prop", "prop": "HVAC_POWER_ON", "expect": "true"}, {"type": "bogus"}],
                "checks": [{"key": "x", "op": "eq", "expected": 1}]}, ensure_ascii=False)

    monkeypatch.setattr(planner, "get_gateway", lambda: Gw())
    monkeypatch.setattr(planner.uimap, "load_map", lambda *a, **k: None)
    c = TestCase(case_id="TC-SET-902", title="设置打开空调", channel=["adb"], precondition="空调已关闭",
                 steps=["打开设置", "点击空调", "打开空调开关"], expected="空调开关为开，空调运行")
    body, how = await planner.ui_plan(c)
    assert how == "model"
    assert [a["type"] for a in body["assertions"]] == ["vision", "ui_state", "vehicle_prop", "rule", "value"]
    assert body["assertions"][1]["after_step"] == 4  # 原序号 7 → 重新编号后 4
    assert len({a["id"] for a in body["assertions"]}) == 5
    assert planner.validate({"schema": planner.SCHEMA, "case_id": c.case_id, "requires": ["adb"], **body}, c)[0] == []


# ---------- 自愈 ----------
async def test_heal_after_reflow_then_replay_passes():
    """空调页改版：原 id 找不到 → 阻塞；云端自愈建议选到新控件 → 确认写回 → 回放通过。"""
    plan = full({
        "steps": [{"id": 1, "action": "launch_app", "package": SETTINGS},
                  {"id": 2, "action": "tap", "target": {"selector": {"key": "nav-空调"}}},
                  {"id": 3, "action": "tap", "target": {"selector": {"key": "hvac_power-开关"}}},
                  {"id": 4, "action": "read_prop", "prop": "HVAC_POWER_ON"}],
        "assertions": [{"id": "a1", "after_step": 4, "type": "vehicle_prop", "prop": "HVAC_POWER_ON", "expect": True}],
        "teardown": [{"action": "close_app", "package": SETTINGS}]}, case_id="TC-SETH-001")
    res = await run(plan, bugs="hvac_reflow", rid="R-heal-1")
    assert res.verdict == "blocked" and res.block_reason == "locator"
    step = heal.failed_step(res.model_dump())
    assert step == 3
    s = await heal.suggest(res.evidence_dir, plan, step, case_step="打开空调开关",
                           decider=ScriptedVlm(["hvac_switch_v2-开关"]))
    assert s["found"] and s["new_target"]["selector"] == {"key": "hvac_switch_v2-开关"} and "hvac_switch_v2" in s["diff"]

    lib = get_caselib()
    c = TestCase(case_id="TC-SETH-001", title="设置打开空调", channel=["adb"], precondition="p", steps=STEPS,
                 expected="开", models=["A01-CN"])
    lib.save_case(c)
    v1 = await planner.save_plan(c, plan)
    c.plan_path, c.plan_version = planner.plan_rel_path(c), v1
    lib.save_case(c)
    out = await heal.confirm(c.case_id, s, "张工")
    assert out["applied"] and out["plan_version"] != v1
    new = yaml.safe_load(planner.read_plan(c.case_id))
    assert new["steps"][2]["target"]["selector"]["key"] == "hvac_switch_v2-开关" and new["meta"]["healed"]
    assert (await run(new, bugs="hvac_reflow", rid="R-heal-2")).verdict == "passed"

    blocked = await run(plan, bugs="hvac_reflow", rid="R-heal-3")  # 找不到时不硬选
    assert not (await heal.suggest(blocked.evidence_dir, plan, 3, decider=ScriptedVlm(["no_such_id"])))["found"]


async def test_locator_block_uploads_only_fail_scene(agent, cloud):  # noqa: F811
    """台架定位失败阻塞：只上传失败现场（截图 + 控件树），供云端自愈；云端按执行编号出建议。"""
    await case_with_plan(steps=[{"id": 1, "action": "key", "key": "home"},
                                {"id": 2, "action": "tap", "target": {"selector": {"key": "没有这个控件"}}},
                                {"id": 3, "action": "screenshot", "domain": "center"}])
    st = await submit()
    run_id = next(iter(get_service().remote))
    await agent.claim_once()
    await agent.drain()
    await st.job
    puts = sorted(p.rsplit("/", 1)[-1] for m, p in agent.net.seen if m == "PUT")
    assert puts == ["fail-02.png", "fail-02.ui.json"]
    row = cloud.run(run_id)
    assert json.loads(row["result"])["block_reason"] == "locator" and heal.failed_step(json.loads(row["result"])) == 2
    ev = get_settings().evidence_dir / run_id
    assert json.loads((ev / "fail-02.ui.json").read_text("utf8"))["elements"]  # 云端收到了控件树
    with pytest.raises(ValueError, match="失败截图无法解析"):  # 假设备的截图不是完整 PNG：明确报错，不出建议
        await heal.suggest(ev, yaml.safe_load(planner.read_plan("TC-RIG-001")), 2, decider=ScriptedVlm(["设置"]))


def test_marks_borrow_inner_text_and_skip_huge():
    from cabin_agent.device.base import UIElement
    els = [UIElement(id=0, key="root", clickable=True, bounds=[0, 0, 1000, 600]),
           UIElement(id=1, key="com.x:id/row", clickable=True, bounds=[100, 100, 500, 160]),
           UIElement(id=2, text="空调", bounds=[110, 110, 200, 150]),
           UIElement(id=3, key="com.x:id/sw", clickable=True, checked=False, bounds=[400, 110, 480, 150])]
    marks = um.marks_from(els, (1000, 600))
    assert [m.key for m in marks] == ["com.x:id/row", "com.x:id/sw"]
    assert marks[0].text == "空调" and marks[1].checkable and marks[0].ratio == [0.3, 0.2167]
    assert "2. （无文字） · id:sw · 开关" in um.marks_text(marks)
    assert um.marks_from([], (1000, 600), [("空调", [0, 0, 10, 10], 0.9), ("噪声", [0, 0, 1, 1], 0.2)])[0].source == "ocr"


# ---------- 网页入口用的接口 ----------
async def test_web_endpoints_for_review_and_heal(cloud, monkeypatch):  # noqa: F811
    """登录用户：录制详情与截图、定位失败列表、自愈详情、建议只能处理一次。"""
    import shutil
    import time as _t

    from cabin_cloud import auth
    tok = auth.issue_session(auth.User(id="u-web", name="李工"))
    web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Authorization": f"Bearer {tok}"})
    try:
        r = await record_hvac(ScriptedVlm(["nav-空调", "hvac_power-开关"]), "TC-SET-960")
        uimap.accept_upload(r, {sha: assets.read_image(sha) for sha in r.image_shas()}, "B2")
        d = (await web.get(f"/api/uimap/recordings/{r.rec_id}/detail")).json()
        assert d["recording"]["status"] == "待确认" and d["plan"]["steps"][0]["action"] == "launch_app"
        img = await web.get(d["image_base"] + d["recording"]["trace"][0]["marked_sha"])
        assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
        assert (await web.get("/api/uimap/images/not-a-sha")).status_code == 400
        ok = await web.post(f"/api/uimap/recordings/{r.rec_id}/confirm", json={"props": [{"prop": "HVAC_POWER_ON", "expect": "true"}]})
        assert ok.json()["status"] == "已确认" and uimap.load_recording(r.rec_id).by == "李工"

        # 定位失败的执行：失败现场在云端证据目录，结果在 run 表
        plan = full({"steps": [{"id": 1, "action": "launch_app", "package": SETTINGS},
                               {"id": 2, "action": "tap", "target": {"selector": {"key": "nav-空调"}}},
                               {"id": 3, "action": "tap", "target": {"selector": {"key": "hvac_power-开关"}}}],
                     "assertions": [{"id": "a1", "type": "rule", "rule": "no_crash"}]}, case_id="TC-SETW-001")
        res = await run(plan, bugs="hvac_reflow", rid="R-web-heal")
        c = TestCase(case_id="TC-SETW-001", title="t", channel=["adb"], precondition="p", steps=STEPS, expected="开")
        c.plan_path = planner.plan_rel_path(c)
        get_caselib().save_case(c)
        await planner.save_plan(c, plan)
        ev = get_settings().evidence_dir / "R-web-heal"
        ev.mkdir(parents=True, exist_ok=True)
        for f in __import__("pathlib").Path(res.evidence_dir).glob("fail-*"):
            if f.resolve() != (ev / f.name).resolve():
                shutil.copy(f, ev / f.name)
        cloud.x("INSERT INTO run(id, case_id, rig_id, status, result, block_reason, verdict, created, updated) "
                "VALUES(?,?,?,?,?,?,?,?,?)", ("R-web-heal", c.case_id, "B2", "done", res.model_dump_json(), "locator",
                                              "blocked", _t.time(), _t.time()))
        rows = (await web.get("/api/heal/candidates")).json()["runs"]
        row = next(x for x in rows if x["run_id"] == "R-web-heal")
        assert row["step"] == 3 and row["has_scene"] and row["suggestion"] is None
        monkeypatch.setattr(heal, "_HealDecider", lambda: ScriptedVlm(["hvac_switch_v2-开关"]))
        s = (await web.post("/api/heal/R-web-heal", json={"case_step": "打开空调开关"})).json()
        assert s["found"] and s["status"] == "待确认"
        det = (await web.get("/api/heal/R-web-heal")).json()
        assert det["fail_shot"].endswith("fail-03.png") and det["marked_shot"].endswith("heal-03.png")
        out = (await web.post("/api/heal/R-web-heal/confirm", json={"ok": True})).json()
        assert out["applied"]
        again = await web.post("/api/heal/R-web-heal/confirm", json={"ok": True})  # 同一条建议不能写回两次
        assert again.status_code == 400 and "已接受" in again.json()["detail"]
        row = next(x for x in (await web.get("/api/heal/candidates")).json()["runs"] if x["run_id"] == "R-web-heal")
        assert row["suggestion"]["status"] == "已接受"
    finally:
        await web.aclose()
