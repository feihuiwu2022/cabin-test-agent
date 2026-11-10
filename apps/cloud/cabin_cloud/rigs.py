"""台架档案（PRD B.1）：来自台架注册记录。车机接在台架 PC 上，云端只读它上报的状态。"""
from __future__ import annotations

import json
import time

from pydantic import BaseModel

from cabin_common.schema import Rig

from .config import get_settings
from .store import get_store

RIG_OFFLINE_S = 60  # 接口约定 A-02：60 秒未收到心跳判定掉线


class RigHealth(BaseModel):
    online: bool
    ota: str = ""
    model: str = ""
    detail: str = ""


def rig_profiles() -> dict[str, Rig]:
    """默认台架还没注册时也列出来，任务下达时提示离线。"""
    s = get_settings()
    rigs = {r["id"]: Rig(id=r["id"], model=r["model"], connection="agent", capabilities=json.loads(r["capabilities"] or "[]"),
                         owner="测试组") for r in get_store().rigs()}
    rigs.setdefault(s.rig_id, Rig(id=s.rig_id, model=s.rig_model, connection="agent", capabilities=[], owner="测试组"))
    return rigs


def rig_health(rig_id: str) -> RigHealth:
    r = get_store().rig(rig_id)
    if not r:
        return RigHealth(online=False, detail="台架未注册")
    online = time.time() - (r["last_seen"] or 0) < RIG_OFFLINE_S
    return RigHealth(online=online, ota=r["ota"] or "", model=r["model"] or "",
                     detail="" if online else f"超过 {RIG_OFFLINE_S} 秒没有心跳")
