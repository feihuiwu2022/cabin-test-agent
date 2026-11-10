"""台架 Agent 主流程：注册 → 心跳 + 领取 + 补传。回放不调用大模型，判定交给云端（R-1）。

用法（台架 PC，backend/ 下）：
  CABIN_CLOUD_URL=http://云端:8800 CABIN_RIG_REGISTER_CODE=<注册码> CABIN_RIG_ID=B1 CABIN_RIG_MODEL=A01-CN \
  CABIN_DEVICE=adb CABIN_ADB_SERIAL=<序列号> uv run cabin-agent
首次注册后令牌写入数据目录，之后不再需要注册码。本地页面：http://127.0.0.1:8810
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import assets, secret
from .config import get_settings, load_local, save_local
from .device import SIM_PACKAGES
from .device.base import DeviceAdapter
from .runner.context import AssertResult, PlanResult, StepResult
from .runner.engine import PlanRunner
from .runner.evidence import Evidence
from .runner.rigchannels import device_capabilities, rig_channels
from cabin_common import vehicles
from cabin_common.retention import prune
from cabin_common.schema import TestCase
from .capture import CaptureChannel
from .client import BACKOFF_S, CloudClient, CloudDown, CloudRejected
from .judge import HttpJudge
from .outbox import Outbox

log = logging.getLogger("cabin.agent")
AGENT_VERSION = "0.1.0"
EVIDENCE_DAYS, EVIDENCE_MAX_BYTES = 7, 20 * 2 ** 30  # PF-UP-04
FILED_KINDS = ("replay", "retest")  # 失败时可能提单，要留证据
HARDWARE = ("audio", "can", "camera")  # 工程师在本地页面声明的外设：喇叭 + 麦克风、CAN 工具、摄像头
VERDICT = {"passed": "pass", "failed": "fail", "blocked": "blocked"}


@dataclass
class AgentConfig:
    cloud_url: str
    rig_id: str
    model: str
    register_code: str = ""
    log_dir: str = ""
    data_dir: Path = field(default_factory=lambda: get_settings().data_dir)
    heartbeat_s: float = 15  # A-02
    idle_backoff_s: tuple[float, ...] = (5, 10, 20, 30)  # PF-TSK-03
    send_backoff_s: tuple[float, ...] = BACKOFF_S


class RigAgent:
    def __init__(self, cfg: AgentConfig, dev: DeviceAdapter, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg, self.dev = cfg, dev
        self.token_file = cfg.data_dir / "rig_token"
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        token = secret.load(self.token_file)
        self.client = CloudClient(cfg.cloud_url, token, transport=transport)
        self.outbox = Outbox(cfg.data_dir / "outbox.sqlite3")
        self.ota = ""
        self.base_caps = device_capabilities(dev)
        self.profile_file = cfg.data_dir / "rig_profile.json"
        saved = json.loads(self.profile_file.read_text("utf8")) if self.profile_file.is_file() else {}
        cfg.model = saved.get("model") or cfg.model
        self.hardware = [h for h in saved.get("hardware", []) if h in HARDWARE]
        self.caps = sorted(self.base_caps | set(self.hardware))
        self.model_warning = ""
        self.packages = SIM_PACKAGES if dev.name == "simcar" else []
        self.state = "idle"
        self.run_id: str | None = None
        self.running: asyncio.Task | None = None
        self.halted = False  # 令牌被吊销：停止领取，等人工处理
        self.wake = asyncio.Event()  # 有待补传的内容

    # ---------- 注册 / 心跳 ----------
    async def register(self) -> None:
        await self.dev.start()
        h = await self.dev.health()
        self.ota = h.ota
        read = vehicles.normalize(h.model or "")  # 能读到车机车型时核对，读不到就以手选为准
        self.model_warning = f"手选 {self.cfg.model}，车机读到 {read}" if read and read != self.cfg.model else ""
        host = {"os": platform.platform(), "disk_free_gb": shutil.disk_usage(self.cfg.data_dir).free // 2 ** 30,
                "model_warning": self.model_warning}
        data = await self.client.call("POST", "/api/v1/rigs/register", json={
            "rig_id": self.cfg.rig_id, "register_code": self.cfg.register_code, "model": self.cfg.model,
            "ota_version": h.ota, "capabilities": self.caps, "agent_version": AGENT_VERSION, "host": host})
        if data and data.get("rig_token"):
            secret.save(self.token_file, data["rig_token"])  # Windows 用 DPAPI 加密
            if load_local(self.cfg.data_dir).get("register_code"):  # 注册码只用一次，之后用令牌
                save_local({"register_code": ""}, self.cfg.data_dir)
            self.client.token = data["rig_token"]
        log.info("台架 %s 已注册：车型 %s · 能力 %s · 车机在线=%s", self.cfg.rig_id, self.cfg.model, self.caps, h.online)

    async def update_profile(self, model: str, hardware: list[str]) -> None:
        """切换车型或声明外设。只能在空闲时改，云端确认后才保存。"""
        if self.state != "idle":
            raise ValueError("台架正在执行，空闲时才能修改")
        unknown = set(hardware) - set(HARDWARE)
        if unknown:
            raise ValueError(f"未知外设：{sorted(unknown)}")
        old = self.cfg.model, self.hardware, self.caps
        self.cfg.model, self.hardware = model, list(hardware)
        self.caps = sorted(self.base_caps | set(hardware))
        try:
            await self.register()
        except (CloudDown, CloudRejected):
            self.cfg.model, self.hardware, self.caps = old
            raise
        self.profile_file.write_text(json.dumps({"model": model, "hardware": hardware}, ensure_ascii=False), "utf8")

    async def heartbeat(self) -> None:
        data = await self.client.call("POST", f"/api/v1/rigs/{self.cfg.rig_id}/heartbeat", json={
            "state": self.state, "run_id": self.run_id, "ota_version": self.ota, "capabilities": self.caps,
            "outbox": self.outbox.counts()})
        for cmd in (data or {}).get("commands", []):
            if cmd.get("type") == "cancel_run" and cmd.get("run_id") == self.run_id and self.running:
                log.info("云端取消 %s", self.run_id)
                self.running.cancel()

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self.heartbeat()
            except CloudDown as e:
                log.warning("心跳失败：%s", e)
            except CloudRejected as e:
                self._on_rejected(e)
            await asyncio.sleep(self.cfg.heartbeat_s)

    def _on_rejected(self, e: CloudRejected) -> None:
        if e.code == 1002:
            self.halted = True
            log.error("令牌无效或已吊销，停止领取：%s", e)
        else:
            log.error("云端拒绝：%s", e)

    # ---------- 领取与执行 ----------
    async def claim_once(self) -> bool:
        claim = await self.client.call("POST", "/api/v1/runs/claim")
        if not claim:
            return False
        await self.execute(claim)
        return True

    async def _work_loop(self) -> None:
        idle = 0
        while True:
            try:
                ran = False if self.halted else await self.claim_once()
            except CloudDown as e:
                log.warning("领取失败：%s", e)
                ran = False
            except CloudRejected as e:
                self._on_rejected(e)
                ran = False
            idle = 0 if ran else min(idle + 1, len(self.cfg.idle_backoff_s) - 1)
            if not ran:
                await asyncio.sleep(self.cfg.idle_backoff_s[idle])

    async def _ensure_assets(self, plan: dict) -> None:
        for name, a in (plan.get("assets") or {}).items():
            sha = (a or {}).get("sha256")
            if sha and not assets.asset_path(sha).is_file():
                data = await self.client.download(f"/api/v1/plans/{plan['case_id']}/assets/{name}")
                if hashlib.sha256(data).hexdigest() == sha:
                    assets.asset_path(sha).write_bytes(data)
        for name, a in (plan.get("images") or {}).items():  # 图标模板（多级定位的图标层）
            sha = (a or {}).get("sha256")
            if sha and not assets.image_path(sha).is_file():
                data = await self.client.download(f"/api/v1/plans/{plan['case_id']}/images/{name}")
                if hashlib.sha256(data).hexdigest() == sha:
                    assets.image_path(sha).write_bytes(data)

    async def execute(self, claim: dict) -> PlanResult:
        run_id, attempt = claim["run_id"], claim["attempt"]
        plan, case = claim["plan_inline"], TestCase(**claim["case"])
        self.state, self.run_id = "running", run_id
        total = len(plan.get("steps") or [])
        seq, sent = 0, 0

        def event(**e: Any) -> None:
            nonlocal seq
            seq += 1
            self.outbox.put("events", {"run_id": run_id, "attempt": attempt, "events": [{"seq": seq, **e}]})
            self.wake.set()

        def queue_files(ev: Evidence) -> None:
            nonlocal sent
            for f in ev.files[sent:]:
                self.outbox.put("file", {"run_id": run_id, "attempt": attempt, "path": str(ev.path(f)), "name": f.name})
            sent = len(ev.files)

        async def on_step(sr: StepResult) -> None:
            event(type="step", step=sr.model_dump(), total=total)

        async def on_assert(phase: str, a: dict, r: AssertResult | None, ev: Evidence) -> None:
            shot = ev.find("screenshot", a.get("after_step")) if phase == "start" else None
            event(type="assert", phase=phase, assertion=a, result=r.model_dump() if r else None,
                  shot=ev.url(shot) if shot else "")

        try:
            await self._ensure_assets(plan)
        except (CloudDown, CloudRejected) as e:
            log.warning("测试音频 / 图标模板下载失败：%s", e)  # 缺失由 Runner 校验，以 plan_invalid 阻塞
        channels = rig_channels(self.dev)
        if claim.get("kind") in FILED_KINDS:  # 试跑失败不提单，不用录
            channels.append(CaptureChannel(self.dev, self.cfg.log_dir))
        runner = PlanRunner(plan, channels=channels, run_id=run_id, case=case,
                            judge=HttpJudge(self.client, run_id), plan_version=claim.get("plan_version", ""),
                            rig=self.cfg.rig_id, packages=self.packages, on_step=on_step, on_assert=on_assert)
        self.running = asyncio.create_task(runner.execute())
        try:
            res = await self.running
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # Agent 自身在退出，不是云端取消
                raise
            res = runner.res
        finally:
            self.running = None
            self.state, self.run_id = "idle", None
        res.ota = self.ota
        if res.verdict == "failed" and claim.get("kind") in FILED_KINDS:  # 只传失败证据，通过的留在本地，到期清理
            queue_files(runner.ev)
            for name in ("manifest.json", "result.json"):
                if (runner.ev.root / name).is_file():
                    self.outbox.put("file", {"run_id": run_id, "attempt": attempt, "path": str(runner.ev.root / name), "name": name})
        elif res.verdict == "blocked" and res.block_reason == "locator":  # 自愈建议要失败现场：只传截图和控件树
            for f in runner.ev.files:
                if f.name.startswith("fail-") and f.name.endswith((".png", ".ui.json")):
                    self.outbox.put("file", {"run_id": run_id, "attempt": attempt, "path": str(runner.ev.path(f)), "name": f.name})
        self.outbox.put("result", {"run_id": run_id, "attempt": attempt, "verdict": VERDICT[res.verdict],
                                   "plan_result": res.model_dump(mode="json")})
        self.wake.set()
        prune(get_settings().evidence_dir, EVIDENCE_DAYS, EVIDENCE_MAX_BYTES, keep={run_id})
        log.info("%s %s · 结论 %s %s", run_id, case.case_id, res.verdict, res.block_reason)
        return res

    # ---------- 补传 ----------
    async def _send(self, kind: str, b: dict) -> None:
        rid, attempt = b["run_id"], b["attempt"]
        if kind == "events":
            await self.client.call("POST", f"/api/v1/runs/{rid}/events", json={"attempt": attempt, "events": b["events"]})
        elif kind == "file":
            p = Path(b["path"])
            if not p.is_file():
                log.warning("证据文件已不存在：%s", p)
                return
            data = p.read_bytes()
            up = await self.client.call("POST", "/api/v1/evidence/upload-url", json={
                "run_id": rid, "attempt": attempt, "file": b["name"], "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()})
            await self.client.call("PUT", up["upload"]["url"], content=data)
        else:
            await self.client.call("POST", f"/api/v1/runs/{rid}/result", headers={"Idempotency-Key": f"{rid}-{attempt}"},
                                   json={"attempt": attempt, "verdict": b["verdict"], "plan_result": b["plan_result"]})

    async def drain(self) -> None:
        """按写入顺序发送，遇到云端不可用就停下等下一轮，保证事件、证据、结果的先后。"""
        while item := self.outbox.head():
            item_id, kind, body = item
            try:
                await self._send(kind, body)
            except CloudRejected as e:
                self._on_rejected(e)
                if e.code == 1002:  # 令牌问题不是这条数据的错，留着等恢复
                    return
                log.error("丢弃被云端拒绝的 %s：%s", kind, body.get("run_id"))
            self.outbox.done(item_id)

    async def _drain_loop(self) -> None:
        fails = 0
        while True:
            try:
                await self.drain()
                fails = 0
                self.wake.clear()
                if not self.outbox.head():
                    await self.wake.wait()
            except CloudDown as e:
                wait = self.cfg.send_backoff_s[min(fails, len(self.cfg.send_backoff_s) - 1)]
                fails += 1
                log.warning("云端不可用，%s 秒后补传（待传 %s）：%s", wait, self.outbox.counts(), e)
                await asyncio.sleep(wait)

    async def run(self, *also) -> None:
        """also：和 Agent 同生命周期的协程，比如本地页面。"""
        while True:
            try:
                await self.register()
                break
            except CloudDown as e:
                log.warning("注册失败，稍后重试：%s", e)
                await asyncio.sleep(self.cfg.send_backoff_s[-1])
        await asyncio.gather(self._heartbeat_loop(), self._work_loop(), self._drain_loop(), *also)

    async def close(self) -> None:
        await self.client.close()
        self.outbox.close()


def selfcheck() -> dict[str, Any]:
    """打包后的自检（构建流水线和装机时用）：本地页面、adb、ffmpeg、数据目录、令牌加密都在不在。"""
    from . import config
    from .runner import vision_local
    s = get_settings()
    out: dict[str, Any] = {"version": AGENT_VERSION, "frozen": config.FROZEN, "data_dir": str(s.data_dir)}
    checks: dict[str, Any] = {}
    checks["web"] = (config.RES_DIR / "web" / "dist" / "index.html").is_file()
    try:
        from adbutils._utils import adb_path
        checks["adb"] = Path(adb_path()).is_file()
    except Exception as e:  # 没有 adb 就连不了车机
        checks["adb"] = f"{type(e).__name__}: {e}"
    try:
        import imageio_ffmpeg
        checks["ffmpeg"] = Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file()
    except Exception as e:
        checks["ffmpeg"] = f"{type(e).__name__}: {e}"
    probe = s.data_dir / ".selfcheck"
    try:
        secret.save(probe, "ok")
        checks["data_dir"] = secret.load(probe) == "ok"
        checks["token_protection"] = "dpapi" if secret.protected() else "file-600"
    finally:
        probe.unlink(missing_ok=True)
    checks["ocr"] = vision_local.ocr_unavailable() or "ok"  # 可选
    checks["template"] = vision_local.template_unavailable() or "ok"  # 可选
    out["checks"] = checks
    out["ok"] = all(checks[k] is True for k in ("web", "adb", "ffmpeg", "data_dir"))
    return out


def _logging(data_dir: Path) -> None:
    """控制台 + 按天滚动的日志文件（保留 14 天，NP-08）。"""
    from logging.handlers import TimedRotatingFileHandler
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(),
              TimedRotatingFileHandler(data_dir / "logs" / "agent.log", when="midnight", backupCount=14, encoding="utf8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def _utf8_stdio() -> None:
    """输出和日志里有中文：Windows 控制台或重定向到文件时默认编码可能是 cp1252 / GBK，统一用 UTF-8，编不了的字不至于让程序崩溃。"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # 被替换成不支持 reconfigure 的流（如测试捕获）
            pass


