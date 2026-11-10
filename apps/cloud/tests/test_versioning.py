"""用例管理：车型字典、打点、PRD 版本与基线、差异生成、参数化测试代码、车型代入。"""
import pytest
import yaml

from cabin_common import vehicles
from cabin_cloud import casegen, planner
from cabin_cloud.caselib import applies, get_caselib
from cabin_cloud.planvars import ResolveError, resolve
from cabin_cloud.prd import load_path
from cabin_cloud.planloader import PlanLoadError, dry_run, load
from cabin_common.schema import Requirement, TestCase

ALL = ["A01-CN", "A01-INTL", "B01-EU", "D01", "E01"]


# ---------- 车型字典 ----------
def test_vehicle_dictionary():
    assert vehicles.codes() == ALL
    assert vehicles.normalize("A01国内") == "A01-CN" and vehicles.normalize("a01-intl") == "A01-INTL"
    assert vehicles.normalize("B01 欧版") == "B01-EU" and vehicles.normalize("C01") == ""
    assert vehicles.find_all("拉取 B01欧版 和 E01 的用例") == ["B01-EU", "E01"]
    ov, errs = vehicles.parse_overrides("A01国际: 跳过；B01-EU: 测试语句=Set AC to 24；XX: 跳过")
    assert ov == {"A01-INTL": {"skip": True}, "B01-EU": {"utterance": "Set AC to 24"}} and len(errs) == 1
    assert vehicles.parse_overrides(vehicles.overrides_to_text(ov))[0] == ov


def test_applies_and_overrides():
    c = TestCase(case_id="TC-X-001", title="t", models=["A01-CN", "E01"], model_overrides={"E01": {"skip": True}})
    assert applies(c, "A01国内") and not applies(c, "E01") and not applies(c, "B01-EU")
    assert applies(TestCase(case_id="TC-X-002", title="t"), "D01")  # 未写车型 = 通用


# ---------- 需求点配对 ----------
def _req(rid, sec, quote, pts=None):
    r = Requirement(req_id=rid, section=sec, quote=quote, points=pts or {c: "●" for c in ALL})
    r.fingerprint = casegen.fingerprint(r)
    return r


def test_pair_requirements_four_kinds():
    old = [_req("REQ-01", "3.1", "说“空调调到 N 度”时设为 N 度"), _req("REQ-02", "3.2", "说“温度高一点”时加 1 度"),
           _req("REQ-03", "3.3", "超出 16 到 32 度时不执行"), _req("REQ-04", "3.4", "说完到播报不超过 1.5 秒")]
    new = [_req("", "3.1", "说“空调调到 N 度”时设为 N 度"),
           _req("", "3.2", "说“温度高一点”时加 2 度"),  # 修改原文
           _req("", "3.3", "超出 16 到 32 度时不执行", {**{c: "●" for c in ALL}, "D01": "—"}),  # 只改打点也算修改
           _req("", "3.7", "说“打开空调”时开启空调")]  # 新增
    deleted = casegen.pair_requirements(new, old)
    assert [(r.req_id, r.change) for r in new] == [("REQ-01", "未改"), ("REQ-02", "修改"), ("REQ-03", "修改"),
                                                   ("REQ-05", "新增")]
    assert [d.req_id for d in deleted] == ["REQ-04"] and deleted[0].change == "删除"


def test_detect_version_and_prd_identity(tmp_path):
    assert casegen.detect_version("文档版本 | 0.1.0") == "0.1.0"
    assert casegen.detect_version("表格：项目 | 内容；项目=文档版本，内容=V1.2.3") == "1.2.3"
    assert casegen.detect_version("没有版本") == ""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("# 语音助手 · 空调温度调节 PRD V0.1.0\n\n正文", "utf8")
    b.write_text("# 语音助手 · 空调温度调节 PRD V0.2.0\n\n正文", "utf8")
    assert casegen.prd_identity(load_path(str(a)), "VOICE") == casegen.prd_identity(load_path(str(b)), "VOICE")


# ---------- 两个版本 + 五个车型的完整闭环 ----------
BODY = "3.1 设定温度。\n\n3.2 高一点。\n\n3.3 超范围。\n"


