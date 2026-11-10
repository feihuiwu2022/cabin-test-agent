"""规范包编辑接口：存进测试代码仓库、权限、校验、并发保护、命中预览、历史与回滚、按文件版本统计。"""
import httpx
import pytest

from cabin_cloud import auth, modules, planner, skills
from cabin_cloud.caselib import get_caselib
from cabin_cloud.config import get_settings
from cabin_cloud.main import app
from cabin_common.schema import TestCase

HVAC = """---
name: 空调
modules: [HVAC]
version: 1
checks:
  - {field: expect_tts, forbid: "^好的$", msg: 播报只写好的, scope: voice}
---
## 编写
空调规则 V1
"""


def client(name):
    tok = auth.issue_session(auth.User(id=f"u-{name}", name=name))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"Authorization": f"Bearer {tok}"})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """独立的测试代码仓库，规范包从出厂默认复制进去。"""
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(skills, "SKILLS_DIR", None)
    planner._repo_ready.clear()
    return tmp_path


async def test_seed_save_history_rollback(repo, monkeypatch):
    monkeypatch.setattr(get_settings(), "admins", ())  # 没配管理员：所有登录用户都能改
    async with client("张工") as c:
        j = (await c.get("/api/skills")).json()
        assert j["open_to_all"] and {f["file"] for f in j["files"]} >= {"默认.md", "空调.md"}
        assert (repo / "plans" / "_skills" / "空调.md").is_file()  # 出厂默认已复制进仓库
        d = (await c.get("/api/skills/空调.md")).json()
        assert d["can_edit"] and d["history"] and d["history"][0]["message"] == "规范包：出厂默认"
        base = d["parsed"]["sha"]
        r = await c.put("/api/skills/空调.md", json={"text": HVAC, "base_sha": base, "message": "补播报规则"})
        assert r.status_code == 200, r.text
        v1 = r.json()
        # 别人在我打开之后改过：拒绝覆盖
        r = await c.put("/api/skills/空调.md", json={"text": HVAC.replace("V1", "V2"), "base_sha": base, "message": "x"})
        assert r.status_code == 409
        r = await c.put("/api/skills/空调.md", json={"text": HVAC.replace("V1", "V2"), "base_sha": v1["sha"], "message": ""})
        assert r.status_code == 400 and "改动说明" in r.json()["detail"]
        r = await c.put("/api/skills/空调.md", json={"text": HVAC.replace("V1", "V2"), "base_sha": v1["sha"], "message": "改 V2"})
        assert r.status_code == 200
        d = (await c.get("/api/skills/空调.md")).json()
        hist = d["history"]
        assert [h["message"] for h in hist[:2]] == ["规范包 空调：改 V2", "规范包 空调：补播报规则"]
        assert hist[0]["author"] == "张工" and "V2" in d["text"]
        old = (await c.get(f"/api/skills/空调.md/rev/{hist[1]['rev']}")).json()["text"]
        assert "V1" in old
        r = await c.post("/api/skills/空调.md/rollback", json={"rev": hist[1]["rev"]})
        assert r.status_code == 200 and r.json()["sha"] == v1["sha"]
        d = (await c.get("/api/skills/空调.md")).json()
        assert "V1" in d["text"] and d["history"][0]["message"].endswith(f"回滚到 {hist[1]['rev'][:8]}")
        assert skills.for_module("HVAC").write.endswith("空调规则 V1")  # 保存即生效


