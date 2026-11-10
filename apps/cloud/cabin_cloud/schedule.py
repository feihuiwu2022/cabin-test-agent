"""定时回归与每日汇总：到点给台架开一轮回归，每天把结果和待审核推到飞书。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import rigplan
from .config import get_settings
from .execsvc.service import get_service
from .integ import feishu
from .store import get_store

log = logging.getLogger("cabin.schedule")

TICK_S = 30
CATCH_UP_MIN = 60  # 云端在到点时重启过，这段时间内补上；再晚就等第二天，免得白天突然开跑
RE_AT = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
VERDICTS = (("passed", "通过"), ("failed", "失败"), ("blocked", "阻塞"), ("flaky", "不稳定"))


def local_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=get_settings().tz_hours)))


def _late(at: str, now: datetime) -> int:
    """现在比 at 晚了多少分钟，还没到时为负数。"""
    h, m = at.split(":")
    return now.hour * 60 + now.minute - int(h) * 60 - int(m)


def _due(at: str, now: datetime) -> bool:
    return 0 <= _late(at, now) <= CATCH_UP_MIN


def get_schedule(rig_id: str) -> dict:
    s = get_store().schedule(rig_id)
    return {"at": s["at"], "enabled": bool(s["enabled"])} if s else {"at": "", "enabled": False}


def save_schedule(rig_id: str, at: str, enabled: bool, by: str, now: datetime | None = None) -> None:
    if enabled and not RE_AT.fullmatch(at):
        raise ValueError("时间格式应为 HH:MM")
    now = now or local_now()
    # 今天已经过点的从明天开始，保存时不触发
    done = now.date().isoformat() if at and _late(at, now) >= 0 else ""
    get_store().save_schedule(rig_id, at, enabled, done, by)


async def fire_schedules(now: datetime | None = None) -> str:
    """到点的台架合成一轮回归。离线或没配用例集的记在批次里，汇总时能看到，不重试。返回批次号。"""
    now, store = now or local_now(), get_store()
    today = now.date().isoformat()
    due = [s["rig_id"] for s in store.schedules() if s["last_date"] != today and _due(s["at"], now)]
    if not due:
        return ""
    rid = store.create_regression("定时", "")
    skipped = []
    for rig_id in due:
        store.mark_schedule(rig_id, today)  # 先记下，派发中途重启也不会重复开跑
        try:
            r = await rigplan.dispatch(rig_id, "", f"S-{rid}", regression_id=rid)
        except (KeyError, ValueError) as e:
            skipped.append({"rig": rig_id, "reason": str(e)})
            continue
        skipped += [{"rig": rig_id, "case_id": x["case_id"], "reason": x["reason"]} for x in r["skipped"]]
    store.set_regression_skipped(rid, skipped)
    log.info("定时回归 %s：台架 %s，未执行 %d 项", rid, due, len(skipped))
    return rid


def digest_fields(since: float) -> tuple[str, list[tuple[str, str]]]:
    store = get_store()
    batches = store.regressions(limit=1000, since=since)
    pending = get_service().pending_reviews()
    by_ver: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in store.batch_verdicts(since):
        by_ver[r["model"], r["ota"]][r["verdict"]] += r["n"]
    fields = [(f"{model} · {ota or '版本未知'}", " · ".join(f"{name} {c[k]}" for k, name in VERDICTS))
              for (model, ota), c in sorted(by_ver.items())]
    active = sum(b["active"] for b in batches)
    title = f"回归汇总 · {len(batches)} 轮 · 待审核 {len(pending)}"
    if not batches:
        fields.append(("回归", "这段时间没有回归"))
    if active:
        fields.append(("进行中", f"{active} 条还没跑完"))
    skipped = [x for b in batches for x in b["skipped"]]
    if skipped:
        fields.append(("未执行", "；".join(f"{x['rig']} {x.get('case_id', '')}：{x['reason']}" for x in skipped[:5]) +
                       (f" 等 {len(skipped)} 项" if len(skipped) > 5 else "")))
    mods = Counter(d.module or "未分模块" for d in pending)
    if mods:
        fields.append(("待审核", "、".join(f"{m} {n}" for m, n in mods.most_common())))
    return title, fields


async def send_digest(now: datetime | None = None) -> bool:
    """每天推一次，范围从上次推送算起，推送失败下一轮再试。"""
    now, store = now or local_now(), get_store()
    today = now.date().isoformat()
    last = store.get_kv("digest")
    if last.get("date") == today or not _due(get_settings().digest_at, now):
        return False
    title, fields = digest_fields(last.get("at") or time.time() - 86400)
    r = await feishu.notify(title, fields, f"{get_settings().public_base_url}/board#review", "red" if "待审核" in dict(fields) else "green")
    if not r["ok"]:
        log.warning("飞书汇总发送失败：%s", r.get("note"))
        return False
    store.set_kv("digest", {"date": today, "at": time.time()})
    return True


async def loop() -> None:
    while True:
        try:
            await fire_schedules()
            await send_digest()
            from .casewf import dispatch_pending_trials  # 台架上线后补派待试跑的用例
            await dispatch_pending_trials()
        except Exception:  # 这一轮出错不能让定时停掉
            log.exception("定时任务出错")
        await asyncio.sleep(TICK_S)
