"""失败证据：录屏循环 + 日志包。执行时一直录，失败时截取前后 1 分钟，通过的不留。

录屏：ADB screenrecord 分段写到车机，只留最近 3 分钟；视觉判定有延迟，所以失败后再按 fail_at 回头截。
部分车机录不了屏，退回每秒截图合成。adb screenrecord 不录声音。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg

from .device.base import DeviceAdapter
from .runner.context import RunContext

log = logging.getLogger("cabin.agent.capture")
BEFORE_S, AFTER_S = 55, 5  # 失败前后保留多久，加起来 1 分钟
SEG_S = 15
KEEP_S = 180  # 循环缓冲 3 分钟
BIT_RATE = 1_500_000  # 1 分钟约 11 MB，给 Jira 30 MB 附件上限留出日志的空间
REMOTE_DIR = "/sdcard/cabin_rec"
RE_LOG_DIR = re.compile(r"/[\w./\-]+")
RE_LOG_FILE = re.compile(r"[\w.\-]+\.(zip|tar|gz|tgz)")
MIN_SEG_S = 1.0  # 短于这个时长，说明 screenrecord 根本没录起来


def _ffmpeg(*args: str) -> None:
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *args], check=True, capture_output=True, timeout=120)


@dataclass
class Clip:
    path: str
    start: float
    end: float


def _window(clips: list[Clip], fail_at: float) -> list[Clip]:
    return [c for c in clips if c.end > fail_at - BEFORE_S and c.start < fail_at + AFTER_S]


class ScreenRecorder:
    def __init__(self, dev: DeviceAdapter, seg_s: float = SEG_S, keep_s: float = KEEP_S):
        self.dev, self.seg_s = dev, seg_s
        self.slots = max(int(keep_s / seg_s), 1)
        self.clips: list[Clip] = []
        self.stopping = False
        self.broken = False  # 录不了屏，改用截图
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.dev.shell(f"mkdir -p {REMOTE_DIR}")
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        n = 0
        while not self.stopping:
            path = f"{REMOTE_DIR}/seg-{n % self.slots:03d}.mp4"
            t0 = time.time()
            try:
                await self.dev.shell(f"screenrecord --bit-rate {BIT_RATE} --time-limit {max(int(self.seg_s), 1)} {path}")
            except Exception as e:
                log.warning("录屏失败：%s", e)
                self.broken = True
                return
            t1 = time.time()
            if t1 - t0 < MIN_SEG_S and not self.stopping:
                log.warning("录屏立即退出，改用截图")
                self.broken = True
                return
            self.clips = [c for c in self.clips if c.path != path] + [Clip(path, t0, t1)]
            n += 1

    async def stop(self) -> None:
        self.stopping = True
        try:
            await self.dev.shell("pkill -INT screenrecord")  # 让当前分段正常收尾，否则 mp4 不完整
        except Exception as e:
            log.warning("停止录屏失败：%s", e)
        if self.task:
            await self.task

    async def clip(self, fail_at: float, out: Path) -> bool:
        picked = _window(self.clips, fail_at)
        if not picked:
            return False
        with tempfile.TemporaryDirectory() as tmp:
            parts = []
            for i, c in enumerate(picked):
                p = Path(tmp) / f"{i:03d}.mp4"
                p.write_bytes(await self.dev.pull(c.path))
                parts.append(p)
            lst = Path(tmp) / "list.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), "utf8")
            skip = max(fail_at - BEFORE_S - picked[0].start, 0)
            whole = Path(tmp) / "whole.mp4"
            await asyncio.to_thread(_ffmpeg, "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(whole))
            # 拼接和裁剪分两步：-ss 和 concat 写在同一条命令里时，后面的分段会丢
            await asyncio.to_thread(_ffmpeg, "-ss", f"{skip:.2f}", "-i", str(whole), "-t", str(BEFORE_S + AFTER_S), "-c", "copy",
                                    "-movflags", "+faststart", str(out))
        return True

    async def cleanup(self) -> None:
        await self.dev.shell(f"rm -rf {REMOTE_DIR}")


class FrameRecorder:
    """每秒截一张存本地，失败时合成视频。"""

    def __init__(self, dev: DeviceAdapter, interval_s: float = 1, keep_s: float = KEEP_S):
        self.dev, self.interval_s = dev, interval_s
        self.limit = max(int(keep_s / interval_s), 1)
        self.dir = Path(tempfile.mkdtemp(prefix="cabin-frames-"))
        self.frames: list[tuple[float, Path]] = []
        self.stopping = False
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        n = 0
        while not self.stopping:
            try:
                p = self.dir / f"{n:08d}.jpg"
                p.write_bytes(await self.dev.screenshot("jpeg"))
                self.frames.append((time.time(), p))
                n += 1
                while len(self.frames) > self.limit:
                    self.frames.pop(0)[1].unlink(missing_ok=True)
            except Exception as e:  # 车机一时取不到图，下一帧再试
                log.debug("截图失败：%s", e)
            await asyncio.sleep(self.interval_s)

    async def stop(self) -> None:
        self.stopping = True
        if self.task:
            await self.task

    async def clip(self, fail_at: float, out: Path) -> bool:
        picked = [p for t, p in self.frames if fail_at - BEFORE_S <= t <= fail_at + AFTER_S]
        if not picked:
            return False
        with tempfile.TemporaryDirectory() as tmp:
            for i, p in enumerate(picked):
                shutil.copy(p, Path(tmp) / f"{i:05d}.jpg")
            await asyncio.to_thread(_ffmpeg, "-framerate", str(1 / self.interval_s), "-i", str(Path(tmp) / "%05d.jpg"),
                                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(out))
        return True

    async def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class CaptureChannel:
    """执行期间录屏，失败时收证据。录屏和取日志出问题都只记告警，不影响测试结论。"""
    name = "capture"
    capabilities: set[str] = set()

    def __init__(self, dev: DeviceAdapter, log_dir: str = "", seg_s: float = SEG_S):
        self.dev, self.log_dir = dev, log_dir
        self.rec: ScreenRecorder | FrameRecorder = ScreenRecorder(dev, seg_s) if dev.name == "adb" else FrameRecorder(dev)

    async def start(self) -> None:
        try:
            await self.rec.start()
        except Exception as e:
            log.warning("录屏启动失败：%s", e)

    async def collect(self, ctx: RunContext, fail_at: float | None) -> None:
        await self.rec.stop()
        try:
            if fail_at is not None:
                await asyncio.sleep(max(fail_at + AFTER_S - time.time(), 0))  # 判定快的时候，把失败后的 5 秒录全
                await self._video(ctx, fail_at)
                await self._logs(ctx)
        finally:
            await self.rec.cleanup()

    async def _video(self, ctx: RunContext, fail_at: float) -> None:
        out = ctx.evidence.root / "video.mp4"
        if isinstance(self.rec, ScreenRecorder) and self.rec.broken and not self.rec.clips:
            ctx.warnings.append("这台车机录不了屏，没有视频")
            return
        if await self.rec.clip(fail_at, out):
            ctx.evidence.add("video.mp4", "video", out.read_bytes(), range=[fail_at - BEFORE_S, fail_at + AFTER_S])
        else:
            ctx.warnings.append("失败时刻附近没有录到视频")

    async def _logs(self, ctx: RunContext) -> None:
        """日志包由台架存放在固定目录，取最新的一个。目录在台架配置 CABIN_LOG_DIR。"""
        if not self.log_dir:
            ctx.warnings.append("没有配置 CABIN_LOG_DIR，没有日志包")
            return
        if not RE_LOG_DIR.fullmatch(self.log_dir):
            ctx.warnings.append(f"CABIN_LOG_DIR 不合法：{self.log_dir}")
            return
        names = [n.strip() for n in (await self.dev.shell(f"ls -t {self.log_dir}")).splitlines()]
        name = next((n for n in names if RE_LOG_FILE.fullmatch(n)), "")
        if not name:
            ctx.warnings.append(f"{self.log_dir} 下没有日志包")
            return
        ctx.evidence.add(name, "log", await self.dev.pull(f"{self.log_dir}/{name}"), source="rig")
