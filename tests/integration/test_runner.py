"""TaskPlan Runner：干跑、回放、失败语义、证据、评估器、与缺陷闭环集成（假设备 + 假判定，不调模型）。"""
import asyncio
import json
import re

import pytest
import yaml

from cabin_agent import assets, runner as agent_runner  # noqa: F401
from cabin_agent.device.base import DeviceAdapter, Health, UIElement
from cabin_agent.runner.actions import registered as actions
from cabin_agent.runner.actions.adb import AdbChannel
from cabin_agent.runner.context import AssertResult, PlanResult, synthesize
from cabin_agent.runner.engine import PlanRunner
from cabin_agent.runner.evaluators import EvalContext, registered as evaluators
from cabin_agent.runner.evidence import Evidence, redact
from cabin_agent.runner import precheck
from cabin_cloud import audioasset, planloader as loader, planner
from cabin_cloud.caselib import get_caselib
from cabin_cloud.execsvc.defects import to_candidates
from cabin_common.schema import TestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeDevice(DeviceAdapter):
    name = "fake"

    def __init__(self, ui=("设置", "显示"), log_after_tap=""):
        self.ui = list(ui)
        self.calls = []
        self.lines = []
        self.log_after_tap = log_after_tap
        self.fail_first_dump = False

    async def health(self):
        return Health(online=True, ota="C01-OTA-1.0.0", packages=[])

    async def shell(self, cmd):
        self.calls.append(("shell", cmd))
        return ""

    async def screenshot(self, fmt="png"):
        self.calls.append(("screenshot",))
        return PNG

    async def dump_ui(self):
        return [UIElement(id=i, text=t, key=t, clickable=True, bounds=[10, 10 + i * 40, 110, 40 + i * 40])
                for i, t in enumerate(self.ui)]

    async def tap(self, x, y):
        self.calls.append(("tap", x, y))
        if self.log_after_tap:
            self.lines.append(self.log_after_tap)

    async def swipe(self, x1, y1, x2, y2, ms=300):
        self.calls.append(("swipe", x1, y1, x2, y2, ms))

    async def key(self, name):
        self.calls.append(("key", name))

    async def text(self, s):
        self.calls.append(("text", s))

    async def logs(self, cursor=None):
        i = int(cursor or 0)
        return "\n".join(self.lines[i:]), len(self.lines)


class FakeJudge:
    def __init__(self, consistent=True, raises=None):
        self.consistent, self.raises, self.n = consistent, raises, 0

    async def vision(self, req):
        self.n += 1
        if self.raises:
            raise self.raises
        return {"consistent": self.consistent, "diff_summary": "说明文案被截断", "reason": "出现省略号", "confidence": 0.93,
                "element_id": 1, "actual_observed": "根据环境光…"}


def plan(**kw):
    p = {"schema": planner.SCHEMA, "case_id": "TC-RUN-001", "title": "t", "requires": ["adb"], "timeout_s": 30,
         "setup": [],
         "steps": [{"id": 1, "action": "key", "key": "home"},
                   {"id": 2, "action": "tap", "target": {"selector": {"text": "设置"}}},
                   {"id": 3, "action": "screenshot", "domain": "center"}],
         "assertions": [{"id": "a1", "after_step": 3, "type": "vision", "expected": "文案完整"},
                        {"id": "a2", "type": "rule", "rule": "no_crash"}],
         "teardown": [{"action": "key", "key": "home"}], "on_fail": "continue"}
    p.update(kw)
    return p


def run(p, dev=None, judge=None, rid="R-t", **kw):
    dev = dev or FakeDevice()
    return PlanRunner(p, channels=[AdbChannel(dev)], run_id=rid, judge=judge or FakeJudge(), **kw).execute(), dev


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import cabin_agent.runner.actions.adb as adb
    import cabin_agent.runner.engine as eng
    monkeypatch.setattr(adb, "SETTLE_S", 0)
    monkeypatch.setattr(eng, "RETRY_INTERVAL_S", 0)


def test_action_registry_matches_planner_library():
    assert set(actions()) <= set(planner.ACTIONS)
    assert set(evaluators()) <= planner.ASSERT_TYPES


