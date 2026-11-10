"""首次试跑：测试代码先在台架跑一次，通过才算已验证。"""
import asyncio

import pytest

from cabin_agent.daemon import AgentConfig, RigAgent
from cabin_cloud import casewf, planner, rigplan, stats
from cabin_cloud.caselib import get_caselib
from cabin_cloud.execsvc import rigapi, service as svc_mod
from cabin_cloud.store import get_store
from test_rigagent import CODE, URL, LocalJudgeStub, Net, agent, cloud  # noqa: F401  复用夹具
from test_runner import FakeDevice, FakeJudge, plan
from cabin_common.schema import TestCase

RULE_ONLY = [{"id": "a2", "type": "rule", "rule": "no_crash"}]


async def make(case_id, **kw) -> TestCase:
    lib = get_caselib()
    c = TestCase(case_id=case_id, title=case_id, channel=["adb"], plan_status="ready", review_status="通过",
                 expected="e", models=["A01-CN"], auto_status="已生成")
    c.plan_path = planner.plan_rel_path(c)
    lib.save_case(c)
    c.plan_version = await planner.save_plan(c, plan(case_id=case_id, **kw))
    lib.save_case(c)
    return lib.get_case(case_id)


async def queued() -> asyncio.Task:
    svc = svc_mod.get_service()
    for _ in range(200):
        if svc.remote:
            return next(iter(svc.remote.values()))[0]
        await asyncio.sleep(0.01)
    raise AssertionError("没有入池")


@pytest.fixture(autouse=True)
def clean(cloud):  # noqa: F811
    cloud.x("DELETE FROM rig_caseset")


async def run_trial(agent, case_id):  # noqa: F811
    await agent.heartbeat()
    await rigplan.start_trial(case_id, "张工", "S-tr")
    st = await queued()
    await agent.claim_once()
    await agent.drain()
    await st.job
    return st


async def test_pass_verifies_case(agent):  # noqa: F811
    await make("TC-TR-001")
    st = await run_trial(agent, "TC-TR-001")
    c = get_caselib().get_case("TC-TR-001")
    assert c.auto_status == "已验证" and c.trial["verdict"] == "passed" and c.trial["rig"] == "B2" and c.trial["by"] == "张工"
    assert st.status.value == "已完成"


async def test_fail_blocks_verification_and_makes_no_defect(agent, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=False)))
    await make("TC-TR-002")
    st = await run_trial(agent, "TC-TR-002")
    c = get_caselib().get_case("TC-TR-002")
    assert c.auto_status == "试跑未通过" and "说明文案被截断" in c.trial["reason"]
    assert st.defects == [] and st.status.value == "已完成"  # 多半是测试代码的问题，不进缺陷审核


async def test_locator_failure_is_a_test_code_problem(agent):  # noqa: F811
    await make("TC-TR-003", steps=[{"id": 1, "action": "tap", "target": {"selector": {"text": "不存在的按钮"}}}],
               assertions=RULE_ONLY)
    await run_trial(agent, "TC-TR-003")
    c = get_caselib().get_case("TC-TR-003")
    assert c.auto_status == "试跑未通过" and "locator" in c.trial["reason"]


async def test_device_lost_does_not_blame_test_code(agent):  # noqa: F811
    class Lost(FakeDevice):
        async def key(self, name):
            raise ConnectionError("adb 断开")

    agent.dev = Lost()
    await make("TC-TR-004", assertions=RULE_ONLY)
    await run_trial(agent, "TC-TR-004")
    c = get_caselib().get_case("TC-TR-004")
    assert c.auto_status == "已生成" and c.trial["env"] is True


async def test_no_online_rig(cloud):  # noqa: F811
    await make("TC-TR-005")
    with pytest.raises(ValueError, match="没有在线"):
        await rigplan.start_trial("TC-TR-005", "张工", "S-tr")
    assert get_caselib().get_case("TC-TR-005").auto_status == "已生成"


async def test_rig_that_cannot_run_is_skipped(agent):  # noqa: F811
    lib = get_caselib()
    c = await make("TC-TR-006", requires=["adb", "can"])
    c.channel = ["adb", "can"]
    lib.save_case(c)
    with pytest.raises(ValueError, match="没有在线"):
        await rigplan.start_trial("TC-TR-006", "张工", "S-tr")


async def test_cannot_start_twice(agent):  # noqa: F811
    await make("TC-TR-007", assertions=RULE_ONLY)
    await rigplan.start_trial("TC-TR-007", "张工", "S-tr")
    with pytest.raises(ValueError, match="正在试跑"):
        await rigplan.start_trial("TC-TR-007", "张工", "S-tr")
    st = await queued()
    await agent.claim_once()
    await agent.drain()
    await st.job


async def test_cancel_returns_to_generated(agent):  # noqa: F811
    await make("TC-TR-008", assertions=RULE_ONLY)
    await rigplan.start_trial("TC-TR-008", "张工", "S-tr")
    st = await queued()
    await svc_mod.get_service().cancel(st.plan.id)
    await asyncio.gather(st.job, return_exceptions=True)
    assert get_caselib().get_case("TC-TR-008").auto_status == "已生成"


