"""TaskPlan 规范：动作库、断言类型与静态校验。云端生成时校验，台架装载时再校验一次。"""
from __future__ import annotations

import json
import re

import yaml

from . import vehicles
from .schema import TestCase

SCHEMA = "cabin/taskplan/v1"
WAKE_WORD = "你好千禾"
CAPABILITIES = {"adb", "ssh_qnx", "cluster_shot", "camera", "arm", "can", "audio", "power"}

# 动作库（台架端 PRD 表15）：动作 → (所需通道, 必填参数)。通道 None 表示通用。
ACTIONS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "mute_host": (None, ()), "unmute_host": (None, ()),
    "record_start": ("audio", ()), "record_stop": ("audio", ()),
    "wake": ("audio", ("audio",)), "play_utterance": ("audio", ("audio",)), "listen_tts": ("audio", ()),
    "read_intent": ("adb", ("tag",)), "launch_app": ("adb", ("package",)), "close_app": ("adb", ("package",)),
    "tap": ("adb|arm", ("target",)), "swipe": ("adb|arm", ("target", "to")), "drag": ("adb|arm", ("target", "to")),
    "input_text": ("adb", ("text",)), "key": ("adb|arm", ("key",)), "wait": (None, ()),
    "screenshot": ("adb|cluster_shot", ()), "camera_shot": ("camera", ()),
    "can_send": ("can", ("msg",)), "can_capture": ("can", ()), "shell": ("adb|ssh_qnx", ()),
    "power_cycle": ("power", ()), "read_value": ("adb", ("read", "save_as")), "read_prop": ("adb", ("prop",)),
}
ASSERT_TYPES = {"rule", "vision", "voice", "text", "timing", "can", "value", "ui_state", "vehicle_prop"}
TARGET_KEYS = ("selector", "ocr", "image", "ratio", "fallback_xy", "arm_point")  # 多级定位
# Android Automotive 车辆属性（VHAL）常用 ID；TaskPlan 里也可直接写十六进制或十进制 ID
VEHICLE_PROPS = {
    "HVAC_POWER_ON": 0x15200510, "HVAC_AC_ON": 0x15200505, "HVAC_AUTO_ON": 0x1520050A,
    "HVAC_TEMPERATURE_SET": 0x15600503, "HVAC_FAN_SPEED": 0x15400500, "HVAC_RECIRC_ON": 0x15200508,
    "HVAC_DEFROSTER": 0x13200504, "HVAC_SEAT_TEMPERATURE": 0x1540050B,
}
KEYS = {"back", "home", "menu", "enter", "power", "volume_up", "volume_down"}
CAN_WHITELIST: set[str] = set()
RE_SIM_CMD = re.compile(r"cabin-hvac (on|off)( \d+)?")  # SimCar 示例车型命令
# 前置条件能下发的命令，只有这几种写法。云端表配错也发不出别的命令，台架装载时再校验一次
SHELL_PATTERNS = (RE_SIM_CMD, re.compile(r"settings put (system|secure|global) [\w.]+ [\w.\-]+"),
                  re.compile(r"input keyevent (\d+|[A-Z_]+)"), re.compile(r"am force-stop [\w.]+"))
RE_READ = re.compile(r"settings:(system|secure|global)/[\w.]+|getprop:[\w.]+")  # read_value 能读的来源
VALUE_OPS = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "ge": lambda a, b: a >= b, "le": lambda a, b: a <= b,
             "gt": lambda a, b: a > b, "lt": lambda a, b: a < b}
RE_PARAM = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")
RE_REDLINE = re.compile(r"刷机|恢复出厂|清除数据|\bwipe\b|\bfactory[_ -]?reset\b", re.I)  # swipe 不能误伤


def shell_allowed(cmd: str) -> bool:
    return any(p.fullmatch(cmd) for p in SHELL_PATTERNS)


def compare(op: str, actual: object, expected: object) -> bool:
    """都是数字时按数字比，否则按文本比；文本不支持大小比较。"""
    try:
        return VALUE_OPS[op](float(actual), float(expected))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return op in ("eq", "ne") and VALUE_OPS[op](str(actual), str(expected))


