"""cabin-integ / 飞书多维表格用例库。

配置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE_URL 后，多维表格为用例主数据。
按模块分表：backend/modules.yaml 里给模块配了 bitable 的，该模块用例写进自己的表，其余写默认表（FEISHU_BITABLE_URL）；
- 生成完的批次写入表格（审核状态=待审核），表格即审核入口；CabinPilot 审核台提交的结论同样回写；
- 表格里人工修改的用例周期同步回 CabinPilot（以表格为准，CabinPilot 记录变更历史），步骤 / 预期 / 通道变化时测试代码置为需重新生成；
- 同步时校验必填、单选取值、用例编号唯一，不合格的行不进入用例库并在界面提示。
未配置时用例库只落本地（caselib）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from cabin_common import vehicles
from .. import modules
from ..config import get_settings
from cabin_common.schema import TestCase

API = "https://open.feishu.cn/open-apis"
TEXT, NUMBER, SINGLE, MULTI = 1, 2, 3, 4
RE_CASE_ID = re.compile(r"^TC-[A-Z0-9]+-\d{3}$")


@dataclass(frozen=True)
class Col:
    name: str
    type: int
    attr: str
    options: tuple[str, ...] = ()
    synced_back: bool = True  # 表格里改了会同步回 CabinPilot


COLS: tuple[Col, ...] = (
    Col("用例编号", TEXT, "case_id", synced_back=False),
    Col("修订号", NUMBER, "case_rev", synced_back=False),
    Col("所属基线", TEXT, "baselines", synced_back=False),
    Col("标题", TEXT, "title"),
    Col("模块", TEXT, "module"),
    Col("需求点", TEXT, "req_id", synced_back=False),
    Col("PRD章节", TEXT, "prd_section", synced_back=False),
    Col("前置条件", TEXT, "precondition"),
    Col("测试步骤", TEXT, "steps"),
    Col("预期结果", TEXT, "expected"),
    Col("类型", SINGLE, "type", ("正向", "边界", "异常")),
    Col("优先级", SINGLE, "priority", ("P0", "P1", "P2")),
    Col("通道", MULTI, "channel", ("audio", "adb", "ssh_qnx", "arm", "can")),
    Col("测试语句", TEXT, "utterance"),
    Col("期望屏幕", TEXT, "expect_screen"),
    Col("期望播报", TEXT, "expect_tts"),
    Col("期望意图", TEXT, "expect_intent"),
    Col("时延上限ms", NUMBER, "latency_max_ms"),
    Col("测试音色", TEXT, "voice_profile"),
    Col("适用车型", MULTI, "models", tuple(vehicles.codes())),
    Col("车型覆盖", TEXT, "model_overrides"),
    Col("来源", SINGLE, "source", ("AI生成", "人工编写", "迁移"), synced_back=False),
    Col("批次", TEXT, "batch_id", synced_back=False),
    Col("自检意见", TEXT, "checks", synced_back=False),
    Col("审核状态", SINGLE, "review_status", ("待审核", "通过", "修改后通过", "驳回", "转需求方")),
    Col("审核人", TEXT, "reviewer"),
    Col("审核意见", TEXT, "review_note"),
    Col("自动化状态", SINGLE, "auto_status", ("未生成", "已生成", "试跑中", "试跑未通过", "已验证", "不适合自动化", "需重新生成"),
        synced_back=False),
    Col("测试代码版本", TEXT, "plan_version", synced_back=False),
)
BY_NAME = {c.name: c for c in COLS}
PLAN_FIELDS = {"steps", "expected", "channel", "utterance", "expect_screen", "expect_tts", "expect_intent",
               "latency_max_ms", "precondition", "models", "model_overrides"}  # 变化即测试代码需重新生成


class BitableError(RuntimeError):
    pass


def parse_url(url: str) -> tuple[str, str, bool]:
    """多维表格链接 → (app_token 或 wiki 节点 token, table_id, 是否 wiki)。"""
    u = urlparse(url.strip())
    m = re.search(r"/(base|wiki)/([A-Za-z0-9]+)", u.path)
    if not m:
        raise BitableError("FEISHU_BITABLE_URL 不是多维表格链接（应形如 https://xxx.feishu.cn/base/<token>?table=<id>）")
    table = (parse_qs(u.query).get("table") or [""])[0]
    return m.group(2), table, m.group(1) == "wiki"


# ---------- 字段值转换 ----------
def to_cell(c: TestCase, col: Col) -> Any:
    v = getattr(c, col.attr)
    if col.attr == "steps":
        return "\n".join(f"{i}. {s}" for i, s in enumerate(v, 1))
    if col.attr == "checks":
        return "；".join(f"{k.kind}：{k.msg}（建议{k.suggest}）" for k in v)
    if col.attr == "baselines":
        return "、".join(v or [])
    if col.attr == "model_overrides":
        return vehicles.overrides_to_text(v)
    if col.type == MULTI:
        return list(v or [])
    if col.type == NUMBER:
        return v
    return v or ""


def cell_text(v: Any) -> str:
    """读回的单元格统一成文本（文本字段可能是富文本片段数组）。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or "")
    return str(v)


