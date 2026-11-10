"""云端—台架接口：注册、心跳、领取、事件、证据、结果、判定。

全部由台架主动发起，云端不反向连接台架。响应用统一包络（code / message / data）返回。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cabin_common.judging import VisionReq, VoiceReq
from cabin_common.results import PlanResult

from cabin_common import vehicles
from ..config import get_settings
from ..judgeclient import LocalJudge
from ..store import get_store
from .service import get_service

API_VERSION = "1"
MAX_EVENTS = 200
MAX_EVIDENCE = 256 * 1024 * 1024  # 日志包可能较大
RE_RIG_ID = re.compile(r"B\d+")
RE_RUN_ID = re.compile(r"R\d{8}-\d{6}")
RE_FILE = re.compile(r"[A-Za-z0-9][\w.\-]*")

router = APIRouter(prefix="/api/v1")


class RigApiError(Exception):
    def __init__(self, code: int, http: int, message: str):
        super().__init__(message)
        self.code, self.http, self.message = code, http, message


def ok(data: Any = None) -> dict:
    return {"code": 0, "message": "ok", "data": data or {}, "request_id": str(uuid.uuid4())}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def _token(authorization: str) -> str:
    return authorization.removeprefix("Bearer ").strip()


def rig_of(authorization: str) -> dict | None:
    return get_store().rig_by_token(token_hash(_token(authorization))) if authorization else None


def current_rig(authorization: str = Header(""), x_cabin_api: str = Header(API_VERSION)) -> dict:
    if x_cabin_api != API_VERSION:
        raise RigApiError(1003, 426, f"接口版本不兼容：云端支持 {API_VERSION}")
    r = rig_of(authorization)
    if not r:
        raise RigApiError(1002, 401, "令牌无效或已吊销")
    if r["state"] != "enabled":
        raise RigApiError(2002, 403, "台架未审批或已停用")
    return r


def _own_run(rig: dict, run_id: str, attempt: int | None = None) -> dict:
    run = get_store().run(run_id) if RE_RUN_ID.fullmatch(run_id) else None
    if not run or run["rig_id"] != rig["id"]:  # 别的台架的执行单元和不存在的一样处理，不泄露编号
        raise RigApiError(1001, 400, f"执行单元 {run_id} 不属于本台架")
    if attempt is not None and attempt != run["attempt"]:
        raise RigApiError(2102, 409, f"attempt 不匹配：云端为 {run['attempt']}")
    return run


# ---------- A-01 / A-02 ----------
class RegisterReq(BaseModel):
    rig_id: str
    register_code: str = ""
    model: str
    ota_version: str = ""
    capabilities: list[str] = Field(default_factory=list)
    agent_version: str = ""
    host: dict[str, Any] = Field(default_factory=dict)


@router.post("/rigs/register")
async def register(body: RegisterReq, authorization: str = Header("")):
    store = get_store()
    if not RE_RIG_ID.fullmatch(body.rig_id):
        raise RigApiError(1001, 400, "台架编号格式应为 B + 数字")
    if body.model not in vehicles.codes():
        raise RigApiError(1001, 400, f"车型 {body.model} 不在车型字典内")
    old = store.rig(body.rig_id)
    token = ""
    if old and authorization and hmac.compare_digest(old["token_hash"], token_hash(_token(authorization))):
        h = old["token_hash"]
    else:
        code = get_settings().rig_register_code
        if not code or not hmac.compare_digest(body.register_code.encode("utf8"), code.encode("utf8")):
            raise RigApiError(1002, 401, "注册码无效")
        if old:  # 注册码不能用来抢占已注册的台架；要重新签发先由管理员删除台架记录
            raise RigApiError(1002, 401, f"台架 {body.rig_id} 已注册，请使用已签发的令牌")
        token = secrets.token_urlsafe(32)
        h = token_hash(token)
    store.save_rig(body.rig_id, h, body.model, body.ota_version, body.capabilities,
                   {"agent_version": body.agent_version, "host": body.host})
    return ok({"rig_token": token, "state": "enabled", "config_version": 1})


class HeartbeatReq(BaseModel):
    state: str = "idle"
    run_id: str | None = None
    ota_version: str = ""
    capabilities: list[str] | None = None
    outbox: dict[str, Any] = Field(default_factory=dict)


@router.post("/rigs/{rig_id}/heartbeat")
async def heartbeat(rig_id: str, body: HeartbeatReq, rig: dict = Depends(current_rig)):
    if rig_id != rig["id"]:
        raise RigApiError(1001, 400, "令牌与台架编号不一致")
    store = get_store()
    store.touch_rig(rig_id, body.state, body.ota_version, body.capabilities, body.outbox)
    cmds = [{"id": f"cancel-{r['id']}", "type": "cancel_run", "run_id": r["id"]} for r in store.runs_to_cancel(rig_id)]
    return ok({"commands": cmds, "config_version": 1})


# ---------- A-03 领取 ----------
@router.post("/runs/claim")
async def claim(rig: dict = Depends(current_rig)):
    run = get_store().claim_run(rig["id"])
    if not run:
        return Response(status_code=204)
    await get_service().on_run_claimed(run)
    return ok({"run_id": run["id"], "kind": run["kind"], "case_id": run["case_id"], "plan_version": run["plan_version"],
               "plan_inline": json.loads(run["plan"]), "case": json.loads(run["case_doc"]), "regression_id": run["regression_id"] or None,
               "timeout_s": run["timeout_s"], "attempt": run["attempt"]})


# ---------- A-05 事件 ----------
class EventsReq(BaseModel):
    attempt: int
    events: list[dict[str, Any]]


@router.post("/runs/{run_id}/events")
async def events(run_id: str, body: EventsReq, rig: dict = Depends(current_rig)):
    run = _own_run(rig, run_id, body.attempt)
    if len(body.events) > MAX_EVENTS:
        raise RigApiError(4001, 413, f"单批事件不能超过 {MAX_EVENTS} 条")
    store, acked = get_store(), run["acked_seq"]
    for e in sorted(body.events, key=lambda x: x.get("seq", 0)):
        seq = e.get("seq", 0)
        if seq <= acked:  # 补传重复，按 seq 去重
            continue
        if seq != acked + 1:
            break
        await get_service().on_run_event(run, e)
        acked = seq
        store.ack_run_seq(run_id, acked)
    return ok({"acked_seq": acked})


# ---------- A-06 证据 ----------
class UploadReq(BaseModel):
    run_id: str
    attempt: int
    file: str
    size: int
    sha256: str


@router.post("/evidence/upload-url")
async def upload_url(body: UploadReq, rig: dict = Depends(current_rig)):
    run = _own_run(rig, body.run_id, body.attempt)
    if not RE_FILE.fullmatch(body.file):
        raise RigApiError(1001, 400, f"文件名不合法：{body.file}")
    if body.size > MAX_EVIDENCE:
        raise RigApiError(4001, 413, "证据文件超限")
    return ok({"evidence_id": f"E{run['id']}-{run['attempt']}",
               "upload": {"method": "PUT", "url": f"/api/v1/evidence/{run['id']}/{body.file}?sha256={body.sha256}",
                          "expires_s": 600}})


@router.put("/evidence/{run_id}/{name}")
async def upload_file(run_id: str, name: str, sha256: str, request: Request, rig: dict = Depends(current_rig)):
    run = _own_run(rig, run_id)
    if not RE_FILE.fullmatch(name):
        raise RigApiError(1001, 400, f"文件名不合法：{name}")
    data = await request.body()
    if len(data) > MAX_EVIDENCE:
        raise RigApiError(4001, 413, "证据文件超限")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RigApiError(1001, 400, f"{name} 哈希不符")
    root = get_settings().evidence_dir / run["id"]
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    return ok()


# ---------- A-07 结果 ----------
class ResultReq(BaseModel):
    attempt: int
    verdict: str
    plan_result: dict[str, Any]


@router.post("/runs/{run_id}/result")
async def result(run_id: str, body: ResultReq, rig: dict = Depends(current_rig)):
    run = _own_run(rig, run_id, body.attempt)
    res = PlanResult(**{**body.plan_result, "evidence_dir": str(get_settings().evidence_dir / run["id"])})
    summary = {"model": rig["model"], "ota": res.ota or rig["ota"], "module": json.loads(run["case_doc"]).get("module", ""),
               "verdict": res.verdict, "block_reason": res.block_reason, "duration_s": res.duration_s}
    if get_store().finish_run(run_id, res.model_dump_json(), summary):  # 只有第一次生效，重复提交返回首次结果
        await get_service().on_run_result(run, res)
    return ok({"accepted": True})


# ---------- A-10 判定（图片随请求上传，台架不持有模型凭证） ----------
@router.post("/judge/vision")
async def judge_vision(meta: str = Form(), images: list[UploadFile] = File(), rig: dict = Depends(current_rig)):
    m = json.loads(meta)
    run = _own_run(rig, m.get("run_id", ""))
    v = await LocalJudge().vision(VisionReq(
        case_id=run["case_id"], expected=m.get("expected", ""), screen=m.get("screen", ""),
        elements=m.get("elements", ""), region=m.get("region"), session=run["session"],
        images=[await f.read() for f in images]))
    return ok(v)


@router.post("/judge/voice")
async def judge_voice(meta: str = Form(), image: UploadFile = File(), tts_audio: UploadFile | None = File(None),
                      rig: dict = Depends(current_rig)):
    m = json.loads(meta)
    run = _own_run(rig, m.get("run_id", ""))
    v = await LocalJudge().voice(VoiceReq(
        case_id=run["case_id"], screen_expected=m.get("screen_expected", ""), tts_expected=m.get("tts_expected", ""),
        image=await image.read(), screen=m.get("screen", ""), elements=m.get("elements", ""),
        tts_audio=await tts_audio.read() if tts_audio else b"", transcript=m.get("transcript"),
        no_tts=bool(m.get("no_tts")), session=run["session"]))
    return ok(v)


# ---------- UI 地图录制（接口约定之外新增）（录制在台架跑，选编号用云端的多模态模型） ----------
@router.post("/uimap/decide")
async def uimap_decide(meta: str = Form(), image: UploadFile = File(), rig: dict = Depends(current_rig)):
    """带编号的截图 → 下一步点哪个编号。提示词里只有用例步骤与控件清单，系统提示在云端。"""
    from .. import uimap
    m = json.loads(meta)
    prompt = str(m.get("prompt") or "")[:8000]
    if not prompt:
        raise RigApiError(1001, 400, "缺少 prompt")
    ans = await uimap.VlmDecider().decide(prompt, await image.read(), f"rig:{rig['id']}:{m.get('rec_id', '')}")
    return ok(ans)


@router.post("/uimap/recordings")
async def uimap_upload(meta: str = Form(), images: list[UploadFile] = File([]), rig: dict = Depends(current_rig)):
    """录制 + 截图上传，保存为待确认。"""
    from cabin_common.uimap import Recording

    from .. import uimap
    try:
        rec = Recording.model_validate_json(meta)
        files = {(f.filename or "").removesuffix(".png"): await f.read() for f in images}
        rec = uimap.accept_upload(rec, files, rig["id"])
    except ValueError as e:
        raise RigApiError(1001, 400, f"录制不合法：{e}") from e
    return ok({"rec_id": rec.rec_id, "status": rec.status, "review_url": f"/api/uimap/recordings/{rec.rec_id}"})


def install(app: FastAPI) -> None:
    @app.exception_handler(RigApiError)
    async def _rig_error(_: Request, e: RigApiError) -> JSONResponse:
        return JSONResponse({"code": e.code, "message": e.message, "data": None, "request_id": str(uuid.uuid4())}, e.http)

    app.include_router(router)