def run(argv: list[str] | None = None) -> None:
    import argparse
    import webbrowser

    import uvicorn

    from . import config, firstrun
    from .localweb import create_app
    ap = argparse.ArgumentParser(prog="cabin-agent", description="CabinPilot 台架 Agent")
    ap.add_argument("--version", action="store_true", help="打印版本后退出")
    ap.add_argument("--selfcheck", action="store_true", help="检查打包内容和数据目录，输出 JSON")
    ap.add_argument("--setup", action="store_true", help="重新打开首次配置页（改云端地址、台架编号等）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开本地页面")
    a = ap.parse_args(argv)
    _utf8_stdio()
    if a.version:
        print(AGENT_VERSION)
        return
    if a.selfcheck:
        r = selfcheck()
        print(json.dumps(r, ensure_ascii=False, indent=1))
        raise SystemExit(0 if r["ok"] else 1)
    s = get_settings()
    _logging(s.data_dir)
    open_browser = config.FROZEN and not a.no_browser
    if not s.configured or a.setup:
        done = asyncio.Event()
        url = f"http://127.0.0.1:{s.ui_port}/"
        log.info("首次配置：在浏览器打开 %s 填云端地址和注册码", url)
        srv = uvicorn.Server(uvicorn.Config(firstrun.create_setup_app(s, done), host="127.0.0.1", port=s.ui_port,
                                            log_level="warning"))

        async def setup() -> None:
            task = asyncio.create_task(srv.serve())
            if open_browser or a.setup:
                webbrowser.open(url)
            await done.wait()
            await asyncio.sleep(0.5)  # 让保存成功的响应先回到页面
            srv.should_exit = True
            await task
        try:
            asyncio.run(setup())
        except KeyboardInterrupt:
            return
        get_settings.cache_clear()
        s = get_settings()
        open_browser = False  # 配置页会自己跳到本地页面
    if s.device == "simcar":
        from .device.simcar import SimCarAdapter  # 只有模拟车机要 playwright，正式包不带
        dev: DeviceAdapter = SimCarAdapter()
    else:
        from .device.adb import AndroidAdbAdapter
        dev = AndroidAdbAdapter(s.adb_serial)
    cfg = AgentConfig(cloud_url=s.cloud_url, rig_id=s.rig_id, model=s.rig_model, register_code=s.register_code, log_dir=s.log_dir)
    agent = RigAgent(cfg, dev)
    from .localweb import PUBLIC
    web = uvicorn.Server(uvicorn.Config(create_app(agent, s.ui_port), host="0.0.0.0" if PUBLIC else "127.0.0.1",
                                        port=s.ui_port, log_level="warning"))
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{s.ui_port}/")
    try:
        asyncio.run(agent.run(web.serve()))
    except KeyboardInterrupt:
        pass
    except CloudRejected as e:
        log.error("注册被拒绝：%s。注册码不对或已用过时，用 --setup 重新填写", e)
        raise SystemExit(f"注册被拒绝：{e}") from e


if __name__ == "__main__":
    run()
