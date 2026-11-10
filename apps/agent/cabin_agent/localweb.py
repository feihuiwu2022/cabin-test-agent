"""本地 Web：工程师在台架 PC 上用的页面。投屏和本机状态在这里，业务与模型仍在云端，由这里带上登录身份代理过去。

只监听 127.0.0.1，并校验 Host 与 Origin：否则浏览器里任何网页都能借本机登录身份调用云端。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .client import CloudDown, CloudRejected
from . import secret
from .config import RES_DIR, get_settings
from .daemon import RigAgent

log = logging.getLogger("cabin.agent.web")
WEB_DIST = RES_DIR / "web" / "dist"
SIMCAR_DIR = RES_DIR / "simcar"
FRAME_S = 0.4
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# 公开演示：页面对外开放（任何人都能打开），但不能登录、不能改台架信息；只在演示容器里开
PUBLIC = os.getenv("CABIN_AGENT_PUBLIC") == "1"
HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "cookie", "authorization", "origin", "referer"}


class ProfileReq(BaseModel):
    model: str
    hardware: list[str]


def _host_ok(value: str) -> bool:
    return value.split("://")[-1].split("/")[0].rsplit(":", 1)[0] in LOCAL_HOSTS


def create_app(agent: RigAgent, ui_port: int, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    app = FastAPI(title="CabinPilot 台架 Agent")
    cloud = agent.cfg.cloud_url.rstrip("/")
    user_file: Path = agent.cfg.data_dir / "user_token"
    state = {"token": secret.load(user_file)}
    http = httpx.AsyncClient(base_url=cloud, timeout=60, transport=transport, trust_env=os.getenv("CABIN_USE_PROXY") == "1")

    def auth_headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {state['token']}"} if state["token"] else {}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        origin = request.headers.get("origin", "")
        if PUBLIC:
            p = request.url.path
            if p.startswith("/auth/") or (p.startswith("/local/") and request.method != "GET"):
                return JSONResponse({"message": "演示版不能登录或修改台架信息"}, 403)
            return await call_next(request)
        if not _host_ok(request.headers.get("host", "")) or (origin and not _host_ok(origin)):
            return JSONResponse({"message": "只允许本机页面访问"}, 403)
        return await call_next(request)

    @app.get("/local/status")
    async def status():
        me = None
        if state["token"]:
            try:
                me = (await http.get("/api/v1/auth/me", headers=auth_headers())).json().get("user")
            except httpx.HTTPError:
                me = None
        return {"rig_id": agent.cfg.rig_id, "model": agent.cfg.model, "state": agent.state, "run_id": agent.run_id,
                "ota": agent.ota, "capabilities": agent.caps, "hardware": agent.hardware, "model_warning": agent.model_warning,
                "outbox": agent.outbox.counts(), "halted": agent.halted,
                "user": me, "board_url": "" if PUBLIC else cloud}  # 公开演示时云端不对外，不显示入口

    @app.put("/local/profile")
    async def profile(body: ProfileReq):
        try:
            await agent.update_profile(body.model, body.hardware)
        except ValueError as e:
            return JSONResponse({"message": str(e)}, 409)
        except CloudRejected as e:
            return JSONResponse({"message": f"云端拒绝：{e}"}, 400)
        except CloudDown:
            return JSONResponse({"message": "云端不可用，没有保存"}, 502)
        return {"ok": True}

    @app.get("/auth/login")
    async def login():
        back = f"http://127.0.0.1:{ui_port}/auth/done"
        return RedirectResponse(f"{cloud}/api/v1/auth/login?{urlencode({'redirect': back})}", 302)

    @app.get("/auth/done")
    async def done(ticket: str = ""):
        r = await http.post("/api/v1/auth/exchange", json={"ticket": ticket})
        if r.status_code != 200:
            return JSONResponse({"message": "登录失败，请重试"}, 400)
        state["token"] = r.json()["data"]["token"]
        secret.save(user_file, state["token"])
        return RedirectResponse("/", 302)

    @app.post("/auth/logout")
    async def logout():
        if state["token"]:
            await http.post("/api/v1/auth/logout", headers=auth_headers())
        state["token"] = ""
        user_file.unlink(missing_ok=True)
        return {"ok": True}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy_api(path: str, request: Request):
        return await _proxy(f"/api/{path}", request)

    @app.get("/evidence/{path:path}")
    async def evidence(path: str, request: Request):
        """本机跑的执行单元证据在本地，其他的到云端取。"""
        root = get_settings().evidence_dir.resolve()
        f = (root / path).resolve()
        if f.is_file() and f.is_relative_to(root):
            return FileResponse(f)
        return await _proxy(f"/evidence/{path}", request)

    async def _proxy(path: str, request: Request) -> Response:
        if ".." in path.split("/"):  # 不然规范化后会转发到云端别的路径
            return JSONResponse({"message": "路径不合法"}, 404)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        try:
            r = await http.request(request.method, path, params=request.query_params, content=await request.body(),
                                   headers={**headers, **auth_headers()})
        except httpx.HTTPError as e:
            return JSONResponse({"message": f"云端不可用：{type(e).__name__}"}, 502)
        keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "x-plan-version", "x-plan-status")}
        return Response(r.content, r.status_code, headers=keep)

    async def _frames(ws: WebSocket) -> None:
        """实时投屏直接取本机车机画面，不经过云端。"""
        while True:
            try:
                jpg = await agent.dev.screenshot("jpeg")
                await ws.send_json({"type": "screen.frame", "rig": agent.cfg.rig_id, "jpg": base64.b64encode(jpg).decode()})
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception as e:  # 车机暂时不可用时页面保持上一帧
                log.debug("投屏失败：%s", e)
                await asyncio.sleep(2)
            await asyncio.sleep(FRAME_S)

    @app.websocket("/ws")
    async def ws_proxy(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        same_site = not origin or origin.split("://")[-1].split("/")[0] == ws.headers.get("host", "")
        if (PUBLIC and not same_site) or (not PUBLIC and (not _host_ok(ws.headers.get("host", "")) or not _host_ok(origin or "127.0.0.1"))):
            await ws.close(4403)
            return
        await ws.accept()
        url = cloud.replace("http", "ws", 1) + "/ws?" + urlencode({**ws.query_params, "surface": "agent", "rig": agent.cfg.rig_id})
        try:
            async with websockets.connect(url, additional_headers=auth_headers(), max_size=2 ** 24,
                                          proxy=None) as up:
                async def down() -> None:
                    async for m in up:
                        await ws.send_text(m if isinstance(m, str) else m.decode("utf8"))

                async def upstream() -> None:
                    while True:
                        await up.send(await ws.receive_text())

                tasks = [asyncio.create_task(c) for c in (down(), upstream(), _frames(ws))]
                await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
                for t in tasks:
                    t.cancel()
        except (OSError, websockets.WebSocketException) as e:
            log.warning("连接云端失败：%s", e)
            await ws.send_json({"type": "error", "text": "云端不可用，正在执行的任务不受影响"})
            await ws.close()
        except WebSocketDisconnect:
            pass

    if SIMCAR_DIR.is_dir():
        app.mount("/simcar", StaticFiles(directory=SIMCAR_DIR, html=True), name="simcar")

    @app.get("/{path:path}")
    async def spa(path: str):
        if not WEB_DIST.exists():
            return JSONResponse({"message": "页面未构建：cd apps/agent/web && npm run build"}, 404)
        f = (WEB_DIST / path).resolve()
        if path and f.is_file() and f.is_relative_to(WEB_DIST.resolve()):
            return FileResponse(f)
        return FileResponse(WEB_DIST / "index.html")

    return app
