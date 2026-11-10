"""cabin-integ / Jira：查重（读）+ 建单（写），三档降级。

一档 REST：配置 JIRA_URL/USER/PASSWORD 时，对沙箱项目调用 REST（查重 JQL + 建单）。
本地沙箱：未配置时，使用本地 SQLite 沙箱项目（含一条已知缺陷种子），完整演示查重与建单。
二档/三档：REST 失败时生成预填建单链接 / 界面草稿。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import vault
from ..config import get_settings
from cabin_common.schema import DefectCandidate, DupResult, JiraResult
from ..store import get_store

SEED = [{
    "key": "A01-51234", "project": "A01", "summary": "[显示异常] 设置-显示：“自动调节亮度”说明文案截断",
    "description": "设置 > 显示 页面，自动调节亮度下方说明文字显示不全，以省略号结尾。",
    "fingerprint": "J-01|com.cabin.sim.settings|设置/显示|TC-DISP-012",
    "found_version": "C01-OTA-1.0.0", "affects": ["C01-OTA-1.0.0"], "status": "Open", "severity": "B",
    "labels": ["seed", "known-issue"],
}]

ATTACH_LIMIT = 30 * 1024 * 1024  # 暂定，Jira 单次附件上限
SEVERITY_BY_RULE = {"J-01": "B", "J-02": "A", "J-03": "A", "J-04": "A", "J-05": "A", "J-06": "B"}


def _ver_key(v: str) -> tuple:
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


class JiraClient:
    def __init__(self) -> None:
        self.s = get_settings()
        self.store = get_store()
        self.transport: httpx.AsyncBaseTransport | None = None
        self.seed()

    @property
    def cfg(self) -> dict:
        """云端配置页优先，其次环境变量。服务账号的密码或 Token 加密存放。"""
        doc = self.store.get_kv("jira")
        secret = vault.unseal(doc["secret"]) if doc.get("secret") else self.s.jira_password
        return {"url": (doc.get("url") or self.s.jira_url).rstrip("/"), "user": doc.get("user") or self.s.jira_user, "secret": secret,
                "token_auth": bool(doc.get("token_auth")), "projects": doc.get("projects") or {}}

    @property
    def rest_enabled(self) -> bool:
        c = self.cfg
        return bool(c["url"] and c["secret"] and (c["user"] or c["token_auth"]))

    def project_of(self, model: str) -> dict:
        p = self.cfg["projects"].get(model) or {}
        return {"project": p.get("project") or self.s.jira_project, "component": p.get("component") or ""}

    def seed(self) -> None:
        for it in SEED:
            if not self.store.q("SELECT key FROM jira_sandbox WHERE key=?", (it["key"],)):
                self.store.x(
                    "INSERT INTO jira_sandbox(key, project, summary, description, fingerprint, found_version, affects,"
                    " status, severity, labels, comments, attachments, dup_of, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["key"], it["project"], it["summary"], it["description"], it["fingerprint"], it["found_version"],
                     json.dumps(it["affects"]), it["status"], it["severity"], json.dumps(it["labels"]), "[]", "[]", "",
                     time.time() - 86400 * 20))

    # ---------- 查重（PRD 表45：按 OTA 版本分类，不过滤） ----------
    async def find_dup(self, d: DefectCandidate) -> DupResult:
        project = self.project_of(d.model)["project"]
        issues = await (self._rest_search(d.fingerprint, project) if self.rest_enabled else self._local_search(d.fingerprint))
        if not issues:
            return DupResult()
        same = [i for i in issues if i["status"] != "Closed" and d.ota in i["affects"] + [i["found_version"]]]
        if same:
            i = same[0]
            return DupResult(category="duplicate", label="同指纹 · 同版本 · 未关闭 → 疑似重复", issue_key=i["key"],
                             issue_summary=i["summary"], issue_version=i["found_version"], suggestion="link")
        open_old = [i for i in issues if i["status"] != "Closed"]
        if open_old:
            i = open_old[0]
            return DupResult(category="continuation", label=f"同指纹 · 旧版本 {i['found_version']} · 未关闭 → 老问题延续",
                             issue_key=i["key"], issue_summary=i["summary"], issue_version=i["found_version"],
                             suggestion="link")
        i = max(issues, key=lambda x: _ver_key(x["found_version"]))
        return DupResult(category="regression", label=f"同指纹 · 旧版本 {i['found_version']} 已修复 → 回归缺陷",
                         issue_key=i["key"], issue_summary=i["summary"], issue_version=i["found_version"],
                         suggestion="create_regression")

    async def _local_search(self, fp: str) -> list[dict]:
        rows = self.store.q("SELECT * FROM jira_sandbox WHERE fingerprint=? ORDER BY created", (fp,))
        return [{**dict(r), "affects": json.loads(r["affects"] or "[]")} for r in rows]

    async def _rest_search(self, fp: str, project: str) -> list[dict]:
        jql = f'project = "{project}" AND labels = "fp-{_fp_label(fp)}" ORDER BY created DESC'
        async with self._http() as c:
            r = await c.get(f"{self.cfg['url']}/rest/api/2/search", params={"jql": jql, "fields": "summary,status,versions"})
            r.raise_for_status()
        out = []
        for it in r.json().get("issues", []):
            f = it["fields"]
            vers = [v["name"] for v in f.get("versions") or []]
            out.append({"key": it["key"], "summary": f["summary"], "status": _status(f["status"]["name"]),
                        "found_version": vers[0] if vers else "", "affects": vers})
        return out

    def _http(self, timeout: float = 20) -> httpx.AsyncClient:
        c = self.cfg
        token = {"Authorization": f"Bearer {c['secret']}"} if c["token_auth"] else {}
        return httpx.AsyncClient(timeout=timeout, auth=None if c["token_auth"] else (c["user"], c["secret"]), headers=token,
                                 transport=self.transport, trust_env=os.getenv("CABIN_USE_PROXY") == "1")

    async def myself(self) -> str:
        """测试连接。"""
        async with self._http() as c:
            r = await c.get(f"{self.cfg['url']}/rest/api/2/myself")
            r.raise_for_status()
        return r.json().get("displayName") or r.json().get("name") or ""

    async def _reporter(self, c: httpx.AsyncClient, name: str) -> str:
        """同事的 Jira 用户名：先看本人填的，再按飞书邮箱查，查到后记下来。"""
        p = self.store.user_profile(name)
        if p.get("jira_user"):
            return p["jira_user"]
        if not p.get("email"):
            return ""
        r = await c.get(f"{self.cfg['url']}/rest/api/2/user/search", params={"username": p["email"]})
        users = r.json() if r.status_code == 200 else []
        found = users[0].get("name", "") if users else ""
        if found:
            self.store.set_jira_user(name, found)
        return found

    async def sync_status(self, key: str) -> str:
        async with self._http() as c:
            r = await c.get(f"{self.cfg['url']}/rest/api/2/issue/{key}", params={"fields": "status"})
            r.raise_for_status()
        return _status(r.json()["fields"]["status"]["name"])

    # ---------- 字段映射（PRD 表46） ----------
    def fields(self, d: DefectCandidate, plan: dict) -> dict:
        regression = d.dup.category == "regression"
        steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan.get("summary") or []))
        desc = (f"*复现步骤（TaskPlan {plan.get('id')} 摘要）*\n{steps}\n\n"
                f"*用例*：{d.case_id}\n*期望*：{d.expected}\n*实际*：{d.summary}\n*判定理由*：{d.reason}\n"
                f"*判定*：{d.judged_by} · 置信度 {d.confidence:.2f}\n*车型*：{d.model}\n*版本*：{d.ota}\n"
                f"*台架*：{'、'.join([d.rig, *d.rigs])}\n*发现人*：{d.finder}\n*审核人*：{d.review.by if d.review else ''}\n"
                f"*时间*：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if d.log_excerpt:
            desc += f"\n*日志片段*\n{{noformat}}\n{d.log_excerpt[:2000]}\n{{noformat}}\n"
        if d.dup.issue_key:
            desc += f"\n*疑似重复*：{d.dup.issue_key}（{d.dup.label}）\n"
        target = self.project_of(d.model)
        return {
            "project": target["project"], "component": target["component"], "finder": d.finder, "issuetype": "Bug",
            "summary": (d.review.summary if d.review and d.review.summary else d.title),
            "description": desc, "severity": (d.review.severity if d.review and d.review.severity else d.severity),
            "found_version": d.ota, "dup_of": d.dup.issue_key, "regression": regression, "fingerprint": d.fingerprint,
            "labels": ["cabin-auto", f"conf-{d.level}", f"fp-{_fp_label(d.fingerprint)}",
                       f"reviewer-{d.review.by if d.review else ''}"] + (["regression"] if regression else []),
            "attachments": [x for x in [d.expected_shot, d.actual_shot] if x] + (["logcat.txt"] if d.log_excerpt else []),
        }

    # ---------- 建单 ----------
    async def create_issue(self, d: DefectCandidate, plan: dict, files: list[Path] | None = None) -> JiraResult:
        f = self.fields(d, plan)
        picked, skipped = pick_attachments(files or [])
        if skipped:
            f["description"] += f"\n*未上传的证据*：{'、'.join(p.name for p in skipped)} 超过附件上限，保存在台架 {d.rig}\n"
        if self.rest_enabled:
            try:
                r = await self._rest_create(f, picked)
                r.skipped = [p.name for p in skipped]
                return r
            except Exception as e:
                return self._prefill(f, note=f"REST 建单失败（{type(e).__name__}），已降级为预填链接")
        r = self._local_create({**f, "attachments": f["attachments"] + [p.name for p in picked]})
        r.attached, r.skipped = [p.name for p in picked], [p.name for p in skipped]
        return r

    def _local_create(self, f: dict) -> JiraResult:
        n = len(self.store.q("SELECT key FROM jira_sandbox WHERE project=?", (f["project"],)))
        key = f"{f['project']}-{618 + n:04d}"
        self.store.x(
            "INSERT INTO jira_sandbox(key, project, summary, description, fingerprint, found_version, affects, status,"
            " severity, labels, comments, attachments, dup_of, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, f["project"], f["summary"], f["description"], f["fingerprint"], f["found_version"],
             json.dumps([f["found_version"]]), "Open", f["severity"], json.dumps(f["labels"]), "[]",
             json.dumps(f["attachments"]), f["dup_of"], time.time()))
        return JiraResult(tier="sandbox-local", key=key, url=f"{self.s.public_base_url}/api/jira/{key}", draft=f,
                          note="本地 Jira 沙箱项目（未配置 JIRA_URL）")

    async def _rest_create(self, f: dict, files: list[Path]) -> JiraResult:
        body = {"fields": {"project": {"key": f["project"]}, "issuetype": {"name": f["issuetype"]},
                           "summary": f["summary"], "description": f["description"], "labels": f["labels"]}}
        if f["found_version"]:
            body["fields"]["versions"] = [{"name": f["found_version"]}]
        if f["component"]:
            body["fields"]["components"] = [{"name": f["component"]}]
        url = self.cfg["url"]
        async with self._http(timeout=120) as c:
            reporter = await self._reporter(c, f["finder"]) if f["finder"] else ""
            if reporter:
                body["fields"]["reporter"] = {"name": reporter}
            r = await c.post(f"{url}/rest/api/2/issue", json=body)
            if r.status_code == 400 and reporter:  # 服务账号没有修改报告人的权限：发现人已写在描述里
                del body["fields"]["reporter"]
                r = await c.post(f"{url}/rest/api/2/issue", json=body)
            r.raise_for_status()
            key = r.json()["key"]
            if f["dup_of"]:
                await c.post(f"{url}/rest/api/2/issueLink", json={
                    "type": {"name": "Duplicate"}, "inwardIssue": {"key": key}, "outwardIssue": {"key": f["dup_of"]}})
            attached = []
            for p in files:
                a = await c.post(f"{url}/rest/api/2/issue/{key}/attachments", headers={"X-Atlassian-Token": "no-check"},
                                 files={"file": (p.name, p.read_bytes())})
                if a.status_code < 300:
                    attached.append(p.name)
        missed = [p.name for p in files if p.name not in attached]
        return JiraResult(tier="rest", key=key, url=f"{url}/browse/{key}", draft=f, attached=attached,
                          note=f"附件上传失败：{'、'.join(missed)}" if missed else "")

    def _prefill(self, f: dict, note: str) -> JiraResult:
        base = self.cfg["url"]
        if not base:
            return JiraResult(tier="draft", draft=f, note=note)
        url = (f"{base}/secure/CreateIssueDetails!init.jspa?pid={quote(f['project'])}&issuetype=1"
               f"&summary={quote(f['summary'])}&description={quote(f['description'][:1500])}")
        return JiraResult(tier="prefill-link", url=url, draft=f, note=note)

    # ---------- 关联已有单（追加评论与证据） ----------
    async def link_existing(self, d: DefectCandidate, issue_key: str) -> JiraResult:
        comment = (f"[CabinPilot] 版本 {d.ota} 仍复现（任务 {d.task_id}，台架 {d.rig}）。{d.summary}。"
                   f"判定：{d.judged_by} · 置信度 {d.confidence:.2f}")
        if self.rest_enabled:
            try:
                async with self._http() as c:
                    r = await c.post(f"{self.cfg['url']}/rest/api/2/issue/{issue_key}/comment", json={"body": comment})
                    r.raise_for_status()
                return JiraResult(tier="rest", key=issue_key, linked_to=issue_key, url=f"{self.cfg['url']}/browse/{issue_key}")
            except Exception as e:
                return JiraResult(tier="draft", linked_to=issue_key, draft={"comment": comment},
                                  note=f"REST 追加评论失败（{type(e).__name__}）")
        rows = self.store.q("SELECT comments, affects FROM jira_sandbox WHERE key=?", (issue_key,))
        if rows:
            cm = json.loads(rows[0]["comments"] or "[]") + [{"at": time.time(), "body": comment}]
            af = json.loads(rows[0]["affects"] or "[]")
            if d.ota and d.ota not in af:
                af.append(d.ota)
            self.store.x("UPDATE jira_sandbox SET comments=?, affects=? WHERE key=?",
                         (json.dumps(cm, ensure_ascii=False), json.dumps(af), issue_key))
        return JiraResult(tier="sandbox-local", key=issue_key, linked_to=issue_key,
                          url=f"{self.s.public_base_url}/api/jira/{issue_key}", note="已追加评论与证据")

    def get_local(self, key: str) -> dict | None:
        r = self.store.q("SELECT * FROM jira_sandbox WHERE key=?", (key,))
        if not r:
            return None
        d = dict(r[0])
        for k in ("affects", "labels", "comments", "attachments"):
            d[k] = json.loads(d[k] or "[]")
        return d


def _status(name: str) -> str:
    return "Closed" if name in ("Closed", "Done", "Resolved", "已关闭", "已解决") else "Open"


def pick_attachments(files: list[Path], limit: int = ATTACH_LIMIT) -> tuple[list[Path], list[Path]]:
    """日志包最优先且不截断，其次截图和文本，最后视频；放不下的不传，建票不因此失败。"""
    rank = {".mp4": 2, ".png": 1, ".txt": 1}
    picked, skipped, total = [], [], 0
    for p in sorted(files, key=lambda x: rank.get(x.suffix, 0)):
        size = p.stat().st_size
        if total + size <= limit:
            picked.append(p)
            total += size
        else:
            skipped.append(p)
    return picked, skipped


def _fp_label(fp: str) -> str:
    import hashlib
    return hashlib.sha1(fp.encode()).hexdigest()[:10]


_jira: JiraClient | None = None


def get_jira() -> JiraClient:
    global _jira
    if _jira is None:
        _jira = JiraClient()
    return _jira
