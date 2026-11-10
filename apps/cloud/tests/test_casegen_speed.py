"""用例生成提速：每组只带出处章节；哪组先完成先自检、先推给页面；跨组照样查重；记录每组耗时与提示词字数。"""
import asyncio

from cabin_cloud import casegen
from cabin_cloud.caselib import get_caselib
from cabin_cloud.prd import load_path

FILLER = "（本节补充说明，无关内容）" * 40


def _doc(tmp_path):
    secs = "".join(f"## 3.{i} 功能{i}\n\n3.{i} 说“功能{i}”时执行动作{i}。{FILLER}\n\n" for i in range(1, 7))
    p = tmp_path / "speed.md"
    p.write_text(f"# 提速 · 车控\n\n文档版本：1.0\n\n# 3 功能需求\n\n{secs}", "utf8")
    return load_path(str(p))


def _req(i):
    return {"section": f"3.{i}", "quote": f"3.{i} 说“功能{i}”时执行动作{i}", "summary": f"功能{i}"}


def _case(rid, utt):
    return {"req_id": rid, "title": utt, "type": "正向", "channel": ["audio", "adb"], "precondition": "车机已唤醒",
            "steps": ["唤醒", "说指令"], "utterance": utt, "expect_screen": f"{utt}已执行", "expect_tts": f"好的，{utt}"}


async def test_groups_carry_only_their_sections_and_stream(monkeypatch, tmp_path):
    prompts: dict[str, str] = {}
    order: list[str] = []

    async def fake(system, prompt, source, session, max_tokens=4000):
        if source == "casegen.extract":
            return {"title": "提速", "module": "车控/提速", "module_code": "SPD", "requirements": [_req(i) for i in range(1, 7)],
                    "params": [], "ambiguities": []}
        first = "REQ-01" in prompt
        prompts["g1" if first else "g2"] = prompt
        if first:  # 第一组慢，第二组先回来
            await asyncio.sleep(0.2)
            return {"cases": [_case(f"REQ-0{i}", f"功能{i}") for i in range(1, 5)] + [_case("REQ-01", "功能6")]}
        return {"cases": [_case("REQ-05", "功能5"), _case("REQ-06", "功能6")]}

    monkeypatch.setattr(casegen, "_llm_json", fake)

    async def emit(kind, data):
        if kind == "casegen.case":
            order.append(data["case"]["req_id"])

    doc = _doc(tmp_path)
    b = await casegen.generate(doc, emit=emit)
    full = len(doc.text())
    # 第二组只带 §3.5 / §3.6 和上级标题，不带 §3.1；提示词远小于整份 PRD
    assert "执行动作5" in prompts["g2"] and "执行动作6" in prompts["g2"] and "执行动作1" not in prompts["g2"]
    assert "执行动作1" in prompts["g1"] and "执行动作5" not in prompts["g1"]
    assert len(prompts["g2"]) < full / 2
    # 先完成的组先推给页面
    assert order[:2] == ["REQ-05", "REQ-06"] and len(order) == 7
    # 跨组查重：第一组后到的“功能6”与第二组已收下的重复
    cases = {c.utterance + c.req_id: c for c in get_caselib().batch_cases(b.batch_id)}
    assert any(x.kind == "重复" for x in cases["功能6REQ-01"].checks)
    assert not any(x.kind == "重复" for x in cases["功能6REQ-06"].checks)
    g = b.params["generate_groups"]
    assert len(g) == 2 and all(x["chars"] < full for x in g) and b.status == "待审核" and len(b.case_ids) == 7


async def test_one_group_failing_keeps_others(monkeypatch, tmp_path):
    async def fake(system, prompt, source, session, max_tokens=4000):
        if source == "casegen.extract":
            return {"title": "提速", "module": "车控/提速", "module_code": "SPD", "requirements": [_req(i) for i in range(1, 7)],
                    "params": [], "ambiguities": []}
        if "REQ-01" in prompt:
            raise ValueError("模型输出无法解析")
        return {"cases": [_case("REQ-05", "功能5")]}

    monkeypatch.setattr(casegen, "_llm_json", fake)
    b = await casegen.generate(_doc(tmp_path))
    assert b.status == "待审核" and len(b.case_ids) == 1 and "无法解析" in b.params["errors"][0]


async def test_concurrency_is_capped(monkeypatch, tmp_path):
    running = peak = 0

    async def fake(system, prompt, source, session, max_tokens=4000):
        nonlocal running, peak
        if source == "casegen.extract":
            return {"title": "提速", "module": "车控/提速", "module_code": "SPD",
                    "requirements": [_req(i) for i in range(1, 7)], "params": [], "ambiguities": []}
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return {"cases": []}

    monkeypatch.setattr(casegen, "_llm_json", fake)
    monkeypatch.setattr(casegen, "REQ_PER_CALL", 1)
    monkeypatch.setattr(casegen, "GEN_CONCURRENCY", 2)
    await casegen.generate(_doc(tmp_path))
    assert peak == 2
