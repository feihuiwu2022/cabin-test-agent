"""OpenAI 兼容适配：网关内部统一用 Anthropic 风格的消息，这里转成 /chat/completions，再转回同样形状的结果。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

STOP_REASONS = {"length": "max_tokens", "tool_calls": "tool_use"}


class ModelError(RuntimeError):
    pass


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Reply:
    content: list[Block]
    stop_reason: str
    usage: Usage


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _image(b: dict) -> dict:
    src = b["source"]
    return {"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}}


def _message(m: dict) -> list[dict]:
    """一条消息可能拆成多条：工具结果在 OpenAI 里是独立的 tool 消息。"""
    role, content = m["role"], m["content"]
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    out = [{"role": "tool", "tool_call_id": b["tool_use_id"], "content": _text(b["content"])}
           for b in content if b["type"] == "tool_result"]
    calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
             for b in content if b["type"] == "tool_use"]
    parts = [{"type": "text", "text": b["text"]} if b["type"] == "text" else _image(b) for b in content if b["type"] in ("text", "image")]
    if calls:
        out.append({"role": role, "content": "".join(p["text"] for p in parts) or None, "tool_calls": calls})
    elif parts:
        out.append({"role": role, "content": parts[0]["text"] if len(parts) == 1 and parts[0]["type"] == "text" else parts})
    return out


def to_messages(system: str, messages: list[dict]) -> list[dict]:
    head = [{"role": "system", "content": system}] if system else []
    return head + [x for m in messages for x in _message(m)]


def to_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
            for t in tools]


def _reply(text: str, calls: dict[int, dict], finish: str, usage: dict) -> Reply:
    content = [Block("text", text=text)] if text else []
    for _, c in sorted(calls.items()):
        try:
            args = json.loads(c["arguments"] or "{}")
        except ValueError:  # 模型给了坏参数：交给工具校验报错，让它重试
            args = {}
        content.append(Block("tool_use", id=c["id"], name=c["name"], input=args))
    # 有的服务带工具调用时 finish_reason 仍是 stop
    stop = "tool_use" if calls else STOP_REASONS.get(finish, "end_turn")
    return Reply(content, stop, Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))


async def _post(c: httpx.AsyncClient, url: str, key: str, body: dict) -> httpx.Response:
    req = c.build_request("POST", f"{url.rstrip('/')}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})
    return await c.send(req, stream=True)


async def stream(url: str, key: str, *, model: str, system: str, messages: list[dict], tools: list[dict] | None,
                 max_tokens: int, transport: httpx.AsyncBaseTransport | None = None) -> AsyncIterator[dict]:
    """事件和 Anthropic 分支相同：{"type":"text","text":...}，最后 {"type":"final","message":Reply}。"""
    body: dict[str, Any] = {"model": model, "messages": to_messages(system, messages), "max_tokens": max_tokens, "stream": True}
    if tools:
        body["tools"] = to_tools(tools)
    text, calls, finish, usage = "", {}, "", {}
    async with httpx.AsyncClient(timeout=180, transport=transport, trust_env=os.getenv("CABIN_USE_PROXY") == "1") as c:
        r = await _post(c, url, key, body)
        if r.status_code == 400 and "max_completion_tokens" in (await r.aread()).decode("utf8", "replace"):  # 新模型换了参数名
            await r.aclose()
            body["max_completion_tokens"] = body.pop("max_tokens")
            r = await _post(c, url, key, body)
        try:
            if r.status_code != 200:
                raise ModelError(f"模型接口返回 {r.status_code}：{(await r.aread()).decode('utf8', 'replace')[:200]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                d = json.loads(line[5:])
                usage = d.get("usage") or usage
                for ch in d.get("choices") or []:
                    delta = ch.get("delta") or {}
                    finish = ch.get("finish_reason") or finish
                    if delta.get("content"):
                        text += delta["content"]
                        yield {"type": "text", "text": delta["content"]}
                    for t in delta.get("tool_calls") or []:
                        cur = calls.setdefault(t.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        fn = t.get("function") or {}
                        cur["id"] = t.get("id") or cur["id"]
                        cur["name"] = fn.get("name") or cur["name"]
                        cur["arguments"] += fn.get("arguments") or ""
        finally:
            await r.aclose()
    yield {"type": "final", "message": _reply(text, calls, finish, usage)}
