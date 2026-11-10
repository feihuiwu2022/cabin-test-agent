"""重复导入与同版本变更：内容去重、已发布版本不可覆盖、升版本 / 作为修订、取代未审完的批次、按章节继承、来源绑定。"""
import pytest

from cabin_cloud import casegen, casewf
from cabin_cloud.bus import bus
from cabin_cloud.caselib import get_caselib
from cabin_cloud.prd import load_path
from test_versioning import C_MAX, C_SET, C_UP, R_MAX, R_SET, R_UP, _extract, _pass_all

BODY = "## 3 功能需求\n\n3.1 设定温度。\n\n3.2 高一点。\n\n3.3 超范围。\n"


def _doc(tmp_path, title, ver, body=BODY, source=""):
    p = tmp_path / f"{abs(hash((title, ver, body)))}.md"
    p.write_text(f"# {title}\n\n" + (f"文档版本：{ver}\n\n" if ver else "") + body, "utf8")
    d = load_path(str(p))
    if source:
        d.source = source
    return d


def _fake(monkeypatch, ex, cases):
    calls = []

    async def fake_llm(system, prompt, source, session, max_tokens=4000):
        calls.append(source)
        if source == "casegen.extract":
            return ex
        return {"cases": [c for c in cases if c["req_id"] in prompt]}

    monkeypatch.setattr(casegen, "_llm_json", fake_llm)
    return calls


R_UP2 = {**R_UP, "quote": "说“温度高一点”时加 2 度"}
C_UP2 = {**C_UP, "title": "高一点 2 度"}


async def test_same_content_is_found_and_not_regenerated(monkeypatch, tmp_path):
    lib = get_caselib()
    _fake(monkeypatch, _extract([R_SET, R_UP], []), [C_SET, C_UP])
    d = _doc(tmp_path, "去重 · 空调", "1.0.0")
    b = await casegen.generate(d)
    assert casegen.find_existing(d).batch_id == b.batch_id  # 待审核中也算已生成
    assert casegen.find_existing(d, "3.1") is None  # 章节范围不同不算
    assert casegen.find_existing(_doc(tmp_path, "去重 · 空调", "1.0.0", BODY + "\n3.4 新需求。\n")) is None
    _pass_all(lib, b)
    assert casegen.find_existing(d).status == "已审核"
    # 飞书：同一文档同一 revision 直接命中
    f1 = _doc(tmp_path, "去重飞书", "1.0.0", source="feishu:TOKDEDUP")
    f1.revision = "7"
    b2 = await casegen.generate(f1)
    f2 = _doc(tmp_path, "去重飞书", "1.0.0", BODY + "\n（排版调整）\n", source="feishu:TOKDEDUP")
    f2.revision = "7"
    assert casegen.find_existing(f2).batch_id == b2.batch_id
    f2.revision = "8"
    assert casegen.find_existing(f2) is None


async def test_published_version_cannot_be_overwritten(monkeypatch, tmp_path):
    lib = get_caselib()
    _fake(monkeypatch, _extract([R_SET, R_UP, R_MAX], []), [C_SET, C_UP, C_MAX])
    b1 = await casegen.generate(_doc(tmp_path, "覆盖 · 空调", "0.6"))
    _pass_all(lib, b1)
    base = lib.get_baseline(f"{b1.prd_id}@0.6")

    # 同版本号、内容变了：不生成，抛 VersionConflict，已发布版本和基线都不动
    calls = _fake(monkeypatch, _extract([R_SET, R_UP2, R_MAX], []), [C_SET, C_UP2, C_MAX])
    d2 = _doc(tmp_path, "覆盖 · 空调", "0.6", BODY.replace("高一点", "高一点（加 2 度）"))
    with pytest.raises(casegen.VersionConflict) as e:
        await casegen.generate(d2)
    assert (e.value.prd_id, e.value.version) == (b1.prd_id, "0.6") and calls == ["casegen.extract"]
    assert lib.get_prd_version(b1.prd_id, "0.6").status == "已发布"
    assert lib.get_baseline(f"{b1.prd_id}@0.6") == base

    # 作为修订：0.6+r2，以 0.6 为基准，只为改了的需求出新修订；不再调抽取
    b2 = await casegen.generate(d2, as_revision=True, extracted=e.value.extracted)
    assert calls == ["casegen.extract", "casegen.generate"]
    assert (b2.prd_version, b2.prev_version) == ("0.6+r2", "0.6")
    assert b2.diff == {"新增": 0, "修改": 1, "未改": 2, "删除": 0}
    (c,) = lib.batch_cases(b2.batch_id)
    assert c.case_rev == 2 and c.case_id in base
    _pass_all(lib, b2)
    assert lib.get_baseline(f"{b1.prd_id}@0.6") == base  # 旧基线原样
    assert lib.get_baseline(f"{b1.prd_id}@0.6+r2") == {**base, c.case_id: 2}
    assert lib.next_revision(b1.prd_id, "0.6") == "0.6+r3"

    # 升版本号：测试给的版本号优先于 PRD 原文，标记测试预填；以最新的 0.6+r2 为基准
    _fake(monkeypatch, _extract([R_SET, R_UP2], []), [C_SET, C_UP2])
    b3 = await casegen.generate(_doc(tmp_path, "覆盖 · 空调", "0.6", BODY.replace("3.3 超范围。", "")),
                                version_override="0.7")
    assert (b3.prd_version, b3.version_source, b3.prev_version) == ("0.7", "测试预填", "0.6+r2")
    assert b3.diff["删除"] == 1 and b3.status == "已审核"  # 只删了需求：继承发布


