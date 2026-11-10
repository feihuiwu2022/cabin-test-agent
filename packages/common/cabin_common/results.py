"""一次回放的结果模型与结论合成（PF-JDG-02）。台架产出，云端据此查重、送审。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["dry", "replay", "retest"]
Verdict = Literal["passed", "failed", "blocked"]


class StepResult(BaseModel):
    id: int
    action: str
    label: str = ""
    result: Literal["ok", "fail"] = "ok"
    error: str = ""
    dur_ms: int = 0
    attempts: int = 1
    screen: str = ""
    shot: str = ""
    bbox: list[int] | None = None
    x: int | None = None
    y: int | None = None
    locator: str = ""  # 命中的定位层：control / ocr / image / ratio / xy


class AssertResult(BaseModel):
    assertion_id: str
    type: str
    result: Literal["pass", "fail", "error", "skipped"]
    confidence: float = 1.0
    detail: dict[str, Any] = Field(default_factory=dict)
    judge_ref: str = ""


class PlanResult(BaseModel):
    run_id: str
    case_id: str
    plan_version: str = ""
    kind: Kind = "replay"
    verdict: Verdict = "blocked"
    block_reason: str = ""  # capability / plan_invalid / locator / wait_timeout / timeout / judge_error …
    block_detail: str = ""
    steps: list[StepResult] = Field(default_factory=list)
    asserts: list[AssertResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ota: str = ""
    duration_s: float = 0
    evidence_dir: str = ""
    manifest: dict[str, Any] = Field(default_factory=dict)


def synthesize(res: PlanResult) -> None:
    """结论合成：任一断言失败则 failed（已有明确缺陷证据，优先于动作失败）；
    动作失败、超时、判定不可用为 blocked；否则 passed。"""
    if any(a.result == "fail" for a in res.asserts):
        res.verdict = "failed"
    elif res.block_reason:
        res.verdict = "blocked"
    elif err := next((a for a in res.asserts if a.result == "error"), None):
        res.verdict, res.block_reason = "blocked", "judge_error"
        res.block_detail = str(err.detail.get("error", ""))
    else:
        res.verdict = "passed"
