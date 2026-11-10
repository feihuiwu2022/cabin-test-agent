"""Runner 主流程：load → setup → steps（到 after_step 触发断言）→ 收尾断言 → teardown → 结论合成。

引擎只认识“步骤”和“断言”，具体动作与判定都经注册表分发。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from .. import assets as audioasset
from ..config import get_settings
from cabin_common.schema import TestCase
from .actions import registered as actions
from .context import ActionError, AssertResult, LocatorError, PlanResult, RunContext, StepResult, WaitTimeout, synthesize
from .evaluators import EvalContext, registered as evaluators
from .evidence import Evidence
from cabin_common.judging import JudgeClient
from .precheck import check

RETRY_INTERVAL_S = 1.0  # PF-RUN-04
StepCb = Callable[[StepResult], Awaitable[None]]
AssertCb = Callable[[str, dict, AssertResult | None, Evidence], Awaitable[None]]


class PlanRunner:
    def __init__(self, plan: dict, *, channels: list[Any], run_id: str, case: TestCase | None = None,
                 judge: JudgeClient | None = None, plan_version: str = "", rig: str = "", packages: list[str] | None = None,
                 session: str = "", trace: bool = True, on_step: StepCb | None = None,
                 on_assert: AssertCb | None = None):
        self.plan, self.case, self.judge = plan, case, judge
        self.channels = {c.name: c for c in channels}
        self.trace, self.on_step, self.on_assert = trace, on_step, on_assert
        cid = plan.get("case_id", "")
        s = get_settings()
        self.ev = Evidence(s.evidence_dir / run_id, f"/evidence/{run_id}",
                           {"evidence_id": f"E{run_id}-1", "run_id": run_id, "attempt": 1, "rig_id": rig,
                            "case_id": cid, "plan_version": plan_version})
        self.ctx = RunContext(plan=plan, evidence=self.ev, channels=self.channels, packages=packages or [],
                              session=session)
        self.ectx = EvalContext(case_id=cid, judge=judge, packages=packages or [], session=session)
        self.res = PlanResult(run_id=run_id, case_id=cid, plan_version=plan_version, evidence_dir=str(self.ev.root))
        self.done_asserts: set[str] = set()

    @property
    def capabilities(self) -> set[str]:
        return {c for ch in self.channels.values() for c in ch.capabilities}

    async def execute(self) -> PlanResult:
        t0 = time.time()
        res = self.res
        chk = check(self.plan, self.capabilities, self.case)
        res.warnings += chk["warnings"]
        if chk["reason"]:
            res.block_reason, res.block_detail = chk["reason"], "；".join(chk["errors"])
            return self._close(t0)
        try:
            for name, a in (self.plan.get("assets") or {}).items():
                self.ctx.assets[name] = audioasset.read_verified(a["sha256"])  # 台架播放前校验（PF：plan_invalid）
            for name, a in (self.plan.get("images") or {}).items():
                self.ctx.images[name] = audioasset.read_image((a or {}).get("sha256") or "")
            for ch in self.channels.values():
                await ch.start()
            await asyncio.wait_for(self._body(), timeout=float(self.plan.get("timeout_s") or 300))
        except asyncio.TimeoutError:
            res.block_reason, res.block_detail = "timeout", "执行单元超时"
        except ActionError as e:  # setup 失败
            res.block_reason, res.block_detail = e.reason, str(e)
        except (audioasset.AssetError, audioasset.ImageAssetError) as e:
            res.block_reason, res.block_detail = "plan_invalid", str(e)
        except asyncio.CancelledError:
            res.block_reason, res.block_detail = "cancelled", "任务被撤销"
            raise
        except Exception as e:  # 通道启动失败等环境问题
            res.block_reason, res.block_detail = "device", f"{type(e).__name__}: {e}"
        finally:
            await self._final_asserts()
            await self._collect()  # 收尾动作会破坏现场，证据要在这之前取
            await self._teardown()
            self._close(t0)
        return res

    def _close(self, t0: float) -> PlanResult:
        res = self.res
        res.warnings += self.ctx.warnings
        synthesize(res)
        res.duration_s = round(time.time() - t0, 2)
        res.manifest = self.ev.close()
        (self.ev.root / "result.json").write_text(json.dumps(res.model_dump(), ensure_ascii=False, indent=1), "utf8")
        return res

    async def _body(self) -> None:
        for s in self.plan.get("setup") or []:
            await self._run_action({**s, "id": 0})
        stop = False
        for s in self.plan.get("steps") or []:
            try:
                sr = await self._step(s)
            except WaitTimeout as e:
                if e.on_timeout != "failed":
                    self._block(e)
                    return
                self.res.asserts.append(AssertResult(assertion_id=f"step{s.get('id')}", type="wait", result="fail",
                                                     detail={"error": str(e)}))
                stop = True
                sr = None
            except ActionError as e:
                self._block(e)
                return
            if sr:
                for a in self.plan.get("assertions") or []:
                    if a.get("after_step") == s.get("id"):
                        r = await self._assert(a)
                        stop = stop or (r.result == "fail" and self.plan.get("on_fail") == "stop")
            if stop:
                return

    def _block(self, e: ActionError) -> None:
        self.res.block_reason, self.res.block_detail = e.reason, str(e)

    async def _run_action(self, s: dict) -> dict:
        return await actions()[s["action"]].handler(self.ctx, s) or {}

    async def _step(self, s: dict) -> StepResult:
        sr = StepResult(id=int(s.get("id") or 0), action=s.get("action", ""))
        t0 = time.time()
        try:
            for attempt in (1, 2):
                sr.attempts = attempt
                try:
                    out = await self._run_action(s)
                    break
                except LocatorError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(RETRY_INTERVAL_S)
            sr.label, sr.shot, sr.bbox = out.get("label", sr.action), out.get("shot", ""), out.get("bbox")
            sr.x, sr.y, sr.locator = out.get("x"), out.get("y"), out.get("locator", "")
        except ActionError as e:
            sr.result, sr.error = "fail", str(e)
            sr.label = sr.label or f"{sr.action}（失败）"
            await self._on_failure(sr)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 设备断开等：归为动作失败
            sr.result, sr.error, sr.label = "fail", f"{type(e).__name__}: {e}", f"{sr.action}（失败）"
            await self._on_failure(sr)
            raise ActionError("device", sr.error) from e
        else:
            sr.dur_ms = int((time.time() - t0) * 1000)
            await self._after_step(sr)
            return sr

    async def _after_step(self, sr: StepResult) -> None:
        for ch in self.channels.values():
            if hasattr(ch, "after_step"):
                await ch.after_step(self.ctx, sr, self.trace)
        self.res.steps.append(sr)
        if self.on_step:
            await self.on_step(sr)

    async def _on_failure(self, sr: StepResult) -> None:
        for ch in self.channels.values():
            if hasattr(ch, "on_failure"):
                try:
                    await ch.on_failure(self.ctx, sr.id)
                except Exception as e:  # 设备已断开时拿不到现场，不掩盖原始错误
                    self.ctx.warnings.append(f"失败现场采集失败：{type(e).__name__}")
        await self._after_step_safe(sr)

    async def _after_step_safe(self, sr: StepResult) -> None:
        try:
            await self._after_step(sr)
        except Exception as e:
            self.ctx.warnings.append(f"失败步骤日志采集失败：{type(e).__name__}")
            if sr not in self.res.steps:
                self.res.steps.append(sr)

    async def _assert(self, a: dict) -> AssertResult:
        self.done_asserts.add(a.get("id", ""))
        if self.on_assert:
            await self.on_assert("start", a, None, self.ev)
        r = await evaluators()[a["type"]](a, self.ev, self.ectx)
        self.res.asserts.append(r)
        if r.result == "fail" and "fail_at" not in self.ev.marks:  # 判定有延迟，失败时刻以截图时间为准
            shot = self.ev.find("screenshot", a.get("after_step"))
            self.ev.mark("fail_at", shot.ts if shot else None)
        if self.on_assert:
            await self.on_assert("end", a, r, self.ev)
        return r

    async def _final_asserts(self) -> None:
        """无 after_step 的断言总要跑（崩溃后定位失败时，no_crash 才是真正的结论）；
        绑定到未执行步骤的断言记为 skipped。"""
        done_steps = {s.id for s in self.res.steps if s.result == "ok"}
        for a in self.plan.get("assertions") or []:
            if a.get("id") in self.done_asserts:
                continue
            step = a.get("after_step")
            if step is None:
                await self._assert(a)
            elif step not in done_steps:
                self.res.asserts.append(AssertResult(assertion_id=a.get("id", ""), type=a.get("type", ""),
                                                     result="skipped", detail={"after_step": step}))

    async def _collect(self) -> None:
        for ch in self.channels.values():
            if hasattr(ch, "collect"):
                try:
                    await ch.collect(self.ctx, self.ev.marks.get("fail_at"))
                except Exception as e:
                    self.ctx.warnings.append(f"证据采集失败：{type(e).__name__}: {e}")

    async def _teardown(self) -> None:
        for s in self.plan.get("teardown") or []:
            try:
                await self._run_action(s)
            except Exception as e:
                self.ctx.warnings.append(f"收尾动作 {s.get('action')} 失败：{e}")
