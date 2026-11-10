"""语音闭环：xlsx 导入、前置条件命令、模拟音频时序、双线判定分类、缺陷映射（假设备 + 假判定）。"""
import io
import json

import pytest
import yaml
from openpyxl import Workbook

from cabin_cloud import audioasset, auth, casexls, planner
from cabin_cloud.caselib import get_caselib
from cabin_cloud import planloader as loader
from cabin_agent.runner.actions.adb import AdbChannel
from cabin_agent.runner.actions.audio import SimAudioChannel
from cabin_cloud.execsvc.defects import to_candidates
from cabin_agent.runner.engine import PlanRunner
from cabin_cloud.judgeclient import LocalJudge, VoiceReq, listen_match, voice_class
from cabin_common.schema import TestCase
from cabin_cloud.store import get_store

from test_runner import FakeDevice, fast  # noqa: F401  复用假设备与提速夹具


def xlsx(rows, head=("标题", "测试语句", "期望屏幕", "期望播报", "前置条件", "类型")):
    wb = Workbook()
    wb.active.append(list(head))
    for r in rows:
        wb.active.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class VoiceDevice(FakeDevice):
    voice_capable = True

    def __init__(self, tts="已为您调到 24 度", delay_ms=0, wake=True):
        super().__init__()
        self.tts, self.delay_ms, self.wake_ok, self.said = tts, delay_ms, wake, []

    async def voice_wake(self, text):
        return {"ok": self.wake_ok, "reply": "我在" if self.wake_ok else ""}

    async def voice_say(self, text):
        self.said.append(text)
        return {"tts": self.tts, "delay_ms": self.delay_ms}


class FakeVoiceJudge:
    """看线由 look 决定，听线用真实 listen_match。"""

    def __init__(self, look=True):
        self.look = look

    async def voice(self, req: VoiceReq):
        listen = not req.no_tts and listen_match(req.tts_expected, req.transcript or "")
        cls = voice_class(self.look, listen, req.no_tts)
        return {"result": "pass" if cls == "pass" else "fail", "class": cls, "transcript": req.transcript or "",
                "lines": {"look": "pass" if self.look else "fail", "listen": "pass" if listen else "fail"}, "confidence": 0.95,
                "look": {"consistent": self.look, "actual_observed": "24℃", "reason": "r"}}


def case(**kw):
    base = dict(case_id="TC-VT-001", title="调到 24 度", channel=["audio", "adb"], utterance="空调调到 24 度", models=["A01-CN"],
                expect_screen="空调温度显示 24℃", expect_tts="已为您调到 24 度", precondition="空调已开启，当前 22℃", latency_max_ms=1500,
                voice_profile="female_a")
    base.update(kw)
    return TestCase(**base)


def voice_plan(c):
    return {"schema": planner.SCHEMA, "case_id": c.case_id, "title": c.title, "requires": ["adb", "audio"], "timeout_s": 30,
            **planner.voice_plan(c), "on_fail": "continue"}


@pytest.fixture(autouse=True)
def fast_audio(monkeypatch):
    import cabin_agent.runner.actions.audio as au
    monkeypatch.setattr(au, "SPEAK_S", 0)


async def run(dev, judge=None, c=None, rid="V-1", plan=None):
    c = c or case()
    p = plan or voice_plan(c)
    if not plan:
        await audioasset.fill_plan(p)
    return await PlanRunner(p, channels=[AdbChannel(dev), SimAudioChannel(dev)], run_id=rid, case=c,
                            judge=judge or FakeVoiceJudge()).execute()


# ---------- xlsx ----------
def test_read_rows_aliases_and_requires_title_column():
    rows = casexls.read_rows(xlsx([("打开空调", "打开空调", "开", "空调已打开", "空调已关闭", "正向")],
                                  head=("用例标题", "语句", "屏幕期望", "播报期望", "前置", "类型")))
    assert rows[0][1]["标题"] == "打开空调" and rows[0][1]["期望播报"] == "空调已打开"
    with pytest.raises(ValueError, match="表头"):
        casexls.read_rows(xlsx([("a",)], head=("随便",)))
    with pytest.raises(ValueError, match="读不了"):
        casexls.read_rows(b"not xlsx")


