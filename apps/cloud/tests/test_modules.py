"""模块路由：modules.yaml → 模块缩写 / 文件夹 / 多维表格；按模块分表写入与同步、换表、PRD 存档、文件夹调整。"""
import httpx
import pytest

from cabin_cloud import casewf, modules, planner
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.integ import bitable
from cabin_cloud.prd import PrdDoc, parse_bytes
from cabin_common.schema import CaseBatch, PrdVersion, TestCase

from test_bitable import FakeFeishu

URL_VOICE = "https://x.feishu.cn/base/bascnVOICE?table=tblV"
URL_DEFAULT = "https://x.feishu.cn/base/bascnDEF?table=tblD"


def _yaml(tmp_path, voice_folder="语音助手", voice_bitable=URL_VOICE, extra=""):
    p = tmp_path / "modules.yaml"
    p.write_text("modules:\n"
                 f"  - {{code: VOICE, name: 语音助手, keywords: [语音], folder: {voice_folder}, bitable: \"{voice_bitable}\"}}\n"
                 "  - {code: NAV, name: 导航, keywords: [导航], folder: 导航, bitable: \"\"}\n" + extra, "utf8")
    modules.reload()
    return p


@pytest.fixture
def routed(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "modules_file", _yaml(tmp_path))
    monkeypatch.setattr(s, "feishu_app_id", "cli_x")
    monkeypatch.setattr(s, "feishu_app_secret", "sec")
    monkeypatch.setattr(s, "feishu_bitable_url", URL_DEFAULT)
    monkeypatch.setattr(bitable, "_clients", {})
    fakes = {URL_VOICE: FakeFeishu(primary="用例编号"), URL_DEFAULT: FakeFeishu(primary="用例编号")}
    for u, f in fakes.items():
        bitable.client_for_url(u)._transport = httpx.MockTransport(f.handler)
    yield tmp_path, fakes[URL_VOICE], fakes[URL_DEFAULT]
    modules.reload()


def _case(code, title):
    lib = get_caselib()
    c = TestCase(case_id=lib.next_id(code), title=title, channel=["adb"], steps=["打开"], expected="显示 24℃",
                 models=["A01-CN"], batch_id="B-mod")
    lib.save_case(c)
    return c


def _ids(fake):
    return {bitable.cell_text(r.get("用例编号")) for r in fake.records.values()}