# ---------- 干跑 ----------
async def test_check_reports_capability_for_voice_plan():
    c = TestCase(case_id="TC-RUN-010", title="v", channel=["audio", "adb"], utterance="打开空调", expect_screen="开",
                 expect_tts="空调已打开", precondition="p")
    p = {"schema": planner.SCHEMA, "case_id": c.case_id, "requires": ["adb", "audio"], **planner.voice_plan(c)}
    assert "测试音频未合成" in "；".join(loader.check(p, {"adb", "audio"}, c)["errors"])
    await audioasset.fill_plan(p)
    r = loader.check(p, {"adb"}, c)
    assert r["reason"] == "capability" and "audio" in "；".join(r["errors"])
    assert loader.check(p, {"adb", "audio"}, c)["reason"] == ""


def test_check_passes_adb_plan_and_flags_invalid():
    assert loader.check(plan(), {"adb"})["reason"] == ""
    bad = plan(steps=[{"id": 1, "action": "fly"}])
    assert loader.check(bad, {"adb"})["reason"] == "plan_invalid"


async def test_dry_run_does_not_touch_device():
    lib = get_caselib()
    c = TestCase(case_id="TC-RUN-011", title="dry", channel=["adb"], plan_status="ready", review_status="通过",
                 plan_path="", expected="e")
    c.plan_path = planner.plan_rel_path(c)
    lib.save_case(c)
    await planner.save_plan(c, plan(case_id=c.case_id))
    r = loader.dry_run(c.case_id, {"adb"})
    assert r["ok"] and r["n_steps"] == 3 and all(s["ready"] for s in r["steps"])
    assert loader.dry_run("TC-NOPE-001", {"adb"})["reason"] == "plan_invalid"


# ---------- 回放 ----------
async def test_replay_pass_writes_evidence_and_runs_teardown():
    coro, dev = run(plan(), rid="R-pass", rig="B2", plan_version="abc")
    r = await coro
    assert r.verdict == "passed" and [s.action for s in r.steps] == ["key", "tap", "screenshot"]
    assert dev.calls[-1] == ("key", "home")  # teardown
    assert {a.assertion_id: a.result for a in r.asserts} == {"a1": "pass", "a2": "pass"}
    m = r.manifest
    assert m["run_id"] == "R-pass" and m["plan_version"] == "abc"
    kinds = {f["kind"] for f in m["files"]}
    assert {"screenshot", "ui"} <= kinds and all(len(f["sha256"]) == 64 for f in m["files"])


async def test_vision_fail_keeps_going_then_failed_and_makes_candidate():
    p = plan()
    p["steps"].append({"id": 4, "action": "key", "key": "back"})
    coro, dev = run(p, judge=FakeJudge(consistent=False), rid="R-vis")
    r = await coro
    assert r.verdict == "failed" and len(r.steps) == 4  # on_fail=continue
    c = to_candidates(r, "T1", "B2", "OTA", "com.x", "/s.png")
    assert len(c) == 1 and c[0].rule == "J-01" and c[0].summary == "说明文案被截断" and c[0].diff_bbox == [10, 50, 110, 80]


async def test_on_fail_stop():
    p = plan(on_fail="stop")
    p["steps"].append({"id": 4, "action": "key", "key": "back"})
    coro, _ = run(p, judge=FakeJudge(consistent=False), rid="R-stop")
    assert len((await coro).steps) == 3


async def test_locator_retry_then_block_and_skip_bound_assertion():
    p = plan()
    p["steps"][1]["target"]["selector"]["text"] = "不存在"
    coro, dev = run(p, rid="R-loc")
    r = await coro
    assert r.verdict == "blocked" and r.block_reason == "locator"
    assert r.steps[-1].result == "fail" and r.steps[-1].attempts == 2
    res = {a.assertion_id: a.result for a in r.asserts}
    assert res == {"a1": "skipped", "a2": "pass"}
    assert any(f["name"].startswith("fail-") for f in r.manifest["files"])  # 失败现场
    assert dev.calls[-1] == ("key", "home")


