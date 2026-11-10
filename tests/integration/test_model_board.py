"""模型配置与 OpenAI 兼容适配、看板的批次详情、覆盖矩阵、KPI。模型接口用模拟服务。"""
import json

import httpx
import pytest

from cabin_cloud import auth, modelopenai, stats
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.main import app
from cabin_cloud.modelgw import get_gateway
from cabin_cloud.store import get_store
from cabin_common import vehicles
from cabin_common.schema import TestCase

URL = "http://cloud"


def sse(*chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf8"))


def text_chunk(t, finish=None):
    return {"choices": [{"delta": {"content": t}, "finish_reason": finish}]}


class FakeOpenAI(httpx.AsyncBaseTransport):
    def __init__(self, *replies):
        self.bodies, self.headers, self.replies = [], [], list(replies)

    async def handle_async_request(self, req):
        self.bodies.append(json.loads(b"".join([x async for x in req.stream])))
        self.headers.append(req.headers.get("authorization", ""))
        self.path = req.url.path
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def gw(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "secret_key", "测试密钥")
    monkeypatch.setattr(s, "admins", ("管理员",))
    store = get_store()
    store.x("DELETE FROM kv")
    g = get_gateway()
    g.reload()
    yield g
    store.x("DELETE FROM kv")
    g.transport = None
    g.reload()


def use_openai(g, *replies):
    get_store().set_kv("model", {"protocol": "openai", "url": "http://llm.test/v1", "chat_model": "m-chat", "vision_model": "m-see"})
    g.reload()
    g.transport = FakeOpenAI(*replies)
    return g.transport


def client(name="管理员"):
    token = auth.issue_session(auth.User(id=name, name=name))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Authorization": f"Bearer {token}"})


# ---------- 适配层 ----------
def test_messages_convert_tools_and_images():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    out = modelopenai.to_messages("系统", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [{"type": "text", "text": "查一下"}, {"type": "tool_use", "id": "c1", "name": "search", "input": {"q": "中"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": {"ok": True}}]},
        {"role": "user", "content": [img, {"type": "text", "text": "看图"}]},
    ])
    assert out[0] == {"role": "system", "content": "系统"} and out[1]["content"] == "你好"
    assert out[2]["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"q": "中"}'} and out[2]["content"] == "查一下"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}
    assert out[4]["content"][0]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_tools_convert():
    t = modelopenai.to_tools([{"name": "a", "description": "d", "input_schema": {"type": "object"}}])
    assert t == [{"type": "function", "function": {"name": "a", "description": "d", "parameters": {"type": "object"}}}]


async def test_text_stream_and_audit(gw):
    fake = use_openai(gw, sse(text_chunk("你"), text_chunk("好", "stop"), {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}))
    evs = [e async for e in gw.stream(system="s", messages=[{"role": "user", "content": "x"}], label="public", source="t", session="S-m")]
    assert [e["text"] for e in evs if e["type"] == "text"] == ["你", "好"]
    msg = evs[-1]["message"]
    assert msg.stop_reason == "end_turn" and msg.content[0].text == "你好" and msg.usage.input_tokens == 3
    assert fake.path == "/v1/chat/completions" and fake.bodies[0]["model"] == "m-chat"


async def test_tool_call_arrives_in_pieces(gw):
    use_openai(gw, sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search", "arguments": '{"q'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '": "x"}'}}]}, "finish_reason": "stop"}]}))
    evs = [e async for e in gw.stream(system="", messages=[{"role": "user", "content": "x"}], tools=[{"name": "search", "input_schema": {}}],
                                      label="public", source="t", session="S-m")]
    msg = evs[-1]["message"]
    assert msg.stop_reason == "tool_use"  # 有的服务带工具调用时 finish_reason 仍是 stop
    assert (msg.content[0].type, msg.content[0].id, msg.content[0].input) == ("tool_use", "c1", {"q": "x"})


async def test_vision_uses_vision_model(gw):
    fake = use_openai(gw, sse(text_chunk('{"ok": true}', "stop")))
    r = await gw.vision_json(system="", prompt="p", images=[b"\x89PNG"], label="public", source="t")
    assert r == {"ok": True} and fake.bodies[0]["model"] == "m-see"


async def test_new_token_parameter_retried(gw):
    bad = httpx.Response(400, json={"error": {"message": "use max_completion_tokens instead"}})
    fake = use_openai(gw, bad, sse(text_chunk("ok", "stop")))
    assert await gw.complete(system="", prompt="x", label="public", source="t") == "ok"
    assert "max_tokens" not in fake.bodies[1] and fake.bodies[1]["max_completion_tokens"] == 1024


async def test_server_error_is_raised(gw):
    use_openai(gw, httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(modelopenai.ModelError, match="401"):
        await gw.complete(system="", prompt="x", label="public", source="t")


async def test_strict_data_still_applies(gw, monkeypatch):
    use_openai(gw, sse(text_chunk("x", "stop")))
    monkeypatch.setattr(gw.s, "strict_data", True)
    with pytest.raises(RuntimeError, match="禁止走外网"):
        await gw.complete(system="", prompt="x", label="sensitive", source="t")


# ---------- 配置接口 ----------
BODY = {"protocol": "openai", "url": "http://llm.test/v1/", "chat_model": "c", "vision_model": "v"}


async def test_token_encrypted_masked_and_applied(gw):
    async with client() as c:
        assert (await c.put("/api/settings/model", json={**BODY, "secret": "sk-plain-abcd"})).status_code == 200
        r = (await c.get("/api/settings/model")).json()
    assert "plain" not in json.dumps(get_store().get_kv("model"), ensure_ascii=False)
    assert r["hint"] == "…abcd" and "secret" not in r and r["from_env"] is False
    assert (gw.protocol, gw.base_url, gw.chat_model, gw.api_key) == ("openai", "http://llm.test/v1", "c", "sk-plain-abcd")
    gw.transport = FakeOpenAI(sse(text_chunk("ok", "stop")))
    await gw.complete(system="", prompt="x", label="public", source="t")
    assert gw.transport.headers[0] == "Bearer sk-plain-abcd"


async def test_blank_token_keeps_old_on_same_url(gw):
    async with client() as c:
        await c.put("/api/settings/model", json={**BODY, "secret": "old-1234"})
        assert (await c.put("/api/settings/model", json={**BODY, "url": "http://llm.test/v1", "chat_model": "c2"})).status_code == 200
    assert gw.api_key == "old-1234" and gw.chat_model == "c2"


async def test_old_token_not_sent_to_new_url(gw):
    async with client() as c:
        await c.put("/api/settings/model", json={**BODY, "secret": "old-1234"})
        r = await c.put("/api/settings/model", json={**BODY, "url": "http://evil.test/v1"})
    assert r.status_code == 409 and gw.base_url == "http://llm.test/v1"


async def test_bad_config_rejected(gw):
    async with client() as c:
        for bad in ({"protocol": "x"}, {"url": "ftp://a"}, {"chat_model": " "}):
            assert (await c.put("/api/settings/model", json={**BODY, "secret": "t", **bad})).status_code == 409


async def test_only_admin_reads_or_changes_model(gw):
    async with client("普通人") as c:
        assert (await c.get("/api/settings/model")).status_code == 403
        assert (await c.put("/api/settings/model", json={**BODY, "secret": "t"})).status_code == 403
        assert (await c.post("/api/settings/model/test")).status_code == 403


async def test_connection_test_tries_chat_and_vision(gw):
    fake = use_openai(gw, sse(text_chunk("ok", "stop")))
    async with client() as c:
        r = await c.post("/api/settings/model/test")
    assert r.status_code == 200 and [b["model"] for b in fake.bodies] == ["m-chat", "m-see"]
    assert fake.bodies[1]["messages"][-1]["content"][0]["type"] == "image_url"
    gw.transport = FakeOpenAI(httpx.Response(404, json={"error": "没有这个模型"}))
    async with client() as c:
        r = await c.post("/api/settings/model/test")
    assert r.status_code == 409 and "404" in r.json()["detail"]


async def test_undecryptable_token_does_not_break_startup(gw):
    get_store().set_kv("model", {"protocol": "openai", "url": "http://llm.test/v1", "secret": "bad-token"})
    gw.reload()
    assert gw.api_key == ""


# ---------- 看板 ----------
@pytest.fixture
def board_data():
    store = get_store()
    for t in ("run", "regression", "defects"):
        store.x(f"DELETE FROM {t}")
    yield store


def add_run(store, case_id, verdict, model="A01-CN", rid="", kind="replay"):
    r = store.create_run("T", "S", "B2", case_id, "v", kind, 300, {}, {}, regression_id=rid)
    store.finish_run(r, "{}", {"model": model, "ota": "O", "module": "m", "verdict": verdict})


async def test_batch_detail_lists_runs(board_data):
    rid = board_data.create_regression("手动", "张工")
    add_run(board_data, "TC-A", "passed", rid=rid)
    add_run(board_data, "TC-A", "failed", rid=rid, kind="retest")
    add_run(board_data, "TC-B", "passed")
    async with client() as c:
        runs = (await c.get(f"/api/board/regressions/{rid}")).json()["runs"]
    assert [(r["case_id"], r["kind"]) for r in runs] == [("TC-A", "replay"), ("TC-A", "retest")]


def test_matrix_shows_latest_and_not_applicable(board_data, monkeypatch):
    code, other = vehicles.codes()[:2]
    cases = [TestCase(case_id="TC-M-1", title="t", module="m", models=[code]), TestCase(case_id="TC-M-2", title="t", module="m", models=[code])]
    monkeypatch.setattr(get_caselib(), "library", lambda: cases)
    add_run(board_data, "TC-M-1", "failed", model=code)
    add_run(board_data, "TC-M-1", "passed", model=code)
    add_run(board_data, "TC-M-1", "failed", model=code, kind="trial")  # 试跑不算
    m = stats.matrix()
    assert m["rows"][0]["cells"][code] == "passed" and m["rows"][0]["cells"][other] == "na"
    assert m["rows"][1]["cells"][code] == ""  # 适用但没跑过


def test_kpi_counts_confirmed_and_reject_rate(board_data):
    def d(i, status, finder):
        return {"id": f"D{i}", "task_id": "T", "fingerprint": "f", "ota": "O", "status": status, "finder": finder}

    for i, (status, finder) in enumerate([("confirmed", "张工"), ("rejected", "张工"), ("linked", "张工"), ("pending", "张工"),
                                          ("confirmed", ""), ("rejected", "李工")]):
        board_data.save_defect(d(i, status, finder))
    rows = {r["finder"]: r for r in stats.kpi()}
    assert set(rows) == {"张工", "李工"}  # 定时任务没有发现人
    assert (rows["张工"]["confirmed"], rows["张工"]["linked"], rows["张工"]["rejected"]) == (1, 1, 1)
    assert rows["张工"]["reject_rate"] == 0.333 and rows["李工"]["reject_rate"] == 1
