"""TaskPlan 的车型代入：参数合并、车型覆盖、${参数名} 替换、音频变体选择。

云端生成测试代码时（合成音频变体）与台架装载时（按本机车型执行）共用这一套规则，保证两边算出的文字一致。
"""
from __future__ import annotations

import copy
import re
from typing import Any

from cabin_common import vehicles

RE_PARAM = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")


class ResolveError(ValueError):
    def __init__(self, reason: str, msg: str):
        super().__init__(msg)
        self.reason = reason  # not_applicable | skip | plan_invalid


def params_for(plan: dict, model: str) -> dict[str, str]:
    return {**{k: str(v) for k, v in (plan.get("params") or {}).items()},
            **{k: str(v) for k, v in ((plan.get("model_params") or {}).get(model) or {}).items()}}


def subst(obj: Any, params: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return RE_PARAM.sub(lambda m: params.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, list):
        return [subst(x, params) for x in obj]
    if isinstance(obj, dict):
        return {k: subst(v, params) for k, v in obj.items()}
    return obj


def apply_overrides(plan: dict, ov: dict[str, Any]) -> None:
    """把车型覆盖落到 TaskPlan 字段上（就地修改）。"""
    if not ov:
        return
    if ov.get("utterance") and "utt.wav" in (plan.get("assets") or {}):
        plan["assets"]["utt.wav"]["text"] = ov["utterance"]
    for a in plan.get("assertions") or []:
        if a.get("type") == "voice":
            exp = a.setdefault("expected", {})
            if ov.get("expect_screen"):
                exp["screen"] = ov["expect_screen"]
            if ov.get("expect_tts"):
                exp["tts"] = ov["expect_tts"]
        elif a.get("type") in ("vision", "text") and ov.get("expected"):
            a["expected"] = ov["expected"]
    if ov.get("precondition"):
        for s in plan.get("setup") or []:
            if s.get("action") == "shell":
                s["note"] = ov["precondition"]


def models_of(plan: dict) -> list[str]:
    """计划适用的车型（去掉覆盖为跳过的）；没写 models 的旧计划视为不限车型。"""
    ms = plan.get("models")
    if ms is None:
        return []
    ov = plan.get("overrides") or {}
    return [m for m in ms if not (ov.get(m) or {}).get("skip")]


def asset_texts(plan: dict, name: str) -> dict[str, str]:
    """某个音频在各适用车型下的实际文字 {车型: 文字}（含车型覆盖与参数代入）。"""
    out = {}
    for m in models_of(plan):
        p = copy.deepcopy(plan)
        apply_overrides(p, (p.get("overrides") or {}).get(m) or {})
        a = (p.get("assets") or {}).get(name) or {}
        if a.get("text"):
            out[m] = subst(a["text"], params_for(plan, m))
    return out


def resolve(plan: dict, model: str) -> dict:
    """按台架车型得到可直接执行的 TaskPlan：去掉版本 / 车型字段，替换参数，音频取对应变体的哈希。"""
    ms = plan.get("models")
    m = vehicles.normalize(model) or model
    if ms is not None:
        if m not in ms:
            raise ResolveError("not_applicable", f"车型 {m} 不在该用例的适用车型内（{', '.join(ms)}）")
        if ((plan.get("overrides") or {}).get(m) or {}).get("skip"):
            raise ResolveError("skip", f"车型覆盖：{m} 跳过该用例")
    p = copy.deepcopy(plan)
    apply_overrides(p, (p.get("overrides") or {}).get(m) or {})
    params = params_for(plan, m)
    for k in ("setup", "steps", "teardown", "assertions"):
        if k in p:
            p[k] = subst(p[k], params)
    for name, a in (p.get("assets") or {}).items():
        a["text"] = subst(a.get("text", ""), params)
        if a.get("variants") is not None:
            a["sha256"] = (a.get("variants") or {}).get(a["text"])
    left = sorted(set(RE_PARAM.findall(str({k: p.get(k) for k in ("setup", "steps", "teardown", "assertions",
                                                                     "assets")}))))
    if left:
        raise ResolveError("plan_invalid", f"车型 {m} 下参数未定义：{', '.join(left)}")
    p["resolved_model"] = m
    return p
