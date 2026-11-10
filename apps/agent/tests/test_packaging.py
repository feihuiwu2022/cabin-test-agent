"""打包与首次启动（台架端 PRD PF-CFG-05 / 06）：配置优先级、令牌保存、首次配置页、注册后清注册码、自检。"""
import asyncio

import httpx
import pytest

from cabin_agent import config, firstrun, secret
from cabin_agent.config import Settings, get_settings, load_local, save_local


def test_config_precedence(tmp_path, monkeypatch):
    """环境变量 > agent.json > 默认值；agent.json 里不认识的键忽略。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "EXE_DIR", tmp_path)  # 没有 .env
    for env in config.ENV_NAMES.values():
        monkeypatch.delenv(env, raising=False)
    save_local({"cloud_url": "http://c", "rig_id": "B9", "ui_port": 8899, "evil": "x"}, tmp_path)
    assert "evil" not in load_local(tmp_path)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert (s.cloud_url, s.rig_id, s.ui_port, s.rig_model, s.configured) == ("http://c", "B9", 8899, "A01-CN", True)
        monkeypatch.setenv("CABIN_RIG_ID", "B10")
        get_settings.cache_clear()
        assert get_settings().rig_id == "B10"
    finally:
        get_settings.cache_clear()


def test_secret_plain_and_legacy(tmp_path):
    f = tmp_path / "rig_token"
    assert secret.load(f) == ""
    secret.save(f, "tok-1")
    assert secret.load(f) == "tok-1"
    if not secret.protected():
        assert oct(f.stat().st_mode)[-3:] == "600"
        f.write_text("dpapi:QUJD", "utf8")  # Windows 上加密的文件拷到别处：读不出，按未注册处理
        assert secret.load(f) == ""
    f.write_text("legacy-token\n", "utf8")  # 旧版明文
    assert secret.load(f) == "legacy-token"


@pytest.mark.skipif(not secret.protected(), reason="只在 Windows 上验证 DPAPI")
def test_secret_dpapi_roundtrip(tmp_path):
    f = tmp_path / "t"
    secret.save(f, "令牌")
    assert f.read_text("utf8").startswith("dpapi:") and "令牌" not in f.read_text("utf8")
    assert secret.load(f) == "令牌"


def _cloud(status=200, body=None):
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/vehicles"
        return httpx.Response(status, json=body if body is not None else {"code": 0, "data": [{"code": "A01-CN"}]})
    return httpx.MockTransport(handle)


async def _post(app, body, host="127.0.0.1:8810"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}") as c:
        return await c.post("/setup", json=body)


async def test_first_run_setup(tmp_path):
    s = Settings(data_dir=tmp_path)
    good = {"cloud_url": "https://cabin.example.com/", "rig_id": " B3 ", "rig_model": "E01", "register_code": "K7"}
    done = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=firstrun.create_setup_app(s, done, _cloud())),
                                 base_url="http://127.0.0.1:8810") as c:
        page = (await c.get("/")).text
        assert "首次配置" in page and "E01" in page
        assert (await c.get("/", headers={"host": "evil.com"})).status_code == 403  # 只给本机
    app_bad = firstrun.create_setup_app(s, done, _cloud(404, {"detail": "x"}))
    r = await _post(app_bad, good)
    assert r.status_code == 400 and "不是 CabinPilot 云端" in r.json()["message"] and not done.is_set()
    app = firstrun.create_setup_app(s, done, _cloud())
    assert (await _post(app, {**good, "cloud_url": "cabin.example.com"})).status_code == 400
    assert "未知车型" in (await _post(app, {**good, "rig_model": "C01"})).json()["message"]
    r = await _post(app, good)
    assert r.json() == {"ok": True} and done.is_set()
    assert load_local(tmp_path) == {"cloud_url": "https://cabin.example.com", "rig_id": "B3", "rig_model": "E01",
                                    "register_code": "K7", "device": "adb", "adb_serial": ""}


async def test_register_clears_code_and_protects_token(tmp_path):
    """注册成功：令牌经 secret 保存，agent.json 里的注册码清空（之后用令牌）。"""
    from test_rigagent import CODE, Net  # noqa: F401  复用集成测试的云端
    from cabin_agent.daemon import AgentConfig, RigAgent
    from test_runner import FakeDevice
    from cabin_cloud.config import get_settings as cloud_settings
    s = cloud_settings()
    old = s.rig_register_code
    s.rig_register_code = "K7-PKG"
    try:
        save_local({"cloud_url": "http://cloud", "register_code": "K7-PKG"}, tmp_path)
        a = RigAgent(AgentConfig("http://cloud", "B21", "A01-CN", "K7-PKG", data_dir=tmp_path), FakeDevice(), Net())
        await a.register()
        await a.close()
    finally:
        s.rig_register_code = old
    assert load_local(tmp_path)["register_code"] == ""
    assert secret.load(tmp_path / "rig_token") == a.client.token and a.client.token


def test_selfcheck_reports_parts():
    from cabin_agent.daemon import selfcheck
    r = selfcheck()
    assert set(r["checks"]) >= {"web", "adb", "ffmpeg", "data_dir", "token_protection"}
    assert r["checks"]["adb"] is True and r["checks"]["ffmpeg"] is True and r["checks"]["data_dir"] is True
    assert r["ok"] == (r["checks"]["web"] is True)  # 源码运行时本地页面可能没构建


def test_selfcheck_output_survives_non_utf8_console():
    """Windows 控制台 / 重定向时编码可能是 cp1252：带中文的自检输出不能让程序崩（流水线上 lite 版踩过）。"""
    import os
    import subprocess
    import sys
    code = ("from cabin_agent.runner import vision_local\n"
            "vision_local.ocr_unavailable = lambda: '未安装 rapidocr'\n"
            "from cabin_agent.daemon import run\n"
            "run(['--selfcheck'])")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env={**os.environ, "PYTHONIOENCODING": "cp1252"}, timeout=120)
    assert b"UnicodeEncodeError" not in r.stderr, r.stderr[-400:]
    assert "未安装 rapidocr" in r.stdout.decode("utf8")