def _doc(tmp_path, ver, title="语音助手 · 空调温度调节", body=BODY):
    p = tmp_path / f"hvac_{ver or 'nover'}.md"
    head = f"文档版本：{ver}\n\n" if ver else ""
    p.write_text(f"# {title}\n\n{head}## 3 功能需求\n\n{body}", "utf8")
    return load_path(str(p))


def _extract(reqs, params):
    return {"title": "空调温度调节", "module": "语音助手/空调", "module_code": "VOICE", "version": "",
            "requirements": reqs, "params": params, "ambiguities": []}


R_SET = {"section": "3.1", "quote": "说“空调调到 N 度”时设为 N 度并播报", "summary": "设定温度"}
R_UP = {"section": "3.2", "quote": "说“温度高一点”时加 1 度", "summary": "相对调节",
        "models": {"D01": "—"}, "model_source": "正文"}
R_MAX = {"section": "3.3", "quote": "说“空调调到最高”时调到上限温度", "summary": "调到上限"}
P_MAX = [{"name": "temp_max", "desc": "温度上限", "default": "32", "values": {"E01": "30"}, "sections": ["3.3"]}]


def _voice(rid, title, utt, scr, tts, typ="正向"):
    return {"req_id": rid, "title": title, "type": typ, "channel": ["audio", "adb"], "precondition": "空调已开启，当前 22℃",
            "steps": ["唤醒", "说指令"], "utterance": utt, "expect_screen": scr, "expect_tts": tts}


C_SET = _voice("REQ-01", "调到 24 度", "空调调到 24 度", "24℃", "已为您调到 24 度")
C_UP = _voice("REQ-02", "高一点", "温度高一点", "23℃", "已为您调到 23 度")
C_MAX = _voice("REQ-03", "调到上限", "空调调到 ${temp_max} 度", "${temp_max}℃", "已为您调到 ${temp_max} 度", "边界")


async def _gen(monkeypatch, doc, ex, cases, version=""):
    seen = []

    async def fake_llm(system, prompt, source, session, max_tokens=4000):
        if source == "casegen.extract":
            return ex
        seen.append(prompt)
        return {"cases": [c for c in cases if c["req_id"] in prompt]}

    monkeypatch.setattr(casegen, "_llm_json", fake_llm)
    return await casegen.generate(doc, session="S-v", version=version), seen


def _pass_all(lib, b):
    lib.submit_review(b.batch_id, [{"case_id": c.case_id, "decision": "pass"} for c in lib.batch_cases(b.batch_id)])


