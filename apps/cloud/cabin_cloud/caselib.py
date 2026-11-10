"""cabin-caselib：用例批次、用例审核与用例库。

一期主数据先落本地结果库（SQLite）；飞书多维表格接入后以多维表格为准，本模块是其同步副本。
只有审核结论为“通过 / 修改后通过”的用例算入库，可被拉取、被测试闭环读取。

版本：用例按“编号 + 修订号”存（test_case 存当前修订，case_rev 存全部修订）；PRD 版本批次审完后发布基线
<prd_id>@<version>，基线记录 {用例编号: 修订号}。车型：适用车型来自需求点打点，车型覆盖可跳过个别车型。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from cabin_common import vehicles
from . import modules
from cabin_common.schema import CASE_REJECT_REASONS, CaseBatch, PrdVersion, TestCase
from .store import get_store

IN_LIBRARY = ("通过", "修改后通过")
EDITABLE = ("title", "precondition", "steps", "expected", "type", "priority", "channel", "utterance",
            "expect_intent", "expect_screen", "expect_tts", "latency_max_ms", "voice_profile", "models")


def model_code(model: str) -> str:
    """车型显示名 / 别名 → 车型代码，如 “A01国内” → A01-CN；车型字典里没有的按原样大写。"""
    if not model.strip():
        return ""
    return vehicles.normalize(model) or re.sub(r"\s+", "", model).upper()


def default_models() -> list[str]:
    """默认打点车型：车型字典全部车型。"""
    return vehicles.codes()


def applies(c: TestCase, model: str) -> bool:
    """用例是否适用该车型：未写适用车型视为通用；车型覆盖为“跳过”的不适用。"""
    m = model_code(model)
    if vehicles.skipped(c.model_overrides, m):
        return False
    return not c.models or m in [model_code(x) for x in c.models]


class CaseLib:
    def __init__(self) -> None:
        self.st = get_store()

    # ---------- 批次 ----------
    def new_batch_id(self) -> str:
        day = datetime.now().strftime("%Y%m%d")
        n = self.st.q("SELECT COUNT(*) AS n FROM case_batch WHERE id LIKE ?", (f"B-{day}-%",))[0]["n"]
        return f"B-{day}-{n + 1:02d}"

    def save_batch(self, b: CaseBatch) -> None:
        self.st.x("INSERT INTO case_batch(id, session, status, doc, created) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
                  (b.batch_id, b.session, b.status, b.model_dump_json(), b.created_at))

    def get_batch(self, batch_id: str) -> CaseBatch | None:
        r = self.st.q("SELECT doc FROM case_batch WHERE id=?", (batch_id,))
        return CaseBatch.model_validate_json(r[0]["doc"]) if r else None

    def list_batches(self, limit: int = 20) -> list[dict]:
        out = []
        for r in self.st.q("SELECT doc FROM case_batch ORDER BY created DESC LIMIT ?", (limit,)):
            b = CaseBatch.model_validate_json(r["doc"])
            out.append({"batch_id": b.batch_id, "doc_name": b.doc_name, "section": b.section, "status": b.status,
                        "n_cases": len(b.case_ids), "created_at": b.created_at})
        return out

    def open_batch(self, user: str) -> CaseBatch | None:
        """此人最近一个还没审完的批次。"""
        rows = self.st.q("SELECT doc FROM case_batch WHERE status IN ('生成中','待审核') ORDER BY created DESC")
        batches = [CaseBatch.model_validate_json(r["doc"]) for r in rows]
        return next((b for b in batches if b.created_by == user), None)

    def find_duplicate(self, content_sha: str, section: str = "", source: str = "", revision: str = "") -> CaseBatch | None:
        """同一份内容（同章节范围）已经生成过的批次；失败、作废的不算。飞书文档先比 revision_id，再比正文哈希。"""
        for r in self.st.q("SELECT doc FROM case_batch WHERE status NOT IN ('失败','已作废') ORDER BY created DESC"):
            b = CaseBatch.model_validate_json(r["doc"])
            if b.section != section:
                continue
            if revision and source and b.doc_source == source and b.doc_revision == revision:
                return b
            if content_sha and b.content_sha == content_sha:
                return b
        return None

    def supersede(self, b: CaseBatch) -> None:
        """待审核的批次被同版本的新导入取代：批次作废；它给已有用例出的新修订不再是当前修订，恢复到上一个已入基线的修订。"""
        b.status = "已作废"
        self.save_batch(b)
        for c in self.batch_cases(b.batch_id):
            prev = [x for x in self.revisions(c.case_id) if x.baselines and x.case_rev != c.case_rev]
            if prev:
                self.save_case(max(prev, key=lambda x: x.case_rev))

    def fail_interrupted(self) -> None:
        """服务重启时，进行到一半的生成任务已经没了，标记失败，否则页面会一直显示生成中。"""
        for r in self.st.q("SELECT doc FROM case_batch WHERE status='生成中'"):
            b = CaseBatch.model_validate_json(r["doc"])
            b.status = "失败"
            self.save_batch(b)

    def batch_cases(self, batch_id: str) -> list[TestCase]:
        return [TestCase.model_validate_json(r["doc"])
                for r in self.st.q("SELECT doc FROM test_case WHERE batch_id=? ORDER BY case_id", (batch_id,))]

    # ---------- 用例 ----------
    def next_id(self, code: str, existing: set[str] = frozenset()) -> str:
        """TC-<模块缩写>-<三位序号>：模块内递增、生成后不变、驳回作废不复用。"""
        code = re.sub(r"[^A-Z0-9]", "", code.upper())[:8] or "GEN"
        r = self.st.q("SELECT n FROM case_seq WHERE code=?", (code,))
        n = r[0]["n"] if r else 0
        pat = re.compile(rf"^TC-{code}-(\d+)$")
        known = [int(m.group(1)) for x in existing if (m := pat.match(x))]
        known += [int(m.group(1)) for row in self.st.q("SELECT case_id FROM test_case WHERE case_id LIKE ?",
                                                       (f"TC-{code}-%",)) if (m := pat.match(row["case_id"]))]
        n = max([n, *known]) + 1
        self.st.x("INSERT INTO case_seq(code, n) VALUES(?,?) ON CONFLICT(code) DO UPDATE SET n=excluded.n", (code, n))
        return f"TC-{code}-{n:03d}"

    def save_case(self, c: TestCase) -> None:
        """保存当前修订；同时写入修订表。"""
        c.updated_at = time.time()
        doc = c.model_dump_json()
        self.st.x("INSERT INTO test_case(case_id, batch_id, module, review_status, plan_status, doc, updated) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET batch_id=excluded.batch_id, "
                  "module=excluded.module, review_status=excluded.review_status, plan_status=excluded.plan_status, "
                  "doc=excluded.doc, updated=excluded.updated",
                  (c.case_id, c.batch_id, c.module, c.review_status, c.plan_status, doc, c.updated_at))
        self.st.x("INSERT INTO case_rev(case_id, rev, doc, updated) VALUES(?,?,?,?) "
                  "ON CONFLICT(case_id, rev) DO UPDATE SET doc=excluded.doc, updated=excluded.updated",
                  (c.case_id, c.case_rev, doc, c.updated_at))

    def save_rev(self, c: TestCase) -> None:
        """只更新某个历史修订（不改当前修订），如给旧修订追加所属基线。"""
        cur = self.get_case(c.case_id)
        if cur and cur.case_rev == c.case_rev:
            self.save_case(c)
            return
        self.st.x("INSERT INTO case_rev(case_id, rev, doc, updated) VALUES(?,?,?,?) "
                  "ON CONFLICT(case_id, rev) DO UPDATE SET doc=excluded.doc, updated=excluded.updated",
                  (c.case_id, c.case_rev, c.model_dump_json(), time.time()))

    def get_case(self, case_id: str, rev: int | None = None) -> TestCase | None:
        if rev is not None:
            r = self.st.q("SELECT doc FROM case_rev WHERE case_id=? AND rev=?", (case_id.upper(), rev))
            if r:
                return TestCase.model_validate_json(r[0]["doc"])
        r = self.st.q("SELECT doc FROM test_case WHERE case_id=?", (case_id.upper(),))
        c = TestCase.model_validate_json(r[0]["doc"]) if r else None
        return c if c and (rev is None or c.case_rev == rev) else None

    def revisions(self, case_id: str) -> list[TestCase]:
        return [TestCase.model_validate_json(r["doc"])
                for r in self.st.q("SELECT doc FROM case_rev WHERE case_id=? ORDER BY rev", (case_id.upper(),))]

    # ---------- PRD 版本与基线 ----------
    def save_prd_version(self, v: PrdVersion) -> None:
        """已发布的版本不可改：只允许原批次更新它（如补存档路径），不允许别的批次覆盖或降级。"""
        cur = self.get_prd_version(v.prd_id, v.version)
        if cur and cur.status == "已发布" and (v.status != "已发布" or v.batch_id != cur.batch_id):
            raise ValueError(f"{v.baseline_id} 已发布，不能覆盖；内容有变请升版本号或作为修订导入")
        self.st.x("INSERT INTO prd_version(id, prd_id, version, status, doc, created) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
                  (v.baseline_id, v.prd_id, v.version, v.status, v.model_dump_json(), v.created_at))

    def get_prd_version(self, prd_id: str, version: str) -> PrdVersion | None:
        r = self.st.q("SELECT doc FROM prd_version WHERE id=?", (f"{prd_id}@{version}",))
        return PrdVersion.model_validate_json(r[0]["doc"]) if r else None

    def prd_versions(self, prd_id: str) -> list[PrdVersion]:
        return [PrdVersion.model_validate_json(r["doc"])
                for r in self.st.q("SELECT doc FROM prd_version WHERE prd_id=? ORDER BY created", (prd_id,))]

    def latest_published(self, prd_id: str, exclude: str = "", family: str = "") -> PrdVersion | None:
        """最近发布的版本。family 非空时只在该版本及其修订（如 0.6、0.6+r2）里找。"""
        vs = [v for v in self.prd_versions(prd_id) if v.status == "已发布" and v.version != exclude
              and (not family or v.version == family or v.version.startswith(family + "+r"))]
        return vs[-1] if vs else None

    def next_revision(self, prd_id: str, version: str) -> str:
        """同版本号内容有变时的修订号：0.6 → 0.6+r2 → 0.6+r3；还没发布的修订号可复用（被新导入取代）。"""
        pub = {v.version for v in self.prd_versions(prd_id) if v.status == "已发布"}
        k = 2
        while f"{version}+r{k}" in pub:
            k += 1
        return f"{version}+r{k}"

    # ---------- PRD 身份：来源 → prd_id（飞书文档 token / 在线链接，绑定后不变） ----------
    def prd_for_source(self, source: str) -> str:
        r = self.st.q("SELECT prd_id FROM prd_source WHERE source=?", (source,))
        return r[0]["prd_id"] if r else ""

    def prd_by_title(self, title_hash: str) -> str:
        """按标题哈希（prd_id 末段）找已有的 PRD；恰好一份时返回，用于调模型前预判基准版本。"""
        ids = {r["prd_id"] for r in self.st.q("SELECT DISTINCT prd_id FROM prd_version WHERE prd_id LIKE ?",
                                              (f"PRD-%-{title_hash}",))}
        return ids.pop() if len(ids) == 1 else ""

    def bind_source(self, source: str, prd_id: str) -> None:
        self.st.x("INSERT INTO prd_source(source, prd_id, bound) VALUES(?,?,?) ON CONFLICT(source) DO NOTHING",
                  (source, prd_id, time.time()))

    def params_for(self, prd_id: str) -> list:
        """该 PRD 最新版本（已发布优先）的车型参数。"""
        vs = self.prd_versions(prd_id)
        pub = [v for v in vs if v.status == "已发布"]
        v = (pub or vs or [None])[-1]
        return v.params if v else []

    def get_baseline(self, baseline_id: str) -> dict[str, int] | None:
        r = self.st.q("SELECT cases FROM baseline WHERE id=?", (baseline_id,))
        return {k: int(v) for k, v in json.loads(r[0]["cases"]).items()} if r else None

    def baselines(self) -> list[dict]:
        return [{"id": r["id"], "prd_id": r["prd_id"], "version": r["version"], "n": len(json.loads(r["cases"])),
                 "published": r["published"]}
                for r in self.st.q("SELECT * FROM baseline ORDER BY published")]

    def baseline_cases(self, baseline_id: str) -> list[TestCase]:
        m = self.get_baseline(baseline_id) or {}
        return [c for cid, rev in sorted(m.items()) if (c := self.get_case(cid, rev))]

    def publish_baseline(self, b: CaseBatch) -> str:
        """批次审完后发布基线：继承的修订 + 本批审核通过的修订；修订的“所属基线”追加本基线。"""
        if not b.prd_id or not b.prd_version:
            return ""
        self.check_publishable(b)
        bid = f"{b.prd_id}@{b.prd_version}"
        revs: dict[str, int] = dict(b.inherited)
        for c in self.batch_cases(b.batch_id):
            if c.review_status in IN_LIBRARY:
                revs[c.case_id] = c.case_rev
        for cid, rev in revs.items():
            c = self.get_case(cid, rev)
            if c and bid not in c.baselines:
                c.baselines.append(bid)
                self.save_rev(c)
        self.st.x("INSERT INTO baseline(id, prd_id, version, cases, published) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET cases=excluded.cases, published=excluded.published",
                  (bid, b.prd_id, b.prd_version, json.dumps(revs), time.time()))
        v = self.get_prd_version(b.prd_id, b.prd_version)
        if v:
            v.status = "已发布"
            self.save_prd_version(v)
        try:  # 测试代码仓库打基线标签（标签指向发布时刻；精确取回按各修订的提交号）
            from . import planner
            planner.tag_baseline(bid)
        except Exception:
            pass
        return bid

    def check_publishable(self, b: CaseBatch) -> None:
        """基线一经发布不可覆盖：同一 <prd_id>@<version> 只能由生成它的批次发布。"""
        if not b.prd_id or not b.prd_version:
            return
        v = self.get_prd_version(b.prd_id, b.prd_version)
        if v and v.status == "已发布" and v.batch_id != b.batch_id:
            raise ValueError(f"基线 {b.prd_id}@{b.prd_version} 已由批次 {v.batch_id} 发布，不能覆盖")
        if b.status == "已作废":
            raise ValueError(f"批次 {b.batch_id} 已被同版本的新导入取代，不能发布")

    def library(self) -> list[TestCase]:
        rows = self.st.q("SELECT doc FROM test_case WHERE review_status IN (?,?) ORDER BY case_id", IN_LIBRARY)
        return [TestCase.model_validate_json(r["doc"]) for r in rows]

    # ---------- 审核（人工门：未审核不入库） ----------
    def submit_review(self, batch_id: str, decisions: list[dict[str, Any]], reviewer: str = "测试工程师") -> dict:
        """decisions: [{case_id, decision: pass|edit|reject|to_req, reason, edits: {字段: 新值}}]。
        未给结论的用例不允许提交；驳回必须带原因。返回各结论计数。"""
        b = self.get_batch(batch_id)
        if not b:
            raise KeyError(f"批次 {batch_id} 不存在")
        if b.status == "已审核":
            raise ValueError(f"批次 {batch_id} 已审核过")
        self.check_publishable(b)
        cases = {c.case_id: c for c in self.batch_cases(batch_id)}
        dec = {d["case_id"]: d for d in decisions}
        missing = [cid for cid in cases if cid not in dec]
        if missing:
            raise ValueError(f"还有 {len(missing)} 条用例未审核：{', '.join(missing[:5])}")
        for d in dec.values():
            if d.get("decision") == "reject" and not d.get("reason"):
                raise ValueError(f"{d['case_id']} 驳回需选择原因")
        n = {"pass": 0, "edit": 0, "reject": 0, "to_req": 0}
        for cid, d in dec.items():
            c = cases.get(cid)
            if not c:
                continue
            kind = d.get("decision", "pass")
            diff: dict[str, Any] = {}
            if kind in ("pass", "edit"):
                for k, v in (d.get("edits") or {}).items():
                    if k in EDITABLE and getattr(c, k) != v:
                        diff[k] = {"from": getattr(c, k), "to": v}
                        setattr(c, k, v)
                kind = "edit" if diff else "pass"
                c.review_status = "修改后通过" if diff else "通过"
            elif kind == "reject":
                c.review_status = "驳回"
            elif kind == "to_req":
                c.review_status = "转需求方"
            else:
                raise ValueError(f"未知审核结论 {kind}")
            n[kind] += 1
            c.reviewer, c.review_note = reviewer, d.get("reason", "")
            self.save_case(c)
            self.st.x("INSERT INTO case_review(case_id, batch_id, decision, reason, diff, reviewer, at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (cid, batch_id, kind, d.get("reason", ""), json.dumps(diff, ensure_ascii=False), reviewer,
                       time.time()))
            # 驳回 / 修改都是生成质量样本，写入评估集
            if kind in ("reject", "edit"):
                self.st.x("INSERT INTO eval_set(defect_id, rule, reason, doc, at) VALUES(?,?,?,?,?)",
                          (cid, "case-gen", d.get("reason") or "修改后通过",
                           json.dumps({"case": c.model_dump(), "diff": diff, "checks": [x.model_dump() for x in c.checks]},
                                      ensure_ascii=False), time.time()))
        b.status = "已审核"
        self.save_batch(b)
        self.publish_baseline(b)
        return n

    def review_stats(self, batch_id: str) -> dict:
        rows = self.st.q("SELECT decision, COUNT(*) AS n FROM case_review WHERE batch_id=? GROUP BY decision",
                         (batch_id,))
        return {r["decision"]: r["n"] for r in rows}

    def current_library(self) -> list[TestCase]:
        """默认拉取范围：各 PRD 最新已发布基线里的修订 + 不属于任何 PRD 的已入库用例（如 Excel 导入）。"""
        latest: dict[str, str] = {}
        for b in self.baselines():
            latest[b["prd_id"]] = b["id"]
        out: dict[str, TestCase] = {}
        for bid in latest.values():
            for c in self.baseline_cases(bid):
                out[c.case_id] = c
        for c in self.library():
            if not c.prd_id and c.case_id not in out:
                out[c.case_id] = c
        return [out[k] for k in sorted(out)]

    # ---------- 拉取（A-15） ----------
    def pull(self, model: str, module: str = "", case_id: str = "", rig_id: str = "",
             updated_since: float = 0, baseline: str = "") -> list[TestCase]:
        """只返回审核通过、适用该车型的用例；baseline 缺省为各 PRD 最新已发布基线。"""
        m = model_code(model)
        if not m:
            raise ValueError("model 必填")
        if baseline:
            if self.get_baseline(baseline) is None:
                raise ValueError(f"基线 {baseline} 不存在")
            pool = self.baseline_cases(baseline)
        else:
            pool = self.current_library()
        out = []
        for c in pool:
            if not applies(c, m):
                continue
            if case_id and c.case_id != case_id.upper():
                continue
            if module and (modules.find(module) or module.upper()) != c.case_id.split("-")[1] \
                    and module not in c.module:
                continue
            if updated_since and c.updated_at < updated_since:
                continue
            out.append(c)
        return out

    @staticmethod
    def a15_row(c: TestCase) -> dict:
        return {"case_id": c.case_id, "case_rev": c.case_rev, "baselines": c.baselines, "title": c.title,
                "module": c.module, "models": c.models, "channel": c.channel,
                "review_status": c.review_status, "auto_status": c.auto_status,
                "plan_status": c.plan_status or "generating", "plan_version": c.plan_version or None,
                "assets": ["wake.wav", "utt.wav"] if c.is_voice and c.plan_status == "ready" else []}


def reject_reasons() -> list[str]:
    return CASE_REJECT_REASONS


_lib: CaseLib | None = None


def get_caselib() -> CaseLib:
    global _lib
    if _lib is None:
        _lib = CaseLib()
    return _lib