# ---------- 静态校验 ----------
def validate(plan: dict, case: TestCase | None = None) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 非空即 invalid，不允许试跑。"""
    errs: list[str] = []
    warns: list[str] = []
    if plan.get("schema") != SCHEMA:
        errs.append(f"schema 必须为 {SCHEMA}")
    if not plan.get("case_id"):
        errs.append("缺少 case_id")
    requires = set(plan.get("requires") or [])
    if not requires:
        errs.append("requires（所需能力）不能为空")
    if requires - CAPABILITIES:
        errs.append(f"未知能力标签：{', '.join(sorted(requires - CAPABILITIES))}")
    if case and requires - set(case.channel or ["adb"]) - {"adb"}:
        errs.append(f"所需能力 {sorted(requires)} 与用例通道 {case.channel} 不一致")
    steps = plan.get("steps") or []
    if not steps:
        errs.append("steps 为空（该通道一期暂无动作模板）")
    assets = plan.get("assets") or {}
    ids = set()
    for s in (plan.get("setup") or []) + steps + (plan.get("teardown") or []):
        where = f"步骤 {s.get('id', '·')} {s.get('action')}"
        if s.get("id") is not None:
            ids.add(s["id"])
        spec = ACTIONS.get(s.get("action", ""))
        if not spec:
            errs.append(f"{where}：动作不在动作库内")
            continue
        chan, need = spec
        for p in need:
            if s.get(p) in (None, "", {}):
                errs.append(f"{where}：缺少参数 {p}")
        if chan and not (set(chan.split("|")) & requires):
            errs.append(f"{where}：需要能力 {chan}，requires 未声明")
        if s.get("audio") and s["audio"] not in assets:
            errs.append(f"{where}：引用的音频 {s['audio']} 不在 assets 中")
        if s.get("action") == "key" and s.get("key") not in KEYS and not str(s.get("key", "")).isdigit():
            errs.append(f"{where}：未知键值 {s.get('key')}")
        if s.get("action") in ("tap", "swipe", "drag"):
            t = s.get("target") or {}
            if not any(t.get(k) for k in TARGET_KEYS):
                errs.append(f"{where}：target 需要 selector / ocr / image / ratio / 坐标之一")
            if t.get("image") and t["image"] not in (plan.get("images") or {}):
                errs.append(f"{where}：图标模板 {t['image']} 不在 images 中")
            r = t.get("ratio")
            if r is not None and not (isinstance(r, list) and len(r) == 2
                                      and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in r)):
                errs.append(f"{where}：ratio 应为 [0~1, 0~1]")
            if (t.get("ratio") or t.get("fallback_xy")) and not (t.get("selector") or t.get("ocr") or t.get("image")):
                warns.append(f"{where}：只有坐标定位，界面布局一变就会点错，建议补控件或文字定位")
        if s.get("action") == "read_prop":
            pv = str(s.get("prop", ""))
            if pv and pv.upper() not in VEHICLE_PROPS and not re.fullmatch(r"0x[0-9a-fA-F]+|\d+", pv):
                errs.append(f"{where}：未知车辆属性 {pv}")
        if s.get("action") == "shell":
            if s.get("cmd") and not shell_allowed(s["cmd"]):
                errs.append(f"{where}：命令不在白名单内")
            elif not s.get("cmd"):
                warns.append(f"前置条件“{s.get('note', '')[:30]}”的车型命令待补充（试跑前确认）")
        if s.get("action") == "read_value" and not RE_READ.fullmatch(str(s.get("read", ""))):
            errs.append(f"{where}：读值来源 {s.get('read')} 不合法")
        if s.get("action") == "can_send" and s.get("msg") not in CAN_WHITELIST:
            errs.append(f"{where}：CAN 报文不在白名单内")
    if "audio" in requires:
        if not assets:
            errs.append("语音用例必须有 assets（测试音频清单）")
        for name, a in assets.items():
            if not (a or {}).get("text"):
                errs.append(f"音频 {name} 缺少文字")
            elif RE_PARAM.search(a["text"]) or a.get("variants"):
                if not a.get("variants") or not all(a["variants"].values()):
                    warns.append(f"音频 {name} 的车型变体待合成")
            elif not a.get("sha256"):
                warns.append(f"音频 {name} 待合成")
    asserts = plan.get("assertions") or []
    if not asserts:
        errs.append("没有断言，无法判定结果")
    for a in asserts:
        aid = a.get("id", "?")
        t = a.get("type")
        if t not in ASSERT_TYPES:
            errs.append(f"断言 {aid}：类型 {t} 无判定方式")
            continue
        if a.get("after_step") is not None and a["after_step"] not in ids:
            errs.append(f"断言 {aid}：after_step={a['after_step']} 不存在")
        if t == "voice":
            exp = a.get("expected") or {}
            if not (exp.get("screen") and exp.get("tts")):
                errs.append(f"断言 {aid}：语音断言需要期望屏幕与期望播报")
        elif t == "vision" and not a.get("expected"):
            errs.append(f"断言 {aid}：视觉断言缺少期望")
        elif t == "text" and not a.get("expected"):
            errs.append(f"断言 {aid}：文本断言缺少期望")
        elif t == "timing" and not a.get("max_ms"):
            errs.append(f"断言 {aid}：时延断言缺少 max_ms")
        elif t == "value":
            saved = {x.get("save_as") for x in steps if x.get("action") == "read_value"}
            if a.get("key") not in saved:
                errs.append(f"断言 {aid}：{a.get('key')} 没有对应的 read_value 步骤")
            if a.get("op", "eq") not in VALUE_OPS or a.get("expected") is None:
                errs.append(f"断言 {aid}：数值断言需要 op 和 expected")
        elif t == "rule" and not a.get("rule"):
            errs.append(f"断言 {aid}：规则断言缺少 rule")
        elif t == "ui_state":
            if not ((a.get("target") or {}).get("selector")):
                errs.append(f"断言 {aid}：界面状态断言需要 target.selector")
            if not any(k in a for k in ("checked", "text", "exists")):
                errs.append(f"断言 {aid}：界面状态断言需要 checked / text / exists 之一")
            if a.get("after_step") is None:
                errs.append(f"断言 {aid}：界面状态断言需要 after_step（截图步骤）")
        elif t == "vehicle_prop":
            src = a.get("source") or a.get("prop")
            reads = {str(x.get("save_as") or x.get("prop")) for x in steps if x.get("action") == "read_prop"}
            if not src or a.get("expect") in (None, ""):
                errs.append(f"断言 {aid}：车辆属性断言需要 prop 与 expect")
            elif str(src) not in reads:
                errs.append(f"断言 {aid}：没有读取 {src} 的 read_prop 步骤")
    # 版本与车型（设计文档 6.7）
    models = plan.get("models")
    if models is not None:
        if not models:
            errs.append("models（适用车型）为空")
        unknown = [m for m in list(models) + list(plan.get("model_params") or {}) + list(plan.get("overrides") or {})
                   if m not in vehicles.codes()]
        if unknown:
            errs.append(f"车型不在车型字典内：{', '.join(sorted(set(unknown)))}")
    defined = set(plan.get("params") or {})
    body = {k: plan.get(k) for k in ("setup", "steps", "teardown", "assertions", "assets", "overrides")}
    used = set(RE_PARAM.findall(json.dumps(body, ensure_ascii=False)))
    if used - defined:
        errs.append(f"引用了未定义的参数：{', '.join(sorted(used - defined))}")
    for m, kv in (plan.get("model_params") or {}).items():
        if set(kv) - defined:
            errs.append(f"车型 {m} 的参数 {', '.join(sorted(set(kv) - defined))} 没有默认值")
    if case is not None and plan.get("case_rev") not in (None, case.case_rev):
        errs.append(f"case_rev={plan.get('case_rev')} 与用例修订 r{case.case_rev} 不一致")
    for name, a in (plan.get("images") or {}).items():
        if not re.fullmatch(r"[0-9a-f]{64}", str((a or {}).get("sha256") or "")):
            errs.append(f"图标模板 {name} 缺少有效的 sha256")
    warns += list((plan.get("meta") or {}).get("uimap_warnings") or [])
    if RE_REDLINE.search(yaml.safe_dump(plan, allow_unicode=True)):
        errs.append("包含红线操作（刷机 / 恢复出厂 / 清除数据）")
    return errs, warns