async def test_regenerated_during_trial_voids_result(agent):  # noqa: F811
    c = await make("TC-TR-009", assertions=RULE_ONLY)
    await rigplan.start_trial("TC-TR-009", "张工", "S-tr")
    st = await queued()
    await agent.claim_once()
    c = get_caselib().get_case("TC-TR-009")
    c.plan_version = "新版本"
    c.auto_status = "已生成"
    get_caselib().save_case(c)
    await agent.drain()
    await st.job
    assert get_caselib().get_case("TC-TR-009").auto_status == "已生成"


async def test_trial_not_counted_in_stats(agent):  # noqa: F811
    await make("TC-TR-010", assertions=RULE_ONLY)
    await run_trial(agent, "TC-TR-010")
    assert get_store().recent_runs(1)[0]["kind"] == "trial"
    assert not any(r["executed"] for r in stats.overview())


async def test_regenerating_needs_new_trial_before_dispatch(agent):  # noqa: F811
    await make("TC-TR-011", assertions=RULE_ONLY)
    await run_trial(agent, "TC-TR-011")
    rigplan.save_caseset("B2", ["TC-TR-011"], "张工")
    lib = get_caselib()
    c = lib.get_case("TC-TR-011")
    c.auto_status = "已生成"  # 测试代码重新生成
    lib.save_case(c)
    await agent.heartbeat()
    r = await rigplan.dispatch("B2", "张工", "S-tr")
    assert r["queued"] == [] and "验证" in r["skipped"][0]["reason"]


async def test_prefers_idle_rig(tmp_path, cloud, agent):  # noqa: F811
    b = RigAgent(AgentConfig(URL, "B3", "A01-CN", CODE, data_dir=tmp_path / "b", send_backoff_s=(0,)), FakeDevice(), Net())
    await b.register()
    await b.heartbeat()
    await agent.heartbeat()
    await make("TC-TR-012", assertions=RULE_ONLY)
    await make("TC-TR-013", assertions=RULE_ONLY)
    first = await rigplan.start_trial("TC-TR-012", "张工", "S-tr")
    st = await queued()
    assert first in ("B2", "B3")
    for _ in range(100):
        if get_store().queued_runs(first):
            break
        await asyncio.sleep(0.01)
    assert await rigplan.start_trial("TC-TR-013", "张工", "S-tr") != first
    for t in list(svc_mod.get_service().remote.values()):
        await svc_mod.get_service().cancel(t[0].plan.id)
    await asyncio.gather(st.job, return_exceptions=True)
    await b.close()


async def test_generation_starts_trial_automatically(agent, monkeypatch):  # noqa: F811
    await agent.heartbeat()
    c = await make("TC-TR-014", assertions=RULE_ONLY)

    async def fake(case, session):
        return {"ok": True, "warnings": [], "case_id": case.case_id, "generated_by": "model"}

    monkeypatch.setattr(planner, "generate_for", fake)
    msgs = []

    class S:
        id, state, user = "S-tr", {}, "张工"

        async def send(self, m):
            msgs.append(m)

        async def log(self, t, k=""):
            msgs.append({"type": "log", "text": t})

        async def say(self, t, src=""):
            msgs.append({"type": "say", "text": t})

    await casewf._run_codegen(S(), "A01-CN", [c])
    assert get_caselib().get_case("TC-TR-014").auto_status == "试跑中"
    st = await queued()
    await svc_mod.get_service().cancel(st.plan.id)
    await asyncio.gather(st.job, return_exceptions=True)


async def test_generation_without_rig_says_so(cloud, monkeypatch):  # noqa: F811
    c = await make("TC-TR-015")

    async def fake(case, session):
        return {"ok": True, "warnings": [], "case_id": case.case_id, "generated_by": "model"}

    monkeypatch.setattr(planner, "generate_for", fake)
    said = []

    class S:
        id, state, user = "S-tr2", {}, "张工"

        async def send(self, m):
            pass

        async def log(self, t, k=""):
            pass

        async def say(self, t, src=""):
            said.append(t)

    await casewf._run_codegen(S(), "A01-CN", [c])
    assert "待试跑" in said[-1] and get_caselib().get_case("TC-TR-015").auto_status == "已生成"


# ---------- 审核通过即自动生成测试代码、派首次试跑（不再需要“拉取”这一步） ----------
class Sess:
    def __init__(self, sid):
        self.id, self.state, self.user, self.msgs, self.said = sid, {}, "张工", [], []

    async def send(self, m):
        self.msgs.append(m)

    async def log(self, t, k=""):
        self.msgs.append({"type": "log", "text": t})

    async def say(self, t, src=""):
        self.said.append(t)


