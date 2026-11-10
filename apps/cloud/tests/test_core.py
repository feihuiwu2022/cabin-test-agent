import json
import time

import pytest

from cabin_cloud.assistant.hooks import HookDenied, pre_tool
from cabin_cloud.assistant.tools import ToolCtx, tools_for
from cabin_cloud.execsvc.service import validate_plan
from cabin_cloud.integ.jira import get_jira
from cabin_common.schema import CaseRef, DefectCandidate, Origin, PlanStep, ExploreTask, Target
from cabin_cloud.store import get_store
from cabin_cloud.voice.asr import normalize
from cabin_cloud.voice.tts import SentenceSplitter


def plan(**kw):
    base = dict(id="T-test", origin=Origin(utterance="x", session="S-t"),
                case_ref=CaseRef(source="local", case_id="TC-DISP-012", expected="完整显示"),
                target=Target(rig="B2", package="com.cabin.sim.settings"), steps=[PlanStep(type="taskplan", case_id="TC-DISP-012")])
    base.update(kw)
    return ExploreTask(**base)


def test_taskplan_validation():
    assert validate_plan(plan()) == []
    assert any("台架" in e for e in validate_plan(plan(target=Target(rig="Z9", package="com.cabin.sim.settings"))))
    assert any("期望" in e for e in validate_plan(plan(case_ref=None)))
    assert any("不支持" in e for e in validate_plan(plan(steps=[PlanStep(type="ai_explore")])))
    assert any("红线" in e for e in validate_plan(plan(steps=[PlanStep(type="taskplan", goal="恢复出厂设置")])))


def _defect(fp, ota):
    return DefectCandidate(id="D-x", task_id="T", rule="J-01", kind="显示异常", title="t", summary="s",
                           fingerprint=fp, ota=ota)


def _issue(key, fp, ver, status):
    get_store().x("INSERT OR REPLACE INTO jira_sandbox(key, project, summary, description, fingerprint, found_version,"
                  " affects, status, severity, labels, comments, attachments, dup_of, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (key, "A01", "s", "", fp, ver, json.dumps([ver]), status, "B", "[]", "[]", "[]", "", time.time()))


@pytest.mark.parametrize("ver,status,ota,cat", [
    ("C01-OTA-1.0.0", "Open", "C01-OTA-1.0.0", "duplicate"),
    ("C01-OTA-0.9.0", "Open", "C01-OTA-1.0.0", "continuation"),
    ("C01-OTA-0.9.0", "Closed", "C01-OTA-1.0.0", "regression"),
])
async def test_dup_classification(ver, status, ota, cat):
    fp = f"J-01|pkg|page|{cat}"
    _issue(f"A01-T{cat}", fp, ver, status)
    r = await get_jira().find_dup(_defect(fp, ota))
    assert r.category == cat and r.issue_key == f"A01-T{cat}"


async def test_dup_new():
    r = await get_jira().find_dup(_defect("J-01|none|none|none", "C01-OTA-1.0.0"))
    assert r.category == "new"


def test_tools_need_login():
    """测试工具只给登录的台架页面用户；没有车型资料、联网搜索这类与测试无关的工具。"""
    assert tools_for(False) == []
    assert {t.name for t in tools_for(True)} == {"read_case", "plan_task", "cancel_task", "query_status"}
    unauth = ToolCtx(session="S", authed=False, utterance="")
    with pytest.raises(HookDenied):
        pre_tool(unauth, "read_case", {"query": "x"})
    lab = ToolCtx(session="S", authed=True, utterance="")
    with pytest.raises(HookDenied):
        pre_tool(lab, "plan_task", {"rig": "B2"})  # 缺 case_id，Schema 校验失败
    with pytest.raises(HookDenied):
        pre_tool(lab, "web_search", {"query": "x"})  # 已删除的工具
    pre_tool(lab, "read_case", {"query": "x"})


def test_sentence_splitter():
    sp = SentenceSplitter()
    out = []
    for ch in "好的。这台车纯电续航500公里！支持超充吗？支持":
        out += sp.feed(ch)
    out += sp.flush()
    assert out == ["好的。", "这台车纯电续航500公里！", "支持超充吗？", "支持"]  # 首句短也立即出声，降低首字时延


def test_asr_hotwords():
    assert normalize("在必二台加测一下") == "在B2台架测一下"
    assert normalize("对比2个结果") == "对比2个结果"


def test_asr_wake_word():
    """机器人唤醒词的常见误识别统一为「你好小禾」。"""
    assert normalize("你好小和，测一下 TC-DISP-001") == "你好小禾，测一下 TC-DISP-001"
    assert normalize("hello xiao he 跑一下回归") == "你好小禾 跑一下回归"
