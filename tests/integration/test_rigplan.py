"""台架配置与派发：车型选择、硬件声明、用例集、缺台架能力清单。"""
import asyncio

import httpx
import pytest

from cabin_agent.client import CloudDown, CloudRejected
from cabin_agent.daemon import AgentConfig, RigAgent
from cabin_agent.localweb import create_app
from cabin_cloud import auth, modules, planner, rigplan
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc import service as svc_mod
from cabin_cloud.main import app
from cabin_common.schema import TestCase
from test_rigagent import CODE, URL, Net, cloud  # noqa: F401  复用夹具
from test_runner import FakeDevice, plan

RULE_ONLY = [{"id": "a2", "type": "rule", "rule": "no_crash"}]


async def make_case(case_id, models=("A01-CN",), requires=("adb",), verified=True) -> TestCase:
    lib = get_caselib()
    c = TestCase(case_id=case_id, title=case_id, channel=list(requires), plan_status="ready", review_status="通过",
                 expected="e", models=list(models), auto_status="已验证" if verified else "已生成")
    c.plan_path = planner.plan_rel_path(c)
    lib.save_case(c)
    await planner.save_plan(c, plan(case_id=case_id, requires=list(requires), assertions=RULE_ONLY))
    return lib.get_case(case_id)


@pytest.fixture
async def agent(tmp_path, cloud):  # noqa: F811
    cloud.x("DELETE FROM rig_caseset")
    net = Net()
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), net)
    a.net = net
    await a.register()
    yield a
    await a.close()


@pytest.fixture
async def web():
    token = auth.issue_session(auth.User(id="u", name="张工"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL,
                                 headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


@pytest.fixture(autouse=True)
def only_test_cases(monkeypatch):
    """用例库在测试之间共享，这里只看本文件建的用例。"""
    lib = get_caselib()
    real = lib.library
    monkeypatch.setattr(lib, "library", lambda: [c for c in real() if c.case_id.startswith("TC-RP-")])


# ---------- 车型与硬件 ----------
async def test_profile_persists_and_reregisters(agent, cloud, tmp_path):  # noqa: F811
    await agent.update_profile("B01-EU", ["can", "camera"])
    r = cloud.rig("B2")
    assert r["model"] == "B01-EU" and "can" in r["capabilities"] and "camera" in r["capabilities"]
    b = RigAgent(AgentConfig(URL, "B2", "A01-CN", "", data_dir=tmp_path / "a"), FakeDevice(), agent.net)
    assert b.cfg.model == "B01-EU" and sorted(b.hardware) == ["camera", "can"]  # 重启后还在
    await b.close()


async def test_profile_refused_while_running(agent):
    agent.state = "running"
    with pytest.raises(ValueError, match="空闲"):
        await agent.update_profile("B01-EU", [])
    assert agent.cfg.model == "A01-CN"


async def test_profile_rejects_unknown_hardware_and_model(agent):
    with pytest.raises(ValueError, match="未知外设"):
        await agent.update_profile("A01-CN", ["laser"])
    with pytest.raises(CloudRejected):
        await agent.update_profile("没这车", [])
    assert agent.cfg.model == "A01-CN" and not (agent.cfg.data_dir / "rig_profile.json").exists()


async def test_profile_not_saved_when_cloud_down(agent):
    agent.net.up = False
    with pytest.raises(CloudDown):
        await agent.update_profile("B01-EU", ["can"])
    assert agent.cfg.model == "A01-CN" and agent.hardware == []


async def test_model_mismatch_warns(agent, cloud):  # noqa: F811
    class Reads(FakeDevice):
        async def health(self):
            h = await super().health()
            h.model = "B01欧版"
            return h

    agent.dev = Reads()
    await agent.register()
    assert "B01-EU" in agent.model_warning
    await agent.update_profile("B01-EU", [])
    assert agent.model_warning == ""


async def test_local_page_saves_profile(agent):
    ui = create_app(agent, 8810, transport=agent.net)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui), base_url="http://127.0.0.1:8810") as c:
        r = await c.put("/local/profile", json={"model": "E01", "hardware": ["camera"]})
        assert r.status_code == 200
        s = (await c.get("/local/status")).json()
        assert s["model"] == "E01" and s["hardware"] == ["camera"]
        agent.state = "running"
        assert (await c.put("/local/profile", json={"model": "D01", "hardware": []})).status_code == 409


async def test_vehicle_list_is_open():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as c:
        rows = (await c.get("/api/v1/vehicles")).json()["data"]
    assert {r["name"] for r in rows} >= {"B01欧版", "A01国内", "A01国际", "D01", "E01"}


# ---------- 能跑什么 ----------
async def test_runnable_follows_model_and_capability(agent):
    await make_case("TC-RP-001")
    await make_case("TC-RP-002", models=("B01-EU",))
    await make_case("TC-RP-003", requires=("adb", "can"))
    assert {c["case_id"] for c in rigplan.runnable("B2")} == {"TC-RP-001"}
    await agent.update_profile("A01-CN", ["can"])
    assert {c["case_id"] for c in rigplan.runnable("B2")} == {"TC-RP-001", "TC-RP-003"}
    await agent.update_profile("B01-EU", [])
    assert {c["case_id"] for c in rigplan.runnable("B2")} == {"TC-RP-002"}


