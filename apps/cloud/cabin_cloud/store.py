"""本地结果库 + 事件日志 + 本地 Jira 沙箱。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, type TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session, id);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, session TEXT, status TEXT, plan TEXT, result TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS defects(
  id TEXT PRIMARY KEY, task_id TEXT, fingerprint TEXT, ota TEXT, status TEXT, doc TEXT, created REAL);
CREATE TABLE IF NOT EXISTS review_memory(
  fingerprint TEXT, ota TEXT, action TEXT, issue_key TEXT, reason TEXT, by TEXT, at REAL, hits INTEGER DEFAULT 1,
  PRIMARY KEY(fingerprint, ota));
CREATE TABLE IF NOT EXISTS eval_set(
  id INTEGER PRIMARY KEY AUTOINCREMENT, defect_id TEXT, rule TEXT, reason TEXT, doc TEXT, at REAL);
CREATE TABLE IF NOT EXISTS jira_sandbox(
  key TEXT PRIMARY KEY, project TEXT, summary TEXT, description TEXT, fingerprint TEXT,
  found_version TEXT, affects TEXT, status TEXT, severity TEXT, labels TEXT, comments TEXT,
  attachments TEXT, dup_of TEXT, created REAL);
CREATE TABLE IF NOT EXISTS case_batch(
  id TEXT PRIMARY KEY, session TEXT, status TEXT, doc TEXT, created REAL);
CREATE TABLE IF NOT EXISTS test_case(
  case_id TEXT PRIMARY KEY, batch_id TEXT, module TEXT, review_status TEXT, plan_status TEXT, doc TEXT, updated REAL);
CREATE INDEX IF NOT EXISTS ix_test_case_batch ON test_case(batch_id);
CREATE TABLE IF NOT EXISTS case_review(
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, batch_id TEXT, decision TEXT, reason TEXT, diff TEXT,
  reviewer TEXT, at REAL);
CREATE TABLE IF NOT EXISTS case_seq(code TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS case_rev(
  case_id TEXT, rev INTEGER, doc TEXT, updated REAL, PRIMARY KEY(case_id, rev));
CREATE TABLE IF NOT EXISTS prd_version(
  id TEXT PRIMARY KEY, prd_id TEXT, version TEXT, status TEXT, doc TEXT, created REAL);
CREATE INDEX IF NOT EXISTS ix_prd_version_prd ON prd_version(prd_id, created);
CREATE TABLE IF NOT EXISTS prd_source(source TEXT PRIMARY KEY, prd_id TEXT, bound REAL);
CREATE TABLE IF NOT EXISTS baseline(
  id TEXT PRIMARY KEY, prd_id TEXT, version TEXT, cases TEXT, published REAL);
CREATE TABLE IF NOT EXISTS audio_asset(
  key TEXT PRIMARY KEY, sha256 TEXT, text TEXT, voice TEXT, speed REAL, model TEXT, size INTEGER, duration_ms INTEGER,
  created REAL);
CREATE TABLE IF NOT EXISTS rig(
  id TEXT PRIMARY KEY, token_hash TEXT, state TEXT, model TEXT, ota TEXT, capabilities TEXT, doc TEXT,
  status TEXT, last_seen REAL);
CREATE UNIQUE INDEX IF NOT EXISTS ix_rig_token ON rig(token_hash);
CREATE TABLE IF NOT EXISTS run(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, task_id TEXT, session TEXT, rig_id TEXT, case_id TEXT,
  plan_version TEXT, kind TEXT, status TEXT, attempt INTEGER DEFAULT 0, acked_seq INTEGER DEFAULT 0, timeout_s INTEGER,
  plan TEXT, case_doc TEXT, result TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS ix_run_claim ON run(rig_id, status, seq);
CREATE TABLE IF NOT EXISTS rig_caseset(rig_id TEXT PRIMARY KEY, case_ids TEXT, updated_by TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS regression(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, source TEXT, by_user TEXT, skipped TEXT DEFAULT '[]', created REAL);
CREATE TABLE IF NOT EXISTS rig_schedule(
  rig_id TEXT PRIMARY KEY, at TEXT, enabled INTEGER, last_date TEXT, updated_by TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS user_profile(user_id TEXT PRIMARY KEY, name TEXT, email TEXT, jira_user TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS user_session(token_hash TEXT PRIMARY KEY, user_id TEXT, name TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS auth_item(key TEXT PRIMARY KEY, value TEXT, expires REAL);
"""