# ---------- 配置 ----------
def test_registry_codes_and_problems(tmp_path, monkeypatch):
    s = get_settings()
    p = _yaml(tmp_path, extra="  - {code: VOICE, name: 重复}\n  - {code: x-1, name: 坏}\n"
                              "  - {code: SEAT, name: 座椅, keywords: [座椅], folder: ../etc, bitable: \"http://abc\"}\n")
    monkeypatch.setattr(s, "modules_file", p)
    modules.reload()
    assert modules.code_for("语音助手/空调") == "VOICE" and modules.code_for("导航/路线") == "NAV"
    assert modules.code_for("新功能", "newf") == "NEWF"
    text = "；".join(modules.problems())
    assert "重复" in text and "X-1" in text.upper()
    seat = modules.get("SEAT")
    assert seat.folder == "SEAT" and seat.bitable == ""  # 不合法的文件夹 / 链接退回默认
    assert modules.get("ZZZ").folder == "ZZZ" and not modules.get("ZZZ").listed
    assert modules.find("语音助手") == "VOICE" and modules.find("voice") == "VOICE"

    # 改文件即生效（按修改时间重新读取）
    import os
    import time
    p.write_text("modules:\n  - {code: VOICE, name: 语音, keywords: [语音], folder: voice}\n", "utf8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert modules.get("VOICE").folder == "voice"

    monkeypatch.setattr(s, "modules_file", tmp_path / "none.yaml")
    assert modules.code_for("显示/亮度") == "DISP" and "内置" in modules.problems()[0]
    modules.reload()


def test_plan_and_prd_paths(routed):
    c = TestCase(case_id="TC-VOICE-901", title="t")
    assert planner.plan_rel_path(c) == "语音助手/cases/TC-VOICE-901.yaml"
    assert planner.plan_rel_path(TestCase(case_id="TC-ZZZ-001", title="t")) == "ZZZ/cases/TC-ZZZ-001.yaml"
    assert planner.prd_rel_path("VOICE", "语音助手_空调温度调节_PRD_V0.1.0.docx", "0.2.0", ".docx") == \
        "语音助手/prd/语音助手_空调温度调节_V0.2.0.docx"
    assert planner.prd_rel_path("NAV", "导航 需求.md", "1.0", ".md") == "导航/prd/导航_需求_V1.0.md"


# ---------- 按模块分表 ----------
async def test_push_and_sync_route_by_module(routed):
    _, fv, fd = routed
    v, n = _case("VOICE", "语音调温"), _case("NAV", "导航到公司")
    ok, note = await casewf.push_bitable(None, [v, n], "写入")
    assert ok and note == ""
    assert _ids(fv) == {v.case_id} and _ids(fd) == {n.case_id}  # 语音进自己的表，导航进默认表
    lib = get_caselib()
    assert lib.get_case(v.case_id).bitable_url == URL_VOICE

    # 两张表都同步，结果合并
    rid = lib.get_case(v.case_id).bitable_record_id
    fv.records[rid]["审核状态"] = "通过"
    r = await casewf.sync_now()
    assert r["ok"] and len(r["tables"]) == 2 and r["rows"] == 2
    assert lib.get_case(v.case_id).review_status == "通过"
    assert {tuple(t["modules"]) for t in r["tables"]} == {("VOICE",), ("NAV", "*")}
    assert casewf.LAST_SYNC["rows"] == 2


async def test_wrong_table_row_is_flagged_and_module_can_move(routed):
    tmp, fv, fd = routed
    lib = get_caselib()
    v = _case("VOICE", "语音调温")
    await casewf.push_bitable(None, [v], "写入")
    # 有人在默认表里手填了一行语音用例：不属于这张表，提示且不同步
    fd.records["recX"] = {"用例编号": "TC-VOICE-950", "标题": "手填", "通道": ["adb"], "预期结果": "x", "审核状态": "通过"}
    r = await casewf.sync_now()
    bad = {x["case_id"]: x["errors"] for x in r["invalid"]}
    assert "另一张表" in bad["TC-VOICE-950"][0] and lib.get_case("TC-VOICE-950") is None

    # 语音模块改回默认表：再次写入时在默认表新建行；旧表里的过期行不再覆盖
    del fd.records["recX"]
    _yaml(tmp, voice_bitable="")
    old_rid = lib.get_case(v.case_id).bitable_record_id
    await casewf.push_bitable(None, [lib.get_case(v.case_id)], "写入")
    cur = lib.get_case(v.case_id)
    assert v.case_id in _ids(fd) and cur.bitable_url == URL_DEFAULT
    fv.records[old_rid]["标题"] = "旧表里的过期修改"
    r = await casewf.sync_now()  # 语音表已不在配置里，只同步默认表
    assert len(r["tables"]) == 1 and lib.get_case(v.case_id).title == "语音调温"


async def test_unconfigured_module_table_stays_local(routed, monkeypatch):
    monkeypatch.setattr(get_settings(), "feishu_bitable_url", "")
    v, n = _case("VOICE", "语音"), _case("NAV", "导航")
    ok, note = await casewf.push_bitable(None, [v, n], "写入")
    assert not ok and "NAV" in note and get_caselib().get_case(v.case_id).bitable_record_id


# ---------- 文件夹 ----------
async def test_archive_prd_and_relocate(routed):
    tmp, _, _ = routed
    lib = get_caselib()
    doc = parse_bytes("语音助手_空调温度调节_PRD_V0.1.0.md", "# 空调温度调节\n\n文档版本：0.1.0\n\n## 1 需求\n调温".encode(), "upload:x")
    b = CaseBatch(batch_id=lib.new_batch_id(), module="语音助手/空调", prd_id="PRD-VOICE-T1", prd_version="0.1.0")
    lib.save_prd_version(PrdVersion(prd_id=b.prd_id, version="0.1.0", batch_id=b.batch_id))
    rel = await planner.archive_prd(b, doc)
    assert rel == "语音助手/prd/语音助手_空调温度调节_V0.1.0.md"
    assert (planner.plans_dir() / rel).read_bytes() == doc.raw
    assert lib.get_prd_version(b.prd_id, "0.1.0").archive_path == rel
    assert rel in planner._git("log", "-1", "--name-only", "--format=")

    online = PrdDoc(name="导航需求", source="feishu:x")  # 在线文档没有原文件：存 Markdown
    b2 = CaseBatch(batch_id=lib.new_batch_id(), module="导航", prd_id="PRD-NAV-T2", prd_version="1.0")
    assert await planner.archive_prd(b2, online) == "导航/prd/导航需求_V1.0.md"

    c = _case("VOICE", "语音调温")
    c.plan_path = planner.plan_rel_path(c)
    (planner.plans_dir() / c.plan_path).parent.mkdir(parents=True, exist_ok=True)
    (planner.plans_dir() / c.plan_path).write_text("steps: []\n", "utf8")
    lib.save_case(c)

    _yaml(tmp, voice_folder="语音")
    moved = dict(planner.relocate())
    assert moved[f"语音助手/cases/{c.case_id}.yaml"] == f"语音/cases/{c.case_id}.yaml"
    assert moved[rel] == "语音/prd/语音助手_空调温度调节_V0.1.0.md"
    assert lib.get_case(c.case_id).plan_path == f"语音/cases/{c.case_id}.yaml"
    assert (planner.plans_dir() / "语音/cases" / f"{c.case_id}.yaml").exists()
    assert lib.get_prd_version(b.prd_id, "0.1.0").archive_path.startswith("语音/prd/")
    assert planner.relocate() == []  # 再跑一次无变化


def test_pull_by_module_name(routed):
    lib = get_caselib()
    v = _case("VOICE", "语音")
    v.review_status = "通过"
    lib.save_case(v)
    got = {c.case_id for c in lib.pull("A01-CN", module="语音助手")}
    assert v.case_id in got and all(x.startswith("TC-VOICE-") for x in got)