async def test_drag_ratio_prefers_slider_over_same_named_title():
    """滑条名常在说明里，标题文字完全相同；按比例拖动要落在滑条上，不能拖到标题。"""
    title = UIElement(id=0, text="亮度", cls="android.widget.TextView", bounds=[112, 210, 176, 253])
    bar = UIElement(id=1, desc="亮度", key="android:id/seekbar", cls="android.widget.SeekBar", bounds=[16, 285, 704, 325])
    dev = FakeDevice()
    dev.dump_ui = lambda: asyncio.sleep(0, [title, bar])
    swipes = []

    async def swipe(*a):
        swipes.append(a)
    dev.swipe = swipe
    ch = AdbChannel(dev)
    await ch.start()

    class Ev:
        facts: dict = {}

        def fact(self, k, v):
            self.facts[k] = v

    class Ctx:
        adb, images, warnings, evidence = ch, {}, [], Ev()

    drag = actions()["drag"].handler
    await drag(Ctx, {"action": "drag", "target": {"selector": {"text": "亮度"}}, "to": {"ratio": 1.0}})
    assert swipes == [(360, 305, 704, 305, 800)]
    # 不是按比例拖动（拖到另一控件）时，文字定位不变
    assert (await ch.locate(Ctx, {"selector": {"text": "亮度"}})).el is title


async def test_crash_outranks_blocked():
    """崩溃后定位失败：真正的结论是崩溃，不是阻塞。"""
    crash = "E AndroidRuntime: FATAL EXCEPTION: main\nE AndroidRuntime: Process: com.cabin.sim.settings, PID: 1\n\tat a.b.C.d(C.java:1)"
    p = plan()
    p["steps"].insert(2, {"id": 5, "action": "tap", "target": {"selector": {"text": "没有了"}}})
    coro, _ = run(p, dev=FakeDevice(log_after_tap=crash), rid="R-crash", packages=["com.cabin.sim.settings"])
    r = await coro
    assert r.verdict == "failed" and r.block_reason == "locator"
    c = to_candidates(r, "T", "B2", "O", "", "/s.png")
    assert [x.rule for x in c] == ["J-02"] and c[0].severity == "A" and "a.b.C.d" in c[0].fingerprint


async def test_crash_of_other_package_ignored():
    crash = "FATAL EXCEPTION: main\nProcess: com.other, PID: 1"
    coro, _ = run(plan(), dev=FakeDevice(log_after_tap=crash), rid="R-other", packages=["com.cabin.sim.settings"])
    assert (await coro).verdict == "passed"


async def test_wait_timeout_default_blocked_or_failed_by_config():
    w = {"id": 2, "action": "wait", "until": {"text": "永不出现"}, "timeout_s": 0.05}
    coro, _ = run(plan(steps=[w], assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}]), rid="R-w1")
    r = await coro
    assert r.verdict == "blocked" and r.block_reason == "wait_timeout"
    coro, _ = run(plan(steps=[{**w, "on_timeout": "failed"}], assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}]),
                  rid="R-w2")
    assert (await coro).verdict == "failed"


async def test_judge_unavailable_is_blocked_not_failed():
    coro, _ = run(plan(), judge=FakeJudge(raises=RuntimeError("网关超时")), rid="R-judge")
    r = await coro
    assert r.verdict == "blocked" and r.block_reason == "judge_error" and "网关超时" in r.block_detail


async def test_run_timeout_blocked_and_teardown():
    class Slow(FakeDevice):
        async def tap(self, x, y):
            await asyncio.sleep(5)

    coro, dev = run(plan(timeout_s=0.05), dev=Slow(), rid="R-slow")
    r = await coro
    assert r.block_reason == "timeout" and dev.calls[-1] == ("key", "home")


async def test_capability_blocks_without_touching_device():
    coro, dev = run(plan(requires=["adb", "can"]), rid="R-cap")
    r = await coro
    assert r.verdict == "blocked" and r.block_reason == "capability" and dev.calls == []


async def test_setup_without_command_warns_and_continues():
    p = plan(setup=[{"action": "shell", "cmd_ref": "precondition:x", "note": "空调已开启"}])
    coro, _ = run(p, rid="R-setup")
    r = await coro
    assert r.verdict == "passed" and any("空调已开启" in w for w in r.warnings)


