"""云端和本地的对话分工，以及刷新页面后恢复用例批次。"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cabin_cloud import casewf
from cabin_cloud.bus import bus
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.gateway import Session
from cabin_cloud.main import app
from cabin_common.schema import CaseBatch, TestCase
from test_casegen import _FakeWS


def reply(ws) -> str:
    return [m for m in ws.sent if m["type"] == "reply.text"][-1]["text"]


def session(surface, sid, user="张工"):
    ws = _FakeWS()
    s = Session(ws, sid, user=user, surface=surface)
    s.tts_on = False
    return s, ws


def batch(bid, user, status="待审核") -> CaseBatch:
    lib = get_caselib()
    b = CaseBatch(batch_id=bid, created_by=user, status=status, doc_name="空调.md", case_ids=[f"TC-SF-{bid[-3:]}"])
    lib.save_batch(b)
    lib.save_case(TestCase(case_id=f"TC-SF-{bid[-3:]}", title="t", batch_id=bid, channel=["adb"]))
    return b


# ---------- 本地页面只管执行 ----------
@pytest.mark.parametrize("text", ["导入示例 PRD 生成用例", "拉取 A01-CN 用例", "把这份需求转成用例"])
async def test_agent_sends_case_requests_to_cloud(text):
    s, ws = session("agent", "S-sf-a1")
    assert await s.route(1, text) is True
    assert "/cases" in reply(ws) and get_settings().public_base_url in reply(ws)


@pytest.mark.parametrize("msg", [{"type": "case.enter"}, {"type": "case.review.submit", "batch_id": "B"},
                                 {"type": "caselib.sync"}, {"type": "plan.trial", "case_id": "TC"}])
async def test_agent_cannot_do_case_operations(msg):
    s, ws = session("agent", "S-sf-a2")
    await s.handle(msg)
    assert ws.sent[-1]["type"] == "error" and "/cases" in ws.sent[-1]["text"]


async def test_agent_page_is_test_mode_for_logged_in_user():
    """台架页面打开就是测试模式：登录用户能下达测试，未登录的只能看。"""
    s, _ = session("agent", "S-sf-a3")
    assert (s.mode, s.authed) == ("lab", True)
    anon, ws = session("agent", "S-sf-a4", user="")
    assert not anon.authed
    assert await anon.route(1, "测一下 TC-DISP-001") is True and "登录" in reply(ws)


# ---------- 云端只管用例 ----------
async def test_cloud_opens_workbench_after_login():
    s, _ = session("cloud", "S-sf-c1")
    await s.handle({"type": "session.open", "tts": False})
    assert (s.mode, s.authed) == ("lab", False)


async def test_anonymous_cloud_session_stays_out():
    s, ws = session("cloud", "S-sf-c2", user="")
    await s.handle({"type": "session.open", "tts": False})
    assert not s.authed and not any(m["type"].startswith("case") for m in ws.sent)


@pytest.mark.parametrize("text", ["在 B2 台架测一下显示设置的亮度页", "跑一下回归"])
async def test_cloud_refuses_execution(text):
    s, ws = session("cloud", "S-sf-c3")
    await s.handle({"type": "session.open", "tts": False})
    assert await s.route(1, text) is True
    assert "本地页面" in reply(ws) and not s.authed


def test_ws_refuses_other_sites():
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/ws", headers={"Origin": "https://evil.com"}):
            pass


# ---------- 恢复 ----------
async def test_resume_restores_own_batch_under_review():
    batch("B-20261107-501", "李工")
    s, ws = session("cloud", "S-sf-r1", user="李工")
    await casewf.resume(s)
    types = [m["type"] for m in ws.sent]
    assert types[:2] == ["casegen.doc", "casegen.batch"] and "casereview.open" in types
    assert s.state["case_batch"] == "B-20261107-501"


async def test_resume_ignores_other_people_and_finished_batches():
    batch("B-20261107-502", "王工")
    batch("B-20261107-503", "赵工", status="已审核")
    s, ws = session("cloud", "S-sf-r2", user="赵工")
    await casewf.resume(s)
    assert ws.sent == []


async def test_resume_lists_cases_while_generating():
    batch("B-20261107-504", "钱工", status="生成中")
    s, ws = session("cloud", "S-sf-r3", user="钱工")
    await casewf.resume(s)
    types = [m["type"] for m in ws.sent]
    assert "casegen.case" in types and "casereview.open" not in types


def test_restart_marks_interrupted_generation_failed():
    b = batch("B-20261107-505", "孙工", status="生成中")
    get_caselib().fail_interrupted()
    assert get_caselib().get_batch(b.batch_id).status == "失败"


async def test_background_job_follows_reconnect():
    """页面刷新：旧连接已断，后台任务的进度发给同一会话的新连接。"""
    old, old_ws = session("cloud", "S-sf-live")
    live = casewf.LiveSession(old)
    bus.attach(old)
    bus.detach(old)
    new, new_ws = session("cloud", "S-sf-live")
    bus.attach(new)
    await live.send({"type": "casegen.case", "case": {}})
    bus.detach(new)
    assert any(m["type"] == "casegen.case" for m in new_ws.sent)
    assert not any(m["type"] == "casegen.case" for m in old_ws.sent)
    assert new.state is old.state  # 重连后还认得进行中的任务


async def test_send_to_closed_page_does_not_kill_job():
    class Gone(_FakeWS):
        async def send_json(self, m):
            raise RuntimeError("连接已关闭")

    s = Session(Gone(), "S-sf-gone", user="周工")
    await s.send({"type": "log"})
    assert s.closed
    await asyncio.wait_for(s.send({"type": "log"}), 1)
