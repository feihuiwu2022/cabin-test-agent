"""证据：动作写入，评估器读取。格式对齐接口约定第 manifest.json。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

RE_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def redact(text: str) -> str:
    """日志脱敏：VIN、手机号（PF-EVD-05）。"""
    return RE_PHONE.sub("1**********", RE_VIN.sub("*****************", text))


class EvidenceFile(BaseModel):
    name: str
    kind: str  # screenshot / ui / log / audio / tts_clip / video
    step: int | None = None
    ts: float = Field(default_factory=time.time)
    sha256: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class Evidence:
    def __init__(self, root: Path, url_prefix: str, head: dict[str, Any]):
        self.root, self.url_prefix, self.head = root, url_prefix, head
        root.mkdir(parents=True, exist_ok=True)
        self.files: list[EvidenceFile] = []
        self.marks: dict[str, float] = {}
        self.facts: dict[str, Any] = {}
        self.log_text = ""

    def add(self, name: str, kind: str, data: bytes, step: int | None = None, **meta: Any) -> EvidenceFile:
        (self.root / name).write_bytes(data)
        f = EvidenceFile(name=name, kind=kind, step=step, sha256=hashlib.sha256(data).hexdigest(), meta=meta)
        self.files.append(f)
        return f

    def mark(self, event: str, at: float | None = None) -> None:
        """记录时间点（t0 / tts_start / tts_end），同一时钟。"""
        self.marks[event] = at if at is not None else time.time()

    def fact(self, key: str, value: Any) -> None:
        """记录检测事实（如 wake=no_response），是否算失败由评估器决定。"""
        self.facts[key] = value

    def add_log(self, text: str) -> None:
        if text:
            self.log_text += text + "\n"

    def find(self, kind: str, step: int | None = None) -> EvidenceFile | None:
        """取指定步骤的证据；step 给定时取该步或之前最近一份，否则取最新一份。"""
        hits = [f for f in self.files if f.kind == kind and (step is None or (f.step or 0) <= step)]
        return hits[-1] if hits else None

    def find_all(self, kind: str, step: int | None = None) -> list[EvidenceFile]:
        return [f for f in self.files if f.kind == kind and (step is None or (f.step or 0) <= step)]

    def path(self, f: EvidenceFile) -> Path:
        return self.root / f.name

    def url(self, f: EvidenceFile | str) -> str:
        return f"{self.url_prefix}/{f if isinstance(f, str) else f.name}"

    def read(self, f: EvidenceFile) -> bytes:
        return self.path(f).read_bytes()

    def read_json(self, f: EvidenceFile) -> Any:
        return json.loads(self.read(f))

    def close(self) -> dict:
        """收尾：写脱敏日志与 manifest，返回 manifest。"""
        if self.log_text:
            self.add("logcat.txt", "log", redact(self.log_text).encode("utf8"), redacted=True)
        m = {**self.head, "marks": self.marks, "facts": self.facts,
             "files": [{"name": f.name, "kind": f.kind, "step": f.step, "ts": f.ts, "sha256": f.sha256, **f.meta}
                       for f in self.files]}
        (self.root / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), "utf8")
        return m
