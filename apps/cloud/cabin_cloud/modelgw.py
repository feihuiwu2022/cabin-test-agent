"""cabin-modelgw：模型调用统一出口。

- 每次调用携带 data_label（public / internal / sensitive）、source（调用来源）并写审计事件。
- 对话和判定走 Anthropic 或 OpenAI 兼容接口，管理员在云端设置页配置，没配时用环境变量。
- strict_data 打开时，非 public 数据拒绝走外网。语音合成固定走 MiniMax。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse

import anthropic
import httpx
import httpx2

from . import modelopenai, vault
from .config import get_settings
from .store import get_store

log = logging.getLogger("cabin.modelgw")
DataLabel = Literal["public", "internal", "sensitive"]
SYSTEM_SESSION = "system"
THINKING_MIN_TOKENS = 8000  # 强制思考的模型，思考也占 max_tokens；预算过小时正文为空


class DemoQuotaError(RuntimeError):
    """公开演示：今天的模型调用额度用完了。"""


class DataPolicyError(RuntimeError):
    pass


def _http_client(timeout: float = 120) -> httpx.AsyncClient:
    # 本机代理对长连接不稳，默认直连；CABIN_USE_PROXY=1 时走环境代理
    return httpx.AsyncClient(timeout=timeout, trust_env=os.getenv("CABIN_USE_PROXY") == "1")


class ModelGateway:
    def __init__(self) -> None:
        self.s = get_settings()
        self.http = _http_client(60)
        self.transport: httpx.AsyncBaseTransport | None = None  # 测试替换
        self._thinking_required: set[str] = set()
        self._used: list = ["", 0]  # 公开演示：[日期, 当天调用次数]
        self.reload()

    def reload(self) -> None:
        """读取云端配置，保存后立即生效。"""
        s, c = self.s, get_store().get_kv("model")
        self.protocol = c.get("protocol", "anthropic")
        self.base_url = c.get("url") or s.llm_base_url
        self.chat_model = c.get("chat_model") or s.chat_model
        self.vision_model = c.get("vision_model") or s.vision_model
        self.api_key = s.llm_api_key
        self.route = f"external:{urlparse(self.base_url).hostname}"
        if c.get("secret"):
            try:
                self.api_key = vault.unseal(c["secret"])
            except vault.VaultError as e:  # 不影响云端启动，调用模型时才报错
                log.error("模型 Token 解不开：%s", e)
                self.api_key = ""
        self.client = anthropic.AsyncAnthropic(
            api_key=self.api_key, base_url=self.base_url, max_retries=2,
            http_client=httpx2.AsyncClient(timeout=180, trust_env=os.getenv("CABIN_USE_PROXY") == "1"),
        )

    # ---------- 审计 / 数据分级 ----------
    def _quota(self) -> None:
        """公开演示：每天调用总数有上限，防止密钥额度被刷光。"""
        if not self.s.demo:
            return
        day = time.strftime("%Y-%m-%d")
        if self._used[0] != day:
            self._used[:] = [day, 0]
        if self._used[1] >= self.s.demo_daily_calls:
            raise DemoQuotaError("今天的演示额度已用完，明天再来试试（或在本地用自己的模型密钥运行）")
        self._used[1] += 1

    def _check(self, label: DataLabel) -> str:
        self._quota()
        if label != "public" and self.s.strict_data:
            raise DataPolicyError(f"{label} 数据禁止走外网模型（CABIN_STRICT_DATA=1）")
        return self.route if label == "public" else f"{self.route}(DEV-ONLY:{label})"

    def _audit(self, session: str, kind: str, data: dict) -> None:
        get_store().append(session or SYSTEM_SESSION, kind, data)

    # ---------- 文本 / 工具调用（流式） ----------
    async def stream(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                     label: DataLabel, source: str, session: str, max_tokens: int = 2048,
                     model: str | None = None, think: bool = False) -> AsyncIterator[dict]:
        """yield {"type":"text","text":...} …，最后 yield {"type":"final","message":Message}。"""
        route = self._check(label)
        model = model or self.chat_model
        t0 = time.time()
        self._audit(session, "model.request", {"model": model, "route": route, "label": label, "source": source,
                                                "n_messages": len(messages), "tools": [t["name"] for t in tools or []]})
        if self.protocol == "openai":
            events = modelopenai.stream(self.base_url, self.api_key, model=model, system=system, messages=messages, tools=tools,
                                        max_tokens=max_tokens, transport=self.transport)
        else:
            events = self._anthropic(model, system, messages, tools, max_tokens, think)
        msg = None
        async for ev in events:
            if ev["type"] == "final":
                msg = ev["message"]
            else:
                yield ev
        self._audit(session, "model.response", {
            "model": model, "route": route, "source": source, "stop": msg.stop_reason,
            "in_tokens": msg.usage.input_tokens, "out_tokens": msg.usage.output_tokens,
            "latency_s": round(time.time() - t0, 2)})
        yield {"type": "final", "message": msg}

    async def _anthropic(self, model: str, system: str, messages: list[dict], tools: list[dict] | None, max_tokens: int,
                         think: bool) -> AsyncIterator[dict]:
        kwargs: dict[str, Any] = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
        if model in self._thinking_required:
            kwargs["max_tokens"] = max(max_tokens, THINKING_MIN_TOKENS)
        elif not think:  # 关闭思考链，降低首字时延（NFR-01）
            kwargs["thinking"] = {"type": "disabled"}
        try:
            async with self.client.messages.stream(**kwargs) as st:
                async for ev in st:
                    if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                        yield {"type": "text", "text": ev.delta.text}
                yield {"type": "final", "message": await st.get_final_message()}
        except anthropic.BadRequestError as e:
            # 部分模型（如 MiniMax-M3.1-Flash）强制自适应思考，不接受 disabled：记住该模型，去掉参数重试
            if "thinking" not in kwargs or "thinking" not in str(e):
                raise
            self._thinking_required.add(model)
            kwargs.pop("thinking")
            kwargs["max_tokens"] = max(max_tokens, THINKING_MIN_TOKENS)
            async with self.client.messages.stream(**kwargs) as st:
                async for ev in st:
                    if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                        yield {"type": "text", "text": ev.delta.text}
                yield {"type": "final", "message": await st.get_final_message()}

    async def complete(self, *, system: str, prompt: str | list, label: DataLabel, source: str,
                       session: str = SYSTEM_SESSION, max_tokens: int = 1024, model: str | None = None,
                       think: bool = False) -> str:
        content = prompt if isinstance(prompt, list) else [{"type": "text", "text": prompt}]
        final = None
        async for ev in self.stream(system=system, messages=[{"role": "user", "content": content}], label=label,
                                    source=source, session=session, max_tokens=max_tokens, model=model,
                                    think=think):
            if ev["type"] == "final":
                final = ev["message"]
        return "".join(b.text for b in final.content if b.type == "text").strip()

    async def vision_json(self, *, system: str, prompt: str, images: list[bytes], label: DataLabel, source: str,
                          session: str = SYSTEM_SESSION, max_tokens: int = 1500, think: bool = False) -> dict:
        content: list[dict] = []
        for img in images:
            content.append({"type": "image", "source": {"type": "base64", "media_type": _mime(img),
                                                         "data": base64.b64encode(img).decode()}})
        content.append({"type": "text", "text": prompt})
        last_err = ""
        for _ in range(2):
            text = await self.complete(system=system, prompt=content, label=label, source=source, session=session,
                                       max_tokens=max_tokens, model=self.vision_model, think=think)
            try:
                return extract_json(text)
            except ValueError as e:
                last_err = f"{e}: {text[:200]}"
        raise ValueError(f"VLM 输出无法解析为 JSON：{last_err}")

    # ---------- TTS ----------
    async def tts(self, text: str, session: str = SYSTEM_SESSION, fmt: str = "mp3", voice: str = "",
                  speed: float = 1.08, sample_rate: int = 32000) -> bytes:
        self._quota()
        body = {"model": self.s.tts_model, "text": text, "stream": False,
                "voice_setting": {"voice_id": voice or self.s.tts_voice, "speed": speed, "vol": 1, "pitch": 0},
                "audio_setting": {"sample_rate": sample_rate, "bitrate": 128000, "format": fmt, "channel": 1}}
        last: Exception | None = None
        for _ in range(3):
            try:
                r = await self.http.post(f"{self.s.minimax_api_base}/v1/t2a_v2", json=body,
                                         headers={"Authorization": f"Bearer {self.s.llm_api_key}"})
                j = r.json()
                if (j.get("base_resp") or {}).get("status_code") != 0:
                    raise RuntimeError(f"TTS 失败：{j.get('base_resp')}")
                return bytes.fromhex(j["data"]["audio"])
            except Exception as e:  # 网络抖动重试
                last = e
        raise RuntimeError(f"TTS 失败：{last}")


def _mime(img: bytes) -> str:
    if img[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


def extract_json(text: str) -> dict:
    """从模型输出中取第一个 JSON 对象（兼容 ```json 包裹）。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else None
    if cand is None:
        start = text.find("{")
        if start < 0:
            raise ValueError("无 JSON")
        depth = 0
        for i, ch in enumerate(text[start:], start):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                cand = text[start:i + 1]
                break
    if cand is None:
        raise ValueError("JSON 不完整")
    return json.loads(cand)


_gw: ModelGateway | None = None


def get_gateway() -> ModelGateway:
    global _gw
    if _gw is None:
        _gw = ModelGateway()
    return _gw
