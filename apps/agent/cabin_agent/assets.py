"""台架本地的测试音频与图标模板缓存。"""
from __future__ import annotations

from pathlib import Path

from cabin_common import audio, images
from cabin_common.audio import AssetError, duration_ms  # noqa: F401
from cabin_common.images import ImageAssetError  # noqa: F401

from .config import get_settings


def assets_dir() -> Path:
    d = get_settings().data_dir / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_path(sha: str) -> Path:
    return audio.asset_file(assets_dir(), sha)


def missing(plan: dict) -> list[str]:
    return audio.missing(plan, assets_dir())


def read_verified(sha: str) -> bytes:
    return audio.read_verified(assets_dir(), sha)


def image_path(sha: str) -> Path:
    return images.image_file(assets_dir(), sha)


def missing_images(plan: dict) -> list[str]:
    return images.missing(plan, assets_dir())


def read_image(sha: str) -> bytes:
    return images.read_verified(assets_dir(), sha)


def store_image(png: bytes) -> str:
    return images.store(assets_dir(), png)
