"""证据目录清理：云端和台架都不长期存证据。一个执行单元一个子目录，按时间和总量删除。"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

DAY_S = 24 * 3600


def prune(root: Path, keep_days: float, max_bytes: int, keep: set[str] = frozenset()) -> list[str]:
    """删过期的目录；总量还超限时从最旧的删起。keep 里的目录不动。返回删掉的目录名。"""
    if not root.is_dir():
        return []
    dirs = []
    for d in root.iterdir():
        if d.is_dir() and d.name not in keep:
            dirs.append((d.stat().st_mtime, sum(f.stat().st_size for f in d.rglob("*") if f.is_file()), d))
    dirs.sort(key=lambda x: x[0])
    total = sum(x[1] for x in dirs)
    cutoff, gone = time.time() - keep_days * DAY_S, []
    for mtime, size, d in dirs:
        if mtime < cutoff or total > max_bytes:
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            gone.append(d.name)
    return gone
