"""离线缓存（PF-UP-04）：事件、证据、结果先落本地，再按写入顺序补传，断网和重启走同一条路。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Outbox:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, body TEXT)")
        self.db.commit()

    def put(self, kind: str, body: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO outbox(kind, body) VALUES(?,?)", (kind, json.dumps(body, ensure_ascii=False)))
        self.db.commit()

    def head(self) -> tuple[int, str, dict] | None:
        row = self.db.execute("SELECT id, kind, body FROM outbox ORDER BY id LIMIT 1").fetchone()
        return (row[0], row[1], json.loads(row[2])) if row else None

    def done(self, item_id: int) -> None:
        self.db.execute("DELETE FROM outbox WHERE id=?", (item_id,))
        self.db.commit()

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT kind, COUNT(*) FROM outbox GROUP BY kind").fetchall())

    def close(self) -> None:
        self.db.close()
