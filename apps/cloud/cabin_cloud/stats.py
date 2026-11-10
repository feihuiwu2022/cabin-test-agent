"""结果统计：按 OTA 版本和车型看覆盖率与通过率。同一用例重复执行时取最新一次。"""
from __future__ import annotations

from cabin_common import vehicles

from .caselib import applies, get_caselib
from .store import get_store


def overview() -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    should: dict[str, int] = {}
    for r in get_store().latest_results():
        k = (r["ota"], r["model"])
        if r["model"] not in should:
            try:
                should[r["model"]] = len(get_caselib().pull(r["model"]))
            except ValueError:
                should[r["model"]] = 0
        row = rows.setdefault(k, {"ota": r["ota"], "model": r["model"], "should": should[r["model"]], "executed": 0,
                                  "passed": 0, "failed": 0, "blocked": 0, "flaky": 0, "bugs": 0, "bugs_open": 0})
        row["executed"] += 1
        row[r["verdict"]] += 1
    for d in get_store().list_defects(status="confirmed") + get_store().list_defects(status="linked"):
        row = rows.get((d.get("ota", ""), d.get("model", "")))
        if row:
            row["bugs"] += 1
            row["bugs_open"] += d.get("jira_status") != "Closed"
    for row in rows.values():
        decided = row["passed"] + row["failed"]
        row["pass_rate"] = round(row["passed"] / decided, 3) if decided else None  # 阻塞和不稳定单独统计，不算失败
        row["coverage"] = round(min(row["executed"] / row["should"], 1), 3) if row["should"] else None
    return sorted(rows.values(), key=lambda x: (x["ota"], x["model"]), reverse=True)


def matrix() -> dict:
    """覆盖矩阵：用例 × 车型。cells 里的值：结论、空串（适用但没跑过）、na（不适用）。"""
    codes = vehicles.codes()
    seen = {(r["case_id"], r["model"]): r for r in get_store().latest_by_case()}
    rows = []
    for c in get_caselib().library():
        cells = {m: seen.get((c.case_id, m), {}).get("verdict", "") if applies(c, m) else "na" for m in codes}
        rows.append({"case_id": c.case_id, "title": c.title, "module": c.module, "cells": cells})
    return {"models": [{"code": m, "name": vehicles.name(m)} for m in codes], "rows": rows}


def kpi() -> list[dict]:
    """人员 KPI：只算审核确认的 Bug，同时展示驳回率。定时任务没有发现人，不计入。"""
    rows: dict[str, dict] = {}
    for status in ("confirmed", "linked", "rejected"):
        for d in get_store().list_defects(status=status):
            if d.get("finder"):
                row = rows.setdefault(d["finder"], {"finder": d["finder"], "confirmed": 0, "linked": 0, "rejected": 0})
                row[status] += 1
    for row in rows.values():
        row["reject_rate"] = round(row["rejected"] / (row["confirmed"] + row["linked"] + row["rejected"]), 3)
    return sorted(rows.values(), key=lambda x: -x["confirmed"])