async def test_gaps_list_cases_no_rig_can_run(agent):
    await make_case("TC-RP-010")
    await make_case("TC-RP-011", requires=("adb", "can"))
    g = {x["case_id"]: x for x in rigplan.gaps()}
    assert "TC-RP-010" not in g and "能力" in g["TC-RP-011"]["reasons"]["B2"]


# ---------- 用例集 ----------
async def test_caseset_only_takes_verified_and_runnable(agent):
    await make_case("TC-RP-020")
    await make_case("TC-RP-021", verified=False)
    await make_case("TC-RP-022", requires=("adb", "can"))
    rigplan.save_caseset("B2", ["TC-RP-020"], "张工")
    with pytest.raises(ValueError, match="验证"):
        rigplan.save_caseset("B2", ["TC-RP-021"], "张工")
    with pytest.raises(ValueError, match="能力"):
        rigplan.save_caseset("B2", ["TC-RP-022"], "张工")
    with pytest.raises(ValueError, match="不存在"):
        rigplan.save_caseset("B2", ["TC-RP-999"], "张工")
    assert rigplan.caseset("B2") == ["TC-RP-020"]


async def test_dispatch_runs_caseset_and_records_finder(agent, cloud, web):  # noqa: F811
    await make_case("TC-RP-030")
    await make_case("TC-RP-031")
    assert (await web.put("/api/board/rigs/B2/caseset", json={"case_ids": ["TC-RP-030", "TC-RP-031"]})).status_code == 200
    await agent.heartbeat()
    r = await web.post("/api/board/rigs/B2/run", json={"session": "S-rp"})
    assert r.json()["queued"] == ["TC-RP-030", "TC-RP-031"] and r.json()["skipped"] == []
    svc = svc_mod.get_service()
    for _ in range(100):
        if len(svc.remote) == 2:
            break
        await asyncio.sleep(0.01)
    assert await agent.claim_once() and await agent.claim_once()
    assert await agent.claim_once() is False
    await agent.drain()
    runs = cloud.recent_runs(2)
    assert {x["case_id"] for x in runs} == {"TC-RP-030", "TC-RP-031"} and {x["owner"] for x in runs} == {"张工"}
    assert all(x["status"] == "done" for x in runs)


async def test_temporary_cases_do_not_change_caseset(agent, web):
    await make_case("TC-RP-040")
    await make_case("TC-RP-041")
    rigplan.save_caseset("B2", ["TC-RP-040"], "张工")
    await agent.heartbeat()
    r = await web.post("/api/board/rigs/B2/run", json={"session": "S-rp2", "case_ids": ["TC-RP-041"]})
    assert r.json()["queued"] == ["TC-RP-041"]
    assert rigplan.caseset("B2") == ["TC-RP-040"]


async def test_dispatch_skips_cases_that_no_longer_fit(agent, web):
    await make_case("TC-RP-050")
    await make_case("TC-RP-051", requires=("adb", "can"))
    await agent.update_profile("A01-CN", ["can"])
    rigplan.save_caseset("B2", ["TC-RP-050", "TC-RP-051"], "张工")
    await agent.update_profile("A01-CN", [])  # 摘下 CAN 工具
    await agent.heartbeat()
    j = (await web.post("/api/board/rigs/B2/run", json={"session": "S-rp3"})).json()
    assert j["queued"] == ["TC-RP-050"] and j["skipped"][0]["case_id"] == "TC-RP-051"


async def test_dispatch_refused_when_offline_or_empty(agent, cloud, web):  # noqa: F811
    assert (await web.post("/api/board/rigs/B2/run", json={})).status_code == 409  # 没配用例集
    cloud.x("UPDATE rig SET last_seen=0")
    assert "离线" in (await web.post("/api/board/rigs/B2/run", json={})).text


async def test_session_rig_decides_default_target(agent):
    from test_casegen import _FakeWS

    from cabin_cloud.gateway import Session
    s = Session(_FakeWS(), "S-rp-rig", surface="agent", rig="B2")
    await s.push_state()
    assert s.ws.sent[-1]["rig"] == "B2"


# ---------- 模块成员 ----------
def test_module_members(tmp_path, monkeypatch):
    f = tmp_path / "m.yaml"
    f.write_text('modules:\n  - {code: VOICE, name: 语音, keywords: [语音], folder: 语音, members: [张三, 李四]}\n  - {code: DISP, name: 显示, members: 王五}\n', "utf8")
    monkeypatch.setattr(get_settings(), "modules_file", f)
    rows = {m.code: m for m in modules.all_modules()}
    assert rows["VOICE"].members == ("张三", "李四") and rows["DISP"].members == ("王五",)
    assert rows["VOICE"].as_dict()["members"] == ["张三", "李四"]