async def test_permissions_and_validation(repo, monkeypatch):
    monkeypatch.setattr(get_settings(), "admins", ("管理员",))
    real = modules.get
    monkeypatch.setattr(modules, "get", lambda code: real(code).__class__(**{**real(code).__dict__, "members": ("李工",)})
                        if code == "HVAC" else real(code))
    async with client("王工") as c:  # 不是成员
        d = (await c.get("/api/skills/空调.md")).json()
        assert not d["can_edit"] and "成员" in d["why"]
        r = await c.put("/api/skills/空调.md", json={"text": HVAC, "base_sha": d["parsed"]["sha"], "message": "x"})
        assert r.status_code == 403
    async with client("李工") as c:  # 模块成员：能改空调，不能改默认包
        d = (await c.get("/api/skills/空调.md")).json()
        assert d["can_edit"]
        bad = HVAC.replace('forbid: "^好的$"', 'forbid: "(a+)+$"')
        p = (await c.post("/api/skills/空调.md/preview", json={"text": bad})).json()
        assert any("嵌套" in x for x in p["problems"])
        r = await c.put("/api/skills/空调.md", json={"text": bad, "base_sha": d["parsed"]["sha"], "message": "x"})
        assert r.status_code == 400 and "嵌套" in r.json()["detail"]
        r = await c.put("/api/skills/空调.md", json={"text": HVAC.replace("[HVAC]", "[HVAC, VOICE]"),
                                                    "base_sha": d["parsed"]["sha"], "message": "x"})
        assert r.status_code == 403  # 不是语音模块的成员
        dd = (await c.get("/api/skills/默认.md")).json()
        assert not dd["can_edit"] and "管理员" in dd["why"]
        r = await c.put("/api/skills/坏 名字.md", json={"text": HVAC, "message": "x"})
        assert r.status_code == 400 and "文件名" in r.json()["detail"]
    async with client("管理员") as c:  # 一个模块只能有一份规范包
        r = await c.put("/api/skills/空调2.md", json={"text": HVAC, "base_sha": "", "message": "新建"})
        assert r.status_code == 400 and "已经有规范包" in r.json()["detail"]


async def test_preview_hits_recent_cases(repo, monkeypatch):
    monkeypatch.setattr(get_settings(), "admins", ())
    lib = get_caselib()
    for i, tts in enumerate(["好的", "已为您调到 24 度"], 1):
        lib.save_case(TestCase(case_id=f"TC-HVAC-9{i:02d}", title=f"t{i}", channel=["audio", "adb"], utterance="调到 24 度",
                               expect_tts=tts, review_status="通过"))
    async with client("张工") as c:
        p = (await c.post("/api/skills/空调.md/preview", json={"text": HVAC})).json()
    (rule,) = p["rules"]
    assert rule["hits"] >= 1 and any(x["case_id"] == "TC-HVAC-901" for x in rule["samples"])
    assert not any(x["case_id"] == "TC-HVAC-902" for x in rule["samples"]) and p["checked_cases"] >= 2


def test_page_format_roundtrip():
    """编辑页拼出的格式（开头 YAML 用 JSON 写法）能被解析，正则里的反斜杠不丢。"""
    text = ('---\nname: "空调"\nmodules: ["HVAC"]\nversion: 2\n'
            'checks: [{"field":"expect_screen","require":"\\\\d+℃|档","msg":"没写温度","suggest":"修改","scope":"all"}]\n'
            '---\n## 编写\n规则\n')
    sk = skills.parse(text, "空调.md")
    assert not sk.problems and sk.version == 2 and sk.rules[0].require == "\\d+℃|档" and sk.write == "规则"


# ---------- 第二期：试生成对比、按驳回原因建议规则 ----------
async def test_trial_compares_saved_and_draft(repo, monkeypatch):
    from cabin_cloud import casegen
    from cabin_common.schema import PrdVersion, Requirement
    monkeypatch.setattr(get_settings(), "admins", ())
    get_caselib().save_prd_version(PrdVersion(prd_id="PRD-HVAC-T1", version="1.0", module_code="HVAC", status="待审核",
                                              requirements=[Requirement(req_id="REQ-01", section="3.1",
                                                                        quote="说调到 N 度时设为 N 度", summary="调温")]))
    systems = []

    async def fake(system, prompt, source, session, max_tokens=4000):
        systems.append(system)
        tts = "好的"
        return {"cases": [{"req_id": "REQ-01", "title": "调温", "channel": ["audio", "adb"], "precondition": "空调已开启",
                           "steps": ["唤醒", "说指令"], "utterance": "调到 24 度", "expect_screen": "24℃", "expect_tts": tts}]}

    monkeypatch.setattr(casegen, "_llm_json", fake)
    draft = HVAC.replace("空调规则 V1", "空调规则 V1\n新规则 N1")  # 草稿：多一条规则，且自检禁止播报只写“好的”
    async with client("张工") as c:
        r = await c.post("/api/skills/空调.md/trial", json={"text": draft})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["code"] == "HVAC" and j["source"] != "手填" and j["reqs"][0]["quote"].startswith("说调到")
        assert j["old"]["flagged"] == 0 and j["new"]["flagged"] == 1  # 草稿的自检规则生效：同样的输出，新规范标出来了
        assert j["new"]["skill"]["name"] == "默认+空调" and sum("新规则 N1" in x for x in systems) == 1  # 只有新规范的提示词带草稿规则
        assert not any(x.case_id.startswith("试-") for x in get_caselib().library())  # 不入库
        # 默认包要选模块；没有需求点的模块提示手填
        dflt = (await c.get("/api/skills/默认.md")).json()["text"]
        assert (await c.post("/api/skills/默认.md/trial", json={"text": dflt})).status_code == 400
        r = await c.post("/api/skills/默认.md/trial", json={"text": dflt, "code": "NAV"})
        assert r.status_code == 400 and "手填" in r.json()["detail"]
        r = await c.post("/api/skills/默认.md/trial", json={"text": dflt, "code": "NAV",
                                                          "reqs": [{"section": "1", "quote": "说导航回家时开始导航"}]})
        assert r.status_code == 200 and r.json()["source"] == "手填"