def from_cell(col: Col, v: Any) -> Any:
    if col.type == MULTI:
        if isinstance(v, list):
            return [x.get("text", x.get("name", "")) if isinstance(x, dict) else str(x) for x in v if x]
        return [x for x in re.split(r"[,，、\s]+", cell_text(v)) if x]
    if col.type == NUMBER:
        try:
            return int(float(cell_text(v))) if cell_text(v) else None
        except ValueError:
            return None
    t = cell_text(v).strip()
    if col.attr == "model_overrides":
        return vehicles.parse_overrides(t)[0]
    if col.attr == "baselines":
        return [x for x in re.split(r"[、,，\s]+", t) if x]
    if col.attr == "steps":
        return [re.sub(r"^\s*\d+[.、)]\s*", "", x).strip() for x in t.splitlines() if x.strip()]
    return t


def validate_row(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """：返回 (TestCase 字段, 问题列表)。"""
    out: dict[str, Any] = {}
    errs: list[str] = []
    for name, col in BY_NAME.items():
        if name not in fields or col.attr in ("checks", "plan_version"):  # 只写不读的列
            continue
        if col.attr == "model_overrides":
            val, oerrs = vehicles.parse_overrides(cell_text(fields[name]))
            errs += oerrs
            out[col.attr] = val
            continue
        val = from_cell(col, fields[name])
        if col.attr == "models":  # 车型可写显示名或代码（A01国内 / A01-CN）
            val = [vehicles.normalize(x) or x for x in val]
        if col.type == SINGLE and val and col.options and val not in col.options:
            errs.append(f"{name}取值“{val}”不在 {'/'.join(col.options)} 内")
            continue
        if col.type == MULTI and col.options and any(x not in col.options for x in val):
            errs.append(f"{name}含未知取值 {[x for x in val if x not in col.options]}")
            continue
        out[col.attr] = val
    cid = out.get("case_id", "")
    if not RE_CASE_ID.match(cid or ""):
        errs.append(f"用例编号“{cid}”格式应为 TC-模块-三位序号")
    if not out.get("title"):
        errs.append("标题为空")
    if "audio" in (out.get("channel") or []):
        miss = [n for n, k in (("测试语句", "utterance"), ("期望屏幕", "expect_screen"), ("期望播报", "expect_tts"))
                if not out.get(k)]
        if miss:
            errs.append(f"语音用例缺{'、'.join(miss)}")
    elif not out.get("expected"):
        errs.append("预期结果为空")
    return out, errs


# ---------- 客户端 ----------
def table_key(url: str) -> tuple[str, str]:
    """同一张表的判定：(app 或 wiki token, table_id)；不是表格链接返回空。"""
    try:
        return parse_url(url)[:2] if url else ("", "")
    except BitableError:
        return ("", "")


class BitableClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, url: str = "") -> None:
        self.s = get_settings()
        self._url = url
        self._transport = transport
        self._token = ""
        self._token_exp = 0.0
        self._app = ""
        self._table = ""
        self._fields_ready = False
        self.last_sync: dict[str, Any] = {}

    @property
    def url(self) -> str:
        return self._url or self.s.feishu_bitable_url

    @property
    def key(self) -> tuple[str, str]:
        return table_key(self.url)

    @property
    def enabled(self) -> bool:
        return bool(self.s.feishu_app_id and self.s.feishu_app_secret and self.url)

    def modules(self) -> list[str]:
        """路由到这张表的模块缩写（默认表还承接所有未单独配表的模块，用 * 表示）。"""
        out = [m.code for m in modules.all_modules() if table_key(self._route(m.code)) == self.key]
        if self.key == table_key(self.s.feishu_bitable_url):
            out.append("*")
        return out

    def _route(self, code: str) -> str:
        return modules.get(code).bitable or self.s.feishu_bitable_url

    def owns(self, case_id: str) -> bool:
        """这个用例按 modules.yaml 是否应该在这张表。"""
        return table_key(self._route(modules.code_of_case(case_id))) == self.key

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=API, timeout=30, transport=self._transport,
                                 trust_env=os.getenv("CABIN_USE_PROXY") == "1")

    async def _call(self, c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
        if time.time() > self._token_exp - 60:
            r = (await c.post("/auth/v3/tenant_access_token/internal",
                              json={"app_id": self.s.feishu_app_id, "app_secret": self.s.feishu_app_secret})).json()
            if not r.get("tenant_access_token"):
                raise BitableError(f"飞书鉴权失败：{r.get('msg')}")
            self._token, self._token_exp = r["tenant_access_token"], time.time() + int(r.get("expire", 7200))
        last = ""
        for attempt in range(3):  #：失败重试 3 次
            try:
                r = await c.request(method, path, headers={"Authorization": f"Bearer {self._token}"}, **kw)
                j = r.json()
            except (httpx.HTTPError, json.JSONDecodeError) as e:
                last = f"{type(e).__name__}"
            else:
                if j.get("code") == 0:
                    return j.get("data") or {}
                last = f"{j.get('code')} {j.get('msg')}"
                if j.get("code") in (91402, 1254040, 1254041, 1254043, 99991672, 99991663):  # 无权限 / 不存在：不重试
                    break
            await asyncio.sleep(0.5 * (attempt + 1))
        hint = "（确认应用已开通多维表格权限，并在表格里加为可编辑协作者）" if "permission" in last.lower() or "91402" in last else ""
        raise BitableError(f"多维表格接口 {path.split('/')[-1]} 失败：{last}{hint}")

    def link(self) -> str:
        return self.url

    async def _resolve(self, c: httpx.AsyncClient) -> str:
        """解析 app_token / table_id，并确保列齐全（缺的列自动创建）。返回表路径前缀。"""
        if not self._app:
            token, table, wiki = parse_url(self.url)
            if wiki:
                node = (await self._call(c, "GET", "/wiki/v2/spaces/get_node", params={"token": token})).get("node") or {}
                if node.get("obj_type") != "bitable":
                    raise BitableError("这个知识库链接不是多维表格")
                token = node["obj_token"]
            if not table:
                items = (await self._call(c, "GET", f"/bitable/v1/apps/{token}/tables")).get("items") or []
                if not items:
                    raise BitableError("多维表格里没有数据表")
                table = items[0]["table_id"]
            self._app, self._table = token, table
        base = f"/bitable/v1/apps/{self._app}/tables/{self._table}"
        if not self._fields_ready:
            items = (await self._call(c, "GET", f"{base}/fields", params={"page_size": 100})).get("items") or []
            have = {f["field_name"] for f in items}
            prim = next((f for f in items if f.get("is_primary")), None)
            if "用例编号" not in have and prim and prim.get("type") == TEXT:
                await self._call(c, "PUT", f"{base}/fields/{prim['field_id']}", json={"field_name": "用例编号", "type": TEXT})
                have.add("用例编号")
            for col in COLS:
                if col.name in have:
                    continue
                body: dict[str, Any] = {"field_name": col.name, "type": col.type}
                if col.options:
                    body["property"] = {"options": [{"name": o} for o in col.options]}
                await self._call(c, "POST", f"{base}/fields", json=body)
            self._fields_ready = True
        return base

    def _fields(self, c: TestCase) -> dict[str, Any]:
        out = {}
        for col in COLS:
            v = to_cell(c, col)
            if v in (None, "", []) and col.attr not in ("review_note", "reviewer"):
                continue
            out[col.name] = v
        return out

    async def upsert(self, cases: list[TestCase]) -> dict[str, str]:
        """写入 / 更新记录（每批 ≤ 500），返回 {case_id: record_id}。"""
        if not cases:
            return {}
        for x in cases:  # 模块改到了另一张表：旧表的记录 ID 作废，按用例编号在新表里找或新建
            if x.bitable_record_id and x.bitable_url and table_key(x.bitable_url) != self.key:
                x.bitable_record_id = ""
        async with self._client() as c:
            base = await self._resolve(c)
            ids: dict[str, str] = {}
            missing = [x for x in cases if not x.bitable_record_id]
            if missing:  # 本地没有记录 ID 的，先按用例编号到表里找，避免重复建行
                known = {cell_text(r["fields"].get("用例编号")): r["record_id"] for r in await self._list(c, base)}
                for x in missing:
                    if x.case_id in known:
                        x.bitable_record_id = known[x.case_id]
            new = [x for x in cases if not x.bitable_record_id]
            old = [x for x in cases if x.bitable_record_id]
            for i in range(0, len(new), 500):
                chunk = new[i:i + 500]
                d = await self._call(c, "POST", f"{base}/records/batch_create",
                                     json={"records": [{"fields": self._fields(x)} for x in chunk]})
                for x, r in zip(chunk, d.get("records") or []):
                    x.bitable_record_id = r["record_id"]
            for i in range(0, len(old), 500):
                chunk = old[i:i + 500]
                await self._call(c, "POST", f"{base}/records/batch_update",
                                 json={"records": [{"record_id": x.bitable_record_id, "fields": self._fields(x)}
                                                   for x in chunk]})
            now = time.time()
            for x in cases:
                x.synced_at, x.bitable_url = now, self.url
                ids[x.case_id] = x.bitable_record_id
            return ids

    async def _list(self, c: httpx.AsyncClient, base: str) -> list[dict]:
        rows, page = [], ""
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page:
                params["page_token"] = page
            d = await self._call(c, "GET", f"{base}/records", params=params)
            rows += d.get("items") or []
            if not d.get("has_more"):
                return rows
            page = d.get("page_token", "")

    async def records(self) -> list[dict]:
        async with self._client() as c:
            return await self._list(c, await self._resolve(c))


# ---------- 同步（表格 → CabinPilot） ----------
async def sync_back(client: BitableClient, on_change: Callable[[str, dict], None] | None = None,
                    route: bool = False) -> dict:
    """以多维表格为准更新本地用例库；返回 {updated, created, invalid, replan, batches_done}。

    route=True（已配置的表）：只认路由到这张表的模块的行，别的模块的行提示“应在另一张表”且不同步，
    避免模块换表后旧表里的过期行把新表的修改盖掉。"""
    from ..caselib import get_caselib
    from ..store import get_store

    lib, st = get_caselib(), get_store()
    rows = await client.records()
    seen: dict[str, str] = {}
    res: dict[str, Any] = {"updated": [], "created": [], "invalid": [], "replan": [], "batches_done": [], "case_ids": [],
                           "rows": len(rows), "at": time.time()}
    for row in rows:
        vals, errs = validate_row(row.get("fields") or {})
        cid = vals.get("case_id", "")
        if cid and cid in seen:
            errs.append(f"用例编号与另一行重复（{seen[cid]}）")
        if cid and not errs and route and not client.owns(cid):
            m = modules.of_case(cid)
            errs.append(f"属于模块 {m.code}（{m.name}），按 modules.yaml 应在另一张表，本行不同步")
        if errs:
            if any(cell_text(v) for v in (row.get("fields") or {}).values()):  # 空行忽略
                res["invalid"].append({"record_id": row["record_id"], "case_id": cid, "errors": errs})
            continue
        seen[cid] = row["record_id"]
        cur = lib.get_case(cid)
        if not cur:  # 表格里人工新增的用例
            c = TestCase(**{k: v for k, v in vals.items() if v not in (None, "")}, bitable_record_id=row["record_id"],
                         bitable_url=client.url, synced_at=time.time())
            c.source = vals.get("source") or "人工编写"
            c.batch_id = vals.get("batch_id") or "bitable"
            lib.save_case(c)
            res["created"].append(cid)
            continue
        diff = {}
        for col in COLS:
            if not col.synced_back or col.attr not in vals:
                continue
            new = vals[col.attr]
            old = getattr(cur, col.attr)
            if col.type == TEXT and col.attr != "steps":
                new, old = new or "", old or ""
            if new != old and not (new in (None, "", []) and old in (None, "", [])):
                diff[col.attr] = {"from": old, "to": new}
        cur.bitable_record_id, cur.bitable_url, cur.synced_at = row["record_id"], client.url, time.time()
        if not diff:
            lib.save_case(cur)
            continue
        for k, d in diff.items():
            setattr(cur, k, d["to"])
        if PLAN_FIELDS & diff.keys() and cur.plan_status:
            cur.plan_status, cur.auto_status = "", "需重新生成"
            res["replan"].append(cid)
        lib.save_case(cur)
        status = diff.get("review_status", {}).get("to", "")
        st.x("INSERT INTO case_review(case_id, batch_id, decision, reason, diff, reviewer, at) VALUES(?,?,?,?,?,?,?)",
             (cid, cur.batch_id, f"bitable:{status}" if status else "bitable:edit", cur.review_note,
              json.dumps(diff, ensure_ascii=False, default=str), cur.reviewer or "多维表格", time.time()))
        if status == "驳回":
            st.x("INSERT INTO eval_set(defect_id, rule, reason, doc, at) VALUES(?,?,?,?,?)",
                 (cid, "case-gen", cur.review_note or "多维表格驳回",
                  json.dumps({"case": cur.model_dump(), "diff": diff}, ensure_ascii=False, default=str), time.time()))
        res["updated"].append({"case_id": cid, "fields": list(diff)})
        if on_change:
            on_change(cid, diff)
    res["case_ids"] = list(seen)
    # 批次里的用例都在表格里审完了 → 批次置为已审核
    for b in [x for x in lib.list_batches(100) if x["status"] == "待审核"]:
        cs = lib.batch_cases(b["batch_id"])
        if cs and all(x.review_status != "待审核" for x in cs):
            bb = lib.get_batch(b["batch_id"])
            bb.status = "已审核"
            lib.save_batch(bb)
            res["batches_done"].append(b["batch_id"])
    client.last_sync = {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}
    client.last_sync["invalid_rows"] = res["invalid"][:20]
    return res


_clients: dict[tuple[str, str], BitableClient] = {}


def client_for_url(url: str) -> BitableClient:
    """同一张表复用一个客户端（令牌、列检查只做一次）；url 为空即默认表。"""
    url = url or get_settings().feishu_bitable_url
    k = table_key(url)
    if k not in _clients:
        _clients[k] = BitableClient(url=url)
    return _clients[k]


def get_bitable(code: str = "") -> BitableClient:
    """模块的用例表；不给模块即默认表。"""
    return client_for_url(modules.bitable_url(code) if code else "")


def all_bitables() -> list[BitableClient]:
    """已启用的全部用例表（默认表 + 各模块单独配的表，去重）。"""
    urls = [get_settings().feishu_bitable_url] + [m.bitable for m in modules.all_modules() if m.bitable]
    out: list[BitableClient] = []
    for u in urls:
        if not u:
            continue
        c = client_for_url(u)
        if c.enabled and c not in out:
            out.append(c)
    return out


def enabled() -> bool:
    return bool(all_bitables())
