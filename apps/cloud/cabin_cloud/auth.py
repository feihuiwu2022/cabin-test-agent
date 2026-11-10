"""用户登录：审核留痕要落到具体的人，台架令牌只代表机器。

feishu：飞书网页授权；lan：局域网部署且没有飞书应用时，填姓名 + 团队口令（CABIN_LAN_PASSCODE）登录；
dev：不接飞书，直接以 CABIN_DEV_USER 登录，只用于本地调试，须显式开启。
云端看板用 Cookie；本地 Agent 的页面在 127.0.0.1，登录后换一张一次性票据，由 Agent 换成令牌保存。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .config import get_settings
from .store import get_store

COOKIE = "cabin_session"
STATE_TTL_S = 300
TICKET_TTL_S = 60
FEISHU = "https://open.feishu.cn"

router = APIRouter(prefix="/api/v1/auth")


class User(BaseModel):
    id: str
    name: str
    email: str = ""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def issue_session(user: User) -> str:
    token = secrets.token_urlsafe(32)
    get_store().save_user_session(_hash(token), user.id, user.name, time.time() + get_settings().session_ttl_s)
    return token


def resolve(token: str) -> User | None:
    row = get_store().user_session(_hash(token)) if token else None
    if row:
        return User(id=row["user_id"], name=row["name"])
    return GUEST if get_settings().demo else None


GUEST = User(id="guest", name="访客")  # 公开演示模式下没登录的人


def token_of(headers, cookies) -> str:
    auth = headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip() if auth else cookies.get(COOKIE, "")


def current_user(req: Request) -> User | None:
    return resolve(token_of(req.headers, req.cookies))


def safe_redirect(url: str) -> str:
    """登录后只回本站相对路径或本机 Agent，防止被当成开放跳转。"""
    if url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    u = urlparse(url)
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"):
        return url
    return "/"


def _finish(user: User, redirect: str) -> RedirectResponse:
    token = issue_session(user)
    get_store().save_user_profile(user.id, user.name, user.email)
    if redirect.startswith("/"):
        resp = RedirectResponse(redirect, 302)
        s = get_settings()
        resp.set_cookie(COOKIE, token, max_age=s.session_ttl_s, httponly=True, samesite="lax",
                        secure=s.public_base_url.startswith("https"))
        return resp
    ticket = secrets.token_urlsafe(24)
    get_store().put_auth_item(_hash(ticket), json.dumps({"token": token, "user": user.model_dump()}), time.time() + TICKET_TTL_S)
    return RedirectResponse(f"{redirect}?{urlencode({'ticket': ticket})}", 302)


@router.get("/login")
async def login(redirect: str = "/"):
    s = get_settings()
    target = safe_redirect(redirect)
    if s.auth_mode == "dev":
        return _finish(User(id="dev", name=s.dev_user), target)
    if s.auth_mode == "lan":
        return HTMLResponse(_lan_page(target))
    if not (s.feishu_app_id and s.feishu_app_secret):
        return JSONResponse({"code": 1002, "message": "未配置飞书应用，无法登录"}, 503)
    state = secrets.token_urlsafe(24)
    get_store().put_auth_item(_hash(state), target, time.time() + STATE_TTL_S)
    q = urlencode({"app_id": s.feishu_app_id, "redirect_uri": f"{s.public_base_url}/api/v1/auth/feishu/callback", "state": state})
    return RedirectResponse(f"{FEISHU}/open-apis/authen/v1/authorize?{q}", 302)


async def feishu_user(code: str) -> User:
    """授权码换用户信息。"""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15, trust_env=os.getenv("CABIN_USE_PROXY") == "1") as c:
        r = await c.post(f"{FEISHU}/open-apis/authen/v2/oauth/token", json={
            "grant_type": "authorization_code", "client_id": s.feishu_app_id, "client_secret": s.feishu_app_secret,
            "code": code, "redirect_uri": f"{s.public_base_url}/api/v1/auth/feishu/callback"})
        tok = r.json().get("access_token")
        if not tok:
            raise ValueError(f"飞书授权失败：{r.json().get('error_description') or r.status_code}")
        u = (await c.get(f"{FEISHU}/open-apis/authen/v1/user_info", headers={"Authorization": f"Bearer {tok}"})).json()
    data = u.get("data") or {}
    if not data.get("open_id"):
        raise ValueError("飞书没有返回用户信息")
    return User(id=data["open_id"], name=data.get("name") or data["open_id"],
                email=data.get("enterprise_email") or data.get("email") or "")


@router.get("/feishu/callback")
async def feishu_callback(code: str = "", state: str = ""):
    target = get_store().take_auth_item(_hash(state)) if state else None
    if target is None or not code:
        return JSONResponse({"code": 1002, "message": "登录已过期，请重新登录"}, 400)
    try:
        return _finish(await feishu_user(code), target)
    except (ValueError, httpx.HTTPError) as e:
        return JSONResponse({"code": 1002, "message": str(e)}, 502)


class ExchangeReq(BaseModel):
    ticket: str


@router.post("/exchange")
async def exchange(body: ExchangeReq):
    raw = get_store().take_auth_item(_hash(body.ticket))
    if not raw:
        return JSONResponse({"code": 1002, "message": "票据无效或已过期"}, 401)
    return {"code": 0, "message": "ok", "data": json.loads(raw)}


@router.get("/me")
async def me(req: Request):
    u = current_user(req)
    return {"user": u.model_dump() if u else None, "auth_mode": get_settings().auth_mode}


@router.post("/logout")
async def logout(req: Request):
    token = token_of(req.headers, req.cookies)
    if token:
        get_store().delete_user_session(_hash(token))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---------- 局域网登录（没有飞书应用时）：姓名 + 共享口令 ----------
LAN_FAIL_LIMIT, LAN_FAIL_WINDOW_S = 10, 600
_lan_fails: dict[str, list[float]] = {}


def _lan_page(target: str, err: str = "") -> str:
    s = get_settings()
    who = ("<select name=\"name\" required>" + "".join(f"<option>{html.escape(n)}</option>" for n in s.lan_users) + "</select>"
           if s.lan_users else "<input name=\"name\" required maxlength=\"20\" autocomplete=\"username\" placeholder=\"你的姓名\">")
    note = "" if s.lan_passcode else "<p class=\"err\">服务器还没配置登录口令（CABIN_LAN_PASSCODE），暂时不能登录。</p>"
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CabinPilot 登录</title><style>
:root{{--bg:#EDF1F4;--panel:#fff;--line:#D3DCE2;--ink:#13202B;--sub:#4E606C;--accent:#C8102E}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0E1418;--panel:#141C21;--line:#26343D;--ink:#E6EDF1;--sub:#9AABB6;--accent:#E5484D}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:360px;margin:12vh auto;padding:0 16px}}form{{display:grid;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}}
label{{display:grid;gap:4px}}input,select{{font:inherit;color:var(--ink);background:transparent;border:1px solid var(--line);border-radius:6px;padding:7px 9px}}
button{{font:inherit;border:0;border-radius:8px;padding:9px;background:var(--accent);color:#fff;cursor:pointer}}.err{{color:var(--accent)}}p{{color:var(--sub)}}
</style><main><h1>CabinPilot 登录</h1><p>局域网登录：填你的姓名和团队口令。审核、编辑记录都记在这个姓名下。</p>{note}
<form method="post" action="/api/v1/auth/lan"><input type="hidden" name="redirect" value="{html.escape(target, quote=True)}">
<label>姓名 {who}</label><label>口令 <input name="passcode" type="password" required autocomplete="current-password"></label>
{f'<p class="err" role="alert">{html.escape(err)}</p>' if err else ''}<button>登录</button></form></main></html>"""


@router.post("/lan")
async def lan_login(req: Request):
    s = get_settings()
    if s.auth_mode != "lan":
        return JSONResponse({"code": 1002, "message": "没有开启局域网登录"}, 404)
    form = await req.form()
    name, code = str(form.get("name") or "").strip()[:20], str(form.get("passcode") or "")
    target = safe_redirect(str(form.get("redirect") or "/"))
    ip = req.client.host if req.client else "?"
    now = time.time()
    fails = [t for t in _lan_fails.get(ip, []) if now - t < LAN_FAIL_WINDOW_S]
    if len(fails) >= LAN_FAIL_LIMIT:
        return HTMLResponse(_lan_page(target, "失败次数太多，请 10 分钟后再试"), 429)
    ok = bool(s.lan_passcode) and hmac.compare_digest(code.encode(), s.lan_passcode.encode()) and bool(name) \
        and (not s.lan_users or name in s.lan_users)
    if not ok:
        _lan_fails[ip] = fails + [now]
        return HTMLResponse(_lan_page(target, "姓名或口令不对"), 401)
    _lan_fails.pop(ip, None)
    return _finish(User(id=f"lan:{name}", name=name), target)
