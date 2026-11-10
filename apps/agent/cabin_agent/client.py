"""云端接口客户端：包络解析、错误分类、退避重试。"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import httpx

API_VERSION = "1"
BACKOFF_S = (1, 2, 4, 8, 30)


class CloudDown(RuntimeError):
    """云端暂时不可用（网络错误、5xx、429）：可重试，事件和结果留在本地。"""


class CloudRejected(RuntimeError):
    """云端明确拒绝（4xx）：不重试。"""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code} {message}")
        self.code = code


class CloudClient:
    def __init__(self, base_url: str, token: str = "", transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 30, backoff: tuple[float, ...] = BACKOFF_S):
        self.token, self.backoff = token, backoff
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                      trust_env=os.getenv("CABIN_USE_PROXY") == "1")

    async def close(self) -> None:
        await self.http.aclose()

    async def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        headers = {"X-Cabin-Api": API_VERSION, "X-Request-Id": str(uuid.uuid4()), **kw.pop("headers", {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = await self.http.request(method, url, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise CloudDown(f"{type(e).__name__}: {e}") from e
        if r.status_code >= 500 or r.status_code == 429:
            raise CloudDown(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            try:
                j = r.json()
                raise CloudRejected(int(j.get("code", r.status_code)), str(j.get("message", "")))
            except ValueError:
                raise CloudRejected(r.status_code, r.text[:200]) from None
        return r

    async def call(self, method: str, url: str, **kw: Any) -> dict | None:
        """一次请求。204 返回 None，其余返回包络里的 data。"""
        r = await self._send(method, url, **kw)
        return None if r.status_code == 204 else r.json().get("data")

    async def call_retry(self, method: str, url: str, **kw: Any) -> dict | None:
        """按退避表重试完仍失败则抛 CloudDown。"""
        for wait in self.backoff:
            try:
                return await self.call(method, url, **kw)
            except CloudDown:
                await asyncio.sleep(wait)
        return await self.call(method, url, **kw)

    async def download(self, url: str) -> bytes:
        return (await self._send("GET", url)).content
