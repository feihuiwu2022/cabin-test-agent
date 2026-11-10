"""进程内消息总线：后台服务 → 会话（WebSocket）。审计类消息同时写事件日志。"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol

from .store import get_store


class SessionSink(Protocol):
    id: str

    async def send(self, msg: dict) -> None: ...

    async def say(self, text: str, src: str = "") -> None: ...


class Bus:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionSink] = {}
        self.watchers: list[Callable[[str, dict], Awaitable[None]]] = []

    def attach(self, s: SessionSink) -> None:
        self.sessions[s.id] = s

    def detach(self, s: SessionSink) -> None:
        if self.sessions.get(s.id) is s:
            self.sessions.pop(s.id, None)

    async def publish(self, session: str, type_: str, data: dict[str, Any], log: bool = True) -> None:
        if log:
            get_store().append(session, type_, data)
        msg = {"type": type_, **data}
        s = self.sessions.get(session)
        if s:
            try:
                await s.send(msg)
            except Exception:
                pass
        for w in list(self.watchers):
            await w(session, msg)

    async def broadcast(self, type_: str, data: dict[str, Any]) -> None:
        for s in list(self.sessions.values()):
            try:
                await s.send({"type": type_, **data})
            except Exception:
                pass

    async def say(self, session: str, text: str, src: str = "") -> None:
        s = self.sessions.get(session)
        if s:
            await s.say(text, src)
        else:
            get_store().append(session, "assistant.message", {"text": text, "delivered": False})

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        return t


_tasks: set[asyncio.Task] = set()
bus = Bus()
