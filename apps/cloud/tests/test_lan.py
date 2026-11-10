"""局域网部署：姓名 + 团队口令登录、名单、失败锁定、给本机 Agent 的票据；启动提示。"""
import dataclasses

import httpx

from cabin_cloud import auth
from cabin_cloud.config import get_settings
from cabin_cloud.main import app, startup_notes


def web():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.23:8800")


async def test_lan_login(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "lan")
    monkeypatch.setattr(s, "lan_passcode", "团队口令")
    monkeypatch.setattr(s, "lan_users", ())
    auth._lan_fails.clear()
    async with web() as c:
        page = (await c.get("/api/v1/auth/login?redirect=/skills")).text
        assert "局域网登录" in page and 'value="/skills"' in page
        r = await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": "错的", "redirect": "/skills"})
        assert r.status_code == 401 and "不对" in r.text
        r = await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": "团队口令", "redirect": "/skills"})
        assert r.status_code == 302 and r.headers["location"] == "/skills" and auth.COOKIE in r.cookies
        c.cookies.set(auth.COOKIE, r.cookies[auth.COOKIE])
        me = (await c.get("/api/v1/auth/me")).json()
        assert me["user"]["name"] == "张工" and me["auth_mode"] == "lan"
        # 本机 Agent 登录：回到 127.0.0.1 带一次性票据
        r = await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": "团队口令",
                                                   "redirect": "http://127.0.0.1:8810/auth/done"})
        assert r.status_code == 302 and r.headers["location"].startswith("http://127.0.0.1:8810/auth/done?ticket=")
        # 外站跳转被拦
        r = await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": "团队口令", "redirect": "https://evil.com"})
        assert r.headers["location"] == "/"


async def test_lan_allowlist_and_lockout(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "lan")
    monkeypatch.setattr(s, "lan_passcode", "k")
    monkeypatch.setattr(s, "lan_users", ("张工", "李工"))
    auth._lan_fails.clear()
    async with web() as c:
        assert "<option>李工</option>" in (await c.get("/api/v1/auth/login")).text
        assert (await c.post("/api/v1/auth/lan", data={"name": "路人", "passcode": "k"})).status_code == 401
        for _ in range(auth.LAN_FAIL_LIMIT):
            await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": "x"})
        r = await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": "k"})
        assert r.status_code == 429  # 锁定期间口令对了也不行
    monkeypatch.setattr(s, "lan_passcode", "")
    auth._lan_fails.clear()
    async with web() as c:
        assert "还没配置登录口令" in (await c.get("/api/v1/auth/login")).text
        assert (await c.post("/api/v1/auth/lan", data={"name": "张工", "passcode": ""})).status_code == 401


async def test_lan_endpoint_off_in_other_modes(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_mode", "feishu")
    async with web() as c:
        assert (await c.post("/api/v1/auth/lan", data={"name": "a", "passcode": "b"})).status_code == 404


def test_startup_notes():
    s = get_settings()
    local = startup_notes(dataclasses.replace(s, host="127.0.0.1"))
    assert any("访问不到" in n for n in local)
    lan = startup_notes(dataclasses.replace(s, host="0.0.0.0", public_base_url="http://127.0.0.1:8800", auth_mode="lan",
                                            lan_passcode=""))
    assert lan[0].startswith("局域网访问地址") and any("CABIN_PUBLIC_URL" in n for n in lan) and any("口令" in n for n in lan)
    ok = startup_notes(dataclasses.replace(s, host="0.0.0.0", public_base_url="http://192.168.1.23:8800", auth_mode="lan",
                                           lan_passcode="k"))
    assert len(ok) == 1
