"""评估器注册表：断言 type → 评估函数。评估器只读证据，不碰设备。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..context import AssertResult
from ..evidence import Evidence
from cabin_common.judging import JudgeClient


@dataclass
class EvalContext:
    case_id: str
    judge: JudgeClient | None = None
    packages: list[str] = field(default_factory=list)
    session: str = ""


Evaluator = Callable[[dict, Evidence, EvalContext], Awaitable[AssertResult]]
REGISTRY: dict[str, Evaluator] = {}


def evaluator(type_: str) -> Callable[[Evaluator], Evaluator]:
    def deco(fn: Evaluator) -> Evaluator:
        REGISTRY[type_] = fn
        return fn
    return deco


def registered() -> dict[str, Evaluator]:
    from . import rule, state, text, timing, value, vision, voice  # noqa: F401  导入即注册
    return REGISTRY


def latest_ui(ev: Evidence, step: int | None) -> dict:
    f = ev.find("ui", step)
    return ev.read_json(f) if f else {"screen": "", "elements": []}
