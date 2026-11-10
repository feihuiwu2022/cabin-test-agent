"""断言结果 → 缺陷候选，之后复用现有的查重、审核、建单闭环。"""
from __future__ import annotations

from ..judge import fingerprint, level_of
from cabin_common.schema import DefectCandidate, short_id
from cabin_common.results import AssertResult, PlanResult


def _base(res: PlanResult, task_id: str, rig: str, ota: str, package: str) -> dict:
    return dict(id=short_id("D"), task_id=task_id, case_id=res.case_id, ota=ota, rig=rig, package=package)


def _rule(a: AssertResult, res: PlanResult, task_id: str, rig: str, ota: str, shot: str) -> DefectCandidate:
    d = a.detail
    rule, kind, pkg, top = d["rule"], d["kind"], d["package"], d.get("frames", "")
    lvl, by = level_of(rule, 0.97)
    return DefectCandidate(**_base(res, task_id, rig, ota, pkg), rule=rule, kind=kind,
                           title=f"[{kind}] {pkg}：{top.split('>')[0] or '进程异常退出'}", summary=f"{kind}：{pkg}",
                           reason=f"logcat 命中规则 {rule}（{kind}），进程属被测包", confidence=0.97, level=lvl,
                           judged_by=by, actual_shot=shot, log_excerpt=d.get("log_excerpt", ""),
                           fingerprint=fingerprint(rule, pkg, "", top), severity="A")


def _vision(a: AssertResult, res: PlanResult, task_id: str, rig: str, ota: str, package: str) -> DefectCandidate:
    d, v = a.detail, a.detail.get("verdict") or {}
    diff = (v.get("diff_summary") or "与期望不一致").strip()
    page = d.get("screen", "")
    lvl, by = level_of("J-01", a.confidence)
    return DefectCandidate(**_base(res, task_id, rig, ota, package), rule="J-01", kind="显示异常",
                           title=f"[显示异常] {page.replace('/', '-')}：{diff}", summary=diff, reason=v.get("reason", ""),
                           confidence=a.confidence, level=lvl, judged_by=by, page=page,
                           expected=d.get("expected", ""), actual_shot=d.get("actual_shot", ""), diff_bbox=d.get("bbox"),
                           fingerprint=fingerprint("J-01", package, page, res.case_id), severity="B")


VOICE_KINDS = {"tts_mismatch": "播报不符", "exec_or_display": "执行或显示异常", "recog_or_exec": "识别或执行异常", "no_tts": "无播报"}


def _voice(a: AssertResult, res: PlanResult, task_id: str, rig: str, ota: str, package: str) -> DefectCandidate:
    d, v = a.detail, a.detail.get("verdict") or {}
    kind = VOICE_KINDS.get(d.get("class", ""), "语音异常")
    page = d.get("screen", "")
    lvl, by = level_of("J-07", a.confidence)
    return DefectCandidate(**_base(res, task_id, rig, ota, package), rule="J-07", kind=kind,
                           title=f"[{kind}] {res.case_id}：{v.get('diff_summary', '')}", summary=v.get("diff_summary") or kind,
                           reason=v.get("reason", ""), confidence=a.confidence, level=lvl, judged_by=by, page=page,
                           expected=d.get("expected", ""), actual_shot=d.get("actual_shot", ""), diff_bbox=d.get("bbox"),
                           fingerprint=fingerprint("J-07", package, d.get("class", ""), res.case_id), severity="B")


def _timing(a: AssertResult, res: PlanResult, task_id: str, rig: str, ota: str, package: str, shot: str) -> DefectCandidate:
    d = a.detail
    summary = f"响应时延 {d.get('latency_ms')} ms，超过上限 {d.get('max_ms')} ms"
    return DefectCandidate(**_base(res, task_id, rig, ota, package), rule="J-08", kind="响应时延",
                           title=f"[响应时延] {res.case_id}：{d.get('latency_ms')} ms", summary=summary, reason=summary,
                           confidence=1.0, level="高", judged_by="规则判定", actual_shot=shot,
                           fingerprint=fingerprint("J-08", package, "", res.case_id), severity="C")