async def test_two_versions_five_models(monkeypatch, tmp_path):
    lib = get_caselib()
    # ---- 0.1.0：首版，3 条需求全部新增；REQ-02 推断 D01 不支持；REQ-03 用车型参数 temp_max
    b1, _ = await _gen(monkeypatch, _doc(tmp_path, "0.1.0"), _extract([R_SET, R_UP, R_MAX], P_MAX), [C_SET, C_UP, C_MAX])
    assert (b1.prd_version, b1.version_source, b1.prev_version) == ("0.1.0", "原文", "")
    assert b1.diff == {"新增": 3, "修改": 0, "未改": 0, "删除": 0}
    up = next(r for r in b1.requirements if r.section == "3.2")
    assert up.point_source == "推断" and up.points["D01"] == "—" and up.points["E01"] == "●"
    cs = {c.req_id: c for c in lib.batch_cases(b1.batch_id)}
    assert cs["REQ-02"].models == ["A01-CN", "A01-INTL", "B01-EU", "E01"] and cs["REQ-01"].models == ALL
    assert not any(k.kind == "预期不可判定" for k in cs["REQ-03"].checks)  # temp_max 已定义
    _pass_all(lib, b1)
    base1 = f"{b1.prd_id}@0.1.0"
    assert lib.get_baseline(base1) == {c.case_id: 1 for c in cs.values()}
    assert base1 in lib.get_case(cs["REQ-01"].case_id).baselines

    # 拉取按打点：D01 拉不到“高一点”
    assert cs["REQ-02"].case_id not in {c.case_id for c in lib.pull("D01")}
    assert {c.case_id for c in cs.values()} <= {c.case_id for c in lib.pull("A01国内")}

    # 参数化测试代码：models / params / model_params / 音频变体
    mx = lib.get_case(cs["REQ-03"].case_id)
    r = await planner.generate_for(mx)
    assert r["ok"], r["errors"]
    plan = yaml.safe_load(planner.read_plan(mx.case_id))
    assert plan["case_rev"] == 1 and plan["baselines"] == [base1] and plan["models"] == ALL
    assert plan["params"] == {"temp_max": "32"} and plan["model_params"] == {"E01": {"temp_max": "30"}}
    v = plan["assets"]["utt.wav"]["variants"]
    assert set(v) == {"空调调到 32 度", "空调调到 30 度"} and all(v.values())
    p30, p32 = resolve(plan, "E01"), resolve(plan, "A01国际")
    assert p30["assets"]["utt.wav"]["sha256"] == v["空调调到 30 度"]
    assert p30["assertions"][0]["expected"] == {"screen": "30℃", "tts": "已为您调到 30 度"}
    assert p32["assertions"][0]["expected"]["screen"] == "32℃"
    plan_e01, _ = load(mx.case_id, "E01")
    assert plan_e01["resolved_model"] == "E01"
    assert dry_run(mx.case_id, {"audio", "adb"}, "E01")["ok"]

    # ---- 0.2.0：REQ-01 未改，REQ-02 原文修改，REQ-03 删除，新增“打开空调”
    R_UP2 = {**R_UP, "quote": "说“温度高一点”时加 2 度"}
    R_ON = {"section": "3.4", "quote": "空调关闭时说“空调调到 N 度”先开空调再设温度", "summary": "未开启时设定"}
    C_UP2 = _voice("REQ-02", "高一点加 2 度", "温度高一点", "24℃", "已为您调到 24 度")
    C_ON = _voice("REQ-04", "未开启时调到 24 度", "空调调到 24 度", "24℃", "已为您打开空调并调到 24 度")
    body2 = "3.1 设定温度。\n\n3.2 高一点，加 2 度。\n\n3.4 未开启时设定。\n\n3.5 上限 E01 30 度，其余 32 度。\n"
    b2, prompts = await _gen(monkeypatch, _doc(tmp_path, "0.2.0", body=body2), _extract([R_SET, R_UP2, R_ON], P_MAX),
                             [C_SET, C_UP2, C_ON])
    assert b2.prev_version == "0.1.0" and b2.diff == {"新增": 1, "修改": 1, "未改": 1, "删除": 1}
    assert not any("REQ-01" in p for p in prompts)  # 未改的需求不再生成
    assert b2.inherited == {cs["REQ-01"].case_id: 1} and cs["REQ-03"].case_id in b2.retired
    new = {c.req_id: c for c in lib.batch_cases(b2.batch_id)}
    assert set(new) == {"REQ-02", "REQ-04"}  # 只审差异
    assert new["REQ-02"].case_id == cs["REQ-02"].case_id and new["REQ-02"].case_rev == 2  # 沿用编号、修订 +1
    _pass_all(lib, b2)
    base2 = f"{b2.prd_id}@0.2.0"
    assert lib.get_baseline(base2) == {cs["REQ-01"].case_id: 1, cs["REQ-02"].case_id: 2, new["REQ-04"].case_id: 1}
    assert set(lib.get_case(cs["REQ-01"].case_id).baselines) == {base1, base2}  # 继承：追加基线

    # 旧基线照常可取：0.1.0 里“高一点”仍是 r1、上限用例仍在
    old = {c.case_id: c for c in lib.pull("A01-CN", baseline=base1)}
    assert old[cs["REQ-02"].case_id].case_rev == 1 and old[cs["REQ-02"].case_id].steps
    assert mx.case_id in old and mx.case_id not in {c.case_id for c in lib.pull("A01-CN")}
    with pytest.raises(ValueError):
        lib.pull("A01-CN", baseline=f"{b2.prd_id}@9.9.9")

    # ---- 0.3.0：只改车型参数值 → 不出新修订、不送审，直接发布；测试代码按新值刷新
    b3, prompts = await _gen(monkeypatch, _doc(tmp_path, "0.3.0", body=body2.replace("30 度", "31 度")),
                             _extract([R_SET, R_UP2, R_ON], [{**P_MAX[0], "values": {"E01": "31"}}]), [])
    assert b3.status == "已审核" and prompts == [] and lib.get_baseline(f"{b3.prd_id}@0.3.0")


