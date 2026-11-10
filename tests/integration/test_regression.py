"""回归闭环：批次、定时、跨台架复测、不稳定、重启恢复、每日汇总。"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cabin_agent import capture
from cabin_agent.daemon import AgentConfig, RigAgent
from cabin_cloud import planner, rigplan, schedule, stats
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc import rigapi, service as svc_mod
from cabin_cloud.execsvc.service import TaskService
from cabin_cloud.integ import feishu
from cabin_common.schema import DefectCandidate, TaskStatus, TestCase
from test_rigagent import CODE, URL, LocalJudgeStub, Net, cloud, submit  # noqa: F401
from test_runner import FakeDevice, FakeJudge, plan

CST = timezone(timedelta(hours=8))
AT = datetime(2026, 11, 7, 20, 5, tzinfo=CST)


def clean(store):
    for t in ("rig_caseset", "rig_schedule", "regression", "defects", "kv", "tasks"):
        store.x(f"DELETE FROM {t}")
    svc_mod.get_service().tasks.clear()


def verdicts(*consistent):
    """判定桩：依次给出每次判定的结论，用完后沿用最后一个。"""
    left = list(consistent)

    def make():
        return LocalJudgeStub(FakeJudge(consistent=left.pop(0) if len(left) > 1 else left[0]))

    return make


async def rig_agent(tmp_path, rig_id):
    a = RigAgent(AgentConfig(URL, rig_id, "A01-CN", CODE, data_dir=tmp_path / rig_id, send_backoff_s=(0,)), FakeDevice(), Net())
    await a.register()
    await a.heartbeat()
    return a


@pytest.fixture
async def b2(tmp_path, cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(capture, "AFTER_S", 0)
    clean(cloud)
    a = await rig_agent(tmp_path, "B2")
    yield a
    await a.close()


@pytest.fixture
async def b3(tmp_path, b2):
    a = await rig_agent(tmp_path, "B3")
    yield a
    await a.close()


async def make_case(case_id="TC-RG-001") -> TestCase:
    lib = get_caselib()
    c = TestCase(case_id=case_id, title="文案", channel=["adb"], plan_status="ready", review_status="通过", expected="文案完整",
                 models=["A01-CN"], auto_status="已验证")
    c.plan_path = planner.plan_rel_path(c)
    lib.save_case(c)
    await planner.save_plan(c, plan(case_id=case_id))
    return lib.get_case(case_id)


def svc() -> TaskService:
    return svc_mod.get_service()


async def settle(cond, tries=300):
    for _ in range(tries):
        await asyncio.sleep(0.01)
        if cond():
            return True
    return False


async def run_on(agent):
    assert await agent.claim_once()
    await agent.drain()


async def stop_all():
    for st in list(svc().tasks.values()):
        await svc().cancel(st.plan.id)


def retest_run(store):
    r = [x for x in store.recent_runs(10) if x["kind"] == "retest" and x["status"] == "queued"]
    return r[0] if r else None


async def fail_on(agent, store, monkeypatch, *judge):
    """跑一轮，等它进入复测。"""
    monkeypatch.setattr(rigapi, "LocalJudge", verdicts(*judge))
    await make_case()
    r = await rigplan.dispatch(agent.cfg.rig_id, "张工", "S-rg", ["TC-RG-001"])
    assert await settle(lambda: svc().remote)
    await run_on(agent)
    assert await settle(lambda: retest_run(store))
    assert svc().pending_reviews() == []  # 复测前不送审
    return r["regression_id"]


def restart(monkeypatch) -> TaskService:
    """进程没了：内存里的任务全丢，库还在。"""
    new = TaskService()
    monkeypatch.setattr(svc_mod, "_svc", new)
    return new


# ---------- 复测 ----------
async def test_reproduced_goes_to_review_with_both_rigs(b2, b3, cloud, monkeypatch):  # noqa: F811
    rid = await fail_on(b2, cloud, monkeypatch, False, False)
    run = retest_run(cloud)
    assert run["rig_id"] == "B3" and run["owner"] == "张工" and run["regression_id"] == rid
    await run_on(b3)
    assert await settle(lambda: svc().pending_reviews())
    d = svc().pending_reviews()[0]
    assert d.rig == "B2" and d.rigs == ["B3"] and d.finder == "张工" and d.status == "pending"
    assert (get_settings().evidence_dir / d.run_id).is_dir()
    assert not (get_settings().evidence_dir / run["id"]).exists()  # 证据留一份就够
    g = cloud.regressions()[0]
    assert (g["id"], g["total"], g["failed"]) == (rid, 1, 1)  # 复测不算进批次用例数


async def test_not_reproduced_is_flaky(b2, b3, cloud, monkeypatch):  # noqa: F811
    await fail_on(b2, cloud, monkeypatch, False, True)
    await run_on(b3)
    assert await settle(lambda: cloud.list_defects(status="flaky"))
    assert svc().pending_reviews() == []
    d = cloud.list_defects(status="flaky")[0]
    assert not (get_settings().evidence_dir / d["run_id"]).exists()  # 不送审，证据不留
    g = cloud.regressions()[0]
    assert (g["flaky"], g["failed"], g["passed"]) == (1, 0, 0)
    row = next(s for s in stats.overview() if s["model"] == "A01-CN")
    assert row["flaky"] >= 1 and svc().tasks[d["task_id"]].status == TaskStatus.done


async def test_single_rig_retests_on_itself(b2, cloud, monkeypatch):  # noqa: F811
    await fail_on(b2, cloud, monkeypatch, False, False)
    assert retest_run(cloud)["rig_id"] == "B2"
    await run_on(b2)
    assert await settle(lambda: svc().pending_reviews())
    assert svc().pending_reviews()[0].rigs == []


async def test_other_ota_is_not_a_retest_rig(b2, b3, cloud, monkeypatch):  # noqa: F811
    cloud.x("UPDATE rig SET ota='另一个版本' WHERE id='B3'")
    monkeypatch.setattr(b3, "ota", "另一个版本", raising=False)
    await fail_on(b2, cloud, monkeypatch, False)
    assert retest_run(cloud)["rig_id"] == "B2"


async def test_no_retest_rig_still_goes_to_review(b2, cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rigapi, "LocalJudge", verdicts(False))
    await make_case()
    await rigplan.dispatch("B2", "张工", "S-rg", ["TC-RG-001"])
    assert await settle(lambda: svc().remote)
    assert await b2.claim_once()
    cloud.x("UPDATE rig SET last_seen=0")  # 跑完时台架都离线了
    monkeypatch.setattr(b2, "heartbeat", lambda: asyncio.sleep(0))
    await b2.drain()
    assert await settle(lambda: svc().pending_reviews())
    assert "未能复测" in svc().pending_reviews()[0].reason


async def test_cancelled_retest_goes_to_review(b2, cloud, monkeypatch):  # noqa: F811
    await fail_on(b2, cloud, monkeypatch, False)
    await svc().cancel(retest_run(cloud)["task_id"])
    assert await settle(lambda: svc().pending_reviews())
    assert "未能复测" in svc().pending_reviews()[0].reason


async def test_new_failure_during_retest_stays_on_parent_task(b2, cloud, monkeypatch):  # noqa: F811
    """原问题没复现，却失败在别处：原问题记不稳定，新问题送审，审完原任务才结束。"""
    await fail_on(b2, cloud, monkeypatch, False)
    held = cloud.list_defects(status="retest")[0]
    other = DefectCandidate(**{**held, "id": "D-other", "task_id": "T-retest", "fingerprint": "别的指纹", "status": "pending"})
    await svc()._retest_verdict(held["task_id"], "B2", "failed", [other])
    await stop_all()
    d = svc().pending_reviews()[0]
    assert d.id == "D-other" and d.task_id == held["task_id"]
    assert cloud.get_defect(held["id"])["status"] == "flaky"


async def test_known_problem_skips_retest(b2, cloud, monkeypatch):  # noqa: F811
    """已经在待审核的问题再失败，直接合并，不用占台架复测。"""
    await fail_on(b2, cloud, monkeypatch, False, False)
    await run_on(b2)
    assert await settle(lambda: svc().pending_reviews())
    await rigplan.dispatch("B2", "张工", "S-rg", ["TC-RG-001"])
    assert await settle(lambda: svc().remote)
    await run_on(b2)
    assert await settle(lambda: cloud.recent_runs(1)[0]["status"] == "done")
    await asyncio.sleep(0.05)
    assert retest_run(cloud) is None and len(svc().pending_reviews()) == 1


async def test_live_task_is_not_retested(b2, cloud, monkeypatch):  # noqa: F811
    """现场下发的任务不是回归，失败直接送审。"""
    monkeypatch.setattr(rigapi, "LocalJudge", verdicts(False))
    await make_case()
    st = await submit("TC-RG-001", "T-rg-live")
    await run_on(b2)
    await st.job
    assert len(svc().pending_reviews()) == 1 and retest_run(cloud) is None


async def test_batch_does_not_notify_per_case(b2, cloud, monkeypatch):  # noqa: F811
    sent = []

    async def notify(title, fields, link="", template="red"):
        sent.append(title)
        return {"tier": "simulated", "ok": True, "title": title, "fields": fields}

    monkeypatch.setattr(feishu, "notify", notify)
    await make_case()
    await rigplan.dispatch("B2", "张工", "S-rg", ["TC-RG-001"])
    assert await settle(lambda: svc().remote)
    await run_on(b2)
    assert await settle(lambda: cloud.regressions()[0]["passed"] == 1)
    await asyncio.sleep(0.05)
    assert sent == []


async def test_deadline_grows_with_queue_position(b2):
    """排在后面的用例要等前面跑完，一轮几十条时不能按第一条的时限超时。"""
    await make_case("TC-RG-010")
    await make_case("TC-RG-011")
    await rigplan.dispatch("B2", "张工", "S-rg", ["TC-RG-010", "TC-RG-011"])
    limits = sorted(st.plan.limits.max_duration_s for st in svc().tasks.values())
    assert limits == [rigplan.PER_CASE_S, rigplan.PER_CASE_S * 2]
    await stop_all()


# ---------- 定时 ----------
async def test_schedule_fires_once_a_day(b2, b3, cloud):  # noqa: F811
    await make_case()
    rigplan.save_caseset("B2", ["TC-RG-001"], "张工")
    rigplan.save_caseset("B3", ["TC-RG-001"], "张工")
    before = AT - timedelta(hours=1)
    schedule.save_schedule("B2", "20:00", True, "张工", before)
    schedule.save_schedule("B3", "20:00", True, "张工", before)
    assert await schedule.fire_schedules(before) == ""  # 没到点
    rid = await schedule.fire_schedules(AT)
    assert rid.startswith("RG") and await schedule.fire_schedules(AT + timedelta(minutes=1)) == ""
    assert await settle(lambda: len(svc().remote) == 2)
    runs = cloud.recent_runs(2)
    assert {r["rig_id"] for r in runs} == {"B2", "B3"} and {r["regression_id"] for r in runs} == {rid}
    assert {r["owner"] for r in runs} == {""}  # 没有发现人，归模块成员
    assert (await b2.client.call("POST", "/api/v1/runs/claim"))["regression_id"] == rid
    assert await schedule.fire_schedules(AT + timedelta(days=1)) != ""  # 第二天再跑
    await stop_all()


async def test_saving_past_time_waits_for_tomorrow(b2):
    schedule.save_schedule("B2", "20:00", True, "张工", AT)
    assert await schedule.fire_schedules(AT + timedelta(minutes=1)) == ""


async def test_late_start_catches_up_only_within_window(b2):
    await make_case()
    rigplan.save_caseset("B2", ["TC-RG-001"], "张工")
    schedule.save_schedule("B2", "20:00", True, "张工", AT - timedelta(hours=1))
    assert await schedule.fire_schedules(AT + timedelta(hours=3)) == ""
    assert await schedule.fire_schedules(AT + timedelta(minutes=30)) != ""
    await stop_all()


async def test_offline_rig_recorded_in_batch(b2, cloud):  # noqa: F811
    await make_case()
    rigplan.save_caseset("B2", ["TC-RG-001"], "张工")
    schedule.save_schedule("B2", "20:00", True, "张工", AT - timedelta(hours=1))
    cloud.x("UPDATE rig SET last_seen=0")
    rid = await schedule.fire_schedules(AT)
    g = cloud.regressions()[0]
    assert g["id"] == rid and g["total"] == 0 and "离线" in g["skipped"][0]["reason"]


def test_bad_time_rejected():
    with pytest.raises(ValueError):
        schedule.save_schedule("B2", "25:00", True, "张工")


# ---------- 重启恢复 ----------
async def test_restart_keeps_queued_run(b2, cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rigapi, "LocalJudge", verdicts(False))
    await make_case()
    await rigplan.dispatch("B2", "张工", "S-rg", ["TC-RG-001"])
    assert await settle(lambda: svc().remote)
    new = restart(monkeypatch)
    assert new.resume() == 1
    await run_on(b2)
    assert await settle(lambda: retest_run(cloud))  # 结果回来后照常进入复测


async def test_restart_while_waiting_for_retest(b2, b3, cloud, monkeypatch):  # noqa: F811
    await fail_on(b2, cloud, monkeypatch, False, False)
    new = restart(monkeypatch)
    assert new.resume() == 1
    await run_on(b3)
    assert await settle(lambda: new.pending_reviews())
    assert new.pending_reviews()[0].rigs == ["B3"]


async def test_orphan_held_candidates_go_to_review(b2, cloud, monkeypatch):  # noqa: F811
    await fail_on(b2, cloud, monkeypatch, False)
    d = cloud.list_defects(status="retest")[0]
    cloud.x("UPDATE run SET status='cancelled' WHERE kind='retest'")  # 复测已经不在了
    new = restart(monkeypatch)
    new.resume()
    assert await settle(lambda: new.pending_reviews())
    assert new.pending_reviews()[0].id == d["id"]


async def test_review_after_restart_finishes_task(b2, cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rigapi, "LocalJudge", verdicts(False))
    await make_case()
    st = await submit("TC-RG-001", "T-rg-review")
    await run_on(b2)
    await st.job
    d = svc().pending_reviews()[0]
    new = restart(monkeypatch)
    await new.review(d.id, "reject", by="张工", reason="判定误报")
    assert cloud.get_task("T-rg-review")["status"] == TaskStatus.done.value


async def test_late_result_still_creates_candidate(b2, cloud, monkeypatch):  # noqa: F811
    """断网超过宽限期：任务已中断，结果补传后失败不能丢。"""
    monkeypatch.setattr(rigapi, "LocalJudge", verdicts(False))
    await make_case()
    st = await submit("TC-RG-001", "T-rg-late")
    assert await b2.claim_once()
    svc().remote.clear()
    st.job.cancel()
    await asyncio.sleep(0.01)
    st.status = TaskStatus.interrupted
    await b2.drain()
    assert await settle(lambda: svc().pending_reviews())


# ---------- 每日汇总 ----------
class Sent:
    def __init__(self):
        self.out, self.fail = [], 0

    async def notify(self, title, fields, link="", template="red"):
        self.out.append((title, dict(fields), link, template))
        return {"tier": "card", "ok": len(self.out) > self.fail, "note": ""}


@pytest.fixture
def sent(monkeypatch):
    s = Sent()
    monkeypatch.setattr(feishu, "notify", s.notify)
    return s


MORNING = datetime.now(CST).replace(hour=8, minute=31)


async def test_digest_sends_once_with_review_link(b2, cloud, monkeypatch, sent):  # noqa: F811
    await fail_on(b2, cloud, monkeypatch, False, False)
    await run_on(b2)
    assert await settle(lambda: svc().pending_reviews())
    assert await schedule.send_digest(MORNING.replace(hour=7)) is False  # 没到点
    assert await schedule.send_digest(MORNING) is True
    assert await schedule.send_digest(MORNING) is False
    title, f, link, tpl = sent.out[0]
    assert "待审核 1" in title and link.endswith("/board#review") and tpl == "red"
    assert f["待审核"] and any("失败 1" in v for v in f.values())


async def test_digest_retries_after_failure(b2, sent):
    sent.fail = 1
    assert await schedule.send_digest(MORNING) is False
    assert await schedule.send_digest(MORNING) is True


async def test_digest_lists_skipped_and_no_regression(b2, cloud, sent):  # noqa: F811
    await schedule.send_digest(MORNING)
    assert sent.out[0][1]["回归"] and sent.out[0][3] == "green"
    cloud.x("DELETE FROM kv")
    rid = cloud.create_regression("定时", "")
    cloud.set_regression_skipped(rid, [{"rig": "B2", "reason": "台架 B2 离线"}])
    await schedule.send_digest(MORNING)
    assert "离线" in sent.out[1][1]["未执行"]
