"""设置接口：个人 Jira 用户名，以及管理员配置的 Jira 公共账号、车型映射和模型。"""
from __future__ import annotations

import base64
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cabin_common import vehicles

from . import vault
from .auth import current_user
from .config import get_settings
from .integ.jira import get_jira
from .modelgw import get_gateway
from .store import get_store

router = APIRouter(prefix="/api/settings")
PROTOCOLS = ("anthropic", "openai")
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")  # 测试视觉模型用


def _admin(req: Request) -> None:
    if current_user(req).name not in get_settings().admins:
        raise HTTPException(403, "只有管理员能修改公共配置")


class MeReq(BaseModel):
    jira_user: str


@router.get("/me")
async def me(req: Request):
    u = current_user(req)
    p = get_store().user_profile(u.name)
    return {"name": u.name, "email": p.get("email", ""), "jira_user": p.get("jira_user", ""), "admin": u.name in get_settings().admins}


@router.put("/me")
async def set_me(body: MeReq, req: Request):
    get_store().set_jira_user(current_user(req).name, body.jira_user.strip())
    return {"ok": True}


class JiraReq(BaseModel):
    url: str
    user: str = ""
    secret: str = ""  # 留空表示不改
    token_auth: bool = False
    projects: dict[str, dict[str, str]] = Field(default_factory=dict)


@router.get("/jira")
async def get_jira_cfg(req: Request):
    _admin(req)
    d = get_store().get_kv("jira")
    return {"url": d.get("url", ""), "user": d.get("user", ""), "has_secret": bool(d.get("secret")), "token_auth": bool(d.get("token_auth")),
            "projects": d.get("projects") or {}, "vehicles": [{"code": c, "name": vehicles.name(c)} for c in vehicles.codes()]}


@router.put("/jira")
async def put_jira_cfg(body: JiraReq, req: Request):
    _admin(req)
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(409, "Jira 地址要以 http:// 或 https:// 开头")
    unknown = set(body.projects) - set(vehicles.codes())
    if unknown:
        raise HTTPException(409, f"车型不在字典里：{sorted(unknown)}")
    store, old = get_store(), get_store().get_kv("jira")
    try:
        secret = vault.seal(body.secret) if body.secret else old.get("secret", "")
    except vault.VaultError as e:
        raise HTTPException(409, str(e)) from e
    store.set_kv("jira", {"url": body.url.rstrip("/"), "user": body.user, "secret": secret, "token_auth": body.token_auth,
                          "projects": body.projects})
    return {"ok": True}


@router.post("/jira/test")
async def test_jira(req: Request):
    _admin(req)
    jira = get_jira()
    if not jira.rest_enabled:
        raise HTTPException(409, "还没有配置 Jira 地址和账号")
    try:
        return {"user": await jira.myself()}
    except (httpx.HTTPError, vault.VaultError) as e:
        raise HTTPException(409, f"连接失败：{type(e).__name__}") from e


class ModelReq(BaseModel):
    protocol: str
    url: str
    chat_model: str
    vision_model: str
    secret: str = ""  # 留空表示不改


@router.get("/model")
async def get_model_cfg(req: Request):
    _admin(req)
    gw, d = get_gateway(), get_store().get_kv("model")
    return {"protocol": gw.protocol, "url": gw.base_url, "chat_model": gw.chat_model, "vision_model": gw.vision_model,
            "hint": d.get("hint", ""), "from_env": not d}


@router.put("/model")
async def put_model_cfg(body: ModelReq, req: Request):
    _admin(req)
    gw, old = get_gateway(), get_store().get_kv("model")
    url = body.url.rstrip("/")
    if body.protocol not in PROTOCOLS:
        raise HTTPException(409, f"协议只能是 {'、'.join(PROTOCOLS)}")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(409, "地址要以 http:// 或 https:// 开头")
    if not (body.chat_model.strip() and body.vision_model.strip()):
        raise HTTPException(409, "模型名不能为空")
    if url != gw.base_url.rstrip("/") and not body.secret:  # 旧 Token 不能发给新地址
        raise HTTPException(409, "换了地址要重新填 Token")
    try:
        secret = vault.seal(body.secret) if body.secret else old.get("secret") or vault.seal(gw.api_key)
    except vault.VaultError as e:
        raise HTTPException(409, str(e)) from e
    hint = f"…{body.secret[-4:]}" if body.secret else old.get("hint", "")
    get_store().set_kv("model", {"protocol": body.protocol, "url": url, "chat_model": body.chat_model.strip(),
                                 "vision_model": body.vision_model.strip(), "secret": secret, "hint": hint})
    gw.reload()
    return {"ok": True}


@router.post("/model/test")
async def test_model_cfg(req: Request):
    """各发一次对话和看图，模型名或视觉能力配错时能当场发现。"""
    _admin(req)
    gw, t0 = get_gateway(), time.time()
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PIXEL).decode()}}
    try:
        await gw.complete(system="", prompt="回复 ok", label="public", source="settings.test", max_tokens=64)
        await gw.complete(system="", prompt=[image, {"type": "text", "text": "回复 ok"}], label="public", source="settings.test",
                          max_tokens=64, model=gw.vision_model)
    except Exception as e:  # 各家接口报错的类型不同，都要提示到页面上
        raise HTTPException(409, f"连接失败：{type(e).__name__}：{str(e)[:200]}") from e
    return {"latency_s": round(time.time() - t0, 2)}