def test_import_goes_through_review_gate_and_reports_bad_rows():
    data = xlsx([("座椅加热 3 档", "座椅加热调到 3 档", "座椅加热 3 档", "已调到 3 档", "座椅已开启", "正向"),
                 ("缺播报", "座椅加热调到 2 档", "2 档", "", "p", "正向"),
                 ("类型错", "座椅加热调到 1 档", "1 档", "已调到 1 档", "p", "大概")])
    lib = get_caselib()
    batch, cases, shown, bad = casexls.import_workbook(data, "t.xlsx", "upload:t.xlsx")
    assert len(cases) == 1 and len(shown) == 3 and {b["row"] for b in bad} == {3, 4}
    assert "期望播报" in bad[0]["errors"][0] and "类型" in bad[1]["errors"][0]
    c = cases[0]
    assert c.review_status == "待审核" and c.source == "迁移" and c.channel == ["audio", "adb"]
    assert c.latency_max_ms == 1500 and c.voice_profile == "female_a" and c.steps and c.case_id.startswith("TC-")
    assert c.case_id not in {x.case_id for x in lib.pull("A01-CN")}  # 未审核不能拉取
    lib.submit_review(batch.batch_id, [{"case_id": c.case_id, "decision": "pass"}])
    assert c.case_id in {x.case_id for x in lib.pull("A01-CN")}


def test_import_ignores_review_status_in_sheet():
    data = xlsx([("偷跑", "座椅通风调到 1 档", "1 档", "已调到 1 档", "p", "通过")],
                head=("标题", "测试语句", "期望屏幕", "期望播报", "前置条件", "审核状态"))
    _, cases, _, _ = casexls.import_workbook(data, "t2.xlsx", "x")
    assert all(c.review_status == "待审核" for c in cases)


async def test_off_topic_goes_to_test_only_assistant():
    """只有测试一个场景：与测试无关的话交给助手，助手的提示词要求只回答测试相关的问题，也没有资料检索和联网工具。"""
    from cabin_cloud.assistant.prompts import system_prompt
    from cabin_cloud.assistant.tools import tools_for
    from cabin_cloud.gateway import Session

    from test_casegen import _FakeWS

    s = Session(_FakeWS(), "S-off", surface="agent")
    s.tts_on = False
    assert await s.route(1, "这台车好看吗") is False and s.mode == "lab"
    p = system_prompt()
    assert "只回答和测试有关的问题" in p and "车型资料" not in p and "web_search" not in p
    assert not {"kb_search", "web_search"} & {t.name for t in tools_for(True)}


def test_sync_intent():
    from cabin_cloud import casewf
    assert casewf.wants("同步多维表格", False) == "sync" and casewf.wants("从飞书读取用例", False) == "sync"


def test_tests_never_touch_real_feishu():
    from cabin_cloud.integ.bitable import get_bitable
    assert not get_bitable().enabled


def test_sample_sheet_parses():
    assert casexls.SAMPLE.exists()
    rows = casexls.read_rows(casexls.SAMPLE.read_bytes())
    assert len(rows) == 8 and all(casexls.to_case(f)[1] == [] for _, f in rows)


def test_precondition_commands():
    f = planner.sim_precondition_cmd
    assert f("空调已开启，当前 22℃") == "cabin-hvac on 22" and f("空调已关闭") == "cabin-hvac off"
    assert f("空调关闭；车机语音已唤醒") == "cabin-hvac off" and f("座椅已加热") == ""
    p = voice_plan(case())
    assert p["setup"][0]["cmd"] == "cabin-hvac on 22"
    errs, warns = planner.validate(p, case())
    assert errs == [] and not any("车型命令" in w for w in warns)
    p["setup"][0]["cmd"] = "rm -rf /"
    assert any("白名单" in e for e in planner.validate(p, case())[0])


async def test_yaml_roundtrip_is_runnable():
    p = voice_plan(case())
    await audioasset.fill_plan(p)
    text = planner.dump_yaml(p)
    assert loader.check(yaml.safe_load(text), {"adb", "audio"}, case())["reason"] == ""


