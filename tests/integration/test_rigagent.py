"""云端—台架闭环：真实的云端接口 + 真实的 Agent，用 ASGI 传输直连，假设备 + 假判定，可模拟断网。"""
import asyncio
import json

import httpx
import pytest

from cabin_agent.client import CloudClient, CloudDown, CloudRejected
from cabin_agent.daemon import AgentConfig, RigAgent
from cabin_agent.outbox import Outbox
from cabin_common.schema import CaseRef, ExploreTask, Origin, PlanStep, Target, TestCase
from cabin_cloud import planner
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc import rigapi, service as svc_mod
from cabin_cloud.main import app
from cabin_cloud.store import get_store
from test_runner import PNG, FakeDevice, FakeJudge, plan

CODE = "7K3Q-9XPA"
URL = "http://cloud"


class Net(httpx.AsyncBaseTransport):
    """可以拔网线的传输层，并记录请求。"""

    def __init__(self):
        self.up = True
        self.inner = httpx.ASGITransport(app=app)
        self.seen = []

    async def handle_async_request(self, request):
        if not self.up:
            raise httpx.ConnectError("网线拔了")
        self.seen.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)


class LocalJudgeStub:
    def __init__(self, judge):
        self.judge = judge

    async def vision(self, req):
        assert req.images == [PNG]  # 图片经 multipart 原样到达云端
        return await self.judge.vision(req)


@pytest.fixture
def cloud(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rig_register_code", CODE)
    monkeypatch.setattr(s, "rig_id", "B2")
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=True)))
    store = get_store()
    store.x("DELETE FROM rig")
    store.x("DELETE FROM run")
    store.x("DELETE FROM review_memory")
    svc_mod.get_service().remote.clear()
    yield store


@pytest.fixture
async def agent(tmp_path, cloud):
    net = Net()
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), net)
    a.net = net
    await a.register()
    yield a
    await a.close()


async def case_with_plan(case_id="TC-RIG-001", **kw) -> TestCase:
    lib = get_caselib()
    c = TestCase(case_id=case_id, title="文案", channel=["adb"], plan_status="ready", review_status="通过",
                 expected="文案完整", models=["A01-CN"])
    c.plan_path = planner.plan_rel_path(c)
    lib.save_case(c)
    await planner.save_plan(c, plan(case_id=case_id, **kw))
    return lib.get_case(case_id)


async def submit(case_id="TC-RIG-001", tid="T-rig-1"):
    p = ExploreTask(id=tid, origin=Origin(utterance="u", session="S-rig"),
                    case_ref=CaseRef(source="库", case_id=case_id, expected="文案完整"),
                    target=Target(rig="B2", package=""), steps=[PlanStep(type="taskplan", case_id=case_id)])
    st = await svc_mod.get_service().submit(p, "S-rig")
    for _ in range(100):
        await asyncio.sleep(0.01)
        if st.status.value == "排队" and svc_mod.get_service().remote:
            return st
    raise AssertionError(f"执行单元没有入池：{st.status}")


def client(token=""):
    return CloudClient(URL, token, transport=httpx.ASGITransport(app=app))


# ---------- 注册与鉴权 ----------
async def test_register_needs_code_and_cannot_take_over(cloud):
    c = client()
    body = {"rig_id": "B7", "model": "A01-CN"}
    with pytest.raises(CloudRejected) as e:
        await c.call("POST", "/api/v1/rigs/register", json={**body, "register_code": "错的"})
    assert e.value.code == 1002
    tok = (await c.call("POST", "/api/v1/rigs/register", json={**body, "register_code": CODE}))["rig_token"]
    assert tok and tok not in json.dumps(cloud.rig("B7"))  # 云端只存哈希
    with pytest.raises(CloudRejected) as e:  # 拿到注册码也不能抢占已注册的台架
        await c.call("POST", "/api/v1/rigs/register", json={**body, "register_code": CODE})
    assert e.value.code == 1002
    assert (await client(tok).call("POST", "/api/v1/rigs/register", json=body))["rig_token"] == ""  # 持令牌更新信息


async def test_register_refused_when_cloud_has_no_code(cloud, monkeypatch):
    monkeypatch.setattr(get_settings(), "rig_register_code", "")
    with pytest.raises(CloudRejected):
        await client().call("POST", "/api/v1/rigs/register", json={"rig_id": "B7", "model": "A01-CN", "register_code": ""})


async def test_register_rejects_unknown_model(cloud):
    with pytest.raises(CloudRejected) as e:
        await client().call("POST", "/api/v1/rigs/register", json={"rig_id": "B7", "model": "没这车", "register_code": CODE})
    assert e.value.code == 1001


async def test_api_requires_token_and_version(agent):
    with pytest.raises(CloudRejected) as e:
        await client("fake-token").call("POST", "/api/v1/runs/claim")
    assert e.value.code == 1002
    with pytest.raises(CloudRejected) as e:
        await agent.client.call("POST", "/api/v1/runs/claim", headers={"X-Cabin-Api": "9"})
    assert e.value.code == 1003


