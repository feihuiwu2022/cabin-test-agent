"""飞书多维表格用例库：写入、建列、去重、反向同步、变更触发、字段校验（用内存假飞书接口）。"""
import dataclasses
import json
import re

import httpx
import pytest

from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.integ.bitable import BitableClient, BitableError, parse_url, sync_back, validate_row
from cabin_common.schema import CaseBatch, TestCase


class FakeFeishu:
    def __init__(self, primary="多行文本"):
        self.fields = [{"field_id": "f0", "field_name": primary, "type": 1, "is_primary": True}]
        self.records: dict[str, dict] = {}
        self.n = 0
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, body = req.url.path, json.loads(req.content or b"{}")
        self.calls.append(f"{req.method} {path.split('/')[-1]}")
        ok = lambda data: httpx.Response(200, json={"code": 0, "data": data})  # noqa: E731
        if path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        if path.endswith("/fields") and req.method == "GET":
            return ok({"items": self.fields})
        if path.endswith("/fields") and req.method == "POST":
            self.fields.append({"field_id": f"f{len(self.fields)}", "is_primary": False, **body})
            return ok({"field": body})
        if re.search(r"/fields/\w+$", path) and req.method == "PUT":
            fid = path.rsplit("/", 1)[1]
            next(f for f in self.fields if f["field_id"] == fid).update(body)
            return ok({})
        if path.endswith("records/batch_create"):
            out = []
            for r in body["records"]:
                self.n += 1
                rid = f"rec{self.n}"
                self.records[rid] = dict(r["fields"])
                out.append({"record_id": rid, "fields": r["fields"]})
            return ok({"records": out})
        if path.endswith("records/batch_update"):
            for r in body["records"]:
                self.records[r["record_id"]].update(r["fields"])
            return ok({"records": body["records"]})
        if path.endswith("/records") and req.method == "GET":
            # 文本字段按飞书返回富文本片段数组，验证读回兼容
            items = [{"record_id": k, "fields": {f: ([{"type": "text", "text": v}] if isinstance(v, str) else v)
                                                 for f, v in fs.items()}} for k, fs in self.records.items()]
            return ok({"items": items, "has_more": False})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})


def client(fake: FakeFeishu) -> BitableClient:
    c = BitableClient(transport=httpx.MockTransport(fake.handler))
    c.s = dataclasses.replace(get_settings(), feishu_app_id="cli_x", feishu_app_secret="s",
                              feishu_bitable_url="https://x.feishu.cn/base/bascnAAA?table=tblBBB&view=vew1")
    return c


def test_bitable_link_routes_to_table_reading():
    from cabin_cloud import casewf
    from cabin_cloud.prd import PrdError, load_url  # noqa: F401
    u = "https://x.feishu.cn/base/AbC123?table=tblX&view=vewY"
    assert casewf.wants(u, False) == "bitable" and casewf.wants(f"读取 {u} 生成 yaml", False) == "bitable"
    assert casewf.wants("https://x.feishu.cn/docx/AbC123 生成用例", False) == "import"


async def test_sheet_link_gets_clear_message():
    from cabin_cloud.prd import PrdError, load_url
    with pytest.raises(PrdError, match="导出"):
        await load_url("https://x.feishu.cn/sheets/AbC")


async def test_read_bitable_pulls_only_this_table(monkeypatch):
    from cabin_cloud import casewf

    fake = FakeFeishu(primary="用例编号")
    bt = client(fake)
    _, cs = _seed_batch()
    lib = get_caselib()
    await bt.upsert(cs)
    a, z = cs
    fake.records[a.bitable_record_id]["审核状态"] = "通过"
    monkeypatch.setattr(casewf, "all_bitables", lambda: [bt])

    class Sink:
        id, state, msgs = "S-t", {}, []

        async def send(self, m):
            self.msgs.append(m)

        async def log(self, text, kind=""):
            pass

        async def say(self, text, src=""):
            pass

    s = Sink()
    reply = await casewf.read_bitable(s, f"{bt.link()}&view=vew1")
    assert "审核通过 1 条" in reply and "待审核 1 条" in reply
    pulled = next(m for m in s.msgs if m["type"] == "casepull.result")
    assert [r["case_id"] for r in pulled["cases"]] == [a.case_id]  # 只处理这张表里通过的
    await s.state["plan_job"]
    assert lib.get_case(a.case_id).plan_status == "ready" and lib.get_case(z.case_id).plan_status == ""
    assert any(m["type"] == "plan.result" and m["case_id"] == a.case_id and m["yaml"] for m in s.msgs)


def test_parse_url():
    assert parse_url("https://x.feishu.cn/base/bascn123?table=tbl9&view=v") == ("bascn123", "tbl9", False)
    assert parse_url("https://x.feishu.cn/wiki/wikcnQ?table=tbl1") == ("wikcnQ", "tbl1", True)
    with pytest.raises(BitableError):
        parse_url("https://x.feishu.cn/docx/abc")