def _fake_codegen(monkeypatch, calls):
    async def fake(case, session):
        calls.append(case.case_id)
        lib = get_caselib()
        c = lib.get_case(case.case_id)
        c.plan_status, c.auto_status = "ready", "已生成"
        lib.save_case(c)
        return {"ok": True, "warnings": [], "case_id": case.case_id, "generated_by": "model"}
    monkeypatch.setattr(planner, "generate_for", fake)


async def _pending_batch(bid, case_id):
    from cabin_common.schema import CaseBatch
    lib = get_caselib()
    c = await make(case_id, assertions=RULE_ONLY)
    c.plan_status, c.auto_status, c.review_status, c.batch_id = "", "未生成", "待审核", bid
    lib.save_case(c)
    lib.save_batch(CaseBatch(batch_id=bid, status="待审核", session="S-auto"))
    return c


async def test_review_pass_generates_and_trials_without_pull(agent, monkeypatch):  # noqa: F811
    await agent.heartbeat()
    calls = []
    _fake_codegen(monkeypatch, calls)
    c = await _pending_batch("B-AUTO-1", "TC-TR-101")
    s = Sess("S-auto-1")
    await casewf.submit_review(s, "B-AUTO-1", [{"case_id": c.case_id, "decision": "pass"}])
    assert "现在生成 1 条测试代码" in s.said[-1] and "拉取" not in s.said[-1]
    await s.state["plan_job"]
    assert calls == [c.case_id] and get_caselib().get_case(c.case_id).auto_status == "试跑中"
    stages = [m["name"] for m in s.msgs if m.get("type") == "stage"]
    assert stages[-2:] == ["测试代码", "首次试跑"]
    card = next(m for m in s.msgs if m["type"] == "casepull.result")
    assert card["model"] == "本批" and [r["case_id"] for r in card["cases"]] == [c.case_id]
    st = await queued()
    await svc_mod.get_service().cancel(st.plan.id)
    await asyncio.gather(st.job, return_exceptions=True)


async def test_no_rig_then_dispatched_when_rig_online(cloud, tmp_path, monkeypatch):  # noqa: F811
    calls = []
    _fake_codegen(monkeypatch, calls)
    c = await _pending_batch("B-AUTO-2", "TC-TR-102")
    s = Sess("S-auto-2")
    await casewf.submit_review(s, "B-AUTO-2", [{"case_id": c.case_id, "decision": "pass"}])
    await s.state["plan_job"]
    assert get_caselib().get_case(c.case_id).auto_status == "已生成" and "台架上线后自动补派" in s.said[-1]
    assert c.case_id not in await casewf.dispatch_pending_trials()  # 还没有台架
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), Net())
    await a.register()  # 台架上线
    await a.heartbeat()
    assert c.case_id in await casewf.dispatch_pending_trials()
    assert get_caselib().get_case(c.case_id).auto_status == "试跑中"
    svc = svc_mod.get_service()
    for tid in list(svc.remote):
        st = svc.remote[tid][0]
        await svc.cancel(st.plan.id)
        await asyncio.gather(st.job, return_exceptions=True)
    await a.close()


async def test_claim_prevents_double_generation(cloud, monkeypatch):  # noqa: F811
    """网页审核、多维表格同步、台架 A-15 可能同时触发：同一条用例只生成一次。"""
    c = await _pending_batch("B-AUTO-3", "TC-TR-103")
    c.review_status = "通过"
    get_caselib().save_case(c)
    first = casewf.claim([c])
    assert [x.case_id for x in first] == [c.case_id] and casewf.claim([c]) == []
    calls = []
    _fake_codegen(monkeypatch, calls)
    await casewf.code_and_trial(first)
    assert calls == [c.case_id] and c.case_id not in casewf._inflight
    assert casewf.claim([get_caselib().get_case(c.case_id)]) == []  # 已就绪：不用再生成


async def test_bitable_review_done_triggers_codegen(cloud, monkeypatch):  # noqa: F811
    """在飞书多维表格里审完的批次，同步回来后自动生成测试代码。"""
    c = await _pending_batch("B-AUTO-4", "TC-TR-104")
    c.review_status = "通过"
    get_caselib().save_case(c)

    class Bt:
        enabled, key = True, "k"

        def link(self):
            return "https://x.feishu.cn/base/b?table=t"

        def modules(self):
            return []

    async def fake_sync(bt, route=True):
        return {"rows": 1, "updated": [c.case_id], "created": [], "invalid": [], "replan": [], "batches_done": ["B-AUTO-4"],
                "case_ids": [c.case_id]}

    monkeypatch.setattr(casewf, "all_bitables", lambda: [Bt()])
    monkeypatch.setattr(casewf, "sync_back", fake_sync)
    spawned = []
    real_spawn = casewf.bus.spawn
    monkeypatch.setattr(casewf.bus, "spawn", lambda coro: spawned.append(real_spawn(coro)) or spawned[-1])
    calls = []
    _fake_codegen(monkeypatch, calls)
    r = await casewf.sync_now()
    assert r["ok"] and len(spawned) == 1
    await spawned[0]
    assert calls == [c.case_id] and get_caselib().get_case(c.case_id).plan_status == "ready"