async def test_token_persisted_and_reused(agent, tmp_path):
    b = RigAgent(AgentConfig(URL, "B2", "A01-CN", "", data_dir=tmp_path / "a"), FakeDevice(), agent.net)
    await b.register()  # 没有注册码也能启动
    assert b.client.token == agent.client.token
    await b.close()


# ---------- 任务池 ----------
async def test_claim_is_exclusive_and_only_for_own_rig(agent, cloud):
    ids = [cloud.create_run("T", "S", "B2", "TC-X-001", "v", "replay", 30, {}, {}) for _ in range(2)]
    other = cloud.create_run("T", "S", "B3", "TC-X-001", "v", "replay", 30, {}, {})
    got = await asyncio.gather(*[asyncio.to_thread(cloud.claim_run, "B2") for _ in range(8)])
    won = [r["id"] for r in got if r]
    assert sorted(won) == sorted(ids)  # 两个执行单元各被领取一次
    assert cloud.run(other)["status"] == "queued"
    assert await agent.client.call("POST", "/api/v1/runs/claim") is None  # B3 的不会发给 B2


async def test_other_rig_cannot_touch_my_run(agent, cloud):
    tok = (await client().call("POST", "/api/v1/rigs/register",
                               json={"rig_id": "B9", "model": "A01-CN", "register_code": CODE}))["rig_token"]
    run = cloud.create_run("T", "S", "B2", "TC-X-001", "v", "replay", 30, {}, {})
    cloud.claim_run("B2")
    with pytest.raises(CloudRejected):
        await client(tok).call("POST", f"/api/v1/runs/{run}/result", json={"attempt": 1, "verdict": "pass", "plan_result": {}})
    assert cloud.run(run)["result"] is None


async def test_evidence_name_and_hash_checked(agent, cloud):
    run = cloud.create_run("T", "S", "B2", "TC-X-001", "v", "replay", 30, {}, {})
    cloud.claim_run("B2")
    for name in ("../../evil.py", "..", "a/b.png"):
        with pytest.raises(CloudRejected):
            await agent.client.call("POST", "/api/v1/evidence/upload-url", json={
                "run_id": run, "attempt": 1, "file": name, "size": 1, "sha256": "x"})
    with pytest.raises(CloudRejected):
        await agent.client.call("PUT", f"/api/v1/evidence/{run}/a.png?sha256=00", content=b"x")


async def test_result_is_idempotent_and_attempt_checked(agent, cloud):
    await case_with_plan()
    st = await submit()
    await agent.claim_once()
    run = next(iter(svc_mod.get_service().remote))
    await agent.drain()
    await st.job
    n = len([e for e in cloud.replay("S-rig") if e["type"] == "run.late_result"])
    res = json.loads(cloud.run(run)["result"])
    await agent.client.call("POST", f"/api/v1/runs/{run}/result", json={"attempt": 1, "verdict": "pass", "plan_result": res})
    assert len([e for e in cloud.replay("S-rig") if e["type"] == "run.late_result"]) == n
    with pytest.raises(CloudRejected) as e:
        await agent.client.call("POST", f"/api/v1/runs/{run}/result", json={"attempt": 2, "verdict": "pass", "plan_result": res})
    assert e.value.code == 2102


# ---------- 闭环 ----------
async def test_full_loop_over_http(agent, cloud):
    await case_with_plan()
    st = await submit()
    run = next(iter(svc_mod.get_service().remote))
    assert await agent.claim_once()
    assert st.status.value == "执行中"
    await agent.drain()
    await st.job
    assert st.status.value == "已完成" and st.plan_result.verdict == "passed"
    assert [s.idx for s in st.run.steps] == [1, 2, 3]
    assert st.run.final_shot.startswith(f"/evidence/{run}/")
    row = cloud.run(run)
    assert row["status"] == "done" and row["acked_seq"] > 0
    assert not agent.outbox.head()
    assert not [p for m, p in agent.net.seen if m == "PUT"]  # 通过的用例只上报摘要，证据留在本地
    assert ("POST", "/api/v1/judge/vision") in agent.net.seen  # 判定走云端
    assert await agent.claim_once() is False


async def test_failure_reaches_review_over_http(agent, cloud, monkeypatch):
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=False)))
    await case_with_plan()
    st = await submit()
    await agent.claim_once()
    await agent.drain()
    await st.job
    pend = svc_mod.get_service().pending_reviews("S-rig")
    assert st.status.value == "候选待审核" and pend[-1].rule == "J-01" and pend[-1].actual_shot
    puts = [p for m, p in agent.net.seen if m == "PUT"]
    assert any(p.endswith("/manifest.json") for p in puts) and any(p.endswith("/result.json") for p in puts)


