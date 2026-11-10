"""cabin-judge：评估器。规则判定优先，VLM 做期望 vs 实际比对。"""
from __future__ import annotations

from cabin_common.crash import RULES, crash_frames  # noqa: F401

from .modelgw import get_gateway

VLM_SYSTEM = """你是座舱车机 UI 测试的评估器（defect-judge）。任务：把"期望"（来自测试用例的预期结果）与"实际"（车机截图）逐项比对，判断是否一致。
口径：
- 以截图中肉眼可见的内容为准。控件列表来自控件树，文本是数据层原文，界面上可能被截断、遮挡或未显示，不能代替截图。
- 只对期望中写明的点做判定；与期望无关的界面差异不算缺陷。
- 典型缺陷：页面空白、图标/控件缺失、错位重叠、文字截断（出现省略号或缺字）、文案错误、开关状态不符。
- 置信度 0~1：差异肉眼明显且与期望直接冲突 ≥0.85；不确定或证据不足 ≤0.6。
只输出一个 JSON 对象，不要输出其他文字。"""

VLM_PROMPT = """期望（用例 {case_id}）：{expected}

当前页面：{screen}
控件列表（编号 文本 @[左,上,右,下]）：
{elements}

请输出 JSON：
{{"consistent": true/false,
  "actual_observed": "截图中与期望相关部分实际看到的内容（原样转写可见文字）",
  "diff_summary": "不一致时用 10~20 字概括差异，例如：说明文案被截断",
  "reason": "判定理由，指出期望与实际的具体冲突",
  "confidence": 0.0,
  "element_id": 出现差异的控件编号或 null}}"""

def level_of(rule: str, conf: float) -> tuple[str, str]:
    if rule in ("J-02", "J-03", "J-04", "J-05"):
        return "高", "规则判定"
    if conf >= 0.8:
        return "中", "AI 判定"
    return "低", "低置信"


def fingerprint(rule: str, pkg: str, page: str, feature: str) -> str:
    return f"{rule}|{pkg}|{page}|{feature}"


async def vlm_compare(case_id: str, expected: str, screen: str, elements: str, shot: bytes | list[bytes],
                      session: str) -> dict:
    """视觉比对：期望 vs 截图。探索任务与 TaskPlan 的 vision 断言共用。
    多张截图按操作先后排列，用于期望里描述了多个状态的用例；控件列表对应最后一张。"""
    images = shot if isinstance(shot, list) else [shot]
    prompt = VLM_PROMPT.format(case_id=case_id, expected=expected, screen=screen, elements=elements or "（无）")
    if len(images) > 1:
        prompt = f"共 {len(images)} 张截图，按操作先后排列；期望中的各个状态要分别在对应截图里核对。\n" + prompt
    verdict = await get_gateway().vision_json(system=VLM_SYSTEM, prompt=prompt, images=images, label="sensitive",
                                              source="judge.vision_check", session=session)
    verdict["confidence"] = float(verdict.get("confidence") or 0)
    return verdict
