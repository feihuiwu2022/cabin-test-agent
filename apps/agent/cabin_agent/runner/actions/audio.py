"""音频通道。一期 SimAudioChannel 用 SimCar 模拟车机语音，验证时序与判定；
真实台架换成音箱 + 麦克风的实现（PF-AUD-01～06），动作与证据格式不变。"""
from __future__ import annotations

import asyncio
import io
import time
import wave
from typing import Any

from ...assets import duration_ms
from ...device.base import DeviceAdapter
from ..context import ActionError, RunContext
from . import action

RATE = 16000
SPEAK_S = 0.4  # 模拟车机播报时长


def silence_wav(seconds: float) -> bytes:
    """模拟通道没有真实声音，写静音 WAV 占位，保证证据结构与真实通道一致。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\0\0" * int(RATE * seconds))
    return buf.getvalue()


class SimAudioChannel:
    name = "audio"
    capabilities = {"audio"}

    def __init__(self, dev: DeviceAdapter):
        self.dev = dev
        self.reply: dict[str, Any] = {}
        self.rec_start = 0.0

    async def start(self) -> None:
        self.reply = {}

    async def stop(self) -> None:
        """模拟通道无需释放设备。"""


def _asset_text(ctx: RunContext, step: dict) -> str:
    return ((ctx.plan.get("assets") or {}).get(step["audio"]) or {}).get("text", "")


@action("record_start", channel="audio")
async def record_start(ctx: RunContext, step: dict) -> dict:
    ctx.audio.rec_start = time.time()
    ctx.evidence.mark("record_start", ctx.audio.rec_start)
    return {"label": "开始录音"}


@action("record_stop", channel="audio")
async def record_stop(ctx: RunContext, step: dict) -> dict:
    dur = max(time.time() - ctx.audio.rec_start, 0.1)
    ctx.evidence.mark("record_stop")
    ctx.evidence.add("audio.wav", "audio", silence_wav(dur), step=step.get("id"), rate=RATE, simulated=True)
    return {"label": "停止录音"}


@action("wake", channel="audio")
async def wake(ctx: RunContext, step: dict) -> dict:
    text = _asset_text(ctx, step)
    await asyncio.sleep(duration_ms(ctx.assets[step["audio"]]) / 1000)
    r = await ctx.audio.dev.voice_wake(text)
    ctx.evidence.fact("wake", "ok" if r.get("ok") else "no_response")
    if not r.get("ok"):  # 未唤醒不继续判后续（PF-AUD-04）
        raise ActionError("wake_failed", f"播放唤醒词“{text}”后车机无回应")
    return {"label": f"唤醒“{text}”，车机回应“{r.get('reply', '')}”"}


@action("play_utterance", channel="audio")
async def play_utterance(ctx: RunContext, step: dict) -> dict:
    text = _asset_text(ctx, step)
    await asyncio.sleep(duration_ms(ctx.assets[step["audio"]]) / 1000)  # 按真实音频时长占用时序
    ctx.audio.reply = await ctx.audio.dev.voice_say(text)
    ctx.evidence.mark("t0")  # 播放结束时刻
    return {"label": f"说“{text}”"}


@action("listen_tts", channel="audio")
async def listen_tts(ctx: RunContext, step: dict) -> dict:
    r = ctx.audio.reply
    tts = r.get("tts", "")
    if not tts:  # 无播报由评估器判失败（PF-AUD-05），这里只记事实
        ctx.evidence.fact("tts", "none")
        return {"label": "收听播报：无播报"}
    await asyncio.sleep(float(r.get("delay_ms", 0)) / 1000)
    ctx.evidence.mark("tts_start")
    await asyncio.sleep(SPEAK_S)
    ctx.evidence.mark("tts_end")
    ctx.evidence.fact("tts", "ok")
    ctx.evidence.add("tts-clip.wav", "tts_clip", silence_wav(SPEAK_S), step=step.get("id"), rate=RATE, simulated=True,
                     transcript=tts)
    return {"label": f"收听播报“{tts}”"}
