"""台架用例集与派发：每台台架有一组固定用例，定时或手动触发，也可以临时换用例。

能否在某台台架执行统一看干跑结果（车型适用、能力覆盖、测试代码校验），规则只有一份。
"""
from __future__ import annotations

import uuid

from cabin_common.schema import CaseRef, ExploreTask, Limits, Origin, PlanStep, Rig, Target, TestCase

from .caselib import get_caselib
from .execsvc.service import get_service
from .planloader import dry_run
from .rigs import rig_health, rig_profiles
from .store import get_store

PER_CASE_S = 300  # 排在第 n 条的用例，最多等 n 倍这么久


def _fit(c: TestCase, rig: Rig) -> dict:
    return dry_run(c.case_id, set(rig.capabilities), rig.model)


def _rig(rig_id: str) -> Rig:
    rig = rig_profiles().get(rig_id)
    if not rig:
        raise KeyError(f"台架 {rig_id} 不存在")
    return rig


def _row(c: TestCase) -> dict:
    return {"case_id": c.case_id, "title": c.title, "module": c.module, "type": c.type, "priority": c.priority,
            "channel": c.channel, "utterance": c.utterance, "batch_id": c.batch_id, "plan_version": c.plan_version,
            "auto_status": c.auto_status}


def runnable(rig_id: str) -> list[dict]:
    """这台台架能跑的用例：审核通过、测试代码就绪、车型适用、能力覆盖。"""
    rig = _rig(rig_id)
    cases = [c for c in get_caselib().pull(rig.model) if c.plan_status == "ready" and _fit(c, rig)["ok"]]
    return sorted((_row(c) for c in cases), key=lambda x: x["case_id"], reverse=True)


def caseset(rig_id: str) -> list[str]:
    return get_store().rig_caseset(rig_id)


def save_caseset(rig_id: str, case_ids: list[str], by: str) -> None:
    """只有已验证、这台台架能跑的用例才能进用例集，否则无人值守时会跑出一批假 Bug。"""
    rig, lib = _rig(rig_id), get_caselib()
    bad = []
    for cid in case_ids:
        c = lib.get_case(cid)
        if not c:
            bad.append(f"{cid} 不存在")
        elif c.auto_status != "已验证":
            bad.append(f"{cid} 还没有验证（{c.auto_status}）")
        elif not (r := _fit(c, rig))["ok"]:
            bad.append(f"{cid}：{'；'.join(r['errors'])}")
    if bad:
        raise ValueError("；".join(bad))
    get_store().save_rig_caseset(rig_id, list(dict.fromkeys(case_ids)), by)


async def _submit(c: TestCase, rig_id: str, user: str, session: str, kind: str = "replay", regression_id: str = "",
                  retest_of: str = "", ahead: int = 0) -> None:
    """ahead：同一次派发里排在前面的用例，它们的执行单元这时可能还没入库。"""
    position = get_store().queued_runs(rig_id) + ahead + 1
    plan = ExploreTask(id=f"T-{uuid.uuid4().hex[:8]}", origin=Origin(utterance="台架用例集", session=session, user=user),
                       case_ref=CaseRef(source="用例库", case_id=c.case_id, expected=c.expected),
                       target=Target(rig=rig_id, package=c.package),
                       steps=[PlanStep(type="taskplan", case_id=c.case_id, goal=c.title, kind=kind, regression_id=regression_id, retest_of=retest_of)],
                       limits=Limits(max_duration_s=PER_CASE_S * position), summary=[c.title])
    await get_service().submit(plan, session)


async def dispatch(rig_id: str, user: str, session: str, case_ids: list[str] | None = None, regression_id: str = "") -> dict:
    """开一轮回归，把一组用例派给台架。case_ids 给定时是临时更换，只影响这一次，不改固定用例集。"""
    rig, lib = _rig(rig_id), get_caselib()
    if not rig_health(rig_id).online:
        raise ValueError(f"台架 {rig_id} 离线")
    ids = caseset(rig_id) if case_ids is None else case_ids
    if not ids:
        raise ValueError(f"台架 {rig_id} 还没有配置用例集")
    regression_id = regression_id or get_store().create_regression("手动", user)
    queued, skipped = [], []
    for cid in ids:
        c = lib.get_case(cid)
        r = _fit(c, rig) if c else {"ok": False, "errors": ["用例不存在"]}
        if c and c.auto_status != "已验证":  # 用例集保存后测试代码可能重新生成过，要重新试跑
            r = {"ok": False, "errors": [f"还没有验证（{c.auto_status}）"]}
        if not r["ok"]:  # 用例集保存后，车型或能力也可能变了
            skipped.append({"case_id": cid, "reason": "；".join(r["errors"])})
            continue
        await _submit(c, rig_id, user, session, regression_id=regression_id, ahead=len(queued))
        queued.append(cid)
    return {"queued": queued, "skipped": skipped, "regression_id": regression_id}


async def retest(case_id: str, rig_id: str, ota: str, user: str, session: str, parent: str, regression_id: str) -> str:
    """换一台同车型、同版本的台架复测；只有一台时在原台架再跑一次。返回台架编号。"""
    c, src = get_caselib().get_case(case_id), _rig(rig_id)
    fit = [r.id for r in rig_profiles().values() if c and r.model == src.model and rig_health(r.id).online
           and rig_health(r.id).ota == ota and _fit(c, r)["ok"]]
    if not fit:
        raise ValueError(f"{case_id} 没有能复测的台架")
    target = min(fit, key=lambda r: (r == rig_id, get_store().queued_runs(r)))
    await _submit(c, target, user, session, kind="retest", regression_id=regression_id, retest_of=parent)
    return target


def _pick_rig(c: TestCase) -> str:
    """选一台在线、能跑的台架，优先空闲的。"""
    store = get_store()
    fit = [r.id for r in rig_profiles().values() if rig_health(r.id).online and _fit(c, r)["ok"]]
    if not fit:
        raise ValueError(f"{c.case_id} 没有在线且能跑的台架")
    return min(fit, key=store.queued_runs)


async def start_trial(case_id: str, user: str, session: str) -> str:
    """首次试跑：生成的测试代码先在真实台架跑一次，通过才算已验证。返回台架编号。"""
    lib = get_caselib()
    c = lib.get_case(case_id)
    if not c or c.plan_status != "ready":
        raise ValueError(f"{case_id} 没有可试跑的测试代码")
    if c.auto_status == "试跑中":
        raise ValueError(f"{case_id} 正在试跑")
    rig_id = _pick_rig(c)
    c.auto_status = "试跑中"
    lib.save_case(c)
    await _submit(c, rig_id, user, session, kind="trial")
    return rig_id


def gaps() -> list[dict]:
    """缺台架能力清单：用例库里没有任何台架能跑的用例，待人工安排。"""
    rigs = list(rig_profiles().values())
    out = []
    for c in get_caselib().library():
        if c.plan_status != "ready":
            continue
        reasons = {}
        for r in rigs:
            fit = _fit(c, r)
            if fit["ok"]:
                reasons = None
                break
            reasons[r.id] = "；".join(fit["errors"])
        if reasons is not None:
            out.append({"case_id": c.case_id, "title": c.title, "module": c.module, "models": c.models, "reasons": reasons})
    return out
