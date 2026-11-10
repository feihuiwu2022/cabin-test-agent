"""确定性断言、前置条件自动设置、结果统计。"""
import json

import pytest

from cabin_agent.runner.engine import PlanRunner
from cabin_agent.runner.actions.adb import AdbChannel
from cabin_cloud import planloader, planner, precommands, stats
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc.defects import to_candidates
from cabin_cloud.modelgw import get_gateway
from cabin_cloud.store import get_store
from cabin_common.schema import TestCase
from cabin_common.taskplan import compare, shell_allowed, validate
from test_rigagent import CODE, URL, Net, case_with_plan, cloud, submit  # noqa: F401  复用夹具
from test_runner import FakeDevice, FakeJudge, fast, plan  # noqa: F401
from cabin_agent.daemon import AgentConfig, RigAgent


class Reads(FakeDevice):
    def __init__(self, value):
        super().__init__()
        self.value = value

    async def shell(self, cmd):
        self.calls.append(("shell", cmd))
        return f"{self.value}\n"


READ = {"id": 4, "action": "read_value", "read": "settings:system/screen_brightness", "save_as": "brightness"}
CHECK = {"id": "a3", "type": "value", "key": "brightness", "op": "eq", "expected": 255}


def value_plan(**kw):
    p = plan(**kw)
    p["steps"].append(READ)
    p["assertions"].append(CHECK)
    return p


async def run(p, dev):
    return await PlanRunner(p, channels=[AdbChannel(dev)], run_id="R-v", judge=FakeJudge()).execute()


# ---------- 确定性断言 ----------
async def test_exact_value_passes():
    dev = Reads(255)
    r = await run(value_plan(), dev)
    assert r.verdict == "passed" and ("shell", "settings get system screen_brightness") in dev.calls


async def test_250_fails_even_though_vision_passes():
    """这条就是亮度 250 被判通过的问题：视觉一致，但系统值不对。"""
    r = await run(value_plan(), Reads(250))
    res = {a.assertion_id: a.result for a in r.asserts}
    assert res == {"a1": "pass", "a2": "pass", "a3": "fail"} and r.verdict == "failed"
    c = to_candidates(r, "T", "B2", "OTA", "pkg", "/shot.png")
    assert len(c) == 1 and c[0].rule == "J-09" and "250" in c[0].summary and c[0].level == "高"


async def test_getprop_source():
    dev = Reads("11")
    p = value_plan()
    p["steps"][-1] = {**READ, "read": "getprop:ro.build.version.release"}
    await run(p, dev)
    assert ("shell", "getprop ro.build.version.release") in dev.calls


@pytest.mark.parametrize("op,a,b,want", [("eq", "255", 255, True), ("eq", "250", 255, False), ("ge", "256", 255, True),
                                         ("lt", "1.5", 2, True), ("eq", "on", "on", True), ("ne", "on", "off", True),
                                         ("ge", "abc", 1, False), ("eq", "null", 255, False)])
def test_compare(op, a, b, want):
    assert compare(op, a, b) is want


@pytest.mark.parametrize("bad", ["settings:system/x; reboot", "shell:rm -rf /", "settings:other/x", "getprop:a b", ""])
def test_read_source_is_restricted(bad):
    p = value_plan()
    p["steps"][-1] = {**READ, "read": bad}
    assert any("read" in e or "读值" in e for e in validate(p)[0])


def test_value_assert_needs_read_step_and_op():
    p = value_plan()
    p["assertions"][-1] = {**CHECK, "key": "没读过"}
    assert any("read_value" in e for e in validate(p)[0])
    p = value_plan()
    p["assertions"][-1] = {**CHECK, "op": "约等于"}
    assert any("op" in e for e in validate(p)[0])


@pytest.mark.parametrize("cmd,ok", [
    ("settings put system screen_brightness 102", True), ("input keyevent 3", True), ("input keyevent KEYCODE_WAKEUP", True),
    ("am force-stop com.android.settings", True), ("cabin-hvac on 22", True), ("rm -rf /sdcard", False),
    ("settings put system x 1; reboot", False), ("settings put system x 1 && rm a", False), ("reboot", False),
    ("am force-stop a b", False), ("input text abc", False)])
def test_shell_grammar(cmd, ok):
    assert shell_allowed(cmd) is ok


async def test_generated_plan_carries_checks(monkeypatch):
    out = json.dumps({"steps": [{"action": "key", "key": "home"}, {**READ, "id": None}],
                      "checks": [{"key": "brightness", "op": "eq", "expected": 255}]})

    async def fake(**kw):
        assert "read_value" in kw["system"]
        return out

    monkeypatch.setattr(get_gateway(), "complete", fake)
    c = TestCase(case_id="TC-S3-001", title="t", channel=["adb"], steps=["调亮度"], expected="亮度 255")
    p, how = await planner.ui_plan(c)
    a3 = next(a for a in p["assertions"] if a["type"] == "value")
    assert how == "model" and a3["key"] == "brightness" and a3["expected"] == 255


