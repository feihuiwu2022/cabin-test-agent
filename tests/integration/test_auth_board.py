"""登录、接口保护、看板与本地 Web 代理。云端和本地页面都是真实应用，经 ASGI 直连。"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cabin_agent.daemon import AgentConfig, RigAgent
from cabin_agent.localweb import create_app
from cabin_cloud import auth
from cabin_cloud.config import get_settings
from cabin_cloud.execsvc import service as svc_mod
from cabin_cloud.main import app
from cabin_cloud.store import get_store
from test_rigagent import CODE, Net, URL, case_with_plan, cloud, submit  # noqa: F401  复用夹具
from test_runner import FakeDevice

UI = 8810


@pytest.fixture
def dev_mode(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "dev")
    monkeypatch.setattr(s, "dev_user", "张工")
    return s


@pytest.fixture
async def web():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, follow_redirects=False) as c:
        yield c


async def login(web) -> str:
    r = await web.get("/api/v1/auth/login", params={"redirect": f"http://127.0.0.1:{UI}/auth/done"})
    ticket = httpx.URL(r.headers["location"]).params["ticket"]
    return (await web.post("/api/v1/auth/exchange", json={"ticket": ticket})).json()["data"]["token"]


# ---------- 接口保护 ----------
@pytest.mark.parametrize("path", ["/api/board", "/api/caselib", "/api/defects", "/api/runnable", "/api/v1/cases?model=A01-CN",
                                  "/evidence/R20261106-000001/step-01.png"])
async def test_protected_without_login(web, path):
    assert (await web.get(path)).status_code == 401


async def test_health_and_rig_paths_stay_open(web):
    assert (await web.get("/api/health")).status_code == 200
    assert (await web.post("/api/v1/runs/claim")).status_code == 401  # 走台架令牌，不是登录拦截


async def test_rig_token_can_read_cases_but_not_board(web, cloud):  # noqa: F811
    tok = (await web.post("/api/v1/rigs/register", json={"rig_id": "B8", "model": "A01-CN", "register_code": CODE})).json()["data"]["rig_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await web.get("/api/v1/cases?model=A01-CN", headers=h)).status_code == 200
    assert (await web.get("/api/board", headers=h)).status_code == 401


# ---------- 登录 ----------
async def test_dev_login_sets_cookie_for_board(web, dev_mode):
    r = await web.get("/api/v1/auth/login", params={"redirect": "/board"})
    assert r.status_code == 302 and r.headers["location"] == "/board"
    assert "httponly" in r.headers["set-cookie"].lower()
    web.cookies.update(r.cookies)
    assert (await web.get("/api/v1/auth/me")).json()["user"]["name"] == "张工"
    assert (await web.get("/api/board")).status_code == 200
    await web.post("/api/v1/auth/logout")
    assert (await web.get("/api/v1/auth/me")).json()["user"] is None


@pytest.mark.parametrize("bad", ["https://evil.com/x", "//evil.com", "http://evil.com/", "/\\evil.com", "javascript:alert(1)"])
def test_open_redirect_refused(bad):
    assert auth.safe_redirect(bad) == "/"


def test_redirect_allows_site_and_local_agent():
    assert auth.safe_redirect("/board") == "/board"
    assert auth.safe_redirect("http://127.0.0.1:8810/auth/done") == "http://127.0.0.1:8810/auth/done"


async def test_ticket_is_one_time(web, dev_mode):
    r = await web.get("/api/v1/auth/login", params={"redirect": f"http://127.0.0.1:{UI}/auth/done"})
    ticket = httpx.URL(r.headers["location"]).params["ticket"]
    assert (await web.post("/api/v1/auth/exchange", json={"ticket": ticket})).status_code == 200
    assert (await web.post("/api/v1/auth/exchange", json={"ticket": ticket})).status_code == 401


async def test_feishu_mode_needs_app_config(web, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "feishu")
    monkeypatch.setattr(s, "feishu_app_id", "")
    assert (await web.get("/api/v1/auth/login")).status_code == 503


async def test_feishu_login_flow(web, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "feishu")
    monkeypatch.setattr(s, "feishu_app_id", "cli_x")
    monkeypatch.setattr(s, "feishu_app_secret", "sec")

    async def fake_user(code):
        assert code == "abc"
        return auth.User(id="ou_1", name="李工")

    monkeypatch.setattr(auth, "feishu_user", fake_user)
    r = await web.get("/api/v1/auth/login", params={"redirect": "/board"})
    loc = httpx.URL(r.headers["location"])
    assert loc.host == "open.feishu.cn" and loc.params["app_id"] == "cli_x"
    state = loc.params["state"]
    assert (await web.get("/api/v1/auth/feishu/callback", params={"code": "abc", "state": "伪造"})).status_code == 400
    r = await web.get("/api/v1/auth/feishu/callback", params={"code": "abc", "state": state})
    assert r.headers["location"] == "/board"
    web.cookies.update(r.cookies)
    assert (await web.get("/api/v1/auth/me")).json()["user"]["name"] == "李工"
    assert (await web.get("/api/v1/auth/feishu/callback", params={"code": "abc", "state": state})).status_code == 400  # state 只能用一次


def test_anonymous_ws_cannot_run_or_review():
    with TestClient(app).websocket_connect("/ws") as ws:
        st = ws.receive_json()
        assert st["user"] == "" and st["authed"] is False
        ws.send_json({"type": "review.action", "defect_id": "D-x", "action": "confirm"})
        assert ws.receive_json()["text"] == "请先登录"


# ---------- 看板 ----------
async def test_board_shows_rig_and_run(web, dev_mode, cloud, tmp_path):  # noqa: F811
    token = await login(web)
    net = Net()
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), net)
    await a.register()
    await a.heartbeat()
    await case_with_plan(assertions=[{"id": "a2", "type": "rule", "rule": "no_crash"}])
    st = await submit()
    await a.claim_once()
    await a.drain()
    await st.job
    b = (await web.get("/api/board", headers={"Authorization": f"Bearer {token}"})).json()
    assert b["rigs"][0]["id"] == "B2" and b["rigs"][0]["online"] is True and b["rigs"][0]["capabilities"] == ["adb"]
    assert b["runs"][0]["case_id"] == "TC-RIG-001" and b["runs"][0]["verdict"] == "passed"
    cloud.x("UPDATE rig SET last_seen=0")
    b = (await web.get("/api/board", headers={"Authorization": f"Bearer {token}"})).json()
    assert b["rigs"][0]["online"] is False  # 心跳断了，看板显示离线
    await a.close()


async def test_board_review_records_reviewer(web, dev_mode, cloud, tmp_path, monkeypatch):  # noqa: F811
    from cabin_cloud.execsvc import rigapi
    from test_rigagent import LocalJudgeStub
    from test_runner import FakeJudge
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=False)))
    token = await login(web)
    net = Net()
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), net)
    await a.register()
    await case_with_plan()
    st = await submit(tid="T-rig-rv")
    await a.claim_once()
    await a.drain()
    await st.job
    h = {"Authorization": f"Bearer {token}"}
    d = (await web.get("/api/board", headers=h)).json()["defects"][-1]
    r = await web.post(f"/api/board/defects/{d['id']}/review", headers=h, json={"action": "reject", "reason": "判定误报"})
    assert r.status_code == 200, r.text
    assert next(x for x in st.defects if x.id == d["id"]).review.by == "张工"
    await a.close()


# ---------- 本地 Web ----------
@pytest.fixture
async def local(tmp_path, cloud, dev_mode):  # noqa: F811
    net = Net()
    a = RigAgent(AgentConfig(URL, "B2", "A01-CN", CODE, data_dir=tmp_path / "a", send_backoff_s=(0,)), FakeDevice(), net)
    await a.register()
    ui = create_app(a, UI, transport=net)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui), base_url=f"http://127.0.0.1:{UI}") as c:
        c.agent = a
        yield c
    await a.close()


async def test_local_page_refuses_other_sites(local):
    assert (await local.post("/api/board", headers={"Origin": "https://evil.com"})).status_code == 403
    assert (await local.get("/local/status", headers={"Host": "evil.com"})).status_code == 403  # DNS 重绑定
    assert (await local.get("/local/status", headers={"Origin": f"http://localhost:{UI}"})).status_code == 200


async def test_local_login_then_proxy_with_user_token(local, web):
    assert (await local.get("/api/board")).status_code == 401  # 没登录，云端拒绝
    r = await local.get("/auth/login")
    assert r.status_code == 302
    cloud_login = httpx.URL(r.headers["location"])
    r = await web.get(cloud_login.path, params=dict(cloud_login.params))
    ticket = httpx.URL(r.headers["location"]).params["ticket"]
    assert (await local.get("/auth/done", params={"ticket": ticket})).status_code == 302
    assert (await local.get("/local/status")).json()["user"]["name"] == "张工"
    assert (await local.get("/api/board")).status_code == 200
    assert (local.agent.cfg.data_dir / "user_token").is_file()
    await local.post("/auth/logout")
    assert not (local.agent.cfg.data_dir / "user_token").exists()
    assert (await local.get("/api/board")).status_code == 401


async def test_local_status_reports_rig_without_cloud(local):
    local.agent.net = None
    s = (await local.get("/local/status")).json()
    assert s["rig_id"] == "B2" and s["state"] == "idle" and json.dumps(s["outbox"]) == "{}"
    assert get_store().rig("B2")


async def test_public_demo_page_is_open_but_cannot_login(local, monkeypatch):
    """公开演示：任何域名都能打开页面，但不能登录、不能改台架信息，也不显示云端入口。"""
    import cabin_agent.localweb as lw
    monkeypatch.setattr(lw, "PUBLIC", True)
    r = await local.get("/local/status", headers={"Host": "demo.example.com"})
    assert r.status_code == 200 and r.json()["board_url"] == ""
    assert (await local.get("/auth/login", headers={"Host": "demo.example.com"})).status_code == 403
    assert (await local.post("/auth/logout", headers={"Host": "demo.example.com"})).status_code == 403
    assert (await local.put("/local/profile", json={"model": "A01-CN", "hardware": []})).status_code == 403