async def test_suggest_from_rejections(repo, monkeypatch):
    import json
    import time

    from cabin_cloud import casegen
    from cabin_cloud.store import get_store
    monkeypatch.setattr(get_settings(), "admins", ())
    st = get_store()
    st.x("DELETE FROM eval_set")
    for i, tts in enumerate(["好的", "好的。", "收到"], 1):
        case = {"case_id": f"TC-HVAC-7{i:02d}", "title": f"调温{i}", "type": "正向", "channel": ["audio", "adb"],
                "utterance": "调到 24 度", "expect_tts": tts, "expect_screen": "24℃", "precondition": "空调已开启"}
        st.x("INSERT INTO eval_set(defect_id, rule, reason, doc, at) VALUES(?,?,?,?,?)",
             (case["case_id"], "case-gen", "预期不可判定", json.dumps({"case": case, "diff": {}}, ensure_ascii=False), time.time()))
    get_caselib().save_case(TestCase(case_id="TC-HVAC-799", title="好用例", channel=["audio", "adb"], utterance="调到 24 度",
                                     expect_tts="已为您调到 24 度", review_status="通过"))
    seen = {}

    async def fake(system, prompt, source, session, max_tokens=4000):
        seen["prompt"] = prompt
        return {"summary": "播报只写了“好的”，判不出对错", "write": ["期望播报要带上数值"], "extract": [], "examples": [],
                "checks": [{"field": "expect_tts", "mode": "forbid", "pattern": "^(好的|收到)[。]?$", "msg": "播报只写好的",
                            "suggest": "修改", "scope": "voice", "why": "3 条驳回"},
                           {"field": "expect_tts", "mode": "forbid", "pattern": "(a+)+", "msg": "坏规则"},
                           {"field": "nope", "mode": "forbid", "pattern": "x", "msg": "坏字段"}]}

    monkeypatch.setattr(casegen, "_llm_json", fake)
    async with client("张工") as c:
        r = await c.post("/api/skills/空调.md/suggest", json={"text": HVAC})
    assert r.status_code == 200, r.text
    j = r.json()
    assert "TC-HVAC-701" in seen["prompt"] and j["samples"] == 3 and j["write"] == ["期望播报要带上数值"]
    good, nested, unknown = j["checks"]
    import re
    already = sum(1 for x in get_caselib().library()
                  if x.case_id.startswith("TC-HVAC") and x.is_voice and re.search("^(好的|收到)[。]?$", x.expect_tts))
    assert good["problem"] == "" and good["catches"] == 3 and good["false_hits"] == already  # 误伤：已通过的用例也会被标出
    assert "嵌套" in nested["problem"] and unknown["problem"] == "字段不认识"
    st.x("DELETE FROM eval_set")
    async with client("张工") as c:
        r = await c.post("/api/skills/空调.md/suggest", json={"text": HVAC})
    assert r.status_code == 400 and "没有被驳回" in r.json()["detail"]
