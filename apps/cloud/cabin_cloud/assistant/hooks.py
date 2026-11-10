"""Hooks：pre_tool / post_tool / pre_reply。confirm_gate、review_gate 在网关与执行服务里强制执行。"""
from __future__ import annotations

import json

from pydantic import ValidationError

from ..store import get_store
from .tools import TOOLS, Tool, ToolCtx

SENSITIVE_KEYS = ("password", "token", "secret", "passcode")


class HookDenied(Exception):
    pass


def pre_tool(ctx: ToolCtx, name: str, raw_args: dict) -> tuple[Tool, object]:
    tool = TOOLS.get(name)
    if tool is None:
        raise HookDenied(f"未知工具 {name}")
    if not ctx.authed:
        raise HookDenied("未登录，测试工具不可用")
    try:
        args = tool.args.model_validate(raw_args)
    except ValidationError as e:
        raise HookDenied(f"参数校验失败：{e.errors()[0]['msg']}")
    return tool, args


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if any(s in k.lower() for s in SENSITIVE_KEYS) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def post_tool(ctx: ToolCtx, name: str, args: dict, result: dict, latency_s: float, error: str = "") -> str:
    """写事件日志；返回回填给模型的（脱敏）文本。"""
    clean = redact(result)
    get_store().append(ctx.session, "tool.result", {
        "tool": name, "args": redact(args), "latency_s": latency_s, "error": error,
        "summary": json.dumps(clean, ensure_ascii=False, default=str)[:600]})
    return json.dumps(clean, ensure_ascii=False, default=str)

