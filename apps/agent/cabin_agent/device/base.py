"""cabin-device：设备抽象层。上层只依赖 DeviceAdapter。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class Unsupported(RuntimeError):
    """适配器不支持该方法（如 QNX 无控件树），Runner 据此跳过或降级。"""


class Health(BaseModel):
    online: bool
    ota: str = ""
    model: str = ""
    build_type: str = ""
    storage_ok: bool = True
    packages: list[str] = []
    detail: str = ""


class UIElement(BaseModel):
    id: int
    text: str = ""
    desc: str = ""
    key: str = ""
    cls: str = ""
    clickable: bool = False
    checked: bool | None = None
    bounds: list[int]  # [l, t, r, b]
    truncated: bool = False

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    def brief(self) -> str:
        s = f"[{self.id}] {self.text or self.key}"
        if self.desc:
            s += f"（说明：{self.desc}）"
        if self.checked is not None:
            s += " 开" if self.checked else " 关"
        s += f" @{self.bounds}"
        return s


class DeviceAdapter(ABC):
    domain: str = "android"
    name: str = "base"
    voice_capable: bool = False  # 能模拟车机语音（SimCar）；真实台架走音箱 + 麦克风

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    @abstractmethod
    async def health(self) -> Health: ...

    @abstractmethod
    async def shell(self, cmd: str) -> str: ...

    @abstractmethod
    async def screenshot(self, fmt: str = "png") -> bytes: ...

    @abstractmethod
    async def dump_ui(self) -> list[UIElement]: ...

    @abstractmethod
    async def tap(self, x: int, y: int) -> None: ...

    @abstractmethod
    async def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None: ...

    @abstractmethod
    async def key(self, name: str) -> None:
        """name: back / home / enter"""

    @abstractmethod
    async def text(self, s: str) -> None: ...

    @abstractmethod
    async def logs(self, cursor: Any = None) -> tuple[str, Any]:
        """增量读日志：返回 (新增文本, 新游标)。"""

    async def current_screen(self) -> str:
        return ""

    async def voice_wake(self, text: str) -> dict:
        raise Unsupported("voice_wake")

    async def voice_say(self, text: str) -> dict:
        raise Unsupported("voice_say")

    async def pull(self, remote: str) -> bytes:
        raise Unsupported("pull")

    async def reset(self) -> None:
        """任务间复位：回到桌面。"""
        await self.key("home")

    async def screen_size(self) -> tuple[int, int]:
        return 1280, 560
