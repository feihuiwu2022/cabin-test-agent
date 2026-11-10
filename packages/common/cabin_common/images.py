"""图像资产（图标模板、UI 地图截图）：文件名即内容哈希。云端存放，台架下载后校验哈希才用，两端规则相同。

TaskPlan 里写 images: {图名: {sha256}}，步骤 target.image 引用图名。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


class ImageAssetError(RuntimeError):
    pass


def image_file(root: Path, sha: str) -> Path:
    return root / f"{sha}.png"


def store(root: Path, png: bytes) -> str:
    sha = hashlib.sha256(png).hexdigest()
    p = image_file(root, sha)
    if not p.is_file():
        p.write_bytes(png)
    return sha


def read_verified(root: Path, sha: str) -> bytes:
    p = image_file(root, sha or "")
    if not sha or not p.is_file():
        raise ImageAssetError(f"图像资产 {str(sha)[:12]} 不存在")
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise ImageAssetError(f"图像资产 {sha[:12]} 哈希不符")
    return data


def missing(plan: dict, root: Path) -> list[str]:
    return [n for n, a in (plan.get("images") or {}).items()
            if not image_file(root, (a or {}).get("sha256") or "").is_file()]
