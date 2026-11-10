"""飞书块解析（表格 / 容器块 / 块 ID）与块级增量抽取：只读变了的章节，没变的需求点不经模型直接沿用。"""
import pytest

from cabin_cloud import casegen
from cabin_cloud.caselib import get_caselib
from cabin_cloud.prd import PrdDoc, _number_headings, feishu_blocks, load_path
from test_versioning import C_SET, C_UP, R_SET, R_UP, _extract, _pass_all


# ---------- 飞书块 ----------
def _t(bid, text, bt=2, parent="doc", **kw):
    key = {2: "text", 12: "bullet", 13: "ordered"}.get(bt) or f"heading{bt - 2}"
    return {"block_id": bid, "block_type": bt, "parent_id": parent, key: {"elements": [{"text_run": {"content": text}}]},
            **kw}


def _table(bid, rows, parent="doc"):
    """rows: [[单元格文字, ...]]；每个单元格一个 cell 块，里面可以有多段文字（用 / 分隔）。"""
    items, cells = [], []
    for i, row in enumerate(rows):
        for j, txt in enumerate(row):
            cid = f"{bid}c{i}{j}"
            kids = [f"{cid}p{k}" for k in range(len(txt.split("/")))]
            items.append({"block_id": cid, "block_type": 32, "parent_id": bid, "children": kids})
            items += [_t(k, part, parent=cid) for k, part in zip(kids, txt.split("/"))]
            cells.append(cid)
    head = {"block_id": bid, "block_type": 31, "parent_id": parent, "children": cells,
            "table": {"cells": cells, "property": {"row_size": len(rows), "column_size": len(rows[0])}}}
    return [head, *items]


def test_feishu_blocks_tree_tables_and_containers():
    items = [
        {"block_id": "doc", "block_type": 1, "children": ["h1", "tb", "co", "gr", "hr", "h2", "li"]},
        _t("h1", "3 功能需求", bt=3),
        *_table("tb", [["需求", "A01-CN", "E01"], ["设定温度", "●", "—/仅选配"]]),
        {"block_id": "co", "block_type": 19, "parent_id": "doc", "children": ["cop"]},
        _t("cop", "注意：温度范围 16~32 度", parent="co"),
        {"block_id": "gr", "block_type": 24, "parent_id": "doc", "children": ["g1", "g2"]},
        {"block_id": "g1", "block_type": 25, "parent_id": "gr", "children": ["g1p"]},
        {"block_id": "g2", "block_type": 25, "parent_id": "gr", "children": ["g2p"]},
        _t("g1p", "左栏", parent="g1"), _t("g2p", "右栏", parent="g2"),
        {"block_id": "hr", "block_type": 22, "parent_id": "doc"},
        _t("h2", "3.1 设定温度", bt=4),
        _t("li", "说“空调调到 N 度”", bt=12, children=["li2"]),
        _t("li2", "设为 N 度并播报", bt=12, parent="li"),
    ]
    blocks = _number_headings(feishu_blocks(list(reversed(items)), "doc"))  # 接口返回顺序不可靠：按树还原
    assert [(b.kind, b.text.splitlines()[0], b.bid) for b in blocks] == [
        ("heading", "3 功能需求", "h1"), ("table", "表格：需求 | A01-CN | E01", "tb"),
        ("para", "注意：温度范围 16~32 度", "cop"), ("para", "左栏", "g1p"), ("para", "右栏", "g2p"),
        ("heading", "3.1 设定温度", "h2"), ("para", "说“空调调到 N 度”", "li"), ("para", "设为 N 度并播报", "li2")]
    assert blocks[1].text.splitlines()[1] == "需求=设定温度；A01-CN=●；E01=— 仅选配"  # 表头与行对上；单元格多段合并
    assert not any(b.bid.startswith("tbc") for b in blocks)  # 单元格里的文字不再散成段落
    assert blocks[6].section == "3.1"
    assert blocks[1].key == "tb" and load_path  # 飞书块用块 ID 作身份


def test_block_key_is_content_hash_without_bid(tmp_path):
    p = tmp_path / "k.md"
    p.write_text("# T\n\n一段 文字\n", "utf8")
    a = load_path(str(p)).blocks[1]
    p.write_text("# T\n\n一段文字\n", "utf8")
    b = load_path(str(p)).blocks[1]
    assert a.key == b.key and a.key.startswith("h:")  # 只差空白不算改动


