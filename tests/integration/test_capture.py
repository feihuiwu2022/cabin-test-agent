"""失败证据：录屏循环、视频截取、日志包、只传失败证据、清理。用真实 ffmpeg 处理视频。"""
import asyncio
import io
import os
import subprocess
import time
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from cabin_agent import capture
from cabin_agent.capture import CaptureChannel, Clip, FrameRecorder, ScreenRecorder, _window
from cabin_agent.config import get_settings
from cabin_agent.daemon import AgentConfig, RigAgent
from cabin_agent.localweb import create_app
from cabin_agent.runner.actions.adb import AdbChannel
from cabin_agent.runner.engine import PlanRunner
from cabin_cloud.execsvc import rigapi
from cabin_common.retention import prune
from test_rigagent import CODE, URL, LocalJudgeStub, Net, agent, case_with_plan, cloud, submit  # noqa: F401
from test_runner import FakeDevice, FakeJudge, fast, plan  # noqa: F401

import httpx


def mp4(path: Path, seconds=1) -> bytes:
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=red:s=64x64:d={seconds}:r=5",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)], check=True)
    return path.read_bytes()


def duration(path: Path) -> float:
    out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = out.split("Duration: ")[1].split(",")[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def jpeg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (65, 65), "blue").save(b, "JPEG")
    return b.getvalue()


class AdbFake(FakeDevice):
    """假 adb：screenrecord 按要求的时长阻塞，pkill 让它立刻返回。"""
    name = "adb"

    def __init__(self, video: bytes = b"", fail_record=False, logs=None):
        super().__init__()
        self.video, self.fail_record, self.log_files = video, fail_record, logs or {}
        self.stop = asyncio.Event()
        self.pulled = []

    async def shell(self, cmd):
        self.calls.append(("shell", cmd))
        if cmd.startswith("screenrecord"):
            if self.fail_record:
                return "ERROR: unable to create encoder"
            try:
                await asyncio.wait_for(self.stop.wait(), 1.2)
            except asyncio.TimeoutError:
                pass
        elif cmd.startswith("pkill"):
            self.stop.set()
        elif cmd.startswith("ls -t"):
            return "\n".join(self.log_files)
        return ""

    async def pull(self, remote):
        self.pulled.append(remote)
        return self.log_files.get(Path(remote).name) or self.video


def test_window_picks_clips_around_failure():
    clips = [Clip("a", 0, 15), Clip("b", 15, 30), Clip("c", 100, 115), Clip("d", 115, 130)]
    assert [c.path for c in _window(clips, 112)] == ["c", "d"]
    assert [c.path for c in _window(clips, 60)] == ["a", "b"]
    assert [c.path for c in _window(clips, 200)] == []


async def test_recorder_rings_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(capture, "MIN_SEG_S", 0)
    dev = AdbFake()
    r = ScreenRecorder(dev, seg_s=1, keep_s=2)
    await r.start()
    await asyncio.sleep(2.8)
    await r.stop()
    assert len(r.clips) <= 2 and len({c.path for c in r.clips}) == len(r.clips)
    assert ("shell", "pkill -INT screenrecord") in dev.calls


async def test_recorder_detects_unsupported_screen():
    r = ScreenRecorder(AdbFake(fail_record=True), seg_s=1)
    await r.start()
    await r.task
    assert r.broken and not r.clips


async def test_clip_trims_to_window(tmp_path, monkeypatch):
    dev = AdbFake(video=mp4(tmp_path / "s.mp4", 2))
    r = ScreenRecorder(dev)
    now = time.time()
    r.clips = [Clip("/a.mp4", now - 4, now - 2), Clip("/b.mp4", now - 2, now)]
    out = tmp_path / "o.mp4"
    assert await r.clip(now - 1, out)
    assert dev.pulled == ["/a.mp4", "/b.mp4"] and 3.5 <= duration(out) <= 4.5


async def test_clip_cuts_start_outside_window(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "BEFORE_S", 1)
    monkeypatch.setattr(capture, "AFTER_S", 1)
    dev = AdbFake(video=mp4(tmp_path / "s.mp4", 2))
    r = ScreenRecorder(dev)
    now = time.time()
    r.clips = [Clip("/a.mp4", now - 4, now - 2), Clip("/b.mp4", now - 2, now)]
    out = tmp_path / "o.mp4"
    assert await r.clip(now - 1, out)  # 只要 now-2 到 now，第一段整段在窗口外
    assert dev.pulled == ["/b.mp4"] and 1.5 <= duration(out) <= 2.5


async def test_frames_fallback_makes_video(tmp_path):
    class Shots(FakeDevice):
        async def screenshot(self, fmt="png"):
            return jpeg()

    r = FrameRecorder(Shots(), interval_s=0.05)
    await r.start()
    await asyncio.sleep(0.5)
    await r.stop()
    out = tmp_path / "f.mp4"
    assert await r.clip(time.time(), out) and out.stat().st_size > 0
    await r.cleanup()
    assert not r.dir.exists()


async def test_frame_ring_is_bounded():
    class Shots(FakeDevice):
        async def screenshot(self, fmt="png"):
            return b"x"

    r = FrameRecorder(Shots(), interval_s=0.01, keep_s=0.05)
    await r.start()
    await asyncio.sleep(0.3)
    await r.stop()
    assert len(r.frames) <= 5
    assert len(list(r.dir.iterdir())) <= 5
    await r.cleanup()