async def test_unmet_precondition_lowers_trust_but_not_crash():
    p = plan(setup=[{"action": "shell", "cmd_ref": "precondition:x", "note": "开关初始为关闭"}])
    coro, _ = run(p, judge=FakeJudge(consistent=False), rid="R-unmet")
    r = await coro
    assert r.manifest["facts"]["setup_unmet"] == ["开关初始为关闭"]
    c = to_candidates(r, "T", "B2", "O", "com.x", "/s.png")[0]
    assert c.level == "低" and c.judged_by == "低置信" and "前置条件未自动设置" in c.reason

    crash = AssertResult(assertion_id="a", type="rule", result="fail",
                         detail={"rule": "J-02", "kind": "Java 崩溃", "package": "p", "frames": ""})
    r2 = PlanResult(run_id="r", case_id="c", asserts=[crash], manifest={"facts": {"setup_unmet": ["x"]}})
    assert to_candidates(r2, "T", "B2", "O", "p", "/s.png")[0].level == "高"


async def test_vision_gets_all_screenshots_in_order():
    seen = {}

    class J(FakeJudge):
        async def vision(self, req):
            seen["n"] = len(req.images)
            return await super().vision(req)

    p = plan(steps=[{"id": 1, "action": "screenshot"}, {"id": 2, "action": "key", "key": "back"},
                    {"id": 3, "action": "screenshot"}])
    coro, _ = run(p, judge=J(), rid="R-multi")
    await coro
    assert seen["n"] == 2


async def test_gateway_budget_for_thinking_models(monkeypatch):
    """强制思考的模型：小预算调用也要抬高，否则思考吃光额度、正文为空。"""
    from cabin_cloud import modelgw

    class Msg:
        stop_reason = "end_turn"
        content = []

        class usage:
            input_tokens = output_tokens = 1

    captured = {}

    class St:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def get_final_message(self):
            return Msg

    gw = modelgw.ModelGateway()
    monkeypatch.setattr(gw.client.messages, "stream", lambda **kw: (captured.update(kw), St())[1])
    gw._thinking_required.add("m")
    async for _ in gw.stream(system="s", messages=[], label="public", source="t", session="S", max_tokens=600, model="m"):
        pass
    assert captured["max_tokens"] >= modelgw.THINKING_MIN_TOKENS and "thinking" not in captured


async def test_replay_uses_no_model(monkeypatch):
    """R-1：操作层不调模型；text / rule 断言全程不经判定服务。"""
    import cabin_cloud.modelgw as gw
    monkeypatch.setattr(gw, "get_gateway", lambda: (_ for _ in ()).throw(AssertionError("回放调用了模型")))
    p = plan(assertions=[{"id": "a1", "after_step": 3, "type": "text", "expected": "设置"},
                         {"id": "a2", "type": "rule", "rule": "no_crash"}])
    judge = FakeJudge()
    coro, _ = run(p, judge=judge, rid="R-nomodel")
    assert (await coro).verdict == "passed" and judge.n == 0


# ---------- 评估器只读证据：可离线重判 ----------
async def test_rejudge_offline_from_evidence(tmp_path):
    ev = Evidence(tmp_path, "/evidence/x", {})
    ev.add("step-03.png", "screenshot", PNG, step=3)
    ev.add("step-03.ui.json", "ui", json.dumps({"screen": "设置/显示", "elements": [
        {"id": 0, "brief": "b", "bounds": [0, 0, 1, 1], "text": "显示", "desc": "", "key": "k"}]}, ensure_ascii=False).encode(), step=3)
    ctx = EvalContext(case_id="TC", judge=FakeJudge(consistent=False))
    r = await evaluators()["vision"]({"id": "a", "after_step": 3, "expected": "x"}, ev, ctx)
    assert r.result == "fail" and r.detail["screen"] == "设置/显示"
    assert (await evaluators()["text"]({"id": "b", "after_step": 3, "expected": "显示", "match": "exact"}, ev, ctx)).result == "pass"
    assert (await evaluators()["text"]({"id": "c", "after_step": 3, "expected": "声音"}, ev, ctx)).result == "fail"


def test_synthesize_precedence():
    a = lambda r: AssertResult(assertion_id="a", type="rule", result=r)  # noqa: E731
    r = PlanResult(run_id="r", case_id="c", block_reason="locator", asserts=[a("fail")])
    synthesize(r)
    assert r.verdict == "failed"
    r = PlanResult(run_id="r", case_id="c", asserts=[a("pass"), a("error")])
    synthesize(r)
    assert r.verdict == "blocked" and r.block_reason == "judge_error"


def test_redact():
    out = redact("vin LSVAU2180N2183294 tel 13812345678 ok")
    assert "LSVAU2180N2183294" not in out and "13812345678" not in out and "ok" in out