# ---------- 块级增量 ----------
def _fsdoc(ver, rows: dict[str, str], token="TOKINC", title="增量 · 空调"):
    """飞书文档：rows = {块 ID: "章节号 文字"}，每段一个块。"""
    kids = ["t", "v", "h", *rows]
    items = [{"block_id": token, "block_type": 1, "children": kids}, _t("t", title, bt=3, parent=token),
             _t("v", f"文档版本：{ver}", parent=token), _t("h", "3 功能需求", bt=4, parent=token)]
    items += [_t(k, v, parent=token) for k, v in rows.items()]
    raw = "|".join(f"{k}={v}" for k, v in rows.items()) + ver
    return PrdDoc(name=title, source=f"feishu:{token}", blocks=_number_headings(feishu_blocks(items, token)),
                  sha=str(abs(hash(raw))), revision=str(abs(hash(raw)) % 997))


R_SET_Q = {**R_SET, "quote": "3.1 说空调调到 N 度时设为 N 度并播报"}
R_UP_Q = {**R_UP, "quote": "3.2 说温度高一点时加 1 度", "models": {}}
R_OFF_Q = {"section": "3.3", "quote": "3.3 说关闭空调时关闭空调", "summary": "关闭"}
C_OFF = {**C_UP, "req_id": "REQ-03", "title": "关空调", "utterance": "关闭空调", "expect_screen": "空调已关闭",
         "expect_tts": "空调已关闭"}
ROWS = {"p1": "3.1 说空调调到 N 度时设为 N 度并播报", "p2": "3.2 说温度高一点时加 1 度", "p3": "3.3 说关闭空调时关闭空调"}


def _fake(monkeypatch, ex, cases):
    seen = []

    async def fake_llm(system, prompt, source, session, max_tokens=4000):
        seen.append((source, prompt))
        if source == "casegen.extract":
            return ex
        return {"cases": [c for c in cases if c["req_id"] in prompt]}

    monkeypatch.setattr(casegen, "_llm_json", fake_llm)
    return seen


async def test_only_changed_sections_are_reread(monkeypatch, tmp_path):
    lib = get_caselib()
    _fake(monkeypatch, _extract([R_SET_Q, R_UP_Q, R_OFF_Q], []), [C_SET, C_UP, C_OFF])
    b1 = await casegen.generate(_fsdoc("0.1", ROWS))
    _pass_all(lib, b1)
    pv = lib.get_prd_version(b1.prd_id, "0.1")
    assert {r.req_id: r.blocks for r in pv.requirements} == {"REQ-01": ["p1"], "REQ-02": ["p2"], "REQ-03": ["p3"]}
    assert {e["k"] for e in pv.blocks} >= {"p1", "p2", "p3"}

    # 0.2：只改了 p2（同一块 ID，内容变了）→ 只把 §3.2 送去抽取，3.1 / 3.3 不经模型沿用
    r_up2 = {**R_UP_Q, "quote": "3.2 说温度高一点时加 2 度"}
    c_up2 = {**C_UP, "title": "高一点 2 度", "expect_screen": "24℃", "expect_tts": "已为您调到 24 度"}
    seen = _fake(monkeypatch, {**_extract([r_up2], []), "module": ""}, [c_up2])
    b2 = await casegen.generate(_fsdoc("0.2", {**ROWS, "p2": "3.2 说温度高一点时加 2 度"}))
    (src, prompt), *gen = seen
    assert src == "casegen.extract" and "加 2 度" in prompt and "关闭空调" not in prompt and "调到 N 度" not in prompt
    assert b2.params["incremental"]["dirty"] == ["1", "3.2"]  # 版本号那一段 + 3.2
    assert b2.diff == {"新增": 0, "修改": 1, "未改": 2, "删除": 0}
    assert b2.module == b1.module  # 模型没给模块：沿用基准
    (c,) = lib.batch_cases(b2.batch_id)
    assert c.req_id == "REQ-02" and c.case_rev == 2
    _pass_all(lib, b2)

    # 0.3：删掉 p3、插入新块 p4（3.4）→ REQ-03 删除，只读 §3.3 / §3.4
    r_new = {"section": "3.4", "quote": "3.4 说打开空调时开启空调", "summary": "开空调"}
    c_new = {**C_UP, "req_id": "REQ-04", "title": "开空调", "utterance": "打开空调", "expect_screen": "空调已开启",
             "expect_tts": "空调已打开"}
    seen = _fake(monkeypatch, _extract([r_new], []), [c_new])
    rows3 = {"p1": ROWS["p1"], "p2": "3.2 说温度高一点时加 2 度", "p4": "3.4 说打开空调时开启空调"}
    b3 = await casegen.generate(_fsdoc("0.3", rows3))
    assert "打开空调" in seen[0][1] and "加 2 度" not in seen[0][1]
    assert b3.diff == {"新增": 1, "修改": 0, "未改": 2, "删除": 1}
    assert [r.req_id for r in b3.requirements if r.change == "新增"] == ["REQ-04"]
    assert b3.retired and lib.get_prd_version(b3.prd_id, "0.3").deleted[0].req_id == "REQ-03"


