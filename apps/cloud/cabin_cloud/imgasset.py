"""云端图像资产（图标模板、UI 地图截图）：和测试音频同一目录，按内容哈希存放。"""
from __future__ import annotations

from cabin_common import images
from cabin_common.images import ImageAssetError  # noqa: F401

from .audioasset import assets_dir


def store(png: bytes) -> str:
    return images.store(assets_dir(), png)


def read_verified(sha: str) -> bytes:
    return images.read_verified(assets_dir(), sha)


def missing(plan: dict) -> list[str]:
    return images.missing(plan, assets_dir())


def exists(sha: str) -> bool:
    return bool(sha) and images.image_file(assets_dir(), sha).is_file()
