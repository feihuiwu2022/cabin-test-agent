"""崩溃与无响应规则：台架本地判定用，云端提单时复用。"""
from __future__ import annotations

import re

RULES = [
    ("J-02", "Java 崩溃", re.compile(r"FATAL EXCEPTION.*?\n.*?Process: (?P<pkg>[\w.]+)", re.S)),
    ("J-03", "应用无响应", re.compile(r"ANR in (?P<pkg>[\w.]+)")),
]


def crash_frames(log: str, n: int = 3) -> str:
    frames = re.findall(r"\tat ([\w.$]+)\(", log)
    return ">".join(frames[:n])
