"""调度看板接口：台架在线状态、执行情况与回归缺陷审核。在线与否只有云端能判断，所以看板放在云端。"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .auth import current_user
from .execsvc.service import get_service
from . import rigplan, schedule, stats
from .config import get_settings
from .rigs import RIG_OFFLINE_S
from .store import get_store

router = APIRouter(prefix="/api/board")
RECENT_RUNS = 50
RECENT_BATCHES = 10


def _rig_row(r: dict, now: float, running: dict[str, str]) -> dict:
    doc = json.loads(r["doc"] or "{}")
    host = doc.get("host") or {}
    return {"id": r["id"], "model": r["model"], "ota": r["ota"] or "", "capabilities": json.loads(r["capabilities"] or "[]"),
            "state": r["state"], "online": now - (r["last_seen"] or 0) < RIG_OFFLINE_S, "status": r["status"],
            "last_seen": r["last_seen"], "outbox": doc.get("outbox") or {}, "agent_version": doc.get("agent_version", ""),
            "run_id": running.get(r["id"], ""), "model_warning": host.get("model_warning", ""),
            "caseset": rigplan.caseset(r["id"]), "schedule": schedule.get_schedule(r["id"])}


def _run_row(r: dict) -> dict:
    return {"id": r["id"], "task_id": r["task_id"], "rig_id": r["rig_id"], "case_id": r["case_id"], "status": r["status"],
            "owner": r.get("owner") or "", "kind": r["kind"],
            "verdict": r["verdict"] or "", "block_reason": r["block_reason"] or "", "duration_s": r["duration_s"] or None,
            "model": r["model"] or "", "ota": r["ota"] or "", "module": r["module"] or "",
            "created": r["created"], "updated": r["updated"]}


def _defect_row(d, me: str) -> dict:
    root = get_settings().evidence_dir / d.run_id if d.run_id else None
    files = [f.name for f in root.iterdir() if f.suffix in (".mp4", ".zip", ".tar", ".gz", ".tgz")] if root and root.is_dir() else []
    return {"id": d.id, "case_id": d.case_id, "rig": d.rig, "rigs": d.rigs, "summary": d.summary, "reason": d.reason, "level": d.level,
            "ota": d.ota, "model": d.model, "module": d.module, "finder": d.finder, "mine": me in d.assignees,
            "actual_shot": d.actual_shot, "dup": d.dup.label if d.dup else "", "dup_key": d.dup.issue_key if d.dup else "",
            "run_id": d.run_id, "files": files}


@router.get("")
async def board(req: Request):
    store, now = get_store(), time.time()
    me = current_user(req).name
    runs = store.recent_runs(RECENT_RUNS)
    running = {r["rig_id"]: r["id"] for r in reversed(runs) if r["status"] in ("running", "cancelling")}
    defects = [_defect_row(d, me) for d in get_service().pending_reviews()]
    return {"now": now, "rigs": [_rig_row(r, now, running) for r in store.rigs()], "runs": [_run_row(r) for r in runs],
            "defects": defects, "gaps": rigplan.gaps(), "stats": stats.overview(), "me": me,
            "regressions": store.regressions(RECENT_BATCHES), "kpi": stats.kpi()}


@router.get("/regressions/{regression_id}")
async def regression_detail(regression_id: str):
    runs = get_store().regression_runs(regression_id)
    return {"runs": [_run_row(r) for r in runs]}


@router.get("/matrix")
async def coverage_matrix():
    return stats.matrix()


class ReviewReq(BaseModel):
    action: str  # confirm / link / reject
    reason: str = ""
    severity: str = ""
    link_key: str = ""


@router.post("/defects/{defect_id}/review")
async def review(defect_id: str, body: ReviewReq, req: Request):
    """回归的失败来自多台台架，由负责人在云端统一审核。"""
    user = current_user(req)
    try:
        await get_service().review(defect_id, body.action, by=user.name, reason=body.reason, severity=body.severity,
                                  link_key=body.link_key)
    except (KeyError, ValueError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str):
    run = get_store().run(run_id)
    if not run:
        raise HTTPException(404, "执行单元不存在")
    return {"ok": await get_service().cancel(run["task_id"], by="看板")}


class CasesetReq(BaseModel):
    case_ids: list[str]


@router.get("/rigs/{rig_id}/caseset")
async def get_caseset(rig_id: str):
    try:
        return {"case_ids": rigplan.caseset(rig_id), "runnable": rigplan.runnable(rig_id)}
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.put("/rigs/{rig_id}/caseset")
async def put_caseset(rig_id: str, body: CasesetReq, req: Request):
    try:
        rigplan.save_caseset(rig_id, body.case_ids, current_user(req).name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class ScheduleReq(BaseModel):
    at: str = ""
    enabled: bool = False


@router.put("/rigs/{rig_id}/schedule")
async def put_schedule(rig_id: str, body: ScheduleReq, req: Request):
    if not get_store().rig(rig_id):
        raise HTTPException(404, f"台架 {rig_id} 不存在")
    try:
        schedule.save_schedule(rig_id, body.at, body.enabled, current_user(req).name)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class RunReq(BaseModel):
    session: str = ""
    case_ids: list[str] | None = None  # 给定时只影响这一次


@router.post("/rigs/{rig_id}/run")
async def run_caseset(rig_id: str, body: RunReq, req: Request):
    """立即运行本台架用例，触发的人就是发现人。"""
    try:
        return await rigplan.dispatch(rig_id, current_user(req).name, body.session or f"S-{rig_id}", body.case_ids)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
