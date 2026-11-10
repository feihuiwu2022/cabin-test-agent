"""公开演示模式：访客免登录、改配置类接口只读、模型调用每日上限、种子用例可运行。"""
import runpy

import httpx
import pytest

from cabin_cloud import modelgw, rigplan
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import BACKEND_DIR, get_settings
from cabin_cloud.main import app, demo_blocked
from cabin_cloud.store import get_store


def web():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_guest_and_readonly(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "demo", False)
    async with web() as c:
        assert (await c.get("/api/runnable")).status_code == 401
    monkeypatch.setattr(s, "demo", True)
    async with web() as c:
        me = (await c.get("/api/v1/auth/me")).json()["user"]
        assert me["name"] == "访客"
        assert (await c.get("/api/runnable")).status_code == 200
        r = await c.put("/api/settings/model", json={"base_url": "https://evil.example.com", "api_key": "x"})
        assert r.status_code == 403 and "演示版" in r.json()["message"]
        assert (await c.post("/api/settings/model/test", json={})).status_code == 403
        assert (await c.put("/api/skills/默认.md", json={})).status_code == 403
    assert demo_blocked("POST", "/api/skills/空调.md/rollback") and not demo_blocked("POST", "/api/skills/空调.md/trial")
    assert demo_blocked("PUT", "/api/board/rigs/B1/caseset") and not demo_blocked("GET", "/api/settings/model")


def test_daily_quota(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "demo", True)
    monkeypatch.setattr(s, "demo_daily_calls", 2)
    gw = modelgw.ModelGateway()
    gw._check("public")
    gw._check("public")
    with pytest.raises(modelgw.DemoQuotaError):
        gw._check("public")
    monkeypatch.setattr(s, "demo", False)
    gw._check("public")  # 非演示模式不限


async def test_seed_cases_are_runnable(monkeypatch):
    monkeypatch.setenv("CABIN_RIG_ID", "B9")
    seed = runpy.run_path(str(BACKEND_DIR / "scripts" / "seed_demo.py"))
    await seed["main"]()
    lib = get_caselib()
    for cid in ("TC-DISP-001", "TC-HVAC-001", "TC-HVAC-002"):
        c = lib.get_case(cid)
        assert c and c.plan_status == "ready" and c.review_status == "通过"
    assert rigplan.caseset("B9") == ["TC-DISP-001", "TC-HVAC-001", "TC-HVAC-002"]
    await seed["main"]()  # 再跑一次不重复
    st = get_store()  # 测试库是共享的：清掉，免得影响按模块拉取用例的测试
    for t in ("test_case", "case_rev"):
        st.x(f"DELETE FROM {t} WHERE case_id IN ('TC-DISP-001','TC-HVAC-001','TC-HVAC-002')")
    st.x("DELETE FROM rig_caseset WHERE rig_id='B9'")
