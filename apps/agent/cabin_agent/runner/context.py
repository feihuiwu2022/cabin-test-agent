"""运行上下文、结果模型与结论合成（PF-JDG-02）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cabin_common.results import AssertResult, Kind, PlanResult, StepResult, Verdict, synthesize  # noqa: F401

from .evidence import Evidence

class ActionError(RuntimeError):
    """动作失败：停止后续步骤，结论 blocked。"""

    def __init__(self, reason: str, msg: str):
        super().__init__(msg)
        self.reason = reason


class LocatorError(ActionError):
    """定位失败：引擎重试 1 次（PF-RUN-04），仍失败按动作失败处理。"""

    def __init__(self, msg: str):
        super().__init__("locator", msg)


class WaitTimeout(ActionError):
    """等待条件超时：TaskPlan 可用 on_timeout 配置为 failed，默认 blocked。"""

    def __init__(self, msg: str, on_timeout: str = "blocked"):
        super().__init__("wait_timeout", msg)
        self.on_timeout = on_timeout




@dataclass
class RunContext:
    plan: dict
    evidence: Evidence
    channels: dict[str, Any]  # 通道名 → 通道实例
    packages: list[str] = field(default_factory=list)  # 被测包；空表示不限
    session: str = ""
    warnings: list[str] = field(default_factory=list)
    assets: dict[str, bytes] = field(default_factory=dict)  # 已校验哈希的测试音频
    images: dict[str, bytes] = field(default_factory=dict)  # 已校验哈希的图标模板（target.image 引用）

    @property
    def adb(self) -> Any:
        return self.channels["adb"]

    @property
    def audio(self) -> Any:
        return self.channels["audio"]