def test_published_records_are_guarded():
    lib = get_caselib()
    from cabin_common.schema import CaseBatch, PrdVersion
    lib.save_prd_version(PrdVersion(prd_id="PRD-G-0001", version="1.0", batch_id="B-G1", status="已发布"))
    with pytest.raises(ValueError):
        lib.save_prd_version(PrdVersion(prd_id="PRD-G-0001", version="1.0", batch_id="B-G2", status="待审核"))
    with pytest.raises(ValueError):
        lib.save_prd_version(PrdVersion(prd_id="PRD-G-0001", version="1.0", batch_id="B-G1", status="待审核"))
    lib.save_prd_version(PrdVersion(prd_id="PRD-G-0001", version="1.0", batch_id="B-G1", status="已发布",
                                    archive_path="x.md"))  # 原批次补存档路径可以
    with pytest.raises(ValueError):
        lib.publish_baseline(CaseBatch(batch_id="B-G2", prd_id="PRD-G-0001", prd_version="1.0", status="已审核"))


async def test_pending_batch_is_superseded(monkeypatch, tmp_path):
    lib = get_caselib()
    _fake(monkeypatch, _extract([R_SET, R_UP], []), [C_SET, C_UP])
    b1 = await casegen.generate(_doc(tmp_path, "取代 · 空调", "0.1"))
    _pass_all(lib, b1)
    up_id = next(c.case_id for c in lib.batch_cases(b1.batch_id) if c.req_id == "REQ-02")

    # 0.2 第一次导入：REQ-02 改了 → 出 r2，待审核
    _fake(monkeypatch, _extract([R_SET, R_UP2], []), [C_SET, C_UP2])
    up2 = BODY.replace("高一点", "高一点加 2 度")
    b2 = await casegen.generate(_doc(tmp_path, "取代 · 空调", "0.2", up2))
    assert [(c.case_id, c.case_rev) for c in lib.batch_cases(b2.batch_id)] == [(up_id, 2)]

    # 0.2 没审完又改回去了：上一批作废，REQ-02 又是未改 → 当前修订恢复为 r1，用例仍在库里
    _fake(monkeypatch, _extract([R_SET, R_UP], []), [C_SET, C_UP])
    b3 = await casegen.generate(_doc(tmp_path, "取代 · 空调", "0.2"))
    assert lib.get_batch(b2.batch_id).status == "已作废" and b3.params["superseded"] == b2.batch_id
    assert lib.get_case(up_id).case_rev == 1 and up_id in {c.case_id for c in lib.library()}
    assert b3.status == "已审核" and b3.prev_version == "0.1"
    with pytest.raises(ValueError):  # 作废的批次不能再提交审核
        lib.submit_review(b2.batch_id, [{"case_id": up_id, "decision": "pass"}])

    # 再出修改：修订号复用 r2，不跳号
    _fake(monkeypatch, _extract([R_SET, R_UP2], []), [C_SET, C_UP2])
    b4 = await casegen.generate(_doc(tmp_path, "取代 · 空调", "0.3", up2))
    assert [(c.case_id, c.case_rev) for c in lib.batch_cases(b4.batch_id)] == [(up_id, 2)]


async def test_other_section_pending_blocks(monkeypatch, tmp_path):
    _fake(monkeypatch, _extract([R_SET], []), [C_SET])
    await casegen.generate(_doc(tmp_path, "并行 · 空调", "0.1"), section="3.1")
    _fake(monkeypatch, _extract([R_UP], []), [C_UP])
    with pytest.raises(casegen.PrdBusy):
        await casegen.generate(_doc(tmp_path, "并行 · 空调", "0.1"), section="3.2")


