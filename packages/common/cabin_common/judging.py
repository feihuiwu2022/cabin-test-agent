"""判定请求与判定客户端协议（接口约定 A-10）。云端是进程内实现，台架是 HTTP 实现。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class VisionReq:
    case_id: str
    expected: str
    images: list[bytes]
    screen: str = ""
    elements: str = ""
    region: list[int] | None = None
    session: str = ""


@dataclass
class VoiceReq:
    case_id: str
    screen_expected: str
    tts_expected: str
    image: bytes
    screen: str = ""
    elements: str = ""
    tts_audio: bytes = b""
    transcript: str | None = None  # 模拟音频直接带转写；真实音频由语音服务理解
    no_tts: bool = False
    session: str = ""


class JudgeClient(Protocol):
    async def vision(self, req: VisionReq) -> dict:
        """返回 {consistent, actual_observed, diff_summary, reason, confidence, element_id}。"""

    async def voice(self, req: VoiceReq) -> dict:
        """返回 {result, class, lines: {look, listen}, actual_values, transcript, confidence}（A-10 双线判定）。"""
