"""cabin-planner：用例 → 测试代码 TaskPlan（cabin/taskplan/v1）+ 静态校验 + git 版本。

- 语音用例按固定时序模板生成，不调用模型；
- 触屏 / ADB 用例由模型把步骤翻译成动作库里的动作（控件选择器优先），失败时按规则兜底；
- 静态校验不通过则 plan_status=invalid，不允许试跑；
- TaskPlan 写入 data/plans（独立 git 仓库），提交号即 plan_version；没有 git 时退回内容哈希。
- 版本与车型：case_rev、baselines、models、params / model_params、overrides；
  ${参数名} 在台架装载时按本机车型代入（runner/loader.resolve）；含参数的测试音频按各适用车型取值合成 variants。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from cabin_common import vehicles
from . import audioasset, modules, uimap
from .caselib import get_caselib
from .config import get_settings
from .modelgw import extract_json, get_gateway
from cabin_common.schema import PrdVersion, TestCase
from cabin_common.taskplan import (ACTIONS, ASSERT_TYPES, CAN_WHITELIST, CAPABILITIES, KEYS, RE_PARAM, RE_REDLINE,  # noqa: F401
                                  RE_SIM_CMD, SCHEMA, WAKE_WORD, validate)



def plans_dir() -> Path:
    d = get_settings().data_dir / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 生成 ----------
def _setup(c: TestCase) -> list[dict]:
    if not c.precondition.strip():
        return []
    # 前置条件命令按车型提供；真实车型命令库为空 → 引用占位，校验给出告警
    cmd = sim_precondition_cmd(c.precondition) if get_settings().device == "simcar" else ""
    if cmd:
        return [{"action": "shell", "cmd": cmd, "note": c.precondition}]
    return [{"action": "shell", "cmd_ref": f"precondition:{c.case_id}", "note": c.precondition}]


def sim_precondition_cmd(text: str) -> str:
    """把空调前置条件翻译成 SimCar 示例命令；翻不出来返回空串。"""
    temp = re.search(r"(\d+)\s*(?:℃|度)", text)
    if re.search(r"空调(?:当前|目前|处于|已经?|为)*(?:关闭|关)", text):
        return "cabin-hvac off" + (f" {temp.group(1)}" if temp else "")
    if re.search(r"空调(?:当前|目前|处于|已经?|为)*(?:开启|打开|开)", text):
        return "cabin-hvac on" + (f" {temp.group(1)}" if temp else "")
    return ""


def voice_plan(c: TestCase) -> dict:
    voice = c.voice_profile or "female_a"
    expected: dict[str, str] = {"screen": c.expect_screen, "tts": c.expect_tts}
    if c.expect_intent:
        expected["intent"] = c.expect_intent
    return {
        "assets": {"wake.wav": {"text": WAKE_WORD, "voice": voice, "sha256": None},
                   "utt.wav": {"text": c.utterance, "voice": voice, "speed": 1.0, "sha256": None}},
        "setup": _setup(c),
        "steps": [
            {"id": 1, "action": "mute_host"},
            {"id": 2, "action": "record_start", "rate": 16000},
            {"id": 3, "action": "wake", "audio": "wake.wav", "wait_s": 3},
            {"id": 4, "action": "play_utterance", "audio": "utt.wav"},
            {"id": 5, "action": "listen_tts", "silence_s": 1.5, "timeout_s": 8},
            {"id": 6, "action": "record_stop"},
            {"id": 7, "action": "screenshot", "domain": "center"},
            {"id": 8, "action": "unmute_host"},
        ],
        "assertions": [
            {"id": "a1", "after_step": 7, "type": "voice", "expected": expected},
            {"id": "a2", "after_step": 5, "type": "timing", "from": "t0", "to": "tts_start",
             "max_ms": c.latency_max_ms or 1500},
            {"id": "a3", "type": "rule", "rule": "no_crash"},
        ],
        "teardown": [{"action": "unmute_host"}],
    }


SYS_UI = """你把座舱触屏测试用例翻译成测试框架动作序列。只输出 JSON：
{"steps": [{"action": "...", ...}], "assert_after": "最后一个用于判定的截图步骤序号",
 "checks": [{"key": "read_value 的 save_as", "op": "eq|ne|ge|le|gt|lt", "expected": 期望值}],
 "assertions": [界面状态 / 车辆属性断言，见下]}