def test_validate_row():
    good = {"用例编号": "TC-VOICE-001", "标题": "调到 24 度", "通道": ["audio", "adb"], "测试语句": "座椅加热调到 3 档",
            "期望屏幕": "24℃", "期望播报": "24 度", "审核状态": "通过", "测试步骤": "1. 唤醒\n2. 说指令"}
    vals, errs = validate_row(good)
    assert errs == [] and vals["steps"] == ["唤醒", "说指令"] and vals["channel"] == ["audio", "adb"]
    _, errs = validate_row({**good, "用例编号": "x1", "审核状态": "大概行", "期望播报": ""})
    text = "；".join(errs)
    assert "格式" in text and "审核状态" in text and "期望播报" in text


def _seed_batch():
    lib = get_caselib()
    b = CaseBatch(batch_id=lib.new_batch_id(), status="待审核", module="语音助手/空调")
    cs = [TestCase(case_id=lib.next_id("BTV"), title="调到 24 度", batch_id=b.batch_id, channel=["audio", "adb"],
                   utterance="座椅加热调到 3 档", expect_screen="24℃", expect_tts="已为您调到 24 度", precondition="空调已开",
                   steps=["唤醒", "说指令"], expected="24℃", models=["A01-CN"]),
          TestCase(case_id=lib.next_id("BTV"), title="调到 16 度", type="边界", batch_id=b.batch_id,
                   channel=["audio", "adb"], utterance="座椅加热调到 1 档", expect_screen="16℃",
                   expect_tts="已为您调到 16 度", precondition="空调已开", steps=["唤醒", "说指令"], models=["A01-CN"])]
    b.case_ids = [c.case_id for c in cs]
    lib.save_batch(b)
    for c in cs:
        lib.save_case(c)
    return b, cs


async def test_upsert_creates_columns_and_no_duplicate_rows():
    fake = FakeFeishu()
    bt = client(fake)
    _, cs = _seed_batch()
    ids = await bt.upsert(cs)
    names = {f["field_name"] for f in fake.fields}
    assert {"用例编号", "审核状态", "测试语句", "通道"} <= names
    assert fake.fields[0]["field_name"] == "用例编号"  # 主字段改名，不另建
    assert len(fake.records) == 2 and set(ids.values()) == set(fake.records)
    row = fake.records[ids[cs[0].case_id]]
    assert row["审核状态"] == "待审核" and row["测试步骤"] == "1. 唤醒\n2. 说指令" and row["通道"] == ["audio", "adb"]

    # 本地丢了 record_id 也不会重复建行（按用例编号回找）
    cs[0].bitable_record_id = ""
    cs[0].review_status = "通过"
    await bt.upsert(cs)
    assert len(fake.records) == 2 and fake.records[ids[cs[0].case_id]]["审核状态"] == "通过"


async def test_sync_back_bitable_wins():
    fake = FakeFeishu(primary="用例编号")
    bt = client(fake)
    b, cs = _seed_batch()
    lib = get_caselib()
    await bt.upsert(cs)
    for c in cs:
        lib.save_case(c)
    a, z = cs
    a.plan_status, a.auto_status = "ready", "已生成"
    lib.save_case(a)
    ra, rz = a.bitable_record_id, z.bitable_record_id
    # 表格里：a 改预期并通过；z 驳回；人工新增一行；一行不合格；一行与 a 编号重复
    fake.records[ra].update({"期望屏幕": "空调温度显示 24℃", "审核状态": "修改后通过", "审核人": "张工"})
    fake.records[rz].update({"审核状态": "驳回", "审核意见": "与已有用例重复"})
    fake.records["recM"] = {"用例编号": "TC-BTV-900", "标题": "人工补充：关闭空调", "通道": ["audio"], "测试语句": "关闭座椅加热",
                            "期望屏幕": "空调开关为关", "期望播报": "空调已关闭", "审核状态": "通过"}
    fake.records["recBad"] = {"用例编号": "TC-BTV-901", "标题": "", "审核状态": "随便"}
    fake.records["recDup"] = {**fake.records[ra]}

    r = await sync_back(bt)
    got_a, got_z = lib.get_case(a.case_id), lib.get_case(z.case_id)
    assert got_a.expect_screen == "空调温度显示 24℃" and got_a.review_status == "修改后通过" and got_a.reviewer == "张工"
    assert got_a.plan_status == "" and got_a.auto_status == "需重新生成" and a.case_id in r["replan"]
    assert got_z.review_status == "驳回"
    assert r["created"] == ["TC-BTV-900"] and lib.get_case("TC-BTV-900").source == "人工编写"
    bad = {x["record_id"]: x["errors"] for x in r["invalid"]}
    assert "recBad" in bad and any("重复" in e for e in bad["recDup"])
    assert b.batch_id in r["batches_done"] and lib.get_batch(b.batch_id).status == "已审核"
    assert {x.case_id for x in lib.pull("A01-CN")} >= {a.case_id, "TC-BTV-900"}
    assert z.case_id not in {x.case_id for x in lib.pull("A01-CN")}

    r2 = await sync_back(bt)  # 再同步一次无变化
    assert r2["updated"] == [] and r2["created"] == []


async def test_permission_error_is_readable():
    def deny(req):
        if req.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        return httpx.Response(200, json={"code": 91402, "msg": "NOTEXIST or no permission"})

    bt = client(FakeFeishu())
    bt._transport = httpx.MockTransport(deny)
    with pytest.raises(BitableError, match="协作者"):
        await bt.records()
