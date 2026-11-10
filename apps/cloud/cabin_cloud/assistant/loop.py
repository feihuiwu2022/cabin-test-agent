"""Agent Loop：调用模型 → 解析工具调用 → Hooks → 执行 → 回填 → 再调用，直到无工具调用或达到步数上限。

文本流按句回调（交给 TTS），被打断时由调用方取消本协程。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable

from ..modelgw import get_gateway
from ..store import get_store
from ..voice.tts import SentenceSplitter
from .hooks import HookDenied, post_tool, pre_tool
from .prompts import system_prompt
from .tools import ToolCtx, tools_for

MAX_STEPS = 6
TOOL_TIMEOUT_S = 40
DEGENERATE = re.compile(r"(.{1,6}?)(?:\s*\1){7,}\s*$", re.S)
SRC_RE = re.compile(r"^\s*[（(]?\s*(来源|出处)\s*[:：]\s*(.+?)[)）]?\s*$")

SentenceCb = Callable[[str], Awaitable[None]]


def _blocks(msg) -> list[dict]:
    out = []
    for b in msg.content:
        if b.type == "text" and b.text:
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


class AgentLoop:
    def __init__(self) -> None:
        self.gw = get_gateway()
        self.store = get_store()

    async def handle(self, ctx: ToolCtx, history: list[dict], on_sentence: SentenceCb,
                     on_tool: Callable[[str, dict], Awaitable[None]] | None = None) -> dict:
        tools = tools_for(ctx.authed)
        tool_schemas = [t.schema() for t in tools]
        system = system_prompt()
        msgs = history + [{"role": "user", "content": ctx.utterance}]
        start_len = len(history)
        self.store.append(ctx.session, "context.inject", {
            "tools": [t.name for t in tools], "history_messages": len(history)})
        label = "internal"
        full_text, src = [], ""
        splitter = SentenceSplitter()

        async def emit_sentence(s: str) -> None:
            nonlocal src
            m = SRC_RE.match(s)
            if m:
                src = m.group(2).strip("。. ")
                return
            full_text.append(s)
            await on_sentence(s)

        retried = False
        final_reply = ""
        step = 0
        while step < MAX_STEPS:
            step += 1
            final, degenerate, seen = None, False, ""
            async for ev in self.gw.stream(system=system, messages=msgs, tools=tool_schemas or None, label=label,
                                           source="agent.lab", session=ctx.session, max_tokens=800,
                                           think=retried):
                if ev["type"] == "text":
                    seen += ev["text"]
                    if DEGENERATE.search(seen[-80:]):  # 输出退化（重复片段）：中止本次生成
                        degenerate = True
                        break
                    for s in splitter.feed(ev["text"]):
                        await emit_sentence(s)
                else:
                    final = ev["message"]
            if degenerate:
                splitter.buf = ""
                self.store.append(ctx.session, "model.degenerate", {"tail": seen[-80:], "retried": retried})
                if not retried:
                    retried = True
                    step -= 1
                    continue
                await emit_sentence("抱歉，我这边没组织好语言，请再说一遍。")
                msgs.append({"role": "assistant", "content": [{"type": "text", "text": "抱歉，请再说一遍。"}]})
                break
            for s in splitter.flush():
                await emit_sentence(s)
            blocks = _blocks(final)
            msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "好的。"}]})
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if final.stop_reason != "tool_use" or not calls:
                break
            results = []
            for c in calls:
                t0 = time.time()
                self.store.append(ctx.session, "tool.call", {"tool": c["name"], "args": c["input"]})
                if on_tool:
                    await on_tool(c["name"], c["input"])
                err = ""
                try:
                    tool, args = pre_tool(ctx, c["name"], c["input"])
                    res = await asyncio.wait_for(tool.handler(ctx, args), TOOL_TIMEOUT_S)
                except HookDenied as e:
                    res, err = {"ok": False, "denied": str(e)}, "denied"
                except asyncio.TimeoutError:
                    res, err = {"ok": False, "error": "工具超时"}, "timeout"
                except Exception as e:
                    res, err = {"ok": False, "error": f"{type(e).__name__}: {e}"}, "error"
                final_reply = final_reply or res.pop("final_reply", "")
                content = post_tool(ctx, c["name"], c["input"], res, round(time.time() - t0, 2), err)
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": content,
                                **({"is_error": True} if err else {})})
            msgs.append({"role": "user", "content": results})
            if final_reply:  # 工具声明了固定回复（如计划复述）：直接播报，结束本回合
                await emit_sentence(final_reply)
                msgs.append({"role": "assistant", "content": [{"type": "text", "text": final_reply}]})
                break
        text = "".join(full_text).strip()
        self.store.append(ctx.session, "assistant.message", {"text": text, "src": src})
        return {"text": text, "src": src, "messages": msgs[start_len:]}
