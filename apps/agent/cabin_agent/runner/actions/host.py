"""通用动作：停讲 / 恢复讲解、等待。"""
from __future__ import annotations

import asyncio
import time

from ..context import RunContext, WaitTimeout
from . import action

DEFAULT_WAIT_S = 10  # PF-RUN-03
POLL_S = 0.3


@action("mute_host")
async def mute_host(ctx: RunContext, step: dict) -> dict:
    ctx.evidence.fact("host_muted", True)
    return {"label": "停讲"}


@action("unmute_host")
async def unmute_host(ctx: RunContext, step: dict) -> dict:
    ctx.evidence.fact("host_muted", False)
    return {"label": "恢复讲解"}


@action("wait")
async def wait(ctx: RunContext, step: dict) -> dict:
    if step.get("seconds"):
        await asyncio.sleep(float(step["seconds"]))
        return {"label": f"等待 {step['seconds']} 秒"}
    until = step.get("until") or {}
    if not any(until.get(k) not in (None, "") for k in ("text", "gone", "key", "desc", "selector", "screen", "checked")):
        return {"label": "等待（无条件）"}
    what = _until_cn(until)
    timeout = float(step.get("timeout_s") or DEFAULT_WAIT_S)
    end = time.time() + timeout
    while True:
        if await ctx.adb.matches(until):
            return {"label": f"等待{what}"}
        if time.time() > end:
            raise WaitTimeout(f"{timeout:g} 秒内未{what}", step.get("on_timeout", "blocked"))
        await asyncio.sleep(POLL_S)


def _until_cn(u: dict) -> str:
    if u.get("gone"):
        return f"“{u['gone']}”消失"
    if u.get("screen"):
        return f"进入 {u['screen']}"
    sel = u.get("selector") or u
    name = sel.get("text") or sel.get("key") or sel.get("desc") or ""
    if "checked" in u or "checked" in sel:
        return f"“{name}”变为{'开' if u.get('checked', sel.get('checked')) else '关'}"
    return f"出现“{name}”"