# ---------- 测试音频 ----------
async def test_assets_are_cached_and_shared(fake_tts):
    a, b = voice_plan(case(utterance="空调调到 27 度")), voice_plan(case(utterance="空调调到 27 度"))
    c = voice_plan(case(utterance="空调调到 28 度"))
    for p in (a, b, c):
        await audioasset.fill_plan(p)
    assert a["assets"]["utt.wav"]["sha256"] == b["assets"]["utt.wav"]["sha256"]
    assert a["assets"]["wake.wav"]["sha256"] == c["assets"]["wake.wav"]["sha256"]  # 唤醒词全局一份
    texts = [x[0] for x in fake_tts]
    assert texts.count("空调调到 27 度") == 1 and texts.count("你好千禾") <= 1
    assert a["assets"]["utt.wav"]["duration_ms"] >= 0 and a["assets"]["utt.wav"]["size"] > 0


async def test_cache_key_covers_voice_and_speed(fake_tts):
    n0 = len(fake_tts)
    await audioasset.ensure("换音色测试", "female_a")
    await audioasset.ensure("换音色测试", "male_b")
    await audioasset.ensure("换音色测试", "female_a", 1.2)
    assert len(fake_tts) == n0 + 3
    await audioasset.ensure("换音色测试", "female_a")
    assert len(fake_tts) == n0 + 3


async def test_lost_file_is_resynthesized(fake_tts):
    r = await audioasset.ensure("文件丢失测试", "female_a")
    audioasset.asset_path(r["sha256"]).unlink()
    n0 = len(fake_tts)
    await audioasset.ensure("文件丢失测试", "female_a")
    assert len(fake_tts) == n0 + 1


def test_cache_key_covers_model():
    assert audioasset.cache_key("a", "v", 1.0, "m") != audioasset.cache_key("a", "v", 1.0, "m2")  # 模型升级不串缓存


async def test_runner_blocks_without_audio():
    dev = VoiceDevice()
    r = await run(dev, rid="V-noasset", plan=voice_plan(case()))
    assert r.verdict == "blocked" and r.block_reason == "plan_invalid" and "未合成" in r.block_detail and dev.calls == []


async def test_runner_blocks_on_tampered_audio():
    p = voice_plan(case(utterance="空调调到 29 度"))
    await audioasset.fill_plan(p)
    audioasset.asset_path(p["assets"]["utt.wav"]["sha256"]).write_bytes(b"tampered")
    dev = VoiceDevice()
    r = await run(dev, rid="V-tamper", plan=p)
    assert r.verdict == "blocked" and "哈希不符" in r.block_detail and dev.said == []


async def test_generate_for_failure_when_tts_down(monkeypatch):
    async def boom(*a):
        raise RuntimeError("网关超时")

    monkeypatch.setattr(audioasset, "synthesize", boom)
    lib = get_caselib()
    c = case(case_id="TC-VT-050", utterance="合成失败测试")
    lib.save_case(c)
    r = await planner.generate_for(lib.get_case(c.case_id))
    got = lib.get_case(c.case_id)
    assert not r["ok"] and "音频合成失败" in r["errors"][0] and got.plan_status == "invalid"
    assert planner.needs_generation(got)  # 下次拉取重试


async def test_needs_generation_for_old_plan_without_audio():
    lib = get_caselib()
    c = case(case_id="TC-VT-051", utterance="旧计划", plan_status="ready", review_status="通过")
    c.plan_path = planner.plan_rel_path(c)
    lib.save_case(c)
    await planner.save_plan(c, {"schema": planner.SCHEMA, **planner.voice_plan(c)})
    assert planner.needs_generation(c)
    await planner.generate_for(c)
    assert not planner.needs_generation(lib.get_case(c.case_id))


