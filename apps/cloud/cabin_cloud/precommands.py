"""前置条件 → 设置命令（preconditions.yaml）。无人值守时没人手动摆环境，只能靠这张表。

装载测试代码时按台架车型展开。匹配不到的小句保留占位，执行时记为前置条件未满足，结论降为低置信。
"""
from __future__ import annotations

import re

import yaml

from .config import get_settings

RE_CLAUSE = re.compile(r"[，,；;。]")
PREFIX = "precondition:"
_cache: dict = {"key": None, "rules": []}


def _rules() -> list[dict]:
    p = get_settings().preconditions_file
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return []
    if _cache["key"] != key:
        raw = yaml.safe_load(p.read_text("utf8")) or {}
        _cache.update(key=key, rules=raw.get("preconditions") or [])
    return _cache["rules"]


def _commands(clause: str, model: str) -> list[str] | None:
    """这一小句要执行的命令；None 表示表里没有。"""
    for r in _rules():
        if r.get("models") and model not in r["models"]:
            continue
        if any(m in clause for m in r.get("match") or []):
            cmd = r.get("cmd") or []
            return [cmd] if isinstance(cmd, str) else list(cmd)
    return None


def expand(plan: dict, model: str) -> dict:
    """把占位的前置条件换成命令，能翻译多少翻译多少。"""
    out: list[dict] = []
    for s in plan.get("setup") or []:
        ref = s.get("cmd_ref") or ""
        if s.get("action") != "shell" or s.get("cmd") or not ref.startswith(PREFIX):
            out.append(s)
            continue
        for clause in filter(None, (c.strip() for c in RE_CLAUSE.split(s.get("note") or ""))):
            cmds = _commands(clause, model)
            if cmds is None:
                out.append({"action": "shell", "cmd_ref": ref, "note": clause})
            else:
                out += [{"action": "shell", "cmd": c, "note": clause} for c in cmds]
    return {**plan, "setup": out}
