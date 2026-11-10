"""测试音频缓存：文件名即内容哈希。云端合成后存放，台架下载后回放，两端校验规则相同。"""
from __future__ import annotations

import hashlib
import io
import wave
from pathlib import Path


class AssetError(RuntimeError):
    pass


def asset_file(root: Path, sha: str) -> Path:
    return root / f"{sha}.wav"


def duration_ms(data: bytes) -> int:
    with wave.open(io.BytesIO(data)) as w:
        return int(w.getnframes() * 1000 / w.getframerate())


def missing(plan: dict, root: Path) -> list[str]:
    """没合成或文件不在的音频（含车型变体）。"""
    out = []
    for n, a in (plan.get("assets") or {}).items():
        a = a or {}
        if a.get("variants") is not None and not a.get("sha256"):
            shas = list(a["variants"].values())
            if not shas or any(not x or not asset_file(root, x).is_file() for x in shas):
                out.append(n)
        elif not a.get("sha256") or not asset_file(root, a["sha256"]).is_file():
            out.append(n)
    return out


def read_verified(root: Path, sha: str) -> bytes:
    """读音频并校验哈希。"""
    p = asset_file(root, sha)
    if not p.is_file():
        raise AssetError(f"音频不存在：{sha[:12]}")
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise AssetError(f"音频哈希不符：{sha[:12]}")
    return data
