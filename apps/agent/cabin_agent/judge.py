"""判定客户端的 HTTP 实现（A-10）：台架不持有模型凭证，判定都交给云端。"""
from __future__ import annotations

import json

from cabin_common.judging import VisionReq, VoiceReq

from .client import CloudClient


class HttpJudge:
    def __init__(self, client: CloudClient, run_id: str):
        self.client, self.run_id = client, run_id

    async def vision(self, req: VisionReq) -> dict:
        meta = {"run_id": self.run_id, "expected": req.expected, "screen": req.screen, "elements": req.elements,
                "region": req.region}
        files = [("images", (f"{i}.png", img, "image/png")) for i, img in enumerate(req.images)]
        return await self.client.call_retry("POST", "/api/v1/judge/vision", data={"meta": json.dumps(meta)}, files=files)

    async def voice(self, req: VoiceReq) -> dict:
        meta = {"run_id": self.run_id, "screen_expected": req.screen_expected, "tts_expected": req.tts_expected,
                "screen": req.screen, "elements": req.elements, "transcript": req.transcript, "no_tts": req.no_tts}
        files = [("image", ("shot.png", req.image, "image/png"))]
        if req.tts_audio:
            files.append(("tts_audio", ("tts.wav", req.tts_audio, "audio/wav")))
        return await self.client.call_retry("POST", "/api/v1/judge/voice", data={"meta": json.dumps(meta)}, files=files)