CASELIB_SCHEMA = 2


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()
        self._add_columns()
        self.db.commit()
        self._listeners: list[Callable[[dict], None]] = []

    def _migrate(self) -> None:
        """用例库结构版本。v2（用例管理：修订 / PRD 版本 / 基线 / 车型字典）与旧数据不兼容，旧用例库数据整体清除。"""
        ver = self.db.execute("PRAGMA user_version").fetchone()[0]
        if ver < CASELIB_SCHEMA:
            for t in ("case_batch", "test_case", "case_review", "case_seq", "case_rev", "prd_version", "baseline"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute(f"PRAGMA user_version = {CASELIB_SCHEMA}")

    def _add_columns(self) -> None:
        """CREATE TABLE IF NOT EXISTS 不会给旧表加列。"""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(run)")}
        for col, typ in (("owner", "TEXT"), ("model", "TEXT"), ("ota", "TEXT"), ("module", "TEXT"), ("verdict", "TEXT"),
                         ("block_reason", "TEXT"), ("duration_s", "REAL"), ("regression_id", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE run ADD COLUMN {col} {typ} DEFAULT {'0' if typ == 'REAL' else repr('')}")

    # ---------- 通用 ----------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid or 0

    # ---------- 台架（A-01 / A-02） ----------
    def rig(self, rig_id: str) -> dict | None:
        rows = self.q("SELECT * FROM rig WHERE id=?", (rig_id,))
        return dict(rows[0]) if rows else None

    def rig_by_token(self, h: str) -> dict | None:
        rows = self.q("SELECT * FROM rig WHERE token_hash=?", (h,))
        return dict(rows[0]) if rows else None

    def rigs(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM rig ORDER BY id")]

    def save_rig(self, rig_id: str, token_hash: str, model: str, ota: str, capabilities: list[str], doc: dict) -> None:
        self.x("INSERT INTO rig(id, token_hash, state, model, ota, capabilities, doc, status, last_seen) "
               "VALUES(?,?,'enabled',?,?,?,?,'idle',?) ON CONFLICT(id) DO UPDATE SET model=excluded.model, "
               "ota=excluded.ota, capabilities=excluded.capabilities, doc=excluded.doc, last_seen=excluded.last_seen",
               (rig_id, token_hash, model, ota, json.dumps(capabilities), json.dumps(doc, ensure_ascii=False), time.time()))

    def touch_rig(self, rig_id: str, status: str, ota: str, capabilities: list[str] | None, outbox: dict) -> None:
        r = self.rig(rig_id) or {}
        doc = {**json.loads(r.get("doc") or "{}"), "outbox": outbox}
        self.x("UPDATE rig SET status=?, ota=?, capabilities=?, doc=?, last_seen=? WHERE id=?",
               (status, ota or r.get("ota", ""), json.dumps(capabilities) if capabilities is not None else r.get("capabilities"),
                json.dumps(doc, ensure_ascii=False), time.time(), rig_id))

    # ---------- 执行单元（A-03 任务池） ----------
    def create_run(self, task_id: str, session: str, rig_id: str, case_id: str, plan_version: str, kind: str,
                   timeout_s: int, plan: dict, case_doc: dict, owner: str = "", regression_id: str = "") -> str:
        now = time.time()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO run(task_id, session, rig_id, case_id, plan_version, kind, status, timeout_s, plan, case_doc, "
                "owner, regression_id, created, updated) VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
                (task_id, session, rig_id, case_id, plan_version, kind, timeout_s, json.dumps(plan, ensure_ascii=False),
                 json.dumps(case_doc, ensure_ascii=False), owner, regression_id, now, now))
            run_id = f"R{time.strftime('%Y%m%d', time.localtime(now))}-{cur.lastrowid:06d}"
            self.db.execute("UPDATE run SET id=? WHERE seq=?", (run_id, cur.lastrowid))
            self.db.commit()
        return run_id

    def run(self, run_id: str) -> dict | None:
        rows = self.q("SELECT * FROM run WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def claim_run(self, rig_id: str) -> dict | None:
        """领取最早排队的执行单元。条件更新保证同一执行单元只被领取一次。"""
        with self._lock:
            row = self.db.execute("SELECT id FROM run WHERE rig_id=? AND status='queued' ORDER BY seq LIMIT 1",
                                  (rig_id,)).fetchone()
            if not row:
                return None
            cur = self.db.execute("UPDATE run SET status='running', attempt=attempt+1, updated=? WHERE id=? AND status='queued'",
                                  (time.time(), row["id"]))
            self.db.commit()
            return self.run(row["id"]) if cur.rowcount == 1 else None

    def ack_run_seq(self, run_id: str, seq: int) -> None:
        self.x("UPDATE run SET acked_seq=?, updated=? WHERE id=?", (seq, time.time(), run_id))

    def finish_run(self, run_id: str, result: str, summary: dict | None = None) -> bool:
        """写入结果和统计字段；已有结果返回 False（A-07 幂等）。取消中的执行单元照常收结果。"""
        s = summary or {}
        with self._lock:
            cur = self.db.execute(
                "UPDATE run SET status='done', result=?, updated=?, model=?, ota=?, module=?, verdict=?, block_reason=?, "
                "duration_s=? WHERE id=? AND result IS NULL",
                (result, time.time(), s.get("model", ""), s.get("ota", ""), s.get("module", ""), s.get("verdict", ""),
                 s.get("block_reason", ""), s.get("duration_s", 0), run_id))
            self.db.commit()
            return cur.rowcount == 1

    def cancel_run(self, run_id: str) -> None:
        """未领取的直接取消；已领取的置为取消中，由下一次心跳下发 cancel_run。"""
        self.x("UPDATE run SET status=CASE status WHEN 'queued' THEN 'cancelled' ELSE 'cancelling' END, updated=? "
               "WHERE id=? AND status IN ('queued','running')", (time.time(), run_id))

    def runs_to_cancel(self, rig_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT id FROM run WHERE rig_id=? AND status='cancelling'", (rig_id,))]

    def unfinished_runs(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM run WHERE status IN ('queued','running') ORDER BY seq")]

    def mark_flaky(self, run_id: str) -> None:
        """复测没有复现：这次失败不算失败，也不算通过，单独统计。"""
        self.x("UPDATE run SET verdict='flaky', updated=? WHERE id=?", (time.time(), run_id))

    # ---------- 回归批次与定时 ----------
    def create_regression(self, source: str, by: str) -> str:
        now = time.time()
        with self._lock:
            cur = self.db.execute("INSERT INTO regression(source, by_user, created) VALUES(?,?,?)", (source, by, now))
            rid = f"RG{time.strftime('%Y%m%d', time.localtime(now))}-{cur.lastrowid:04d}"
            self.db.execute("UPDATE regression SET id=? WHERE seq=?", (rid, cur.lastrowid))
            self.db.commit()
        return rid

    def set_regression_skipped(self, regression_id: str, skipped: list[dict]) -> None:
        self.x("UPDATE regression SET skipped=? WHERE id=?", (json.dumps(skipped, ensure_ascii=False), regression_id))

    def regressions(self, limit: int = 20, since: float = 0) -> list[dict]:
        """批次和它的进度。复测不算进批次的用例数。"""
        rows = self.q(
            "SELECT g.id, g.source, g.by_user, g.skipped, g.created, COUNT(r.seq) AS total, "
            "COALESCE(SUM(r.verdict='passed'),0) AS passed, COALESCE(SUM(r.verdict='failed'),0) AS failed, "
            "COALESCE(SUM(r.verdict='blocked'),0) AS blocked, COALESCE(SUM(r.verdict='flaky'),0) AS flaky, "
            "COALESCE(SUM(r.status IN ('queued','running','cancelling')),0) AS active "
            "FROM regression g LEFT JOIN run r ON r.regression_id=g.id AND r.kind='replay' "
            "WHERE g.created>=? GROUP BY g.seq ORDER BY g.seq DESC LIMIT ?", (since, limit))
        return [{**dict(r), "skipped": json.loads(r["skipped"])} for r in rows]

    def batch_verdicts(self, since: float) -> list[dict]:
        return [dict(r) for r in self.q(
            "SELECT r.model, r.ota, r.verdict, COUNT(*) AS n FROM run r JOIN regression g ON r.regression_id=g.id "
            "WHERE g.created>=? AND r.kind='replay' AND r.verdict!='' GROUP BY r.model, r.ota, r.verdict", (since,))]

    def schedule(self, rig_id: str) -> dict | None:
        rows = self.q("SELECT * FROM rig_schedule WHERE rig_id=?", (rig_id,))
        return dict(rows[0]) if rows else None

    def schedules(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM rig_schedule WHERE enabled=1 ORDER BY rig_id")]

    def save_schedule(self, rig_id: str, at: str, enabled: bool, last_date: str, by: str) -> None:
        self.x("INSERT INTO rig_schedule VALUES(?,?,?,?,?,?) ON CONFLICT(rig_id) DO UPDATE SET at=excluded.at, "
               "enabled=excluded.enabled, last_date=excluded.last_date, updated_by=excluded.updated_by, updated=excluded.updated",
               (rig_id, at, int(enabled), last_date, by, time.time()))

    def mark_schedule(self, rig_id: str, date: str) -> None:
        self.x("UPDATE rig_schedule SET last_date=? WHERE rig_id=?", (date, rig_id))

    def get_kv(self, key: str) -> dict:
        rows = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else {}

    def set_kv(self, key: str, value: dict) -> None:
        self.x("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))

    def save_user_profile(self, user_id: str, name: str, email: str) -> None:
        self.x("INSERT INTO user_profile(user_id, name, email) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
               "name=excluded.name, email=excluded.email", (user_id, name, email))

    def user_profile(self, name: str) -> dict:
        rows = self.q("SELECT * FROM user_profile WHERE name=?", (name,))
        return dict(rows[0]) if rows else {}

    def set_jira_user(self, name: str, jira_user: str) -> None:
        self.x("UPDATE user_profile SET jira_user=? WHERE name=?", (jira_user, name))

    def rig_caseset(self, rig_id: str) -> list[str]:
        rows = self.q("SELECT case_ids FROM rig_caseset WHERE rig_id=?", (rig_id,))
        return json.loads(rows[0]["case_ids"]) if rows else []

    def save_rig_caseset(self, rig_id: str, case_ids: list[str], by: str) -> None:
        self.x("INSERT INTO rig_caseset VALUES(?,?,?,?) ON CONFLICT(rig_id) DO UPDATE SET case_ids=excluded.case_ids, "
               "updated_by=excluded.updated_by, updated=excluded.updated", (rig_id, json.dumps(case_ids), by, time.time()))

    def queued_runs(self, rig_id: str) -> int:
        """台架上还没跑完的执行单元数。"""
        return self.q("SELECT COUNT(*) AS n FROM run WHERE rig_id=? AND status IN ('queued','running','cancelling')",
                      (rig_id,))[0]["n"]

    def latest_results(self) -> list[dict]:
        """每个 版本 + 车型 + 用例 最新一次的结论。试跑验证的是测试代码，复测的结论记在原执行单元上，都不单独计入。"""
        return [dict(r) for r in self.q(
            "SELECT ota, model, module, case_id, verdict FROM run WHERE seq IN "
            "(SELECT MAX(seq) FROM run WHERE verdict != '' AND kind = 'replay' GROUP BY ota, model, case_id)")]

    def latest_by_case(self) -> list[dict]:
        """覆盖矩阵用：每个 车型 + 用例 最新一次的结论，不分版本。"""
        return [dict(r) for r in self.q(
            "SELECT model, case_id, verdict, ota FROM run WHERE seq IN "
            "(SELECT MAX(seq) FROM run WHERE verdict != '' AND kind = 'replay' GROUP BY model, case_id)")]

    def regression_runs(self, regression_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM run WHERE regression_id=? ORDER BY seq", (regression_id,))]

    def recent_runs(self, limit: int) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM run ORDER BY seq DESC LIMIT ?", (limit,))]

    # ---------- 登录 ----------
    def save_user_session(self, token_hash: str, user_id: str, name: str, expires: float) -> None:
        self.x("INSERT INTO user_session VALUES(?,?,?,?)", (token_hash, user_id, name, expires))

    def user_session(self, token_hash: str) -> dict | None:
        rows = self.q("SELECT * FROM user_session WHERE token_hash=? AND expires>?", (token_hash, time.time()))
        return dict(rows[0]) if rows else None

    def delete_user_session(self, token_hash: str) -> None:
        self.x("DELETE FROM user_session WHERE token_hash=?", (token_hash,))

    def put_auth_item(self, key: str, value: str, expires: float) -> None:
        self.x("INSERT INTO auth_item VALUES(?,?,?)", (key, value, expires))

    def take_auth_item(self, key: str) -> str | None:
        """登录 state 与一次性票据：取出即删除，过期视同不存在。"""
        with self._lock:
            row = self.db.execute("SELECT value, expires FROM auth_item WHERE key=?", (key,)).fetchone()
            self.db.execute("DELETE FROM auth_item WHERE key=? OR expires<?", (key, time.time()))
            self.db.commit()
        return row["value"] if row and row["expires"] > time.time() else None

    # ---------- 事件日志（追加写，唯一事实源 ADR-06） ----------
    def on_event(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def append(self, session: str, type_: str, data: dict[str, Any] | None = None) -> dict:
        ev = {"session": session, "ts": time.time(), "type": type_, "data": data or {}}
        ev["id"] = self.x(
            "INSERT INTO events(session, ts, type, data) VALUES(?,?,?,?)",
            (session, ev["ts"], type_, json.dumps(ev["data"], ensure_ascii=False, default=str)),
        )
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def replay(self, session: str) -> list[dict]:
        return [
            {"id": r["id"], "ts": r["ts"], "type": r["type"], "data": json.loads(r["data"])}
            for r in self.q("SELECT * FROM events WHERE session=? ORDER BY id", (session,))
        ]

    def sessions(self) -> list[dict]:
        return [dict(r) for r in self.q(
            "SELECT session, MIN(ts) AS started, MAX(ts) AS last, COUNT(*) AS n FROM events GROUP BY session ORDER BY last DESC LIMIT 50")]

    # ---------- 任务 ----------
    def save_task(self, task_id: str, session: str, status: str, plan: dict, result: dict | None = None) -> None:
        self.x(
            "INSERT INTO tasks(id, session, status, plan, result, updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, plan=excluded.plan, "
            "result=COALESCE(excluded.result, tasks.result), updated=excluded.updated",
            (task_id, session, status, json.dumps(plan, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False) if result is not None else None, time.time()),
        )

    def get_task(self, task_id: str) -> dict | None:
        r = self.q("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not r:
            return None
        d = dict(r[0])
        d["plan"] = json.loads(d["plan"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def list_tasks(self, limit: int = 20) -> list[dict]:
        rows = self.q("SELECT id, session, status, plan, updated FROM tasks ORDER BY updated DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            p = json.loads(r["plan"])
            out.append({"id": r["id"], "status": r["status"], "updated": r["updated"],
                        "rig": p.get("target", {}).get("rig"), "case": (p.get("case_ref") or {}).get("case_id"),
                        "utterance": p.get("origin", {}).get("utterance")})
        return out

    # ---------- 缺陷候选 ----------
    def save_defect(self, d: dict) -> None:
        self.x(
            "INSERT INTO defects(id, task_id, fingerprint, ota, status, doc, created) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
            (d["id"], d["task_id"], d["fingerprint"], d["ota"], d["status"],
             json.dumps(d, ensure_ascii=False, default=str), d.get("created_at", time.time())),
        )

    def get_defect(self, defect_id: str) -> dict | None:
        r = self.q("SELECT doc FROM defects WHERE id=?", (defect_id,))
        return json.loads(r[0]["doc"]) if r else None

    def list_defects(self, task_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT doc FROM defects WHERE 1=1", []
        if task_id:
            sql += " AND task_id=?"; args.append(task_id)
        if status:
            sql += " AND status=?"; args.append(status)
        return [json.loads(r["doc"]) for r in self.q(sql + " ORDER BY created DESC", tuple(args))]

    # ---------- 审核记忆 ----------
    def remember_review(self, fingerprint: str, ota: str, action: str, issue_key: str, reason: str, by: str) -> None:
        self.x(
            "INSERT INTO review_memory(fingerprint, ota, action, issue_key, reason, by, at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(fingerprint, ota) DO UPDATE SET action=excluded.action, issue_key=excluded.issue_key, "
            "reason=excluded.reason, by=excluded.by, at=excluded.at",
            (fingerprint, ota, action, issue_key, reason, by, time.time()),
        )

    def recall_review(self, fingerprint: str, ota: str) -> dict | None:
        r = self.q("SELECT * FROM review_memory WHERE fingerprint=? AND ota=?", (fingerprint, ota))
        return dict(r[0]) if r else None

    def bump_review_hit(self, fingerprint: str, ota: str) -> None:
        self.x("UPDATE review_memory SET hits=hits+1 WHERE fingerprint=? AND ota=?", (fingerprint, ota))

    def add_eval_sample(self, defect: dict, reason: str) -> None:
        self.x("INSERT INTO eval_set(defect_id, rule, reason, doc, at) VALUES(?,?,?,?,?)",
               (defect["id"], defect["rule"], reason, json.dumps(defect, ensure_ascii=False, default=str), time.time()))

    def false_positive_stats(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT rule, reason, COUNT(*) AS n FROM eval_set GROUP BY rule, reason")]

    def reset_runtime(self) -> None:
        """演示重置：清任务、候选、审核记忆、事件；Jira 沙箱恢复种子数据。"""
        for t in ("events", "tasks", "defects", "review_memory", "eval_set"):
            self.x(f"DELETE FROM {t}")
        self.x("DELETE FROM jira_sandbox WHERE key NOT IN (SELECT key FROM jira_sandbox WHERE labels LIKE '%seed%')")


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(get_settings().db_path)
    return _store
