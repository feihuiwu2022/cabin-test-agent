"""cabin-voice / ASR：本地离线 SenseVoice（sherpa-onnx），数据不出网。

浏览器端 VAD 切句后上传整句音频（WAV），这里做识别 + 热词纠正。
"""
from __future__ import annotations

import asyncio
import io
import re
import time

import numpy as np
import soundfile as sf

from ..config import get_settings

# 热词纠正：车型名、功能名、台架编号、APP 名（ASR 常见误识别 → 规范写法）
HOTWORDS = [
    (r"台加", "台架"),
    (r"[bB必逼]\s*[二2两](?=号|台|\b|[^\d])", "B2"),
    (r"[bB必逼]\s*[一1](?=号|台|\b|[^\d])", "B1"),
    (r"(?i)(你好|hello|hi)\s*(小[和河荷合]|xiao\s*he)", "你好小禾"),
    (r"(?i)\bt\s*c\s*[-杠]\s*(?=[a-z0-9])", "TC-"),
    (r"自动条件亮度|自动调解亮度", "自动调节亮度"),
    (r"吉拉|基拉", "Jira"),
    (r"非书", "飞书"),
]


def normalize(text: str) -> str:
    t = re.sub(r"<\|[^|]*\|>", "", text).strip()
    for pat, rep in HOTWORDS:
        t = re.sub(pat, rep, t)
    return t


class ASR:
    def __init__(self) -> None:
        self._rec = None
        self._lock = asyncio.Lock()

    def _load(self):
        if self._rec is None:
            import sherpa_onnx
            d = get_settings().asr_model_dir
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"), use_itn=True, language="zh",
                num_threads=4)
        return self._rec

    @staticmethod
    def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if sr != 16000:  # 线性重采样到 16k
            n = int(len(audio) * 16000 / sr)
            audio = np.interp(np.linspace(0, len(audio), n, endpoint=False), np.arange(len(audio)), audio).astype(np.float32)
            sr = 16000
        return audio, sr

    def _recognize(self, audio: np.ndarray, sr: int) -> str:
        rec = self._load()
        st = rec.create_stream()
        st.accept_waveform(sr, audio)
        rec.decode_stream(st)
        return st.result.text

    async def transcribe(self, data: bytes) -> dict:
        t0 = time.time()
        audio, sr = self.decode_audio(data)
        if len(audio) < sr * 0.2:
            return {"text": "", "raw": "", "duration": len(audio) / sr, "latency_s": 0}
        async with self._lock:
            raw = await asyncio.to_thread(self._recognize, audio, sr)
        return {"text": normalize(raw), "raw": raw, "duration": round(len(audio) / sr, 2),
                "latency_s": round(time.time() - t0, 2)}


_asr: ASR | None = None


def get_asr() -> ASR:
    global _asr
    if _asr is None:
        _asr = ASR()
    return _asr
