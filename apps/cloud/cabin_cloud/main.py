"""FastAPI 入口：WebSocket 网关 + REST + 静态资源（证据、SimCar、前端构建产物）。"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from cabin_common import vehicles
from cabin_common.retention import prune

from . import audioasset, auth, board, casewf, heal, imgasset, modules, planner, rigplan, schedule, settings_api, skills, skills_api, uimap
from .bus import bus
from .caselib import get_caselib
from .casexls import XLSX_EXT
from .config import BACKEND_DIR, get_settings
from .rigs import rig_health, rig_profiles
from .execsvc import rigapi
from .execsvc.service import get_service, rig_capabilities
from .gateway import serve_ws
from .integ.bitable import all_bitables, enabled as bitable_enabled
from .integ.jira import get_jira
from .modelgw import get_gateway
from .prd import SUPPORTED, uploads_dir
from .planvars import ResolveError, resolve
from .planloader import dry_run
from .store import get_store
from .voice.asr import get_asr

EVIDENCE_DAYS, EVIDENCE_MAX_BYTES = 7, 20 * 2 ** 30
FRONTEND_DIST = BACKEND_DIR / "web" / "dist"  # 用例工作台与调度看板
OPEN_API = ("/api/v1/rigs/", "/api/v1/runs/", "/api/v1/evidence/", "/api/v1/judge/", "/api/v1/auth/",  # 台架令牌或登录接口自行鉴权
            "/api/v1/uimap/",
            "/api/v1/vehicles",  # 车型字典不含业务数据
            "/api/health")
RIG_READABLE = ("/api/v1/cases", "/api/v1/plans/")  # A-15 / A-11：台架令牌也可读


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    get_jira()  # 写入沙箱种子
    get_caselib().fail_interrupted()
    get_service().resume()
    if (get_settings().asr_model_dir / "model.int8.onnx").exists():
        get_asr()._load()  # 预热 ASR
    else:  # 没有模型也能启动：只是不能语音输入，打字照常
        print(f"[CabinPilot] 没有语音识别模型（{get_settings().asr_model_dir}），语音输入不可用", flush=True)
    if bitable_enabled():
        bus.spawn(casewf.sync_loop())
    bus.spawn(_sweep_evidence())
    bus.spawn(schedule.loop())
    yield


async def _sweep_evidence() -> None:
    """云端只临时存失败证据：未处理的 7 天后过期，总量超限时删最旧的。"""
    while True:
        get_service().expire_reviews()
        prune(get_settings().evidence_dir, EVIDENCE_DAYS, EVIDENCE_MAX_BYTES)
        await get_service().sync_jira()
        await asyncio.sleep(3600)


app = FastAPI(title="CabinPilot 座舱智能测试机器人", version="0.1.0", lifespan=lifespan)
rigapi.install(app)
app.include_router(auth.router)
app.include_router(board.router)
app.include_router(settings_api.router)
app.include_router(skills_api.router)


DEMO_READONLY = re.compile(r"^/api/(settings/|skills/[^/]+(/rollback)?$|board/rigs/[^/]+/(caseset|schedule)$|caselib/sync$)")


def demo_blocked(method: str, path: str) -> bool:
    """公开演示：改配置、改规范包、改台架排程的接口只读（谁都不能改，模型密钥和地址不会被换掉）。"""
    return method not in ("GET", "HEAD") and bool(DEMO_READONLY.match(path))


@app.middleware("http")
async def require_login(request: Request, call_next):
    """接口和证据默认要求登录，放行的路径列在 OPEN_API。"""
    p = request.url.path
    if get_settings().demo and demo_blocked(request.method, p):
        return JSONResponse({"code": 1004, "message": "演示版不能修改这项设置"}, 403)
    guarded = (p.startswith("/api/") and not p.startswith(OPEN_API)) or p.startswith("/evidence/")
    if guarded and not auth.current_user(request):
        if not (p.startswith(RIG_READABLE) and rigapi.rig_of(request.headers.get("authorization", ""))):
            return JSONResponse({"code": 1002, "message": "请先登录"}, 401)
    return await call_next(request)


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await serve_ws(ws)


@app.post("/api/asr")
async def asr(file: UploadFile = File(...), session: str = ""):
    data = await file.read()
    if not (get_settings().asr_model_dir / "model.int8.onnx").exists():
        return JSONResponse({"code": 1005, "message": "没有语音识别模型，请打字输入"}, 503)
    r = await get_asr().transcribe(data)
    # 识别结果也进事件日志：前端因唤醒词 / 空文本丢弃的话同样可追溯（P-3）
    get_store().append(session or "system", "asr.result", {k: r[k] for k in ("text", "raw", "duration", "latency_s")})
    return r


@app.get("/api/tts")
async def tts(text: str):
    return Response(await get_gateway().tts(text[:300]), media_type="audio/mpeg")


@app.get("/api/health")
async def health():
    s = get_settings()
    h = rig_health(s.rig_id)
    gw = get_gateway()
    return {"model": gw.chat_model, "vision_model": gw.vision_model, "llm_base": gw.base_url,
            "asr": "sherpa-onnx SenseVoice (local)", "tts": s.tts_model, "device": s.device, "rig": h.model_dump(),
            "jira": "rest" if get_jira().rest_enabled else "sandbox-local",
            "feishu": "webhook" if s.feishu_webhook else "simulated", "strict_data": s.strict_data,
            "bitable": {"enabled": bitable_enabled(), "tables": len(all_bitables()), "last_sync": casewf.LAST_SYNC}}


@app.get("/api/modules")
async def module_routes():
    """模块路由（backend/modules.yaml）：每个模块的文件夹、用例表，以及已入库用例数。"""
    counts: dict[str, int] = {}
    for c in get_caselib().library():
        k = modules.code_of_case(c.case_id)
        counts[k] = counts.get(k, 0) + 1
    listed = modules.all_modules()
    extra = [modules.get(k) for k in counts if k not in {m.code for m in listed}]
    return {"file": str(get_settings().modules_file), "problems": modules.problems(),
            "default_bitable": get_settings().feishu_bitable_url,
            "modules": [{**m.as_dict(), "cases": counts.get(m.code, 0)} for m in listed + extra]}


@app.get("/api/rigs")
async def rigs():
    return [r.model_dump() for r in rig_profiles().values()]


@app.get("/api/sessions")
async def sessions():
    return get_store().sessions()


@app.get("/api/sessions/{sid}/events")
async def events(sid: str):
    return get_store().replay(sid)


@app.get("/api/tasks")
async def tasks():
    return {"live": get_service().summary(), "history": get_store().list_tasks(20)}


@app.get("/api/tasks/{tid}")
async def task(tid: str):
    t = get_store().get_task(tid)
    if not t:
        raise HTTPException(404)
    t["defects"] = get_store().list_defects(task_id=tid)
    return t


@app.get("/api/defects")
async def defects(status: str | None = None):
    return get_store().list_defects(status=status)


@app.get("/api/eval/stats")
async def eval_stats():
    return get_store().false_positive_stats()


@app.get("/api/jira/{key}")
async def jira_issue(key: str, format: str = "html"):
    it = get_jira().get_local(key)
    if not it:
        raise HTTPException(404, f"本地沙箱中没有 {key}")
    if format == "json":
        return it
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td><pre>{html.escape(json.dumps(v, ensure_ascii=False, indent=1) if isinstance(v, (list, dict)) else str(v))}</pre></td></tr>"
                   for k, v in it.items())
    return HTMLResponse(f"<meta charset=utf-8><title>{key}</title><style>body{{font:14px sans-serif;margin:24px}}th{{text-align:left;vertical-align:top;padding:4px 12px}}pre{{white-space:pre-wrap;margin:0}}</style>"
                        f"<h2>Jira 本地沙箱 · {html.escape(key)}</h2><table>{rows}</table>")


# ---------- 用例闭环（一期 A） ----------
@app.post("/api/prd/upload")
async def prd_upload(file: UploadFile = File(...), session: str = ""):
    name = Path(file.filename or "prd").name
    if Path(name).suffix.lower() not in SUPPORTED + XLSX_EXT:
        raise HTTPException(400, f"不支持的格式，支持 {' / '.join(SUPPORTED + XLSX_EXT)}")
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413, "文件超过 30MB")
    fid = f"{uuid.uuid4().hex[:10]}{Path(name).suffix.lower()}"
    (uploads_dir() / fid).write_bytes(data)
    get_store().append(session or "system", "prd.upload", {"id": fid, "name": name, "size": len(data)})
    return {"id": fid, "name": name, "size": len(data)}


@app.get("/api/case-batches")
async def case_batches():
    return get_caselib().list_batches()


@app.get("/api/case-batches/{bid}")
async def case_batch(bid: str):
    lib = get_caselib()
    b = lib.get_batch(bid)
    if not b:
        raise HTTPException(404)
    return {"batch": b.model_dump(), "cases": [c.model_dump() for c in lib.batch_cases(bid)],
            "review": lib.review_stats(bid)}


@app.post("/api/caselib/sync")
async def caselib_sync():
    """手动触发多维表格 → CabinPilot 同步。"""
    return await casewf.sync_now()


@app.get("/api/caselib")
async def caselib():
    return [c.model_dump() for c in get_caselib().library()]


@app.get("/api/v1/cases")
async def a15_cases(model: str = "", rig_id: str = "", module: str = "", case_id: str = "", updated_since: str = "",
                    baseline: str = ""):
    """A-15 拉取用例：只返回审核通过、适用该车型的用例；baseline 缺省为各 PRD 最新已发布基线。
    测试代码在审核通过时就自动生成；这里只给漏掉的（或车型参数取值已变的）补生成（generating）。"""
    if not model:
        return JSONResponse({"code": 1001, "msg": "model 必填"}, 400)
    since = 0.0
    if updated_since:
        try:
            since = datetime.fromisoformat(updated_since).timestamp()
        except ValueError:
            return JSONResponse({"code": 1001, "msg": "updated_since 格式错误"}, 400)
    lib = get_caselib()
    try:
        cases = lib.pull(model, module=module, case_id=case_id, rig_id=rig_id, updated_since=since, baseline=baseline)
    except ValueError as e:
        return JSONResponse({"code": 1001, "msg": str(e)}, 400)
    need = casewf.claim(cases)  # 审核通过时一般已生成；这里只补漏，和审核入口共用占位，不会重复生成
    for c in need:
        c.plan_status = "generating"
        lib.save_rev(c)
    if need:
        bus.spawn(casewf.code_and_trial(need))
    return {"code": 0, "data": {"cases": [lib.a15_row(c) for c in cases], "next_cursor": None}, "ts": time.time()}


@app.get("/api/v1/plans/{case_id}")
async def taskplan(case_id: str, rev: int | None = None):
    """下载 TaskPlan（测试代码，YAML）；rev 缺省为最新修订。车型代入在台架装载时完成。"""
    c = get_caselib().get_case(case_id, rev)
    text = planner.read_plan(case_id, rev)
    if not c or text is None:
        raise HTTPException(404, f"{case_id} 尚无测试代码")
    return Response(text, media_type="text/yaml; charset=utf-8",
                    headers={"X-Plan-Version": c.plan_version, "X-Plan-Status": c.plan_status,
                             "X-Plan-Sha256": hashlib.sha256(text.encode("utf8")).hexdigest()})


@app.get("/api/v1/plans/{case_id}/assets/{name}")
async def plan_asset(case_id: str, name: str, model: str = "", rev: int | None = None):
    """A-11 下载测试音频。台架按 TaskPlan 里的 sha256 校验；含车型变体的音频需带 model（车型代码）。"""
    text = planner.read_plan(case_id, rev)
    plan = yaml.safe_load(text) if text else {}
    asset = (plan.get("assets") or {}).get(name)
    if asset and asset.get("variants") is not None:
        try:
            asset = (resolve(plan, model or get_settings().rig_model).get("assets") or {}).get(name)
        except ResolveError as e:
            return JSONResponse({"code": 2202, "msg": str(e)}, 404)
    if not asset or not asset.get("sha256"):
        return JSONResponse({"code": 2202, "msg": f"测试音频不存在：{name}"}, 404)
    try:
        data = audioasset.read_verified(asset["sha256"])
    except audioasset.AssetError as e:
        return JSONResponse({"code": 2202, "msg": str(e)}, 404)
    return Response(data, media_type="audio/wav", headers={"X-Asset-Sha256": asset["sha256"]})


@app.get("/api/v1/plans/{case_id}/images/{name}")
async def plan_image(case_id: str, name: str, rev: int | None = None):
    """下载图标模板（多级定位的图标层）。台架按 TaskPlan 里的 sha256 校验。"""
    text = planner.read_plan(case_id, rev)
    img = ((yaml.safe_load(text) if text else {}).get("images") or {}).get(name)
    if not img or not img.get("sha256"):
        return JSONResponse({"code": 2202, "msg": f"图标模板不存在：{name}"}, 404)
    try:
        data = imgasset.read_verified(img["sha256"])
    except imgasset.ImageAssetError as e:
        return JSONResponse({"code": 2202, "msg": str(e)}, 404)
    return Response(data, media_type="image/png", headers={"X-Asset-Sha256": img["sha256"]})


# ---------- 触屏执行：UI 地图录制回看 / 确认 ----------
@app.get("/api/uimap/recordings")
async def uimap_list(status: str = ""):
    return {"recordings": uimap.list_recordings(status)}


@app.get("/api/uimap/recordings/{rec_id}/detail")
async def uimap_detail(rec_id: str):
    """回看页用：录制全文 + 截图地址（/api/uimap/images/<哈希>）。"""
    r = uimap.load_recording(rec_id)
    if not r:
        raise HTTPException(404, "录制不存在")
    return {"recording": r.model_dump(), "image_base": "/api/uimap/images/",
            "plan": uimap.plan_from_recording(r) if r.outcome == "done" else None}


@app.get("/api/uimap/images/{sha}")
async def uimap_image(sha: str):
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise HTTPException(400, "哈希不合法")
    try:
        return Response(imgasset.read_verified(sha), media_type="image/png")
    except imgasset.ImageAssetError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/uimap/recordings/{rec_id}", response_class=HTMLResponse)
async def uimap_report(rec_id: str):
    r = uimap.load_recording(rec_id)
    if not r:
        raise HTTPException(404, "录制不存在")
    return uimap.report_html(r)


@app.post("/api/uimap/recordings/{rec_id}/confirm")
async def uimap_confirm(rec_id: str, body: dict, request: Request):
    """确认后并入 UI 地图，可用于生成测试代码；驳回的不并入。确认人取登录用户。"""
    user = auth.current_user(request)
    try:
        r = uimap.confirm(rec_id, user.name if user else "测试工程师", ok=bool(body.get("ok", True)),
                          props=body.get("props"))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {"rec_id": r.rec_id, "status": r.status}


# ---------- 自愈建议：定位失败阻塞的执行 → 新控件建议 → 人工确认写回 ----------
def _heal_run(run_id: str) -> tuple[dict, dict, Path]:
    run = get_store().run(run_id)
    if not run or not run.get("result"):
        raise HTTPException(404, "执行不存在或还没有结果")
    return run, json.loads(run["result"]), get_settings().evidence_dir / run_id


@app.get("/api/heal/candidates")
async def heal_candidates(limit: int = 50):
    """定位失败阻塞的执行（最近的在前），带已有的自愈建议状态。"""
    out = []
    for r in get_store().q("SELECT id, case_id, rig_id, updated, result FROM run WHERE block_reason='locator' "
                           "ORDER BY updated DESC LIMIT ?", (min(max(limit, 1), 200),)):
        res = json.loads(r["result"] or "{}")
        step = heal.failed_step(res)
        ev = get_settings().evidence_dir / r["id"]
        sug = heal.load_suggestion(ev, step) if step else None
        failed = next((s for s in res.get("steps") or [] if s.get("id") == step), {})
        out.append({"run_id": r["id"], "case_id": r["case_id"], "rig_id": r["rig_id"], "at": r["updated"], "step": step,
                    "label": failed.get("label", ""), "error": failed.get("error", ""),
                    "has_scene": bool(step) and (ev / f"fail-{step:02d}.png").is_file(),
                    "suggestion": {k: sug.get(k) for k in ("status", "found", "reason")} if sug else None})
    return {"runs": out}


@app.get("/api/heal/{run_id}")
async def heal_detail(run_id: str):
    run, res, ev = _heal_run(run_id)
    step = heal.failed_step(res)
    if not step:
        raise HTTPException(400, "这次执行不是定位失败阻塞")
    shot = ev / f"fail-{step:02d}.png"
    return {"run_id": run_id, "case_id": run["case_id"], "step": step,
            "fail_shot": f"/evidence/{run_id}/{shot.name}" if shot.is_file() else "",
            "suggestion": heal.load_suggestion(ev, step),
            "marked_shot": f"/evidence/{run_id}/heal-{step:02d}.png" if (ev / f"heal-{step:02d}.png").is_file() else ""}


@app.post("/api/heal/{run_id}")
async def heal_suggest(run_id: str, body: dict):
    """调用多模态模型给出建议；不改变该次执行的结论。"""
    run, res, ev = _heal_run(run_id)
    step = int(body.get("step") or 0) or heal.failed_step(res)
    if not step:
        raise HTTPException(400, "这次执行不是定位失败阻塞")
    plan = yaml.safe_load(planner.read_plan(run["case_id"]) or "") or {}
    try:
        return await heal.suggest(ev, plan, step, case_step=str(body.get("case_step") or ""), session=f"heal:{run_id}")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/heal/{run_id}/confirm")
async def heal_confirm(run_id: str, body: dict, request: Request):
    """接受则写回测试代码（新提交）；拒绝则计入评估集。确认人取登录用户。"""
    run, res, ev = _heal_run(run_id)
    step = int(body.get("step") or 0) or heal.failed_step(res)
    f = ev / f"heal-{int(step or 0):02d}.json"
    if not step or not f.is_file():
        raise HTTPException(404, "还没有这一步的自愈建议")
    sug = json.loads(f.read_text("utf8"))
    if sug.get("status", "待确认") != "待确认":
        raise HTTPException(400, f"这条建议已{sug['status']}")
    user = auth.current_user(request)
    by = user.name if user else "测试工程师"
    try:
        out = await heal.confirm(run["case_id"], sug, by, ok=bool(body.get("ok", True)))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    heal.mark(ev, int(step), "已接受" if out["applied"] else "已拒绝", by)
    return out


@app.get("/api/casegen/skills")
async def casegen_skills():
    """用例编写规范包：各文件的解析问题、每个模块实际生效的规范（默认包 + 模块包），以及按规范版本统计的审核结论。"""
    stats = skills.review_stats(get_store())
    files = [{"name": x.name, "file": x.file, "modules": x.modules, "version": x.version, "sha": x.sha,
              "rules": len(x.rules), "problems": x.problems} for x in skills.load_all()]
    per_module = []
    for m in modules.all_modules():
        sk = skills.for_module(m.code)
        per_module.append({"code": m.code, "name": m.name, "skill": sk.meta() if sk else None,
                           "review": stats.get(sk.sha) if sk else None})
    return {"files": files, "modules": per_module, "review_by_sha": stats}


@app.get("/api/runnable")
async def runnable(rig: str = ""):
    """台架可执行的用例（审核通过、测试代码就绪、干跑通过）。只读，不触发生成。"""
    rig = rig or get_settings().rig_id
    if rig not in rig_profiles():
        raise HTTPException(404, f"台架 {rig} 不存在")
    return {"rig": rig, "cases": rigplan.runnable(rig)}


@app.get("/api/v1/vehicles")
async def vehicle_list():
    """车型字典，台架 Agent 的车型下拉框用。"""
    return {"code": 0, "message": "ok", "data": [{"code": c, "name": vehicles.name(c)} for c in vehicles.codes()]}


@app.get("/api/v1/plans/{case_id}/dry-run")
async def plan_dry_run(case_id: str):
    """干跑（PF-RUN-11）：校验动作、参数、能力，不操作车机。"""
    return dry_run(case_id, rig_capabilities())


@app.post("/api/demo/reset")
async def demo_reset():
    get_store().reset_runtime()
    get_jira().seed()
    svc = get_service()
    for st in list(svc.tasks.values()):
        if st.job and not st.job.done():
            st.job.cancel()
    svc.tasks.clear()
    return {"ok": True}


app.mount("/evidence", StaticFiles(directory=get_settings().evidence_dir), name="evidence")


@app.get("/{path:path}")
async def spa(path: str):
    if not FRONTEND_DIST.exists():
        return JSONResponse({"msg": "看板未构建：cd apps/cloud/web && npm run build"}, 404)
    f = FRONTEND_DIST / path
    if path and f.is_file():
        return FileResponse(f)
    return FileResponse(FRONTEND_DIST / "index.html")


def lan_addresses() -> list[str]:
    """本机的局域网 IPv4 地址（同一 Wi-Fi 下别人用这些地址访问）。"""
    import socket
    found: list[str] = []
    try:  # 走默认路由的那块网卡（不会真的发包）
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.connect(("10.255.255.255", 1))
            found.append(u.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(found) if not ip.startswith(("127.", "169.254."))]


def startup_notes(s=None) -> list[str]:
    """启动时给部署的人看：从哪里访问、哪些配置不对会导致别人用不了。"""
    s = s or get_settings()
    notes = []
    if s.host in ("0.0.0.0", "::"):
        ips = lan_addresses()
        notes.append("局域网访问地址：" + ("、".join(f"http://{ip}:{s.port}" for ip in ips) or "（没找到局域网地址）"))
        if "127.0.0.1" in s.public_base_url or "localhost" in s.public_base_url:
            notes.append(f"CABIN_PUBLIC_URL 还是 {s.public_base_url}：飞书登录回跳和台架连接会失败，"
                         f"改成本机局域网地址，如 http://{ips[0] if ips else '192.168.x.x'}:{s.port}")
    else:
        notes.append(f"只监听 {s.host}，局域网里的其他电脑访问不到；要给大家用，设 CABIN_HOST=0.0.0.0")
    if s.auth_mode == "lan" and not s.lan_passcode:
        notes.append("开了局域网登录（CABIN_AUTH_MODE=lan）但没设团队口令 CABIN_LAN_PASSCODE：谁都登录不了")
    if s.auth_mode == "feishu" and not (s.feishu_app_id and s.feishu_app_secret):
        notes.append("没配飞书应用（FEISHU_APP_ID / SECRET）：登录不了。没有飞书应用时用 CABIN_AUTH_MODE=lan")
    return notes


def run() -> None:
    s = get_settings()
    for n in startup_notes(s):
        print(f"[CabinPilot] {n}", flush=True)
    uvicorn.run("cabin_cloud.main:app", host=s.host, port=s.port, log_level="info")