async def test_a11_asset_download():
    from fastapi.testclient import TestClient

    from cabin_cloud.main import app
    lib = get_caselib()
    c = case(case_id="TC-VT-052", utterance="下载测试", plan_status="ready", review_status="通过")
    lib.save_case(c)
    await planner.generate_for(lib.get_case(c.case_id))
    c = lib.get_case(c.case_id)
    token = auth.issue_session(auth.User(id="t", name="测试"))
    api = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    r = api.get(f"/api/v1/plans/{c.case_id}/assets/utt.wav")
    plan = yaml.safe_load(planner.read_plan(c.case_id))
    assert r.status_code == 200 and r.headers["x-asset-sha256"] == plan["assets"]["utt.wav"]["sha256"]
    assert api.get(f"/api/v1/plans/{c.case_id}/assets/no.wav").status_code == 404
    assert len(api.get(f"/api/v1/plans/{c.case_id}").headers["x-plan-sha256"]) == 64


# ---------- 判定 ----------
@pytest.mark.parametrize("exp,got,ok", [("已为您调到 24 度", "已为您调到 24 度。", True), ("已为您调到 24 度", "已为您调到 26 度", False),
                                        ("空调已打开", "", False), ("空调已打开", "好的，空调已打开", True)])
def test_listen_match(exp, got, ok):
    assert listen_match(exp, got) is ok


@pytest.mark.parametrize("look,listen,no_tts,cls", [(True, True, False, "pass"), (True, False, False, "tts_mismatch"),
                                                    (False, True, False, "exec_or_display"), (False, False, False, "recog_or_exec"),
                                                    (True, False, True, "no_tts")])
def test_voice_class_matrix(look, listen, no_tts, cls):
    assert voice_class(look, listen, no_tts) == cls


async def test_local_judge_refuses_real_audio_without_voice_service():
    with pytest.raises(RuntimeError, match="语音服务未接入"):
        await LocalJudge().voice(VoiceReq("c", "s", "t", b"", tts_audio=b"RIFF", transcript=None))


# ---------- 执行 ----------
async def test_voice_pass_writes_timeline_and_evidence():
    dev = VoiceDevice(delay_ms=20)
    r = await run(dev, rid="V-pass")
    assert r.verdict == "passed", r
    assert dev.said == ["空调调到 24 度"] and ("shell", "cabin-hvac on 22") in dev.calls
    m = r.manifest
    assert {"t0", "tts_start", "tts_end"} <= set(m["marks"]) and m["facts"]["wake"] == "ok" and m["facts"]["host_muted"] is False
    assert {f["kind"] for f in m["files"]} >= {"audio", "tts_clip", "screenshot", "ui"}
    assert {a.type: a.result for a in r.asserts} == {"voice": "pass", "timing": "pass", "rule": "pass"}


async def test_tts_mismatch_makes_candidate():
    r = await run(VoiceDevice(tts="已为您调到 26 度"), rid="V-tts")
    assert r.verdict == "failed"
    c = to_candidates(r, "T", "B2", "O", "", "/s.png")
    assert [x.rule for x in c] == ["J-07"] and c[0].kind == "播报不符" and "tts_mismatch" in c[0].fingerprint


async def test_screen_wrong_is_exec_or_display():
    r = await run(VoiceDevice(), judge=FakeVoiceJudge(look=False), rid="V-look")
    assert r.verdict == "failed" and to_candidates(r, "T", "B2", "O", "", "/s")[0].kind == "执行或显示异常"


async def test_no_tts_is_failed_timing_skipped():
    r = await run(VoiceDevice(tts=""), rid="V-none")
    assert r.verdict == "failed"
    assert {a.type: a.result for a in r.asserts}["timing"] == "skipped"
    assert to_candidates(r, "T", "B2", "O", "", "/s")[0].kind == "无播报"


async def test_slow_tts_is_timing_failure():
    r = await run(VoiceDevice(delay_ms=60), c=case(latency_max_ms=10), rid="V-slow")
    assert r.verdict == "failed"
    c = to_candidates(r, "T", "B2", "O", "", "/s")
    assert [x.rule for x in c] == ["J-08"] and "超过上限" in c[0].summary


async def test_wake_failure_blocks_and_still_restores_host():
    dev = VoiceDevice(wake=False)
    r = await run(dev, rid="V-wake")
    assert r.verdict == "blocked" and r.block_reason == "wake_failed" and dev.said == []
    assert r.manifest["facts"]["wake"] == "no_response" and r.manifest["facts"]["host_muted"] is False  # teardown 恢复讲解
