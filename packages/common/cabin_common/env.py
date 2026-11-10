"""运行环境：加载 .env（已存在的环境变量优先，不覆盖）。云端和台架各读各的文件。"""
from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # 依赖缺失时退化为纯环境变量模式
        return
    load_dotenv(path, override=False)