可用动作（只能用这些）：
- {"action":"launch_app","package":"包名"}  启动应用到首页（起点）；{"action":"close_app","package":"包名"}
- {"action":"key","key":"home|back"}  回到桌面 / 返回
- {"action":"tap","target":<定位>}  点击
- {"action":"drag","target":<定位>,"to":{"ratio":0.0到1.0}}  拖动滑条
- {"action":"swipe","target":<定位>,"to":{"direction":"up|down|left|right"}}
- {"action":"input_text","text":"..."}
- {"action":"wait","until":{"text":"出现的文字"},"timeout_s":10}  或 {"action":"wait","seconds":2}；等开关状态：{"action":"wait","until":{"selector":{"text":"开关名"},"checked":true},"timeout_s":3}
- {"action":"screenshot","domain":"center"}  截图并保存控件树（界面状态断言要放在它之后）
- {"action":"read_value","read":"settings:system/screen_brightness","save_as":"brightness"}  读系统值，read 只能是 settings:<system|secure|global>/<键> 或 getprop:<属性>
- {"action":"read_prop","prop":"车辆属性名"}  读车辆真实状态（Android Automotive）
<定位> = {"selector":{"text":"控件文字"},"ocr":"控件文字"}：控件树找不到时用截图 OCR 再找；知道 resource-id 时写 selector.key。
断言（可选，放在 "assertions"）：
- {"type":"ui_state","after_step":截图步骤序号,"target":{"selector":{"text":"开关名"}},"checked":true}  开关状态
- {"type":"ui_state","after_step":截图步骤序号,"target":{"selector":{"text":"控件名"}},"text":"应显示的文字"}
- {"type":"vehicle_prop","prop":"车辆属性名","expect":"true|false|数值"}  车辆状态（与 read_prop 配对）
已知应用：{apps}
已知车辆属性：HVAC_POWER_ON 空调电源、HVAC_AC_ON 压缩机(A/C)、HVAC_AUTO_ON 自动空调、HVAC_TEMPERATURE_SET 设定温度、HVAC_FAN_SPEED 风量、HVAC_RECIRC_ON 内循环。
规则：
- 预期里的数值能从系统读到时必须用 read_value 读出来，再写进 checks，不要只靠截图判断（例：屏幕亮度 100% 即 screen_brightness 等于 255）。
- 已知被测应用包名时从 launch_app 开始，否则从回到桌面开始；每个用例步骤翻译成 1~2 个动作；需要观察结果时加 screenshot。
- 打开 / 关闭开关后：先 wait 到开关状态，再 screenshot，再加 ui_state 断言判开关状态。
- 预期涉及车辆状态（如空调打开、温度设定）时：加 read_prop 步骤和 vehicle_prop 断言，和界面断言一起做双线判定。
- 不要编造界面上没有的文字，用用例里出现的文字。"""


def _ui_fallback(c: TestCase) -> list[dict]:
    steps: list[dict] = [{"action": "key", "key": "home"}]
    for s in c.steps:
        m = re.search(r"[“\"「]([^”\"」]+)[”\"」]", s)
        word = m.group(1) if m else re.sub(r"^(点击|进入|打开|选择|切换|查看|定位)", "", s).split("→")[-1].strip()
        if re.match(r"^(查看|观察|截图|读取|定位)", s):
            steps.append({"action": "screenshot", "domain": "center"})
        elif re.search(r"返回", s):
            steps.append({"action": "key", "key": "back"})
        elif word:
            steps.append({"action": "tap", "target": {"selector": {"text": word[:16]}}})
    if steps[-1]["action"] != "screenshot":
        steps.append({"action": "screenshot", "domain": "center"})
    return steps


async def ui_plan(c: TestCase, session: str = "") -> tuple[dict, str]:
    how = "model"
    checks: list[dict] = []
    extra: list[dict] = []
    pkg = case_package(c)
    umap = uimap.load_map(pkg) if pkg else None
    images: dict = {}
    map_warns: list[str] = []
    try:
        prompt = (f"用例 {c.case_id}：{c.title}\n页面：{c.page or '—'}\n前置条件：{c.precondition}\n"
                  f"步骤：\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(c.steps, 1)) + f"\n预期：{c.expected}")
        if umap:
            prompt += f"\n\n界面上实际存在的控件（UI 地图，只能引用这些控件的文字）：\n{uimap.map_hint(umap)}"
        text = await get_gateway().complete(system=SYS_UI.replace("{apps}", apps_hint()), prompt=prompt,
                                            label="internal", source="planner.ui", session=session, max_tokens=6000)
        data = extract_json(text)
        steps = [s for s in data.get("steps") or [] if isinstance(s, dict) and s.get("action")]
        checks = [{k: x.get(k) for k in ("key", "op", "expected")} for x in data.get("checks") or [] if isinstance(x, dict)]
        extra = [a for a in data.get("assertions") or []
                 if isinstance(a, dict) and a.get("type") in ("ui_state", "vehicle_prop")]
        if not steps:
            raise ValueError("空步骤")
        if steps[-1].get("action") != "screenshot":
            steps.append({"action": "screenshot", "domain": "center"})
        if umap:
            steps, map_warns, images = uimap.enrich(steps, umap)
            how = "model+uimap"
    except Exception:
        steps, how, checks, extra = _ui_fallback(c), "rule-fallback", [], []
    # 模型给的步骤序号可能不连续：重新编号，断言里的 after_step 跟着换
    remap: dict = {}
    for i, s in enumerate(steps, 1):
        old = s.pop("id", None)
        if old is not None:
            remap[old] = i
        steps[i - 1] = {"id": i, **s}
    shot = steps[-1]["id"]
    asserts: list[dict] = [{"id": "a1", "after_step": shot, "type": "vision", "expected": c.expected,
                            "region": None}]  # 首次试跑时由视觉模型补全区域与基线截图
    for a in extra:
        a = {k: v for k, v in a.items() if k != "id"}
        if a.get("after_step") is not None:
            a["after_step"] = remap.get(a["after_step"], a["after_step"])
        elif a["type"] == "ui_state":
            a["after_step"] = shot
        asserts.append({"id": f"a{len(asserts) + 1}", **a})
    asserts.append({"id": f"a{len(asserts) + 1}", "type": "rule", "rule": "no_crash"})
    asserts += [{"id": f"a{len(asserts) + i}", "type": "value", **k} for i, k in enumerate(checks, 1)]
    body: dict[str, Any] = {"setup": _setup(c), "steps": steps, "assertions": asserts,
                            "teardown": [{"action": "key", "key": "home"}]}
    if images:
        body["images"] = images
    if map_warns:
        body["meta"] = {"uimap_warnings": map_warns}
    return body, how


def _confirmed_recording(c: TestCase):
    """该用例最近一次人工确认、且走完了的 UI 地图录制。"""
    recs = [r for r in uimap.recordings_for(c.case_id) if r.outcome == "done"]
    return recs[-1] if recs else None


def case_package(c: TestCase) -> str:
    """用例涉及的被测应用：按页面 / 标题 / 步骤里出现的应用名匹配 CABIN_APP_PACKAGES。"""
    text = " ".join([c.page, c.title, *c.steps])
    return next((pkg for name, pkg in get_settings().app_packages.items() if name in text), "")


def apps_hint() -> str:
    """被测应用的包名（云端配置 CABIN_APP_PACKAGES，形如 设置=com.android.car.settings;空调=…）。"""
    apps = get_settings().app_packages
    return "；".join(f"{k}={v}" for k, v in apps.items()) or "（未配置，从回到桌面开始）"


def _case_text(c: TestCase) -> str:
    return " ".join([c.precondition, c.utterance, c.expect_screen, c.expect_tts, c.expected, " ".join(c.steps),
                     " ".join(str(v) for kv in c.model_overrides.values() for v in kv.values())])


def param_block(c: TestCase) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """用例引用到的车型参数：(默认值, 车型差异值)。取值来自该 PRD 最新版本的车型参数表（生成时快照）。"""
    used = set(RE_PARAM.findall(_case_text(c)))
    if not used:
        return {}, {}
    ps = {p.name: p for p in (get_caselib().params_for(c.prd_id) if c.prd_id else [])}
    params = {n: ps[n].default for n in sorted(used) if n in ps}
    mp: dict[str, dict[str, str]] = {}
    for n in params:
        for m, v in ps[n].values.items():
            if v != params[n]:
                mp.setdefault(m, {})[n] = v
    return params, mp


def plan_models(c: TestCase) -> list[str]:
    return [m for m in (c.models or vehicles.codes())]


async def build_plan(c: TestCase, session: str = "") -> tuple[dict, str]:
    requires = sorted({ch for ch in c.channel if ch in CAPABILITIES}) or ["adb"]
    head: dict[str, Any] = {"schema": SCHEMA, "case_id": c.case_id, "case_rev": c.case_rev, "title": c.title,
                            "baselines": list(c.baselines), "models": plan_models(c),
                            "requires": requires, "timeout_s": 120 if c.is_voice else 180}
    params, mp = param_block(c)
    if params:
        head["params"] = params
        if mp:
            head["model_params"] = mp
    if c.model_overrides:
        head["overrides"] = {m: dict(v) for m, v in c.model_overrides.items()}
    if c.is_voice:
        body, how = voice_plan(c), "voice-template"
    elif ("adb" in requires or "arm" in requires) and (rec := _confirmed_recording(c)):
        body, how = {"setup": _setup(c), **uimap.plan_from_recording(rec)}, "uimap-recording"
    elif "adb" in requires or "arm" in requires:
        body, how = await ui_plan(c, session)
    else:
        body, how = {"setup": _setup(c), "steps": [], "assertions": []}, "unsupported"
    meta = {"generated_by": how, "batch_id": c.batch_id, "req_id": c.req_id, "prd_section": c.prd_section,
            "prd_id": c.prd_id, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    plan = {**head, **body, "on_fail": "continue", "meta": {**meta, **(body.get("meta") or {})}}
    if c.is_voice:
        await audioasset.fill_plan(plan, session)  # 下发前合成并写回哈希
    return plan, how




# ---------- 存储（git） ----------
_git_lock = asyncio.Lock()


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(plans_dir()), *args], capture_output=True, text=True, encoding="utf8",
                       timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


_repo_ready: set[str] = set()


def _ensure_repo() -> bool:
    if not shutil.which("git"):
        return False
    if not (plans_dir() / ".git").exists():
        _git("init", "-q")
        _git("config", "user.name", "CabinPilot Planner")
        _git("config", "user.email", "cabin@localhost")
    if str(plans_dir()) not in _repo_ready:  # 模块文件夹是中文名：git 输出不转义
        _git("config", "core.quotepath", "false")
        _repo_ready.add(str(plans_dir()))
    return True


def plan_rel_path(c: TestCase) -> str:
    """最新修订：<模块文件夹>/cases/<用例>.yaml（修订历史在 git 提交里）；为旧修订补生成时放 history/，不覆盖最新修订。
    模块文件夹见 backend/modules.yaml。"""
    folder = modules.of_case(c.case_id).folder
    latest = max((x.case_rev for x in get_caselib().revisions(c.case_id)), default=c.case_rev)
    if c.case_rev < latest:
        return f"{folder}/cases/history/{c.case_id}.r{c.case_rev}.yaml"
    return f"{folder}/cases/{c.case_id}.yaml"


RE_NAME_VER = re.compile(r"(?i)[\s_\-·]*(?:PRD)?[\s_\-]*[Vv]?\d+(?:\.\d+){1,3}$")
RE_NAME_BAD = re.compile(r'[\\/:*?"<>|\s]+')


def prd_rel_path(code: str, name: str, version: str, ext: str) -> str:
    """PRD 原文存档路径：<模块文件夹>/prd/<文件名去掉版本号>_V<版本><扩展名>。"""
    stem = RE_NAME_VER.sub("", Path(name).stem) or Path(name).stem or "PRD"
    stem = RE_NAME_BAD.sub("_", stem).strip("_.") or "PRD"
    return f"{modules.get(code).folder}/prd/{stem}_V{version}{ext}"


async def archive_prd(batch, doc) -> str:
    """把 PRD 原文按版本存进模块文件夹并提交 git；PRD 版本记录里记下路径。没有版本信息时不存。"""
    if not batch.prd_id or not batch.prd_version:
        return ""
    code = batch.prd_id.split("-")[1] if batch.prd_id.count("-") >= 2 else modules.code_for(batch.module)
    raw, ext = doc.raw, Path(doc.name).suffix.lower()
    if not raw or not ext:  # 在线文档：存成 Markdown 文本
        raw, ext = doc.text().encode("utf8"), ".md"
    rel = prd_rel_path(code, doc.name, batch.prd_version, ext)
    p = plans_dir() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    async with _git_lock:
        try:
            if await asyncio.to_thread(_ensure_repo):
                await asyncio.to_thread(_git, "add", rel)
                if await asyncio.to_thread(_git, "status", "--porcelain", rel):
                    await asyncio.to_thread(_git, "commit", "-q", "-m",
                                            f"PRD {batch.prd_id}@{batch.prd_version} {doc.title}"[:120])
        except Exception:
            pass
    lib = get_caselib()
    pv = lib.get_prd_version(batch.prd_id, batch.prd_version)
    if pv and pv.archive_path != rel:
        pv.archive_path = rel
        lib.save_prd_version(pv)
    return rel


def relocate() -> list[tuple[str, str]]:
    """modules.yaml 改了 folder 后，把已有的测试代码 / PRD 存档挪到新文件夹（git mv），并更新记录的路径。
    只挪工作区里存在的文件；旧修订按提交号从 git 取回，路径不用改。返回 [(旧, 新)]。"""
    lib = get_caselib()
    moved: list[tuple[str, str]] = []
    repo = _ensure_repo()

    def mv(old: str, new: str) -> bool:
        if old == new or not (plans_dir() / old).exists():
            return False
        (plans_dir() / new).parent.mkdir(parents=True, exist_ok=True)
        if repo:
            try:
                _git("mv", "-f", old, new)
            except Exception:
                (plans_dir() / old).replace(plans_dir() / new)
        else:
            (plans_dir() / old).replace(plans_dir() / new)
        moved.append((old, new))
        return True

    for r in lib.st.q("SELECT case_id FROM test_case ORDER BY case_id"):
        revs = lib.revisions(r["case_id"])
        latest = max((x.case_rev for x in revs), default=0)
        for c in revs:
            if not c.plan_path or (c.case_rev != latest and "/history/" not in c.plan_path):
                continue
            new = plan_rel_path(c)
            if mv(c.plan_path, new) or (c.plan_path != new and (plans_dir() / new).exists()):
                c.plan_path = new
                lib.save_rev(c)
    for r in lib.st.q("SELECT doc FROM prd_version"):
        pv = PrdVersion.model_validate_json(r["doc"])
        if not pv.archive_path:
            continue
        new = prd_rel_path(pv.prd_id.split("-")[1], Path(pv.archive_path).name, pv.version,
                           Path(pv.archive_path).suffix)
        if mv(pv.archive_path, new) or (pv.archive_path != new and (plans_dir() / new).exists()):
            pv.archive_path = new
            lib.save_prd_version(pv)
    if repo and moved:
        try:
            _git("commit", "-q", "-m", f"按 modules.yaml 调整模块文件夹（{len(moved)} 个文件）")
        except Exception:
            pass
    return moved


def dump_yaml(plan: dict) -> str:
    return yaml.safe_dump(plan, allow_unicode=True, sort_keys=False, width=200)


async def save_plan(c: TestCase, plan: dict) -> str:
    rel = plan_rel_path(c)
    p = plans_dir() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    text = f"# {c.case_id} r{c.case_rev} {c.title} · 由 CabinPilot 自用例生成，回放时不调用大模型\n" + dump_yaml(plan)
    p.write_text(text, "utf8")
    async with _git_lock:
        try:
            if await asyncio.to_thread(_ensure_repo):
                await asyncio.to_thread(_git, "add", rel)
                if await asyncio.to_thread(_git, "status", "--porcelain", rel):
                    await asyncio.to_thread(_git, "commit", "-q", "-m", f"TaskPlan {c.case_id} r{c.case_rev} {c.title}")
                return (await asyncio.to_thread(_git, "log", "-1", "--format=%h", "--", rel))[:8]
        except Exception:
            pass
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def read_plan(case_id: str, rev: int | None = None) -> str | None:
    """读测试代码。rev 为历史修订时按该修订记录的提交号从 git 取回（同一文件路径，不同提交）。"""
    lib = get_caselib()
    c = lib.get_case(case_id, rev)
    if not c or not c.plan_path:
        return None
    cur = lib.get_case(case_id)
    if rev is not None and cur and cur.case_rev != rev and c.plan_version:
        try:
            return _git("show", f"{c.plan_version}:{c.plan_path}")
        except Exception:
            if "/history/" not in c.plan_path:
                return None
    p = plans_dir() / c.plan_path
    return p.read_text("utf8") if p.exists() else None


def tag_baseline(baseline_id: str) -> None:
    """基线发布时在测试代码仓库打标签（指向发布时刻；精确取回按各修订记录的提交号）。"""
    if not _ensure_repo():
        return
    try:
        _git("rev-parse", "HEAD")
    except Exception:
        return  # 还没有任何测试代码
    _git("tag", "-f", re.sub(r"[^A-Za-z0-9._@-]", "_", baseline_id))


def needs_generation(c: TestCase) -> bool:
    """拉取时要（重新）生成：还没就绪；语音用例的测试音频没合成；或车型参数取值已变（只刷新参数，不出新修订）。"""
    if c.plan_status in ("ready", "unsuitable"):
        if c.plan_status != "ready":
            return False
        text = read_plan(c.case_id, c.case_rev)
        if text is None:
            return True
        plan = yaml.safe_load(text)
        if c.is_voice and audioasset.missing(plan):
            return True
        params, mp = param_block(c)
        return (plan.get("params") or {}) != params or (plan.get("model_params") or {}) != mp
    return True


def plan_view(c: TestCase) -> dict | None:
    """已生成测试代码的展示信息（与 generate_for 的结果同构）。"""
    text = read_plan(c.case_id, c.case_rev)
    if text is None:
        return None
    plan = yaml.safe_load(text)
    errs, warns = validate(plan, c)
    return {"case_id": c.case_id, "title": c.title, "ok": not errs, "errors": errs, "warnings": warns,
            "plan_status": c.plan_status, "plan_version": c.plan_version, "plan_path": c.plan_path,
            "auto_status": c.auto_status, "trial": c.trial,
            "generated_by": (plan.get("meta") or {}).get("generated_by", ""), "n_steps": len(plan.get("steps") or []),
            "n_asserts": len(plan.get("assertions") or []), "requires": plan.get("requires"), "yaml": dump_yaml(plan)}


async def generate_for(c: TestCase, session: str = "") -> dict:
    """生成 + 校验 + 入 git，回写用例的自动化状态。返回结果摘要（给界面 / 对话）。"""
    lib = get_caselib()
    c.plan_status = "generating"
    lib.save_rev(c)
    try:
        plan, how = await build_plan(c, session)
        errs, warns = validate(plan, c)
        version = await save_plan(c, plan)
    except Exception as e:
        c.plan_status, c.auto_status = "invalid", "需重新生成"
        lib.save_rev(c)
        return {"case_id": c.case_id, "ok": False, "errors": [f"生成失败：{type(e).__name__} {e}"], "warnings": []}
    c.plan_path, c.plan_version = plan_rel_path(c), version
    if errs:
        c.plan_status, c.auto_status = "invalid", "需重新生成"
    else:
        c.plan_status, c.auto_status = "ready", "已生成"
        c.trial = {}
    lib.save_rev(c)
    return {"case_id": c.case_id, "case_rev": c.case_rev, "title": c.title, "ok": not errs, "errors": errs, "warnings": warns,
            "plan_status": c.plan_status, "plan_version": version, "plan_path": c.plan_path, "generated_by": how,
            "auto_status": c.auto_status, "trial": c.trial,
            "n_steps": len(plan.get("steps") or []), "n_asserts": len(plan.get("assertions") or []),
            "requires": plan.get("requires"), "yaml": dump_yaml(plan)}