async def test_offline_rig_refused(cloud):
    await case_with_plan()
    p = ExploreTask(id="T-rig-off", origin=Origin(utterance="u", session="S-rig"),
                    case_ref=CaseRef(source="库", case_id="TC-RIG-001", expected="文案完整"),
                    target=Target(rig="B2", package=""), steps=[PlanStep(type="taskplan", case_id="TC-RIG-001")])
    st = await svc_mod.get_service().submit(p, "S-rig")
    await st.job
    assert st.status.value == "环境异常"


async def test_ai_explore_refused_in_cloud_mode(cloud):
    p = ExploreTask(id="T-rig-ai", origin=Origin(utterance="u", session="S-rig"),
                    case_ref=CaseRef(source="库", case_id="X", expected="e"), target=Target(rig="B2", package=""),
                    steps=[PlanStep(type="ai_explore", goal="g")])
    assert any("AI 探索" in e for e in svc_mod.validate_plan(p))


# ---------- 断网 ----------
async def test_offline_run_finishes_then_backfills_in_order(agent, cloud):
    """规则断言不依赖云端：断网时跑完，事件、证据、结果留在本地，恢复后按序补传。"""
    await case_with_plan(assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}])
    st = await submit()
    claim = await agent.client.call("POST", "/api/v1/runs/claim")
    run = claim["run_id"]
    agent.net.up = False
    res = await agent.execute(claim)
    assert res.verdict == "passed"
    with pytest.raises(CloudDown):
        await agent.drain()
    counts = agent.outbox.counts()
    assert counts["result"] == 1 and counts["events"] >= 3
    assert cloud.run(run)["result"] is None
    agent.net.up, agent.net.seen = True, []
    await agent.drain()
    await st.job
    assert st.status.value == "已完成" and not agent.outbox.head()
    assert agent.net.seen[-1] == ("POST", f"/api/v1/runs/{run}/result")


async def test_network_drop_midway_does_not_duplicate_events(agent, cloud):
    await case_with_plan(assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}])
    st = await submit()
    await agent.claim_once()
    item_id, kind, body = agent.outbox.head()
    await agent._send(kind, body)  # 已送达，但确认没回来：下一轮会重发
    await agent.drain()
    await st.job
    assert [s.idx for s in st.run.steps] == [1, 2, 3]


async def test_outbox_survives_agent_restart(agent, cloud, tmp_path):
    await case_with_plan(assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}])
    st = await submit()
    claim = await agent.client.call("POST", "/api/v1/runs/claim")
    agent.net.up = False
    await agent.execute(claim)
    agent.net.up = True
    b = RigAgent(AgentConfig(URL, "B2", "A01-CN", "", data_dir=tmp_path / "a"), FakeDevice(), agent.net)
    await b.drain()  # 新进程接着传
    await st.job
    assert st.status.value == "已完成" and not b.outbox.head()
    await b.close()


async def test_judge_unavailable_is_blocked_not_failed(agent, cloud):
    await case_with_plan()
    st = await submit()
    claim = await agent.client.call("POST", "/api/v1/runs/claim")
    agent.client.backoff = ()
    agent.net.up = False
    res = await agent.execute(claim)
    assert res.verdict == "blocked" and res.block_reason == "judge_error"
    agent.net.up = True
    await agent.drain()
    await st.job
    assert st.status.value == "执行中断"


def test_outbox_keeps_order(tmp_path):
    o = Outbox(tmp_path / "o.sqlite3")
    o.put("events", {"n": 1})
    o.put("file", {"n": 2})
    assert o.counts() == {"events": 1, "file": 1}
    i, k, b = o.head()
    assert (k, b) == ("events", {"n": 1})
    o.done(i)
    assert o.head()[2] == {"n": 2}
    o.close()


# ---------- 取消 ----------
async def test_cancel_before_claim_never_runs(agent, cloud):
    await case_with_plan()
    st = await submit()
    run = next(iter(svc_mod.get_service().remote))
    await svc_mod.get_service().cancel(st.plan.id)
    await asyncio.gather(st.job, return_exceptions=True)
    assert cloud.run(run)["status"] == "cancelled"
    assert await agent.claim_once() is False


async def test_cancel_running_reaches_rig_through_heartbeat(agent, cloud):
    gate = asyncio.Event()

    class Slow(FakeDevice):
        async def tap(self, x, y):
            await gate.wait()

    agent.dev = Slow()
    await case_with_plan(assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}])
    st = await submit()
    run = next(iter(svc_mod.get_service().remote))
    job = asyncio.create_task(agent.claim_once())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if agent.dev.calls:
            break
    await svc_mod.get_service().cancel(st.plan.id)
    await asyncio.gather(st.job, return_exceptions=True)
    assert cloud.run(run)["status"] == "cancelling"
    await agent.heartbeat()
    await job
    await agent.drain()
    row = cloud.run(run)
    assert row["status"] == "done" and json.loads(row["result"])["block_reason"] == "cancelled"