def run_with(dev, judge, **kw):
    ch = CaptureChannel(dev, seg_s=1, **kw)
    p = plan()
    return ch, PlanRunner(p, channels=[AdbChannel(dev), ch], run_id=f"R-cap-{time.time_ns()}", judge=judge).execute()


async def test_failure_keeps_video_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MIN_SEG_S", 0)
    monkeypatch.setattr(capture, "AFTER_S", 0)
    dev = AdbFake(video=mp4(tmp_path / "s.mp4"), logs={"old.txt": b"x", "cabin-1.tar.zip": b"LOGS"})
    _, coro = run_with(dev, FakeJudge(consistent=False), log_dir="/data/log")
    r = await coro
    names = {f["name"]: f for f in r.manifest["files"]}
    assert r.verdict == "failed" and "video.mp4" in names and names["cabin-1.tar.zip"]["kind"] == "log"
    assert "fail_at" in r.manifest["marks"]
    assert ("shell", "rm -rf /sdcard/cabin_rec") in dev.calls  # 车机上不留录像


async def test_pass_keeps_no_video(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MIN_SEG_S", 0)
    dev = AdbFake(video=mp4(tmp_path / "s.mp4"))
    _, coro = run_with(dev, FakeJudge(consistent=True))
    r = await coro
    assert r.verdict == "passed" and "video.mp4" not in {f["name"] for f in r.manifest["files"]}
    assert ("shell", "rm -rf /sdcard/cabin_rec") in dev.calls


async def test_capture_problems_do_not_change_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "AFTER_S", 0)

    class Broken(AdbFake):
        async def pull(self, remote):
            raise ConnectionError("adb 断开")

    monkeypatch.setattr(capture, "MIN_SEG_S", 0)
    _, coro = run_with(Broken(video=b"x"), FakeJudge(consistent=False))
    r = await coro
    assert r.verdict == "failed" and any("证据采集失败" in w for w in r.warnings)


async def test_missing_log_dir_only_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "AFTER_S", 0)
    monkeypatch.setattr(capture, "MIN_SEG_S", 0)
    dev = AdbFake(video=mp4(tmp_path / "s.mp4"))
    _, coro = run_with(dev, FakeJudge(consistent=False))
    r = await coro
    assert any("CABIN_LOG_DIR" in w for w in r.warnings)


@pytest.mark.parametrize("bad", ["/data; rm -rf /", "data/log", "/a b"])
async def test_log_dir_is_validated(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "AFTER_S", 0)
    monkeypatch.setattr(capture, "MIN_SEG_S", 0)
    dev = AdbFake(video=mp4(tmp_path / "s.mp4"))
    _, coro = run_with(dev, FakeJudge(consistent=False), log_dir=bad)
    r = await coro
    assert any("不合法" in w for w in r.warnings) and ("shell", "ls -t") not in [(c[0], str(c[-1])[:5]) for c in dev.calls]


# ---------- 上传 ----------
async def test_only_failed_replay_uploads_evidence(agent, cloud, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(capture, "AFTER_S", 0)
    monkeypatch.setattr(rigapi, "LocalJudge", lambda: LocalJudgeStub(FakeJudge(consistent=False)))
    await case_with_plan()
    st = await submit()
    await agent.claim_once()
    assert agent.outbox.counts().get("file", 0) >= 1
    await agent.drain()
    await st.job
    puts = [p for m, p in agent.net.seen if m == "PUT"]
    assert any(p.endswith(".png") for p in puts)


async def test_local_page_serves_own_evidence(agent, tmp_path):  # noqa: F811
    root = get_settings().evidence_dir / "R20261107-999999"
    root.mkdir(parents=True, exist_ok=True)
    (root / "step-01.png").write_bytes(b"PNG")
    ui = create_app(agent, 8810, transport=agent.net)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui), base_url="http://127.0.0.1:8810") as c:
        r = await c.get("/evidence/R20261107-999999/step-01.png")
        assert r.status_code == 200 and r.content == b"PNG"
        assert (await c.get("/evidence/..%2F..%2Fpyproject.toml")).status_code in (401, 404)  # 编码路径，客户端不会规范化


# ---------- 清理 ----------
def _dir(root, name, age_days=0, size=10):
    d = root / name
    d.mkdir()
    (d / "f").write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(d, (t, t))


def test_prune_by_age(tmp_path):
    _dir(tmp_path, "old", 8)
    _dir(tmp_path, "new", 1)
    assert prune(tmp_path, 7, 10 ** 9) == ["old"] and (tmp_path / "new").exists()


def test_prune_by_size_removes_oldest(tmp_path):
    _dir(tmp_path, "a", 3, 100)
    _dir(tmp_path, "b", 2, 100)
    _dir(tmp_path, "c", 1, 100)
    assert prune(tmp_path, 7, 150) == ["a", "b"]


def test_prune_keeps_current_run(tmp_path):
    _dir(tmp_path, "cur", 9)
    assert prune(tmp_path, 7, 10 ** 9, keep={"cur"}) == []