def _text(a: AssertResult, res: PlanResult, task_id: str, rig: str, ota: str, package: str, shot: str) -> DefectCandidate:
    d = a.detail
    want = str(d.get("expected") or d.get("error") or "")
    page = d.get("screen", "")
    return DefectCandidate(**_base(res, task_id, rig, ota, package), rule="J-06", kind="文本不符",
                           title=f"[文本不符] {res.case_id}：{want[:24]}", summary=f"未找到期望文本：{want[:24]}",
                           reason=f"断言 {a.assertion_id} 未通过", confidence=1.0, level="高", judged_by="规则判定",
                           page=page, expected=want, actual_shot=shot,
                           fingerprint=fingerprint("J-06", package, page, f"{res.case_id}|{a.assertion_id}"), severity="B")


def _value(a: AssertResult, res: PlanResult, task_id: str, rig: str, ota: str, package: str, shot: str) -> DefectCandidate:
    d = a.detail
    summary = f"{d.get('key')} 实际 {d.get('actual')}，期望 {d.get('op')} {d.get('expected')}"
    return DefectCandidate(**_base(res, task_id, rig, ota, package), rule="J-09", kind="数值不符",
                           title=f"[数值不符] {res.case_id}：{summary[:40]}", summary=summary, reason=summary,
                           confidence=1.0, level="高", judged_by="规则判定", expected=str(d.get("expected")), actual_shot=shot,
                           fingerprint=fingerprint("J-09", package, str(d.get("key")), res.case_id), severity="B")


def _distrust(c: DefectCandidate, unmet: list[str]) -> None:
    """前置条件没有自动设置时，起始状态不确定，判定可能是误报：降为低置信并写明原因，交人工核对。
    崩溃规则不受起始状态影响，不降级。"""
    if c.rule in ("J-02", "J-03", "J-08"):
        return
    c.level, c.judged_by = "低", "低置信"
    c.reason = f"前置条件未自动设置（{'；'.join(unmet)[:60]}），结论需人工核对。{c.reason}"


def _failure_key(c: DefectCandidate, a: AssertResult) -> str:
    """失败类型：崩溃看调用栈，语音看分类，其他看规则本身。"""
    d = a.detail
    return str(d.get("frames") or d.get("class") or "")


def to_candidates(res: PlanResult, task_id: str, rig: str, ota: str, package: str, last_shot: str, model: str = "") -> list[DefectCandidate]:
    fails = [a for a in res.asserts if a.result == "fail"]
    crash = [a for a in fails if a.type == "rule"]
    pairs = [(a, _rule(a, res, task_id, rig, ota, last_shot)) for a in crash]
    for a in fails:
        if a.type == "vision" and not crash:  # 崩溃引起的视觉差异由规则覆盖，不重复出候选
            pairs.append((a, _vision(a, res, task_id, rig, ota, package)))
        elif a.type == "voice" and not crash:
            pairs.append((a, _voice(a, res, task_id, rig, ota, package)))
        elif a.type == "timing":
            pairs.append((a, _timing(a, res, task_id, rig, ota, package, last_shot)))
        elif a.type == "value":
            pairs.append((a, _value(a, res, task_id, rig, ota, package, last_shot)))
        elif a.type in ("text", "wait"):
            pairs.append((a, _text(a, res, task_id, rig, ota, package, last_shot)))
    for a, c in pairs:
        c.model = model
        c.fingerprint = "|".join((model, res.case_id, a.assertion_id, c.rule, _failure_key(c, a)))
    out = [c for _, c in pairs]
    unmet = (res.manifest.get("facts") or {}).get("setup_unmet") or []
    if unmet:
        for c in out:
            _distrust(c, unmet)
    return out
