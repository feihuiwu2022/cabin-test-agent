"""voice：双线判定（看屏幕 + 听播报），经 JudgeClient（A-10）。屏幕或播报任一不符即失败。"""
from __future__ import annotations

from ..context import AssertResult
from ..evidence import Evidence
from cabin_common.judging import VoiceReq
from . import EvalContext, evaluator, latest_ui

CLASS_TEXT = {"tts_mismatch": "播报与期望不符", "exec_or_display": "屏幕与期望不符，播报正确", "recog_or_exec": "屏幕和播报都不符",
              "no_tts": "车机没有播报"}


@evaluator("voice")
async def voice(a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult:
    aid, step, exp = a.get("id", ""), a.get("after_step"), a.get("expected") or {}
    shot, clip = ev.find("screenshot", step), ev.find("tts_clip", step)
    no_tts = ev.facts.get("tts") == "none"
    if not shot or not (clip or no_tts):
        return AssertResult(assertion_id=aid, type="voice", result="error", detail={"error": "缺少截图或播报片段"})
    if ctx.judge is None:
        return AssertResult(assertion_id=aid, type="voice", result="error", detail={"error": "未配置判定服务"})
    ui = latest_ui(ev, step)
    try:
        r = await ctx.judge.voice(VoiceReq(
            case_id=ctx.case_id, screen_expected=str(exp.get("screen", "")), tts_expected=str(exp.get("tts", "")),
            image=ev.read(shot), screen=ui["screen"], elements="\n".join(e["brief"] for e in ui["elements"]),
            tts_audio=ev.read(clip) if clip else b"", transcript=clip.meta.get("transcript") if clip else None,
            no_tts=no_tts, session=ctx.session))
    except Exception as e:  # 判定不可用不等于被测功能失败
        return AssertResult(assertion_id=aid, type="voice", result="error",
                            detail={"error": f"{type(e).__name__}: {e}", "actual_shot": ev.url(shot)})
    look, cls = r.get("look") or {}, r.get("class", "")
    eid = look.get("element_id")
    bbox = next((e["bounds"] for e in ui["elements"] if e["id"] == eid), None) if eid is not None else None
    ok = r.get("result") == "pass"
    said = r.get("transcript") or "（无播报）"
    verdict = {"consistent": ok, "actual_observed": f"屏幕：{look.get('actual_observed', '')}；播报：{said}",
               "diff_summary": "" if ok else CLASS_TEXT.get(cls, cls),
               "reason": look.get("reason", "") if ok or r["lines"]["look"] == "fail" else f"期望播报“{exp.get('tts')}”，实际“{said}”",
               "confidence": float(r.get("confidence") or 0), "element_id": eid}
    return AssertResult(assertion_id=aid, type="voice", result="pass" if ok else "fail", confidence=verdict["confidence"],
                        detail={"verdict": verdict, "class": cls, "lines": r.get("lines"), "transcript": r.get("transcript", ""),
                                "bbox": bbox, "actual_shot": ev.url(shot), "screen": ui["screen"],
                                "expected": f"屏幕：{exp.get('screen')}；播报：{exp.get('tts')}"})