async def test_param_change_refreshes_plan_without_new_revision(monkeypatch, tmp_path):
    lib = get_caselib()
    C_MAX1 = {**C_MAX, "req_id": "REQ-01"}
    b1, _ = await _gen(monkeypatch, _doc(tmp_path, "1.0.0", "空调上限 PRD"), _extract([R_MAX], P_MAX), [C_MAX1])
    _pass_all(lib, b1)
    c = lib.batch_cases(b1.batch_id)[0]
    assert (await planner.generate_for(c))["ok"] and not planner.needs_generation(lib.get_case(c.case_id))
    b2, _ = await _gen(monkeypatch, _doc(tmp_path, "1.1.0", "空调上限 PRD"),
                       _extract([R_MAX], [{**P_MAX[0], "values": {"E01": "31"}}]), [])
    assert b2.status == "已审核"
    c = lib.get_case(c.case_id)
    assert c.case_rev == 1 and planner.needs_generation(c)  # 参数取值变了
    assert (await planner.generate_for(c))["ok"]
    plan = yaml.safe_load(planner.read_plan(c.case_id))
    assert plan["model_params"] == {"E01": {"temp_max": "31"}} and "空调调到 31 度" in plan["assets"]["utt.wav"]["variants"]


async def test_missing_version_is_prefilled(monkeypatch, tmp_path):
    b, _ = await _gen(monkeypatch, _doc(tmp_path, "", "无版本 PRD"), _extract([R_SET], []), [C_SET], version="2.0.0")
    assert (b.prd_version, b.version_source) == ("2.0.0", "测试预填")
    b, _ = await _gen(monkeypatch, _doc(tmp_path, "", "无版本 PRD 二"), _extract([R_SET], []), [C_SET])
    assert b.prd_version.startswith("未标注-") and b.version_source == "测试预填"


# ---------- 车型覆盖、校验、旧格式兼容 ----------
async def test_override_skip_and_utterance():
    lib = get_caselib()
    c = TestCase(case_id="TC-VOV-001", title="调到 24 度", channel=["audio", "adb"], utterance="空调调到 24 度",
                 expect_screen="24℃", expect_tts="已为您调到 24 度", precondition="空调已开启", models=ALL,
                 review_status="通过",
                 model_overrides={"A01-INTL": {"utterance": "Set temperature to 24", "expect_tts": "Temperature set to 24"},
                                  "D01": {"skip": True}})
    lib.save_case(c)
    assert c.case_id not in {x.case_id for x in lib.pull("D01")}
    assert (await planner.generate_for(c))["ok"]
    plan = yaml.safe_load(planner.read_plan(c.case_id))
    assert set(plan["assets"]["utt.wav"]["variants"]) == {"空调调到 24 度", "Set temperature to 24"}
    intl = resolve(plan, "A01-INTL")
    assert intl["assertions"][0]["expected"]["tts"] == "Temperature set to 24"
    with pytest.raises(ResolveError) as e:
        resolve(plan, "D01")
    assert e.value.reason == "skip"
    with pytest.raises(PlanLoadError):
        load(c.case_id, "C01")
    assert dry_run(c.case_id, {"audio", "adb"}, "D01")["reason"] == "skip"


def test_validate_params_and_models():
    c = TestCase(case_id="TC-VOV-002", title="t", channel=["audio", "adb"], utterance="调到 ${temp_max} 度",
                 expect_screen="${temp_max}℃", expect_tts="${temp_max} 度", precondition="p")
    plan = {"schema": planner.SCHEMA, "case_id": c.case_id, "case_rev": 1, "requires": ["adb", "audio"],
            "models": ["A01-CN", "C01"], **planner.voice_plan(c)}
    errs, _ = planner.validate(plan, c)
    text = "\n".join(errs)
    assert "车型不在车型字典内：C01" in text and "未定义的参数：temp_max" in text
    plan.update(models=["A01-CN"], params={"temp_max": "32"}, model_params={"E01": {"temp_min": "16"}})
    errs, _ = planner.validate(plan, c)
    assert errs == ["车型 E01 的参数 temp_min 没有默认值"]


def test_legacy_plan_without_models_still_resolves():
    plan = {"schema": planner.SCHEMA, "case_id": "TC-OLD-001", "steps": [{"id": 1, "action": "mute_host"}]}
    assert resolve(plan, "E01")["steps"] == plan["steps"]
