"""车型字典：车型代码、显示名、别名，以及车型覆盖的文本格式。

一期内置 5 个默认车型，用例默认全部打点（●）。代码是用例、打点、参数、TaskPlan models 的唯一标识。
"""
from __future__ import annotations

import re
from typing import Any

# (代码, 显示名, 别名)
VEHICLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("A01-CN", "A01国内", ("A01国内", "A01-CN", "A01CN")),
    ("A01-INTL", "A01国际", ("A01国际", "A01-INTL", "A01INTL")),
    ("B01-EU", "B01欧版", ("B01欧版", "B01-EU", "B01EU", "B01")),
    ("D01", "D01", ("D01",)),
    ("E01", "E01", ("E01",)),
)
_ALIAS = {re.sub(r"[\s·_]", "", a).upper(): code for code, _, aliases in VEHICLES for a in aliases}
_NAMES = {code: name for code, name, _ in VEHICLES}
# 长别名优先，避免 “B01” 抢先匹配 “B01欧版”
_RE = re.compile("|".join(sorted((re.escape(a) for _, _, al in VEHICLES for a in al), key=len, reverse=True)),
                 re.I)


def codes() -> list[str]:
    return [c for c, _, _ in VEHICLES]


def name(code: str) -> str:
    return _NAMES.get(code, code)


def normalize(text: str) -> str:
    """车型显示名 / 别名 / 代码 → 车型代码；认不出返回空串。"""
    return _ALIAS.get(re.sub(r"[\s·_]", "", text or "").upper(), "")


def find_all(text: str) -> list[str]:
    """从一句话里找出提到的车型代码（按出现顺序，去重）。"""
    out: list[str] = []
    for m in _RE.finditer(re.sub(r"(?<=[A-Za-z0-9])\s+(?=[一-龥])", "", text or "")):
        c = normalize(m.group(0))
        if c and c not in out:
            out.append(c)
    return out


# ---------- 车型覆盖 ----------
OVERRIDE_FIELDS = {"测试语句": "utterance", "期望屏幕": "expect_screen", "期望播报": "expect_tts",
                   "前置条件": "precondition", "预期结果": "expected"}
_FIELD_CN = {v: k for k, v in OVERRIDE_FIELDS.items()}


def overrides_to_text(ov: dict[str, dict[str, Any]]) -> str:
    """{车型: {skip: True} | {字段: 值}} → “A01-INTL: 跳过；B01-EU: 测试语句=…”。"""
    parts = []
    for code, kv in (ov or {}).items():
        if kv.get("skip"):
            parts.append(f"{code}: 跳过")
            continue
        for k, v in kv.items():
            parts.append(f"{code}: {_FIELD_CN.get(k, k)}={v}")
    return "；".join(parts)


def parse_overrides(text: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """反解析车型覆盖文本，返回 (覆盖, 问题列表)。"""
    out: dict[str, dict[str, Any]] = {}
    errs: list[str] = []
    for part in re.split(r"[；;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"([^:：]+)[:：]\s*(.+)$", part)
        code = normalize(m.group(1)) if m else ""
        if not m or not code:
            errs.append(f"车型覆盖“{part[:20]}”无法识别车型")
            continue
        body = m.group(2).strip()
        if re.fullmatch(r"跳过|不适用|skip", body, re.I):
            out.setdefault(code, {})["skip"] = True
            continue
        kv = re.match(r"([^=＝]+)[=＝](.*)$", body)
        attr = OVERRIDE_FIELDS.get(kv.group(1).strip(), "") if kv else ""
        if not attr:
            errs.append(f"车型覆盖“{part[:20]}”字段只能是 {'/'.join(OVERRIDE_FIELDS)} 或“跳过”")
            continue
        out.setdefault(code, {})[attr] = kv.group(2).strip()
    return out, errs


def skipped(ov: dict[str, dict[str, Any]], code: str) -> bool:
    return bool((ov or {}).get(code, {}).get("skip"))
