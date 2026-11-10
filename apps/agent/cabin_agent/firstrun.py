"""首次启动配置（台架端 PRD PF-CFG-06）：没有云端地址时，在本地页面填云端地址、台架编号、车型和注册码，取代 .env。

只监听 127.0.0.1，校验 Host / Origin。保存前先连一次云端，地址填错当场提示。保存到数据目录的 agent.json，
注册成功后注册码会被清掉（之后用台架令牌）。
"""
from __future__ import annotations

import asyncio
import html
import importlib.util
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from cabin_common import vehicles

from .config import Settings, save_local

LOCAL_HOSTS = ("127.0.0.1", "localhost")


class SetupReq(BaseModel):
    cloud_url: str
    rig_id: str
    rig_model: str
    register_code: str
    device: str = "adb"
    adb_serial: str = ""


def _host_ok(value: str) -> bool:
    return value.split("://")[-1].split("/")[0].rsplit(":", 1)[0] in LOCAL_HOSTS


async def check_cloud(url: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """能连上云端并且是 CabinPilot 云端时返回空串，否则返回原因。"""
    try:
        async with httpx.AsyncClient(base_url=url, timeout=10, transport=transport,
                                     trust_env=os.getenv("CABIN_USE_PROXY") == "1") as c:
            r = await c.get("/api/v1/vehicles")
    except httpx.HTTPError as e:
        return f"连不上 {url}：{type(e).__name__}"
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or not (isinstance(j, dict) and j.get("code") == 0 and isinstance(j.get("data"), list)):
        return f"{url} 不是 CabinPilot 云端（/api/v1/vehicles 返回 HTTP {r.status_code}）"
    return ""


def simcar_available() -> bool:
    """正式包不带 playwright，只有开发环境能选模拟车机。"""
    return importlib.util.find_spec("playwright") is not None


def page(s: Settings) -> str:
    opts = "".join(f'<option value="{c}"{" selected" if c == s.rig_model else ""}>{html.escape(vehicles.name(c))}（{c}）</option>'
                   for c in vehicles.codes())
    v = lambda x: html.escape(str(x or ""), quote=True)  # noqa: E731
    sim = (f'<option value="simcar"{" selected" if s.device == "simcar" else ""}>SimCar 模拟车机（演示）</option>'
           if simcar_available() else "")
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CabinPilot 台架 Agent · 首次配置</title>
<style>
:root{{--bg:#EDF1F4;--panel:#fff;--line:#D3DCE2;--ink:#13202B;--sub:#4E606C;--accent:#C8102E}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0E1418;--panel:#141C21;--line:#26343D;--ink:#E6EDF1;--sub:#9AABB6;--accent:#E5484D}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:560px;margin:40px auto;padding:0 16px}}h1{{font-size:20px;margin:0 0 4px}}p{{color:var(--sub);margin:0 0 16px}}
form{{display:grid;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}}
label{{display:grid;gap:4px;font-weight:600}}small{{color:var(--sub);font-weight:400}}
input,select{{font:inherit;color:var(--ink);background:transparent;border:1px solid var(--line);border-radius:6px;padding:7px 9px}}
button{{font:inherit;border:0;border-radius:8px;padding:9px;background:var(--accent);color:#fff;cursor:pointer}}
button:disabled{{opacity:.6}}#msg{{min-height:1.6em}}.err{{color:var(--accent)}}
</style>
<main><h1>CabinPilot 台架 Agent · 首次配置</h1>
<p>填好后保存，Agent 会连上云端完成注册。以后要改，删掉数据目录里的 agent.json 或用 --setup 启动。</p>
<form id="f">
<label>云端地址<small>测试平台的网址，如 https://cabin.example.com</small><input name="cloud_url" required value="{v(s.cloud_url)}" placeholder="https://"></label>
<label>台架编号<small>在云端台架管理里唯一，如 B3</small><input name="rig_id" required value="{v(s.rig_id)}"></label>
<label>车型<select name="rig_model">{opts}</select></label>
<label>注册码<small>台架管理员在云端台架管理里生成，只在首次注册用</small><input name="register_code" required autocomplete="off" value="{v(s.register_code)}"></label>
<label>车机连接<select name="device"><option value="adb">ADB（USB 或网络）</option>{sim}</select></label>
<label>ADB 序列号<small>只连了一台车机时留空</small><input name="adb_serial" value="{v(s.adb_serial)}"></label>
<button id="b">保存并连接云端</button><div id="msg" role="status"></div>
</form></main>
<script>
const f=document.getElementById('f'),m=document.getElementById('msg'),b=document.getElementById('b');
f.onsubmit=async e=>{{e.preventDefault();b.disabled=true;m.className='';m.textContent='正在连接云端…';
 const r=await fetch('/setup',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.fromEntries(new FormData(f)))}}).catch(()=>null);
 const j=r?await r.json().catch(()=>({{}})):{{message:'本机服务没有响应'}};
 if(r&&r.ok){{m.textContent='已保存，Agent 正在启动，几秒后自动打开本地页面…';setTimeout(()=>location.replace('/'),5000)}}
 else{{m.className='err';m.textContent=j.message||'保存失败';b.disabled=false}}}};
</script></html>"""


def create_setup_app(s: Settings, done: asyncio.Event, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    app = FastAPI(title="CabinPilot 台架 Agent 首次配置")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        origin = request.headers.get("origin", "")
        if not _host_ok(request.headers.get("host", "")) or (origin and not _host_ok(origin)):
            return JSONResponse({"message": "只允许本机页面访问"}, 403)
        return await call_next(request)

    @app.post("/setup")
    async def setup(body: SetupReq):
        url = body.cloud_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"message": "云端地址要以 http:// 或 https:// 开头"}, 400)
        if body.rig_model not in vehicles.codes():
            return JSONResponse({"message": f"未知车型 {body.rig_model}"}, 400)
        if body.device not in ("adb", "simcar") or (body.device == "simcar" and not simcar_available()):
            return JSONResponse({"message": f"未知车机连接方式 {body.device}"}, 400)
        why = await check_cloud(url, transport)
        if why:
            return JSONResponse({"message": why}, 400)
        save_local({"cloud_url": url, "rig_id": body.rig_id.strip(), "rig_model": body.rig_model,
                    "register_code": body.register_code.strip(), "device": body.device,
                    "adb_serial": body.adb_serial.strip()}, s.data_dir)
        done.set()
        return {"ok": True}

    @app.get("/{path:path}")
    async def index(path: str):
        return HTMLResponse(page(s))

    return app
