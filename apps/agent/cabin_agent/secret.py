"""台架本机令牌的保存（台架令牌、本地页面登录令牌）。

Windows：用 DPAPI（CryptProtectData，当前用户范围）加密后写盘，拷到别的机器或别的账户解不开。
其它系统：明文 + 权限 600。读的时候兼容旧的明文文件，下次保存时自动换成加密格式。
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

PREFIX = "dpapi:"
ENTROPY = b"cabin-agent"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    src, ent, out = blob(data), blob(ENTROPY), Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 两个函数的参数顺序相同：输入、描述、附加熵、保留、提示、标志（0x1 不弹界面）、输出
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
        raise OSError(f"DPAPI {'加密' if protect else '解密'}失败：{ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protected() -> bool:
    return sys.platform == "win32"


def save(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if protected():
        path.write_text(PREFIX + base64.b64encode(_dpapi(value.encode("utf8"), True)).decode(), "utf8")
    else:
        path.write_text(value, "utf8")
        os.chmod(path, 0o600)


def load(path: Path) -> str:
    """读不到、解不开（换了机器或账户）时返回空串，台架会按未注册处理。"""
    if not path.is_file():
        return ""
    text = path.read_text("utf8").strip()
    if not text.startswith(PREFIX):
        return text  # 旧的明文文件
    if not protected():
        return ""
    try:
        return _dpapi(base64.b64decode(text[len(PREFIX):]), False).decode("utf8")
    except (OSError, ValueError):
        return ""
