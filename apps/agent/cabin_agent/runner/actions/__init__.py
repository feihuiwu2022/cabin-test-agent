"""动作注册表：动作名 → 处理函数。动作名与参数以 planner.ACTIONS（台架端 PRD 表 15）为准。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ..context import RunContext

Handler = Callable[[RunContext, dict], Awaitable[dict | None]]


@dataclass(frozen=True)
class ActionDef:
    name: str
    channel: str | None  # None 表示不依赖具体通道
    handler: Handler


REGISTRY: dict[str, ActionDef] = {}


def action(name: str, channel: str | None = None) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = ActionDef(name, channel, fn)
        return fn
    return deco


def registered() -> dict[str, ActionDef]:
    from . import adb, audio, host  # noqa: F401  导入即注册
    return REGISTRY