async def test_section_import_inherits_other_sections(monkeypatch, tmp_path):
    lib = get_caselib()
    _fake(monkeypatch, _extract([R_SET, R_UP, R_MAX], []), [C_SET, C_UP, C_MAX])
    b1 = await casegen.generate(_doc(tmp_path, "章节 · 空调", "0.1"))
    _pass_all(lib, b1)
    base = lib.get_baseline(f"{b1.prd_id}@0.1")
    # 0.2 只导入 §3.2：3.1 / 3.3 不算删除，用例继承；§3.2 新增一条需求，编号不撞
    r_new = {"section": "3.2", "quote": "说“温度低一点”时减 1 度", "summary": "调低"}
    c_new = {**C_UP, "req_id": "REQ-04", "title": "低一点", "utterance": "温度低一点", "expect_screen": "21℃",
             "expect_tts": "已为您调到 21 度"}
    _fake(monkeypatch, _extract([R_UP, r_new], []), [c_new])
    b2 = await casegen.generate(_doc(tmp_path, "章节 · 空调", "0.2", BODY + "\n3.2.1 低一点。\n"), section="3.2")
    assert b2.diff == {"新增": 1, "修改": 0, "未改": 3, "删除": 0}
    assert sorted(r.req_id for r in b2.requirements) == ["REQ-01", "REQ-02", "REQ-03", "REQ-04"]
    assert set(b2.inherited) == set(base) and not b2.retired
    _pass_all(lib, b2)
    assert len(lib.get_baseline(f"{b1.prd_id}@0.2")) == len(base) + 1


async def test_feishu_source_keeps_identity_after_rename(monkeypatch, tmp_path):
    lib = get_caselib()
    _fake(monkeypatch, _extract([R_SET], []), [C_SET])
    b1 = await casegen.generate(_doc(tmp_path, "飞书 · 空调", "0.1", source="feishu:TOKRENAME"))
    _pass_all(lib, b1)
    assert lib.prd_for_source("feishu:TOKRENAME") == b1.prd_id
    # 标题改了、模块判断也变了：仍是同一份 PRD，和 0.1 对比
    ex = {**_extract([R_SET], []), "module": "车控/空调", "module_code": "HVAC"}
    _fake(monkeypatch, ex, [C_SET])
    b2 = await casegen.generate(_doc(tmp_path, "空调温度（改名）", "0.2", source="feishu:TOKRENAME"))
    assert b2.prd_id == b1.prd_id and b2.prev_version == "0.1" and b2.diff["未改"] == 1


# ---------- 对话流程 ----------
class _Sess:
    def __init__(self, sid):
        self.id, self.state, self.user, self.said = sid, {}, "张工", []

    async def send(self, msg):
        pass

    async def log(self, text, kind=""):
        pass

    async def say(self, text, src=""):
        self.said.append(text)


async def _finish(s):
    await s.state["casegen_job"]


async def test_chat_dedupe_conflict_and_revision(monkeypatch, tmp_path):
    lib = get_caselib()
    s = _Sess("S-reimport")
    bus.sessions.pop(s.id, None)
    p = tmp_path / "chat_prd.md"
    p.write_text("# 对话 · 空调\n\n文档版本：0.6\n\n" + BODY, "utf8")
    _fake(monkeypatch, _extract([R_SET, R_UP], []), [C_SET, C_UP])
    assert "收到" in await casewf.start_import(s, f"把 {p} 转成用例", None)
    await _finish(s)
    b1 = lib.get_batch(s.state["case_batch"])
    _pass_all(lib, b1)

    # 同内容再发：不生成
    calls = _fake(monkeypatch, _extract([R_SET, R_UP], []), [C_SET, C_UP])
    s.state.pop("casegen_job")
    reply = await casewf.start_import(s, f"把 {p} 转成用例", None)
    assert "不再重复生成" in reply and f"{b1.prd_id}@0.6" in reply and "casegen_job" not in s.state and not calls

    # 改了内容、版本号没改：问测试
    p.write_text("# 对话 · 空调\n\n文档版本：0.6\n\n" + BODY.replace("高一点", "高一点 2 度"), "utf8")
    calls = _fake(monkeypatch, _extract([R_SET, R_UP2], []), [C_SET, C_UP2])
    await casewf.start_import(s, f"把 {p} 转成用例", None)
    await _finish(s)
    assert "作为修订" in s.said[-1] and s.state["prd_choice"]["version"] == "0.6"
    assert await casewf.resolve_choice(s, "随便聊聊") is None
    assert "已发布的版本号" in await casewf.resolve_choice(s, "版本 0.6")
    assert "修订" in await casewf.resolve_choice(s, "作为修订")
    await _finish(s)
    b2 = lib.get_batch(s.state["case_batch"])
    assert b2.prd_version == "0.6+r2" and calls == ["casegen.extract", "casegen.generate"]
    assert "prd_choice" not in s.state and await casewf.resolve_choice(s, "作为修订") is None


async def test_chat_version_is_passed_through(monkeypatch, tmp_path):
    lib = get_caselib()
    s = _Sess("S-chatver")
    bus.sessions.pop(s.id, None)
    p = tmp_path / "nover_prd.md"
    p.write_text("# 无版本号 · 空调\n\n" + BODY, "utf8")
    _fake(monkeypatch, _extract([R_SET], []), [C_SET])
    await casewf.start_import(s, f"把 {p} 转成用例，版本 2.1", None)
    await _finish(s)
    b = lib.get_batch(s.state["case_batch"])
    assert (b.prd_version, b.version_source) == ("2.1", "测试预填")