# ---------- 前置条件 ----------
PRE = "设备已解锁且亮屏，处于桌面；自动调整亮度已关闭；当前亮度低于100%"


def setup_plan(note):
    return {"setup": [{"action": "shell", "cmd_ref": "precondition:TC-X-001", "note": note}]}


def test_expand_translates_all_clauses():
    s = precommands.expand(setup_plan(PRE), "A01-CN")["setup"]
    assert [x["cmd"] for x in s] == ["settings put system screen_brightness_mode 0", "settings put system screen_brightness 102"]


def test_expand_keeps_unmatched_as_unmet():
    s = precommands.expand(setup_plan("处于桌面；空调已开到 22 度"), "A01-CN")["setup"]
    assert len(s) == 1 and s[0]["note"] == "空调已开到 22 度" and "cmd" not in s[0]


def test_expand_keeps_existing_commands():
    p = {"setup": [{"action": "shell", "cmd": "cabin-hvac on 22", "note": "空调"}]}
    assert precommands.expand(p, "A01-CN")["setup"] == p["setup"]


def test_expand_by_model(tmp_path, monkeypatch):
    f = tmp_path / "p.yaml"
    f.write_text("preconditions:\n  - {match: [静音], models: [B01-EU], cmd: 'input keyevent 164'}\n  - {match: [静音], cmd: 'input keyevent 91'}\n", "utf8")
    monkeypatch.setattr(get_settings(), "preconditions_file", f)
    assert precommands.expand(setup_plan("静音"), "B01-EU")["setup"][0]["cmd"] == "input keyevent 164"
    assert precommands.expand(setup_plan("静音"), "E01")["setup"][0]["cmd"] == "input keyevent 91"


async def test_load_expands_and_dangerous_table_entry_is_rejected(cloud, tmp_path, monkeypatch):  # noqa: F811
    c = await case_with_plan("TC-S3-010", setup=[{"action": "shell", "cmd_ref": "precondition:TC-S3-010", "note": "开机"}])
    f = tmp_path / "p.yaml"
    f.write_text("preconditions:\n  - {match: [开机], cmd: 'rm -rf /sdcard'}\n", "utf8")
    monkeypatch.setattr(get_settings(), "preconditions_file", f)
    r = planloader.dry_run(c.case_id, {"adb"}, "A01-CN")
    assert not r["ok"] and any("白名单" in e for e in r["errors"])


async def test_setup_commands_run_and_nothing_is_unmet():
    p = value_plan(setup=precommands.expand(setup_plan(PRE), "A01-CN")["setup"])
    dev = Reads(255)
    r = await run(p, dev)
    assert ("shell", "settings put system screen_brightness 102") in dev.calls
    assert "setup_unmet" not in r.manifest["facts"]


# ---------- 统计 ----------
def finish(store, case_id, verdict, ota="OTA-1", model="A01-CN"):
    run = store.create_run("T", "S", "B2", case_id, "v", "replay", 30, {}, {})
    store.claim_run("B2")
    store.finish_run(run, "{}", {"model": model, "ota": ota, "module": "显示", "verdict": verdict, "block_reason": "", "duration_s": 2})
    return run


async def test_stats_counts_latest_result_and_keeps_blocked_apart(cloud):  # noqa: F811
    for cid in ("TC-S3-020", "TC-S3-021", "TC-S3-022", "TC-S3-023"):
        await case_with_plan(cid)
    finish(cloud, "TC-S3-020", "failed")
    finish(cloud, "TC-S3-020", "passed")  # 重跑通过，只算最新一次
    finish(cloud, "TC-S3-021", "failed")
    finish(cloud, "TC-S3-022", "blocked")
    row = next(r for r in stats.overview() if r["ota"] == "OTA-1")
    assert (row["executed"], row["passed"], row["failed"], row["blocked"]) == (3, 1, 1, 1)
    assert row["pass_rate"] == 0.5  # 阻塞没有结论，不算失败
    assert row["should"] == len(get_caselib().pull("A01-CN")) and 0 < row["coverage"] <= 1


async def test_stats_separates_versions_and_models(cloud):  # noqa: F811
    finish(cloud, "TC-S3-030", "passed", ota="OTA-2")
    finish(cloud, "TC-S3-030", "failed", ota="OTA-3")
    finish(cloud, "TC-S3-030", "passed", ota="OTA-3", model="B01-EU")
    keys = {(r["ota"], r["model"]) for r in stats.overview()}
    assert {("OTA-2", "A01-CN"), ("OTA-3", "A01-CN"), ("OTA-3", "B01-EU")} <= keys


async def test_result_summary_reaches_columns(tmp_path, cloud):  # noqa: F811
    net = Net()
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), net)
    await a.register()
    await a.heartbeat()
    await case_with_plan("TC-S3-040", assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}])
    st = await submit("TC-S3-040", tid="T-s3-1")
    await a.claim_once()
    await a.drain()
    await st.job
    r = get_store().recent_runs(1)[0]
    assert (r["model"], r["ota"], r["verdict"]) == ("A01-CN", "C01-OTA-1.0.0", "passed") and r["duration_s"] > 0
    await a.close()
