"""用例编写规范包：解析与校验、默认包 + 模块包叠加、注入提示词、额外自检、批次记录、按版本统计审核结论。"""
import httpx

from cabin_cloud import casegen, skills
from cabin_cloud.caselib import get_caselib
from cabin_cloud.main import app
from cabin_cloud.prd import load_path
from cabin_cloud.store import get_store
from cabin_common.schema import TestCase

BASE = """---
name: 默认
modules: ["*"]
checks:
  - {field: expected, forbid: "^正常$", msg: 只写了正常}
---
## 编写
通用规则 G1
"""
HVAC = """---
name: 空调
modules: [hvac]
version: 3
checks:
  - {field: expect_tts, forbid: "^好的$", msg: 播报只写好的, scope: voice}
  - {field: nope, msg: x, forbid: y}
  - {field: expected, msg: x, forbid: "(unclosed"}
---
## 抽取
空调抽取规则 X1
## 编写
空调编写规则 W1
## 示例
{"title": "示例 E1"}
"""


def _dir(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / f"{name}.md").write_text(text, "utf8")
    return tmp_path


def test_parse_validate_and_layering(tmp_path):
    d = _dir(tmp_path, 默认=BASE, 空调=HVAC)
    by = {x.name: x for x in skills.load_all(d)}
    h = by["空调"]
    assert h.modules == ["HVAC"] and h.version == 3 and len(h.rules) == 1  # 两条坏规则被拒
    assert any("field" in p for p in h.problems) and any("正则" in p for p in h.problems)
    assert (h.extract, h.write, h.examples) == ("空调抽取规则 X1", "空调编写规则 W1", '{"title": "示例 E1"}')
    m = skills.for_module("HVAC", d)
    assert m.name == "默认+空调" and len(m.rules) == 2 and "通用规则 G1" in m.write and "空调编写规则 W1" in m.write
    assert skills.for_module("NAV", d).name == "默认"
    assert skills.prompt_extra(m, "extract").endswith("空调抽取规则 X1")
    gen = skills.prompt_extra(m, "write")
    assert "通用规则 G1" in gen and "示例 E1" in gen
    assert skills.parse("没有开头").problems and skills.for_module("HVAC", tmp_path / "none") is None


def test_extra_checks_scope():
    sk = skills.parse(HVAC, "空调.md")
    voice = TestCase(case_id="TC-H-1", title="t", channel=["audio", "adb"], utterance="调到 24 度", expect_tts="好的")
    ui = TestCase(case_id="TC-H-2", title="t", channel=["adb"], expect_tts="好的")
    assert skills.check(voice, sk) == [("播报只写好的", "修改")] and skills.check(ui, sk) == []


async def test_generate_uses_module_skill(monkeypatch, tmp_path):
    (tmp_path / "sk").mkdir()
    d = _dir(tmp_path / "sk", 默认=BASE, 空调=HVAC)
    monkeypatch.setattr(skills, "SKILLS_DIR", d)
    seen = {}

    async def fake(system, prompt, source, session, max_tokens=4000):
        seen[source] = system
        if source == "casegen.extract":
            return {"title": "空调", "module": "空调", "module_code": "HVAC",
                    "requirements": [{"section": "3.1", "quote": "3.1 说调到 N 度时设为 N 度", "summary": "调温"}],
                    "params": [], "ambiguities": []}
        return {"cases": [{"req_id": "REQ-01", "title": "调温", "channel": ["audio", "adb"], "precondition": "空调已开启",
                           "steps": ["唤醒", "说指令"], "utterance": "调到 24 度", "expect_screen": "24℃", "expect_tts": "好的"}]}

    monkeypatch.setattr(casegen, "_llm_json", fake)
    p = tmp_path / "hvac.md"
    p.write_text("# 空调 · 规范包\n\n文档版本：1.0\n\n## 3 功能\n\n3.1 说调到 N 度时设为 N 度\n", "utf8")
    b = await casegen.generate(load_path(str(p)))
    assert "空调编写规则 W1" in seen["casegen.generate"] and "示例 E1" in seen["casegen.generate"]
    assert "空调抽取规则 X1" not in seen["casegen.extract"]  # 抽取前还不知道模块：只用默认包
    assert b.params["skill"]["name"] == "默认+空调" and b.params["skill"]["sha"]
    (c,) = get_caselib().batch_cases(b.batch_id)
    assert any(x.kind == "规范" and x.msg == "播报只写好的" for x in c.checks) and c.suggestion == "修改"

    # 审核后按规范版本统计；接口能看到每个模块实际生效的规范
    get_caselib().submit_review(b.batch_id, [{"case_id": c.case_id, "decision": "reject", "reason": "预期不可判定"}])
    st = skills.review_stats(get_store())[b.params["skill"]["sha"]]
    assert st["reject"] == 1 and st["pass_rate"] == 0 and st["reasons"] == [("预期不可判定", 1)]
    from cabin_cloud import auth
    tok = auth.issue_session(auth.User(id="u-sk", name="规范"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {tok}"}) as web:
        j = (await web.get("/api/casegen/skills")).json()
    hv = next(m for m in j["modules"] if m["code"] == "HVAC")
    assert hv["skill"]["name"] == "默认+空调" and hv["review"]["reject"] == 1
    assert any(f["name"] == "空调" and f["problems"] for f in j["files"])


def test_shipped_skills_parse_cleanly():
    """仓库里带的规范包没有解析问题，每个模块都能拿到规范。"""
    files = skills.load_all()
    assert files and all(not f.problems for f in files), [(f.file, f.problems) for f in files if f.problems]
    assert any(f.is_default for f in files)
    for code in ("VOICE", "HVAC", "SET", "NAV"):
        assert skills.for_module(code) is not None
