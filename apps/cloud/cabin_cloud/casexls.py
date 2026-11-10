"""cabin-casexls：从 .xlsx 用例表导入用例。

表头沿用多维表格列名（integ/bitable.COLS），首行为表头。导入后照常自检、人工审核，审核通过才入库，
所以 xlsx 和 PRD 生成走同一条闭环，之后同样拉取、生成 YAML、由 Runner 执行。
"""
from __future__ import annotations

import io
import re
from typing import Any

from openpyxl import load_workbook

from .casegen import module_code, self_check
from .cases import get_cases
from .caselib import default_models, get_caselib
from .config import BACKEND_DIR
from .integ.bitable import BY_NAME, validate_row
from cabin_common.schema import CaseBatch, Requirement, TestCase

XLSX_EXT = (".xlsx",)
RE_XLS_SAMPLE = re.compile(r"(语音)?用例(模拟)?表|模拟表")
SAMPLE = BACKEND_DIR / "data" / "samples" / "语音用例模拟表.xlsx"
ALIASES = {"用例ID": "用例编号", "编号": "用例编号", "用例标题": "标题", "用例名称": "标题", "前置": "前置条件", "步骤": "测试步骤",
           "预期": "预期结果", "语句": "测试语句", "屏幕期望": "期望屏幕", "播报期望": "期望播报", "时延上限": "时延上限ms",
           "车型": "适用车型", "需求": "需求点"}
VOICE_STEPS = ["唤醒车机", "播放测试语句", "收听播报并截图"]
PLACEHOLDER = "TC-XLS-000"


def sample_xlsx():
    return SAMPLE


def read_rows(data: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
    except Exception as e:
        raise ValueError(f"读不了这份 .xlsx：{type(e).__name__}") from e
    rows = ws.iter_rows(values_only=True)
    head = next(rows, None)
    if not head:
        raise ValueError("表格是空的")
    names = [ALIASES.get(str(h).strip(), str(h).strip()) if h is not None else "" for h in head]
    if "标题" not in names:
        raise ValueError(f"第一行要有表头，至少包含“标题”。读到的表头：{[n for n in names if n][:8]}")
    out = []
    for i, r in enumerate(rows, 2):
        row = {n: v for n, v in zip(names, r) if n and n in BY_NAME and v not in (None, "")}
        if row:
            out.append((i, row))
    return out


def to_case(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    f = dict(fields)
    has_id = bool(f.get("用例编号"))
    f.setdefault("用例编号", PLACEHOLDER)
    if isinstance(f.get("通道"), str):
        f["通道"] = re.split(r"[,，、\s+]+", f["通道"].strip())
    elif "通道" not in f:
        f["通道"] = ["audio", "adb"] if f.get("测试语句") else ["adb"]
    if isinstance(f.get("适用车型"), str):
        f["适用车型"] = re.split(r"[,，、\s]+", f["适用车型"].strip())
    vals, errs = validate_row(f)
    if not has_id:
        vals.pop("case_id", None)
    return {k: v for k, v in vals.items() if v not in (None, "", [])}, errs


def import_workbook(data: bytes, name: str, source: str, session: str = "", created_by: str = "") -> tuple[CaseBatch, list[TestCase], list[str], list[dict]]:
    """返回 (批次, 用例, 用于展示的行文本, 不合格行)。"""
    lib = get_caselib()
    raw = read_rows(data)
    good, bad, shown = [], [], []
    for row_no, fields in raw:
        shown.append(f"第 {row_no} 行：{fields.get('标题', '')} | {fields.get('测试语句', '')} | {fields.get('期望屏幕', '')} | {fields.get('期望播报', '')}")
        vals, errs = to_case(fields)
        if errs:
            bad.append({"row": row_no, "errors": errs})
        else:
            good.append((fields, vals))
    batch = CaseBatch(batch_id=lib.new_batch_id(), session=session, created_by=created_by, doc_name=name, doc_source=source,
                      status="待审核", params={"imported_from": "xlsx", "rows": len(raw), "invalid": len(bad)})
    first_module = next((v.get("module") for _, v in good if v.get("module")), "") or "语音助手"
    batch.module = first_module
    batch.models = next((v.get("models") for _, v in good if v.get("models")), None) or default_models()
    groups: dict[str, str] = {}
    cases: list[TestCase] = []
    taken = set(get_cases()._local)
    for fields, vals in good:
        key = str(fields.get("需求点") or vals.get("module") or first_module)
        rid = groups.setdefault(key, f"REQ-{len(groups) + 1:02d}")
        if rid not in {r.req_id for r in batch.requirements}:
            batch.requirements.append(Requirement(req_id=rid, section=str(fields.get("PRD章节", "")), quote=key, summary=key[:30]))
        cid = vals.pop("case_id", "")
        if not cid or lib.get_case(cid) or cid in taken:  # 编号缺失或已被占用：重新分配，不覆盖已有用例
            cid = lib.next_id(module_code(vals.get("module") or first_module), taken)
        vals.update(case_id=cid, req_id=rid, source="迁移", batch_id=batch.batch_id,
                    module=vals.get("module") or first_module, models=vals.get("models") or batch.models)
        vals.pop("review_status", None)  # 审核状态不从表里继承：入库必须过人工门
        vals.pop("auto_status", None)
        c = TestCase(**vals)
        if c.is_voice:
            c.latency_max_ms = c.latency_max_ms or 1500
            c.voice_profile = c.voice_profile or "female_a"
            c.steps = c.steps or list(VOICE_STEPS)
            c.expected = c.expected or f"{c.expect_screen}；播报“{c.expect_tts}”"
        cases.append(c)
    self_check(cases, lib.library())
    for c in cases:
        lib.save_case(c)
    batch.case_ids = [c.case_id for c in cases]
    if not cases:
        batch.status = "失败"
    lib.save_batch(batch)
    return batch, cases, shown, bad
