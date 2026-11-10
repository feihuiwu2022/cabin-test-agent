"""判定服务的云端实现：经模型网关做视觉比对与双线判定（A-10）。"""
from __future__ import annotations

import re

from cabin_common.judging import VisionReq, VoiceReq

from .judge import vlm_compare


def _norm(s: str) -> str:
    return re.sub(r"[\s，。,.!！?？、“”\"']", "", s or "")


def listen_match(expected: str, transcript: str) -> bool:
    """听线（模拟通道用）：文案互相包含且数值一致。真实通道由语音大模型理解播报。"""
    e, t = _norm(expected), _norm(transcript)
    return bool(t) and (e in t or t in e) and re.findall(r"\d+", e) == re.findall(r"\d+", t)


def voice_class(look: bool, listen: bool, no_tts: bool) -> str:
    """双线合成：屏幕或播报任一不符即失败，分类用于定位问题环节。"""
    if no_tts:
        return "no_tts"
    if look and listen:
        return "pass"
    if look:
        return "tts_mismatch"
    return "exec_or_display" if listen else "recog_or_exec"


class LocalJudge:
    async def vision(self, req: VisionReq) -> dict:
        exp = req.expected + (f"（只判定屏幕区域 {req.region}）" if req.region else "")
        return await vlm_compare(req.case_id, exp, req.screen, req.elements, req.images, req.session)

    async def voice(self, req: VoiceReq) -> dict:
        if req.transcript is None and not req.no_tts:
            raise RuntimeError("语音服务未接入，无法理解真实播报音频（A-10）")
        v = await vlm_compare(req.case_id, req.screen_expected, req.screen, req.elements, req.image, req.session)
        look = bool(v.get("consistent", True))
        listen = not req.no_tts and listen_match(req.tts_expected, req.transcript or "")
        cls = voice_class(look, listen, req.no_tts)
        return {"result": "pass" if cls == "pass" else "fail", "class": cls,
                "lines": {"look": "pass" if look else "fail", "listen": "pass" if listen else "fail"},
                "actual_values": {"screen": v.get("actual_observed", ""), "tts": req.transcript or ""},
                "transcript": req.transcript or "", "confidence": v.get("confidence", 0), "look": v}
