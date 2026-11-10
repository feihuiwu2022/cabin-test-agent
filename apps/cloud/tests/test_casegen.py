"""一期 A 用例闭环：PRD 解析 → 生成 + 自检 → 审核入库 → 拉取 → 测试代码 + 静态校验（模型调用打桩）。"""
import io
import zipfile
from pathlib import Path

import pytest
import yaml

from cabin_common import vehicles
from cabin_cloud import casegen, caselib, planner
from cabin_cloud.cases import get_cases
from cabin_cloud.config import BACKEND_DIR, ROOT_DIR
from cabin_cloud.prd import find_ref, find_section, load_path, parse_bytes, parse_markdown
from cabin_common.schema import TestCase

SAMPLE = BACKEND_DIR / "data" / "samples" / "语音助手_空调控制_PRD.md"


def _docx(paras: list[tuple[str, str]]) -> bytes:
    """最小 docx：[(样式, 文本)]，样式为空表示正文；"TABLE" 表示一行表格（| 分隔）。"""
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = []
    rows = []
    for style, text in paras + [("", "")]:
        if style == "TABLE":
            rows.append(text.split("|"))
            continue
        if rows:
            trs = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>"
                          for r in rows)
            body.append(f"<w:tbl>{trs}</w:tbl>")
            rows = []
        if text:
            ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
            body.append(f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>")
    doc = f'<?xml version="1.0"?><w:document {W}><w:body>{"".join(body)}</w:body></w:document>'
    styles = (f'<?xml version="1.0"?><w:styles {W}>'
              '<w:style w:styleId="1"><w:name w:val="heading 1"/></w:style>'
              '<w:style w:styleId="2"><w:name w:val="标题 2"/></w:style></w:styles>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()


# ---------- 解析 ----------
def test_parse_markdown_sections_and_table():
    doc = load_path(str(SAMPLE))
    assert doc.title.startswith("语音助手 PRD")
    assert doc.has_section("4.1") and doc.has_section("4.2")
    t41 = doc.text("4.1")
    assert "4.1.2" in t41 and "车窗" not in t41
    assert "表格：指令 | 条件 | 车机行为 | 播报" in t41 and "指令=关闭空调" in t41
    para = next(b for b in doc.blocks if b.text.startswith("4.1.3"))
    assert para.section == "4.1.3"


def test_parse_docx_headings_and_table():
    data = _docx([("1", "4 车控语音"), ("2", "4.1 空调控制"), ("", "说“打开空调”时开启空调。"),
                  ("TABLE", "指令|播报"), ("TABLE", "关闭空调|空调已关闭"), ("2", "车窗控制")])
    doc = parse_bytes("x.docx", data, "upload:x.docx")
    heads = [(b.level, b.text, b.section) for b in doc.blocks if b.kind == "heading"]
    assert heads == [(1, "4 车控语音", "4"), (2, "4.1 空调控制", "4.1"), (2, "车窗控制", "4.2")]
    assert any(b.kind == "table" and "指令=关闭空调" in b.text for b in doc.blocks)


def test_find_ref_and_section():
    assert find_ref("把 https://x.feishu.cn/docx/AbC123 的 §4.1 转成用例") == ("url", "https://x.feishu.cn/docx/AbC123")
    assert find_ref(r"把 E:\docs\voice_prd.docx 转成用例") == ("path", r"E:\docs\voice_prd.docx")
    assert find_ref("把 ./prd/a.md 转成用例") == ("path", "./prd/a.md")
    assert find_section("只生成 §4.1") == "4.1"
    assert find_section("4.1.2节") == "4.1.2"
    assert find_section("把这份 PRD 转成用例") == ""


def test_parse_md_ignores_empty():
    assert parse_markdown("\n\n") == []


# ---------- 自检 ----------
def _tc(cid, **kw):
    base = dict(case_id=cid, title="调到 24 度", req_id="REQ-01", precondition="空调已开启", steps=["说指令"],
                channel=["audio", "adb"], utterance="空调调到 24 度", expect_screen="空调温度显示 24℃",
                expect_tts="已为您调到 24 度", expected="屏幕 24℃")
    base.update(kw)
    return TestCase(**base)


def test_self_check_rules():
    cs = [_tc("TC-X-001"),
          _tc("TC-X-002"),  # 与 001 重复
          _tc("TC-X-003", utterance="空调调到 26 度", expect_screen="空调温度显示 24℃", expect_tts="好的"),
          _tc("TC-X-004", channel=["adb"], utterance="", expect_screen="", expect_tts="", precondition="",
              expected="界面显示正常", title="页面")]
    casegen.self_check(cs, [])
    k = {c.case_id: [x.kind for x in c.checks] for c in cs}
    assert k["TC-X-001"] == [] and cs[0].suggestion == ""
    assert "重复" in k["TC-X-002"] and cs[1].suggestion == "驳回"
    assert "预期不可判定" in k["TC-X-003"]
    assert {"缺前置条件", "预期不可判定"} <= set(k["TC-X-004"]) and cs[3].suggestion == "修改"


def test_module_code():
    assert casegen.module_code("语音助手/空调") == "VOICE"
    assert casegen.module_code("设置/显示/亮度") == "DISP"
    assert casegen.module_code("显示设置") == "DISP"
    assert casegen.module_code("未知模块", "abc") == "ABC"


# ---------- 测试代码 ----------
def test_voice_plan_validates():
    c = _tc("TC-VOICE-900", models=["C01"], precondition="车机已启动")
    plan = {"schema": planner.SCHEMA, "case_id": c.case_id, "requires": ["adb", "audio"], **planner.voice_plan(c)}
    errs, warns = planner.validate(plan, c)
    assert errs == []
    assert any("待合成" in w for w in warns) and any("车型命令待补充" in w for w in warns)
    acts = [s["action"] for s in plan["steps"]]
    assert acts[:5] == ["mute_host", "record_start", "wake", "play_utterance", "listen_tts"]


def test_validate_catches_errors():
    c = _tc("TC-VOICE-901")
    plan = {"schema": planner.SCHEMA, "case_id": c.case_id, "requires": ["adb"], **planner.voice_plan(c)}
    plan["steps"].append({"id": 20, "action": "fly"})
    plan["steps"].append({"id": 21, "action": "tap", "target": {}})
    plan["steps"].append({"id": 22, "action": "can_send", "msg": "0x123#01"})
    plan["assertions"].append({"id": "a9", "type": "vision"})
    errs, _ = planner.validate(plan, c)
    text = "\n".join(errs)
    for frag in ("动作不在动作库内", "需要能力 audio", "target 需要 selector", "CAN 报文不在白名单", "视觉断言缺少期望"):
        assert frag in text, frag


def test_ui_fallback_plan():
    c = TestCase(case_id="TC-DISP-900", title="自动亮度说明文案", channel=["adb"], precondition="车机在桌面",
                 steps=["进入设置", "点击“显示”", "查看“自动调节亮度”说明文案"], expected="说明文案完整显示")
    steps = planner._ui_fallback(c)
    assert steps[0] == {"action": "key", "key": "home"}
    assert {"action": "tap", "target": {"selector": {"text": "显示"}}} in steps
    assert steps[-1]["action"] == "screenshot"


def test_redline_word_boundary():
    c = TestCase(case_id="TC-DISP-901", title="滑条", channel=["adb"], expected="读数 10%")
    plan = {"schema": planner.SCHEMA, "case_id": c.case_id, "requires": ["adb"],
            "steps": [{"id": 1, "action": "swipe", "target": {"selector": {"text": "亮度"}}, "to": {"direction": "left"}},
                      {"id": 2, "action": "screenshot"}],
            "assertions": [{"id": "a1", "after_step": 2, "type": "vision", "expected": "读数 10%"}]}
    assert planner.validate(plan, c)[0] == []
    plan["steps"].append({"id": 3, "action": "shell", "cmd": "pm clear 清除数据"})
    assert any("红线" in e for e in planner.validate(plan, c)[0])


# ---------- 闭环：生成 → 审核 → 入库 → 拉取 → 代码 ----------
FAKE_EXTRACT = {"title": "空调控制", "module": "语音助手/空调", "module_code": "VOICE", "version": "",
                "requirements": [{"section": "4.1.1", "quote": "说“打开空调”时开启空调", "summary": "打开空调"},
                                 {"section": "4.1.2", "quote": "空调调到 N 度", "summary": "设定温度"}],
                "ambiguities": [{"section": "4.1.5", "quote": "有点冷", "question": "“有点冷”是否自动升温"}]}
FAKE_CASES = {"cases": [
    {"req_id": "REQ-01", "title": "打开空调", "type": "正向", "channel": ["audio", "adb"], "precondition": "空调关闭",
     "steps": ["唤醒", "说指令"], "utterance": "打开空调", "expect_screen": "空调开关为开", "expect_tts": "空调已打开"},
    {"req_id": "REQ-02", "title": "调到 24 度", "type": "正向", "channel": ["audio", "adb"],
     "precondition": "空调已开启，当前 22℃", "steps": ["唤醒", "说指令"], "utterance": "空调调到 24 度",
     "expect_screen": "空调温度显示 24℃", "expect_tts": "已为您调到 24 度"},
    {"req_id": "REQ-02", "title": "调到 24 度", "type": "正向", "channel": ["audio", "adb"],
     "precondition": "空调已开启，当前 22℃", "steps": ["唤醒", "说指令"], "utterance": "空调调到 24 度",
     "expect_screen": "空调温度显示 24℃", "expect_tts": "已为您调到 24 度"},
    {"req_id": "REQ-99", "title": "不存在的需求", "steps": ["x"]},
]}


async def test_full_loop(monkeypatch):
    async def fake_llm(system, prompt, source, session, max_tokens=4000):
        return FAKE_EXTRACT if source == "casegen.extract" else FAKE_CASES

    monkeypatch.setattr(casegen, "_llm_json", fake_llm)
    events = []

    async def emit(kind, data):
        events.append(kind)

    doc = load_path(str(SAMPLE))
    batch = await casegen.generate(doc, section="4.1", session="S-t", emit=emit)
    assert batch.status == "待审核" and batch.models == vehicles.codes()  # 未提车型：默认 5 个车型全部打点
    assert batch.prd_version and batch.version_source in ("原文", "测试预填") and batch.diff["新增"] == 2
    assert [r.req_id for r in batch.requirements] == ["REQ-01", "REQ-02"] and len(batch.ambiguities) == 1
    lib = caselib.get_caselib()
    cases = lib.batch_cases(batch.batch_id)
    assert len(cases) == 3 and all(c.case_id.startswith("TC-VOICE-") for c in cases)
    assert all(c.voice_profile == "female_a" and c.latency_max_ms == 1500 for c in cases)
    dup = [c for c in cases if c.suggestion == "驳回"]
    assert len(dup) == 1
    assert events[0] == "casegen.batch" and events[-1] == "casegen.done"

    # 未审核：不可拉取，审核不完整不可提交，驳回须有原因
    assert all(c.batch_id != batch.batch_id for c in lib.pull("A01-CN"))
    with pytest.raises(ValueError):
        lib.submit_review(batch.batch_id, [{"case_id": cases[0].case_id, "decision": "pass"}])
    with pytest.raises(ValueError):
        lib.submit_review(batch.batch_id, [{"case_id": c.case_id, "decision": "reject"} for c in cases])

    ok = [c for c in cases if c.suggestion != "驳回"]
    decisions = [{"case_id": ok[0].case_id, "decision": "pass"},
                 {"case_id": ok[1].case_id, "decision": "edit", "edits": {"precondition": "空调已开启，当前 22℃；语音已唤醒"}},
                 {"case_id": dup[0].case_id, "decision": "reject", "reason": "与已有用例重复"}]
    n = lib.submit_review(batch.batch_id, decisions)
    assert n == {"pass": 1, "edit": 1, "reject": 1, "to_req": 0}
    assert lib.get_case(ok[1].case_id).review_status == "修改后通过"
    with pytest.raises(ValueError):  # 不能重复提交
        lib.submit_review(batch.batch_id, decisions)

    # 入库后可被测试闭环 read_case 读到
    c, _ = await get_cases().get(ok[1].case_id)
    assert c and "24℃" in c.expected

    # 拉取：只有通过的；车型不符拉不到
    pulled = lib.pull("A01国内")
    assert {x.case_id for x in pulled} >= {ok[0].case_id, ok[1].case_id}
    assert dup[0].case_id not in {x.case_id for x in pulled}
    assert all(x.batch_id != batch.batch_id for x in lib.pull("C01"))  # 不在打点里的车型拉不到
    assert lib.get_baseline(f"{batch.prd_id}@{batch.prd_version}")  # 审完即发布基线

    # 测试代码：语音模板 + 静态校验 + 版本
    r = await planner.generate_for(lib.get_case(ok[1].case_id))
    assert r["ok"] and r["generated_by"] == "voice-template" and r["plan_version"]
    c2 = lib.get_case(ok[1].case_id)
    assert c2.plan_status == "ready" and c2.auto_status == "已生成"
    text = planner.read_plan(c2.case_id)
    plan = yaml.safe_load(text)
    assert plan["assets"]["utt.wav"]["text"] == "空调调到 24 度"
    assert plan["assertions"][0]["expected"]["screen"] == "空调温度显示 24℃"
    assert plan["setup"][0]["note"].endswith("语音已唤醒")  # 审核修改进入测试代码
    row = lib.a15_row(c2)
    assert row["plan_status"] == "ready" and row["assets"] == ["wake.wav", "utt.wav"]


def test_sample_exists():
    assert SAMPLE.exists() and Path(SAMPLE).stat().st_size > 200


def test_self_check_boundary_not_duplicate():
    """只差参数值的边界用例不是重复；同测试语句、同类型、同期望屏幕才是。"""
    cs = [_tc("TC-X-101", utterance="空调调到 26 度", expect_screen="26℃", expect_tts="26 度"),
          _tc("TC-X-102", type="边界", utterance="空调调到 16 度", expect_screen="16℃", expect_tts="16 度"),
          _tc("TC-X-103", type="边界", utterance="空调调到 32 度", expect_screen="32℃", expect_tts="32 度"),
          _tc("TC-X-104", type="边界", utterance="空调调到32度。", expect_screen="32℃", expect_tts="32 度", title="另一个标题"),
          TestCase(case_id="TC-X-105", title="亮度滑条拖到 10%", channel=["adb"], precondition="p", steps=["拖到最左"],
                   expected="读数 10%"),
          TestCase(case_id="TC-X-106", title="亮度滑条拖到 100%", channel=["adb"], precondition="p", steps=["拖到最右"],
                   expected="读数 100%"),
          _tc("TC-X-107", utterance="空调调到 26 度", precondition="空调已关闭", expect_screen="空调开启，显示 26℃",
              expect_tts="已为您打开空调并调到 26 度", title="关闭时调温")]  # 同语句不同前置 / 期望，不是重复
    casegen.self_check(cs, [])
    dup = {c.case_id for c in cs if any(k.kind == "重复" for k in c.checks)}
    assert dup == {"TC-X-104"}


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


async def test_case_workbench_does_not_run_tests():
    from cabin_cloud.assistant.tools import tools_for
    from cabin_cloud.gateway import Session

    ws = _FakeWS()
    s = Session(ws, "S-case")
    s.tts_on = False
    await s.handle({"type": "case.enter"})
    assert (s.mode, s.authed) == ("lab", False)
    assert ws.sent[-1]["type"] == "session.state" and ws.sent[-1]["authed"] is False
    assert tools_for(False) == []  # 台架执行工具不挂载
    assert await s.route(1, "在 B2 台架测一下显示设置的亮度页") is True
    assert "本地页面" in [m for m in ws.sent if m["type"] == "reply.text"][-1]["text"]
    assert casegen_wants("导入示例 PRD 生成用例") == "import"  # 用例闭环照常可用


def casegen_wants(text):
    from cabin_cloud import casewf
    return casewf.wants(text, False)