async def test_no_changed_requirement_skips_model(monkeypatch, tmp_path):
    lib = get_caselib()
    rows = {**ROWS, "n1": "备注：本页为示例"}
    _fake(monkeypatch, _extract([R_SET_Q, R_UP_Q], []), [C_SET, C_UP])
    b1 = await casegen.generate(_fsdoc("1.0", rows, token="TOKSKIP", title="跳过 · 空调"))
    _pass_all(lib, b1)
    # 同版本号，只删了一段备注：版本冲突 → 升版本号；全程不调抽取（没有要重读的块）
    seen = _fake(monkeypatch, _extract([R_SET_Q], []), [])
    d2 = _fsdoc("1.0", ROWS, token="TOKSKIP", title="跳过 · 空调")
    with pytest.raises(casegen.VersionConflict) as e:
        await casegen.generate(d2)
    b2 = await casegen.generate(d2, version_override="1.1", extracted=e.value.extracted)
    assert seen == [] and b2.status == "已审核" and b2.diff["未改"] == 2
    assert lib.get_baseline(f"{b2.prd_id}@1.1") == lib.get_baseline(f"{b1.prd_id}@1.0")


async def test_base_mismatch_falls_back_to_full_extract(monkeypatch, tmp_path):
    lib = get_caselib()
    tok, title = "TOKFULL", "回退 · 空调"
    _fake(monkeypatch, _extract([R_SET_Q, R_UP_Q], []), [C_SET, C_UP])
    _pass_all(lib, await casegen.generate(_fsdoc("0.6", ROWS, tok, title)))
    seen = _fake(monkeypatch, _extract([R_SET_Q, {**R_UP_Q, "quote": "3.2 说温度高一点时加 3 度"}], []), [C_UP])
    _pass_all(lib, await casegen.generate(_fsdoc("0.7", {**ROWS, "p2": "3.2 说温度高一点时加 3 度"}, tok, title)))
    # 又改了 0.6（作为修订）：预判基准是 0.7，实际基准是 0.6 → 增量作废，全量重读
    seen = _fake(monkeypatch, _extract([R_SET_Q, R_UP_Q, R_OFF_Q], []), [C_OFF])
    d = _fsdoc("0.6", {**ROWS, "p3": "3.3 说关闭空调时关闭空调并播报"}, tok, title)
    with pytest.raises(casegen.VersionConflict) as e:
        await casegen.generate(d)
    b = await casegen.generate(d, as_revision=True, extracted=e.value.extracted)
    extracts = [p for s, p in seen if s == "casegen.extract"]
    assert len(extracts) == 2 and "调到 N 度" in extracts[1]  # 第二次是全文
    assert (b.prd_version, b.prev_version) == ("0.6+r2", "0.6") and "incremental" not in b.params
    assert b.diff == {"新增": 1, "修改": 0, "未改": 2, "删除": 0}


async def test_markdown_insert_renumbers_carried_sections(monkeypatch, tmp_path):
    """非飞书来源按内容哈希比对；前面插入章节导致编号后移时，沿用的需求点章节号跟着更新。"""
    lib = get_caselib()

    def md(ver, body):
        p = tmp_path / f"renum_{ver}.md"
        p.write_text(f"# 编号 · 空调\n\n文档版本：{ver}\n\n{body}", "utf8")
        return load_path(str(p))

    r1 = {"section": "1.2", "quote": "说空调调到 N 度时设为 N 度", "summary": "设定温度"}
    _fake(monkeypatch, _extract([r1], []), [{**C_SET}])
    b1 = await casegen.generate(md("0.1", "## 设定温度\n\n说空调调到 N 度时设为 N 度\n"))
    assert b1.requirements[0].section == "1.2"
    _pass_all(lib, b1)
    r0 = {"section": "1.2", "quote": "说打开空调时开启空调", "summary": "开空调"}
    c0 = {**C_UP, "req_id": "REQ-02", "title": "开空调", "utterance": "打开空调", "expect_screen": "空调已开启",
          "expect_tts": "空调已打开"}
    seen = _fake(monkeypatch, _extract([r0], []), [c0])
    b2 = await casegen.generate(md("0.2", "## 开空调\n\n说打开空调时开启空调\n\n## 设定温度\n\n说空调调到 N 度时设为 N 度\n"))
    assert "设为 N 度" not in seen[0][1]
    carried = next(r for r in b2.requirements if r.req_id == "REQ-01")
    assert carried.change == "未改" and carried.section == "1.2"
    assert b2.diff == {"新增": 1, "修改": 0, "未改": 1, "删除": 0}
