"""cabin-casegen：PRD → 需求点 / 待确认项 → 用例 → AI 自检 → 批次。

模型只负责理解与起草；编号、字段补全、自检规则与批次记录由本模块确定性完成，结果可复现、可审计。

版本与车型：PRD 版本号只校验存在，读不到由测试预填；每个需求点带车型打点
（原文 / 推断 / 默认）与指纹。同一 PRD 的新版本与上一已发布版本按需求点配对，只为新增、修改的需求生成用例；
未改需求的用例继承（不送审），修改需求的用例沿用编号、修订号 +1。

重复导入与同版本变更：
- 身份：飞书文档 / 在线链接首次识别出 prd_id 后与来源绑定（prd_source），之后改标题、模块判断漂移都不影响。
- 去重：同一内容（同章节范围；飞书再比 revision_id）已生成过的，不再生成（find_existing，由调用方先查）。
- 已发布的版本不可覆盖：版本号没变但内容变了，抛 VersionConflict，由测试选择升版本号（version_override）
  或作为修订（as_revision，记为 0.6+r2，以该版本最新修订为对比基准）。
- 同版本还没审完的批次：同章节范围的新导入取代它（作废）；不同章节范围的先审完再导入。
- 按章节导入时只和基准里同章节的需求点配对，其它章节的需求点与用例原样继承。

块级增量（plan_incremental）：每个版本存块快照（飞书块 ID / 内容哈希），每个需求点记出处块。调模型前能认出基准版本时，
只把内容变了的块所在章节送去抽取；出处块都没变的需求点直接沿用（未改），不经模型，结果确定。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
from typing import Any, Awaitable, Callable

from cabin_common import vehicles
from . import modules, skills
from .cases import get_cases
from .caselib import IN_LIBRARY, default_models, get_caselib
from .modelgw import extract_json, get_gateway
from dataclasses import dataclass, field

from .prd import Block, PrdDoc, _in_section, render
from cabin_common.schema import Ambiguity, CaseBatch, CheckItem, ModelParam, PrdVersion, Requirement, TestCase

PROMPT_VERSION = "casegen/v3"
MAX_DOC_CHARS = 60000
REQ_PER_CALL = 4
GEN_CONCURRENCY = 4  # 同时在跑的生成调用数，避免撞模型限流
CTX_CHARS = 8000  # 每组生成调用带的 PRD 原文上限（只带出处章节）
Emit = Callable[[str, dict], Awaitable[None]]

# 模块缩写在 backend/modules.yaml 维护（modules.code_for）；模型给出的缩写作为兜底
CHANNELS = ("audio", "adb", "ssh_qnx", "arm", "can")
VOICE_DEFAULT = "female_a"
LATENCY_DEFAULT = 1500
VAGUE = re.compile(r"正常|合理|流畅|符合预期|良好|友好|及时|快速|稳定|准确")
OBSERVABLE = re.compile(r"\d|[“\"「'‘].+?[”\"」'’]|不变|置灰|为[开关]|无.{0,4}(崩溃|闪退|报错)")

SYS_EXTRACT = """你是座舱测试需求分析师。阅读 PRD，抽取可测试的需求点、车型打点与车型参数，并找出需要需求方确认的问题。
只输出一个 JSON 对象，不要输出其它内容：
{"title": "文档或章节标题", "module": "模块路径，如 语音助手/空调", "module_code": "模块缩写，大写英文，如 VOICE",
 "version": "PRD 写明的文档版本号，如 0.1.0；没写就给空串",
 "requirements": [{"section": "所在章节编号，如 4.1.2", "quote": "原文片段（照抄，≤80 字）", "summary": "需求摘要（≤14 字）",
   "models": {"车型": "●|—"}, "model_source": "表格|正文|无"}],
 "params": [{"name": "英文小写下划线，如 temp_max", "desc": "说明", "default": "多数车型的值",
   "values": {"车型": "该车型的不同值"}, "sections": ["引用它的需求章节"]}],
 "ambiguities": [{"section": "章节编号", "quote": "原文片段", "question": "需要需求方回答的问题（一句话）"}]}
车型打点规则：
- 已知车型：{vehicles}。
- PRD 有车型打点表（需求 × 车型）时，按表给出每个需求的 models，model_source=表格；○ / 选配 按 ● 处理。
- 正文写了车型限定（如“仅 A01 国际支持”“B01 欧版不支持”）时，给出全部已知车型的 ● / —，model_source=正文。
- 需求没有提到车型时，models 给空对象，model_source=无。
- 同一行为在不同车型上数值不同（如“E01 最高 30℃，其余 32℃”）时，抽取为 params，不要拆成多条需求。
需求点规则：
- 一个需求点 = 一个“触发条件 → 系统响应”。同一触发条件下的多个结果（如执行动作 + 屏幕显示 + 语音播报）合为一个需求点，不要拆开；表格每一行是一个需求点。
- 功能描述、背景、目标、名词解释不算需求点。
- 缺少约束（如没给范围、时限、失败行为）、前后冲突或写明“未定义 / 待定”的，放进 ambiguities，不要放进 requirements。
- 不要编造 PRD 没写的内容。"""
VEHICLE_HINT = "、".join(f"{vehicles.name(c)}（{c}）" for c in vehicles.codes())
SYS_EXTRACT = SYS_EXTRACT.replace("{vehicles}", VEHICLE_HINT)
RE_DOC_VERSION = re.compile(r"(?:文档版本|版本号|版本|Version)[^\d\n]{0,12}?[Vv]?(\d+(?:\.\d+){1,3})", re.I)
RE_PARAM = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}")

SYS_GENERATE = """你是座舱测试工程师，按需求点编写测试用例。只输出一个 JSON 对象：
{"cases": [{"req_id": "REQ-01", "title": "≤16 字", "type": "正向|边界|异常", "priority": "P0|P1|P2",
  "channel": ["audio" 和/或 "adb" "ssh_qnx" "arm" "can"],
  "precondition": "前置条件（具体状态，如：空调已开启，当前 22℃；车机语音已唤醒）",
  "steps": ["步骤 1", "步骤 2"],
  "expected": "可判定的预期结果",
  "page": "界面页面路径，如 设置/显示；语音用例可留空",
  "utterance": "语音用例的测试语句，非语音用例留空",
  "expect_screen": "语音用例期望屏幕，如 空调温度显示 24℃", "expect_tts": "语音用例期望播报，如 已为您调到 24 度",
  "expect_intent": "期望意图（可选），如 AC_SET_TEMP=24", "latency_max_ms": 1500,
  "concerns": ["你对这条用例不确定的地方，如 PRD 未定义的行为；没有就给空数组"]}]}
规则：
- 每个需求点按需要覆盖正向、边界、异常，通常 1~3 条，不超过 {max_per_req} 条；同一测试语句 / 同一操作只写一条，不要为同一行为写重复用例。
- 语音用例必须同时写期望屏幕和期望播报（双线判定），两者缺一不可。
- 时延类需求只写 1 条专门用例（type=边界），选一个代表性指令；其它语音用例已自带时延断言，不用另写。
- 预期必须可观测、可判定：写出具体数值、文案或状态；禁止只写“正常”“合理”“流畅”“符合预期”。
- 通道：车主说话 / 语音助手类 → ["audio","adb"]（看屏幕用 adb 截图）；触屏操作界面 → ["adb"]；仪表 / QNX → ["ssh_qnx"]；物理按键 → ["arm"]；车身信号 → ["can"]。
- 语音用例期望值由测试语句参数推出：说“空调调到 26 度”→ 期望屏幕 26℃，期望播报含“26 度”；异常用例按 PRD 规定的拒绝行为写期望。
- 步骤写测试人员 / 机器人要做的动作，一步一个动作。
- 给了“车型参数”时：数值随车型不同的地方写 ${参数名}，不要写具体值（如 测试语句“空调调到 ${temp_max} 度”、期望屏幕“${temp_max}℃”）。"""


class VersionConflict(Exception):
    """PRD 版本号与已发布的版本相同，但内容变了：需要测试决定升版本号还是作为修订。"""

    def __init__(self, prd_id: str, version: str, name: str, extracted: dict):
        super().__init__(f"{prd_id}@{version} 已发布，内容有变")
        self.prd_id, self.version, self.name, self.extracted = prd_id, version, name, extracted


class PrdBusy(Exception):
    """同一 PRD 同版本还有一个不同章节范围的批次没审完。"""


def content_sha(doc: PrdDoc, section: str = "") -> str:
    return hashlib.sha256(doc.text(section).encode()).hexdigest()[:16]


def find_existing(doc: PrdDoc, section: str = "") -> CaseBatch | None:
    """这份内容（同章节范围）是否已经生成过：返回已有批次，没有返回 None。"""
    return get_caselib().find_duplicate(content_sha(doc, section), section, doc.source, doc.revision)


STABLE_SOURCES = ("feishu:", "url:")  # 来源本身能唯一标识文档的，绑定 prd_id


async def _llm_json(system: str, prompt: str, source: str, session: str, max_tokens: int = 8000) -> dict:
    gw = get_gateway()
    last = ""
    for attempt in range(2):
        text = await gw.complete(system=system, prompt=prompt, label="internal", source=source, session=session,
                                 max_tokens=max_tokens, think=attempt > 0)
        try:
            return extract_json(text)
        except (ValueError, Exception) as e:  # 输出截断 / 夹杂文字：开思考链重试一次
            last = f"{type(e).__name__}: {text[:160]}"
    raise ValueError(f"模型输出无法解析：{last}")


def module_code(module: str, suggested: str = "") -> str:
    """模块路径 → 模块缩写（规则与关键词在 modules.yaml，见 modules.code_for）。"""
    return modules.code_for(module, suggested)


# ---------- 自检 ----------
def _sig(c: TestCase) -> str:
    return " ".join([c.title, c.utterance, " ".join(c.steps), c.expected, c.expect_screen, c.expect_tts])


def _norm_utt(s: str) -> str:
    return re.sub(r"[\s，。,.!！?？“”\"']", "", s or "")


def _nums(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


def self_check(cases: list[TestCase], library: list[TestCase], params: set[str] | None = None,
               skill: "skills.Skill | None" = None) -> None:
    """就地写入 checks 与 suggestion。重复：与本批更早用例或用例库相似度 ≥ 0.85。"""
    seen: list[TestCase] = []
    for c in cases:
        checks: list[CheckItem] = [x for x in c.checks if x.kind == "PRD 未定义"]
        for other in seen + library:
            if other.case_id == c.case_id:
                continue
            if c.is_voice and other.is_voice:
                # 语音用例：同一测试语句、同一类型、同一期望屏幕才算重复；“调到 16 度”与“调到 26 度”是不同的边界点，
                # “空调关闭时调到 24 度”（期望先开空调）与“调到 24 度”也不同
                dup = (_norm_utt(c.utterance) == _norm_utt(other.utterance) and c.type == other.type
                       and _norm_utt(c.expect_screen) == _norm_utt(other.expect_screen))
                r = 1.0 if dup else 0.0
            else:
                r = difflib.SequenceMatcher(None, _sig(c), _sig(other)).ratio()
                dup = r >= 0.85 and _nums(_sig(c)) == _nums(_sig(other))  # 只差参数值的不算重复
            if dup:
                checks.append(CheckItem(kind="重复", msg=f"与 {other.case_id}“{other.title}”重复度高（{max(r, 0.85):.2f}）",
                                        suggest="驳回"))
                break
        if not c.precondition.strip():
            checks.append(CheckItem(kind="缺前置条件", msg="缺少前置条件，执行起点不确定"))
        if c.is_voice:
            miss = [n for n, v in (("测试语句", c.utterance), ("期望屏幕", c.expect_screen), ("期望播报", c.expect_tts))
                    if not v.strip()]
            if miss:
                checks.append(CheckItem(kind="预期不可判定", msg=f"语音用例缺少{'、'.join(miss)}"))
            elif c.type != "异常":
                lost = _nums(c.utterance) - _nums(c.expect_screen + c.expect_tts)
                if lost:
                    checks.append(CheckItem(kind="预期不可判定",
                                            msg=f"测试语句中的数值 {'、'.join(sorted(lost))} 未出现在期望屏幕 / 播报中"))
        exp = " ".join([c.expected, c.expect_screen, c.expect_tts])
        if VAGUE.search(exp) and not OBSERVABLE.search(exp):
            checks.append(CheckItem(kind="预期不可判定", msg=f"预期“{c.expected[:24]}”没有可观测的量化条件"))
        undefined = sorted(set(RE_PARAM.findall(_sig(c) + c.precondition)) - set(params or ()))
        if params is not None and undefined:
            checks.append(CheckItem(kind="预期不可判定", msg=f"引用了未定义的车型参数 {', '.join(undefined)}"))
        if not c.channel:
            checks.append(CheckItem(kind="通道不符", msg="无法判断执行通道，请人工指定"))
        elif c.utterance and "audio" not in c.channel:
            checks.append(CheckItem(kind="通道不符", msg="有测试语句但通道不含语音（audio）"))
        elif "audio" in c.channel and not c.utterance:
            checks.append(CheckItem(kind="通道不符", msg="通道为语音但没有测试语句"))
        checks += [CheckItem(kind="规范", msg=msg, suggest=sug) for msg, sug in skills.check(c, skill)]  # 模块规范包的额外规则
        c.checks = checks
        kinds = {x.suggest for x in checks}
        c.suggestion = "驳回" if "驳回" in kinds else "修改" if "修改" in kinds else ""
        seen.append(c)


# ---------- 版本、打点、配对（设计文档 4.1–4.3） ----------
def _norm_text(s: str) -> str:
    return re.sub(r"[\s，。,.;；:：“”\"'（）()、]", "", s or "")


def fingerprint(r: Requirement) -> str:
    """原文 + 打点的指纹；参数值不在内（只改参数值不算需求修改）。"""
    raw = _norm_text(r.quote) + "|" + json.dumps(sorted(r.points.items()), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def detect_version(text: str) -> str:
    m = RE_DOC_VERSION.search(text[:6000])
    return m.group(1) if m else ""


def title_hash(doc: PrdDoc) -> str:
    """去掉版本号的标题哈希（prd_id 末段）。"""
    title = re.sub(r"(?i)[\s_\-·（(]*(?:PRD)?[\s_\-]*[Vv]?\d+(?:\.\d+){1,3}[)）]?", "", doc.title or doc.name)
    title = re.sub(r"[\s（）()·_\-]|PRD|产品需求文档|（示例）", "", title, flags=re.I) or doc.name
    return hashlib.sha1(title.encode()).hexdigest()[:4].upper()


def prd_identity(doc: PrdDoc, code: str) -> str:
    """同一 PRD 跨版本稳定的编号：模块缩写 + 去掉版本号的标题。"""
    return f"PRD-{code}-{title_hash(doc)}"


# ---------- 块级增量 ----------
def snapshot(blocks: list[Block]) -> list[dict[str, str]]:
    return [{"k": b.key, "h": b.digest, "s": b.section} for b in blocks]


def map_blocks(reqs: list[Requirement], blocks: list[Block]) -> None:
    """给需求点记出处块：原文片段出现在哪些块里；找不到时取同章节最像的块，再不行取同章节全部块。
    出处为空的需求点下次一律重读（宁可多读，不漏改）。"""
    body = [b for b in blocks if b.kind != "heading"]
    for r in reqs:
        q = _norm_text(r.quote)
        hits = [b for b in body if q and q[:24] in _norm_text(b.text)]
        if not hits:
            same = [b for b in body if b.section == r.section or _in_section(b.section, r.section)]
            scored = sorted(((difflib.SequenceMatcher(None, q, _norm_text(b.text)).ratio(), i) for i, b in enumerate(same)),
                            reverse=True)
            hits = [same[scored[0][1]]] if scored and scored[0][0] >= 0.4 else same
        r.blocks = list(dict.fromkeys(b.key for b in hits))


@dataclass
class Incremental:
    base_id: str  # 基准版本 <prd_id>@<version>
    text: str = ""  # 送去抽取的原文（只含变了的章节）
    dirty: list[str] = field(default_factory=list)  # 要重读的章节
    in_scope: list[Requirement] = field(default_factory=list)  # 参与配对的基准需求点
    clean: list[Requirement] = field(default_factory=list)  # 出处没变、直接沿用的基准需求点
    changed: int = 0  # 变了的块数（新增 + 修改 + 删除）
    keys: set[str] = field(default_factory=set)  # 重读的正文块


def plan_incremental(doc: PrdDoc, section: str, base: PrdVersion) -> Incremental | None:
    """和基准版本的块快照比，算出要重读的章节。基准没有快照时返回 None（走全量）。"""
    if not base.blocks:
        return None
    new = [b for b in doc.blocks if not section or _in_section(b.section, section)]
    old = {e["k"]: e["h"] for e in base.blocks if not section or _in_section(e["s"], section)}
    changed = [b for b in new if old.get(b.key) != b.digest]
    removed = set(old) - {b.key for b in new}
    bad = {b.key for b in changed} | removed
    in_sec = [r for r in base.requirements if not section or _in_section(r.section, section)]
    dirty_reqs = [r for r in in_sec if not r.blocks or set(r.blocks) & bad]
    # 重读范围：变了的块所在章节（只取本章节的块，不含子章节——否则挂在一级标题下的版本号一改就整篇重读）；
    # 变了的需求点按出处块所在章节；没记出处的需求点按它的章节整棵子树
    sec_of = {b.key: b.section for b in doc.blocks}
    old_sec = {e["k"]: e["s"] for e in base.blocks}
    exact = {b.section for b in changed}
    tree: set[str] = set()
    for r in dirty_reqs:
        if r.blocks:
            exact |= {sec_of.get(k, old_sec.get(k, r.section)) for k in r.blocks}
        else:
            tree.add(r.section)
    secs = sorted(exact | tree)
    reread = [b for b in new if b.section in exact or any(_in_section(b.section, d) for d in tree)
              or (b.kind == "heading" and any(_in_section(d, b.section) for d in secs))]  # 上级标题给模型上下文
    keys = {b.key for b in reread}
    dirty_ids = {r.req_id for r in dirty_reqs}
    body_keys = {b.key for b in reread if b.kind != "heading"}
    in_scope = [r for r in in_sec if r.req_id in dirty_ids or set(r.blocks) & body_keys]
    clean = []
    for r in base.requirements:
        if r in in_scope:
            continue
        c = r.model_copy(update={"change": "未改"})
        c.section = next((sec_of[k] for k in r.blocks if k in sec_of), r.section)  # 章节号可能因插入章节而变
        clean.append(c)
    return Incremental(base_id=base.baseline_id, text=render(reread)[:MAX_DOC_CHARS] if secs else "", dirty=secs,
                       in_scope=in_scope, clean=clean, changed=len(changed) + len(removed), keys=body_keys)


def _points(raw: dict) -> tuple[dict[str, str], str]:
    got = {}
    for k, v in (raw.get("models") or {}).items() if isinstance(raw.get("models"), dict) else []:
        code = vehicles.normalize(str(k))
        if code:
            got[code] = "—" if str(v).strip() in ("—", "-", "×", "x", "X", "否", "不支持") else "●"
    src = {"表格": "原文", "正文": "推断"}.get(str(raw.get("model_source", "")), "默认")
    if not got:
        return {c: "●" for c in default_models()}, "默认"
    return {c: got.get(c, "●") for c in default_models()}, (src if src != "默认" else "推断")


def _params(raw: list, reqs: list[Requirement]) -> list[ModelParam]:
    out = []
    by_sec = {r.section: r.req_id for r in reqs}
    for p in raw or []:
        name = re.sub(r"[^a-z0-9_]", "", str(p.get("name", "")).lower())
        if not name or not re.match(r"^[a-z_]", name):
            continue
        vals = {vehicles.normalize(str(k)): str(v) for k, v in (p.get("values") or {}).items()
                if vehicles.normalize(str(k))}
        out.append(ModelParam(name=name, desc=str(p.get("desc", ""))[:40], default=str(p.get("default", "")),
                              values=vals, req_ids=[by_sec[x.lstrip("§")] for x in p.get("sections") or []
                                                    if x.lstrip("§") in by_sec]))
    return out


def _similar(r: Requirement, o: Requirement) -> float:
    """配对规则：原文相似 ≥ 0.8，或同章节且相似 ≥ 0.5；不满足返回 0。"""
    ratio = difflib.SequenceMatcher(None, _norm_text(r.quote), _norm_text(o.quote)).ratio()
    return ratio if ratio >= 0.8 or (r.section and r.section == o.section and ratio >= 0.5) else 0.0


def pair_requirements(new: list[Requirement], old: list[Requirement],
                      others: list[Requirement] | tuple = ()) -> list[Requirement]:
    """新版本需求点与上一版本配对：同章节且原文相似 ≥ 0.5，或原文相似 ≥ 0.8。返回上一版本中被删除的需求点。
    就地设置 new 的 req_id（配对成功沿用旧编号）、fingerprint、change。
    others：不参与配对、但编号不能撞的需求点（按章节导入时其它章节的）。"""
    free = list(old)
    for r in new:
        r.fingerprint = fingerprint(r)
        best, score = None, 0.0
        for o in free:
            ratio = _similar(r, o)
            if ratio and ratio > score:
                best, score = o, ratio
        if best:
            free.remove(best)
            r.req_id = best.req_id
            r.change = "未改" if r.fingerprint == best.fingerprint else "修改"
        else:
            r.req_id, r.change = "", "新增"
    nums = [int(m.group(1)) for x in [*old, *others] if (m := re.match(r"REQ-(\d+)$", x.req_id))]
    n = max(nums, default=0)
    for r in new:
        if not r.req_id:
            n += 1
            r.req_id = f"REQ-{n:02d}"
    for o in free:
        o.change = "删除"
    return free


# ---------- 主流程 ----------
def _norm_case(raw: dict, reqs: dict[str, Requirement], models: list[str] | None = None) -> TestCase | None:
    rid = str(raw.get("req_id", "")).upper()
    if rid not in reqs:
        return None
    ch = [x for x in (raw.get("channel") or []) if x in CHANNELS]
    steps = raw.get("steps") or []
    if isinstance(steps, str):
        steps = [s for s in re.split(r"[；;\n]|\d+[.、]\s*", steps) if s.strip()]
    c = TestCase(case_id="", title=str(raw.get("title", ""))[:40] or reqs[rid].summary, req_id=rid,
                 prd_section=reqs[rid].section, precondition=str(raw.get("precondition", "")).strip(),
                 steps=[str(s).strip() for s in steps if str(s).strip()], expected=str(raw.get("expected", "")).strip(),
                 type=raw.get("type") if raw.get("type") in ("正向", "边界", "异常") else "正向",
                 priority=raw.get("priority") if raw.get("priority") in ("P0", "P1", "P2") else "P1",
                 channel=ch, utterance=str(raw.get("utterance") or "").strip().strip("“”\""),
                 expect_screen=str(raw.get("expect_screen") or "").strip(),
                 expect_tts=str(raw.get("expect_tts") or "").strip(),
                 expect_intent=str(raw.get("expect_intent") or "").strip(), page=str(raw.get("page") or "").strip(),
                 models=list(models) if models is not None else reqs[rid].models)
    if c.is_voice:
        c.latency_max_ms = int(raw.get("latency_max_ms") or LATENCY_DEFAULT)
        c.voice_profile = VOICE_DEFAULT
        if not c.expected:
            c.expected = "；".join(x for x in (c.expect_screen, f"播报“{c.expect_tts}”" if c.expect_tts else "") if x)
    c.checks = [CheckItem(kind="PRD 未定义", msg=str(x)[:80], suggest="转需求方" if "未定义" in str(x) else "确认")
                for x in (raw.get("concerns") or []) if str(x).strip()][:2]
    return c


def reqs_msg(batch: CaseBatch, params: list | tuple = ()) -> dict:
    return {"type": "casegen.reqs", "batch_id": batch.batch_id, "requirements": [r.model_dump() for r in batch.requirements],
            "ambiguities": [a.model_dump() for a in batch.ambiguities], "module": batch.module, "models": batch.models,
            "prd_id": batch.prd_id, "version": batch.prd_version, "version_source": batch.version_source,
            "prev_version": batch.prev_version, "diff": batch.diff, "params": [p.model_dump() for p in params],
            "incremental": batch.params.get("incremental")}


async def generate(doc: PrdDoc, *, section: str = "", session: str = "", emit: Emit | None = None,
                   max_per_req: int = 5, version: str = "", created_by: str = "", version_override: str = "",
                   as_revision: bool = False, extracted: dict | None = None) -> CaseBatch:
    """version：测试在对话里给的版本号；PRD 里读得到版本号时以 PRD 为准。
    version_override：测试确认的新版本号，优先于 PRD 原文（PRD 没升版本号但内容变了）。
    as_revision：版本号与已发布的相同、内容有变时，作为该版本的修订（0.6+r2）。
    extracted：上一次的需求抽取结果（版本确认后续跑，不再调模型）。
    同版本已发布且没给以上决定时抛 VersionConflict；同版本有不同章节范围的批次没审完时抛 PrdBusy。"""
    lib = get_caselib()

    async def say(kind: str, data: dict) -> None:
        if emit:
            await emit(kind, data)

    text = doc.text(section)[:MAX_DOC_CHARS]
    # 0) 块级增量：调模型前能认出这份 PRD（来源已绑定 / 标题唯一）、基准版本有块快照时，只读变了的章节
    stable = doc.source.startswith(STABLE_SOURCES)
    pre_id = (lib.prd_for_source(doc.source) if stable else "") or lib.prd_by_title(title_hash(doc))
    scope_base = lib.latest_published(pre_id) if pre_id else None
    inc = plan_incremental(doc, section, scope_base) if scope_base else None
    pre_skill = skills.for_module(scope_base.module_code if scope_base else "")  # 抽取前只知道已认出的 PRD 的模块
    if inc:
        text = inc.text
    batch = CaseBatch(batch_id=lib.new_batch_id(), session=session, created_by=created_by, doc_name=doc.name, doc_source=doc.source,
                      doc_sha=doc.sha, content_sha=content_sha(doc, section), doc_revision=doc.revision, section=section,
                      params={"model": get_gateway().chat_model, "prompt_version": PROMPT_VERSION,
                              "max_per_req": max_per_req, "chars": len(text)})
    lib.save_batch(batch)
    await say("casegen.batch", {"batch": batch.model_dump()})
    t0 = time.time()

    # 1) 需求点 + 待确认项 + 打点 + 参数
    async def extract(scope: Incremental | None) -> dict:
        tag = scope.base_id if scope else ""
        if extracted is not None and extracted.get("_scope", "") == tag:
            return extracted
        if scope and not scope.dirty:  # 没有变了的块：不调模型
            return {"_scope": tag}
        body = scope.text if scope else doc.text(section)[:MAX_DOC_CHARS]
        part = f"（只含有改动的章节：{'、'.join('§' + d for d in scope.dirty)}）" if scope else ""
        got = await _llm_json(SYS_EXTRACT + skills.prompt_extra(pre_skill, "extract"),
                              f"《{doc.name}》{('§' + section) if section else ''}{part}\n\n{body}", "casegen.extract", session)
        got["_scope"] = tag
        return got

    def parse(ex: dict) -> list[Requirement]:
        out = []
        for r in ex.get("requirements") or []:
            pts, src = _points(r)
            out.append(Requirement(req_id="", section=str(r.get("section", "")).lstrip("§"),
                                   quote=str(r.get("quote", ""))[:200], summary=str(r.get("summary", ""))[:30],
                                   points=pts, point_source=src))
        map_blocks(out, [b for b in doc.blocks if not section or _in_section(b.section, section)])
        batch.ambiguities = [Ambiguity(id=f"Q-{i:02d}", section=str(a.get("section", "")).lstrip("§"),
                                       quote=str(a.get("quote", ""))[:200], question=str(a.get("question", ""))[:120])
                             for i, a in enumerate(ex.get("ambiguities") or [], 1)]
        return out

    ex = await extract(inc)
    reqs_new = parse(ex)
    if ex.get("module") or not scope_base:
        batch.module = str(ex.get("module") or doc.title)[:40]
        code = module_code(batch.module, str(ex.get("module_code", "")))
    else:  # 增量且模型没给模块：沿用基准版本的
        batch.module, code = scope_base.module, scope_base.module_code or module_code(scope_base.module)

    # 版本号：测试确认的新版本号 > PRD 原文 > 测试在对话里给的 / 预填（后两者标记“测试预填”，转产品部补正）
    found = detect_version(doc.text()) or str(ex.get("version") or "").strip().lstrip("Vv")
    if version_override:
        ver, ver_src = version_override.strip().lstrip("Vv"), "测试预填"
    elif found:
        ver, ver_src = found, "原文"
    else:
        ver, ver_src = (version.strip().lstrip("Vv") or f"未标注-{time.strftime('%Y%m%d')}"), "测试预填"
    # 身份：已绑定的来源沿用绑定的 prd_id
    prd_id = (lib.prd_for_source(doc.source) if stable else "") or prd_identity(doc, code)

    def stop(exc: Exception) -> Exception:
        batch.status, batch.params["error"] = "失败", str(exc)
        lib.save_batch(batch)
        return exc

    # 已发布的版本不可覆盖：内容变了要么升版本号，要么作为修订（以该版本最新修订为基准）
    prev: PrdVersion | None = None
    cur = lib.get_prd_version(prd_id, ver)
    if cur and cur.status == "已发布":
        if not as_revision:
            raise stop(VersionConflict(prd_id, ver, doc.title or doc.name, ex))
        prev = lib.latest_published(prd_id, family=ver)
        ver = lib.next_revision(prd_id, ver)
        cur = lib.get_prd_version(prd_id, ver)
    # 同版本还没审完的批次：同章节范围的被取代，不同章节范围的先审完
    if cur and cur.status != "已发布":
        old_b = lib.get_batch(cur.batch_id)
        if old_b and old_b.status in ("生成中", "待审核"):
            if old_b.section != section:
                where = f"§{old_b.section}" if old_b.section else "全文"
                raise stop(PrdBusy(f"{prd_id}@{ver} 还有批次 {old_b.batch_id}（{where}）没审完，请先审完再导入"))
            lib.supersede(old_b)
            batch.params["superseded"] = old_b.batch_id
    if prev is None:
        prev = lib.latest_published(prd_id, exclude=ver)
    if stable:
        lib.bind_source(doc.source, prd_id)
    if inc and (not prev or prev.baseline_id != inc.base_id):  # 实际基准和预判的不同：增量作废，全量重读
        inc, text = None, doc.text(section)[:MAX_DOC_CHARS]
        ex = await extract(None)
        reqs_new = parse(ex)
    if inc:
        batch.params["incremental"] = {"base": inc.base_id, "dirty": inc.dirty, "changed_blocks": inc.changed,
                                       "carried": len(inc.clean)}

    # 配对：增量时只和重读范围里的需求点配对；按章节导入时只和同章节的配对；其余原样继承
    base_reqs = prev.requirements if prev else []
    if inc:
        in_scope, out_scope = inc.in_scope, inc.clean
        # 新抽到的需求点出处必须在这次重读的块里；出处在范围外的（模型借上级标题等上下文编出来的）丢掉，
        # 原文和沿用的基本相同、又配不上重读范围的也丢掉
        def same(r: Requirement, o: Requirement) -> bool:
            return difflib.SequenceMatcher(None, _norm_text(r.quote), _norm_text(o.quote)).ratio() >= 0.95

        n0 = len(reqs_new)
        reqs_new = [r for r in reqs_new if set(r.blocks) & inc.keys and (any(_similar(r, o) for o in in_scope)
                                                                          or not any(same(r, o) for o in out_scope))]
        batch.params["incremental"]["dropped"] = n0 - len(reqs_new)
    else:
        in_scope = [r for r in base_reqs if not section or _in_section(r.section, section)]
        out_scope = [r.model_copy(update={"change": "未改"}) for r in base_reqs if r not in in_scope]
    deleted = pair_requirements(reqs_new, in_scope, out_scope)
    order = {r.req_id: i for i, r in enumerate(reqs_new)}
    reqs_new += out_scope
    batch.requirements = reqs_new
    params = _params(ex.get("params") or [], reqs_new)
    if inc and prev:  # 增量：没重读到的车型参数沿用基准版本的
        kept = {r.req_id for r in out_scope}
        names = {p.name for p in params}
        params += [p for p in prev.params if p.name not in names and (not p.req_ids or set(p.req_ids) <= kept)]
    # 块快照：本次读到的范围用新文档，按章节导入时范围外沿用基准的
    snap = snapshot([b for b in doc.blocks if not section or _in_section(b.section, section)])
    if section and prev:
        snap += [e for e in prev.blocks if not _in_section(e["s"], section)]
    batch.prd_id, batch.prd_version, batch.version_source = prd_id, ver, ver_src
    batch.prev_version = prev.version if prev else ""
    batch.diff = {k: sum(1 for r in reqs_new if r.change == k) for k in ("新增", "修改", "未改")}
    batch.diff["删除"] = len(deleted)
    batch.models = sorted({m for r in reqs_new for m in r.models}, key=default_models().index) or default_models()
    pv = PrdVersion(prd_id=prd_id, name=doc.title or doc.name, version=ver, version_source=ver_src,
                    prev_version=batch.prev_version, module=batch.module, module_code=code, doc_source=doc.source,
                    doc_sha=doc.sha, doc_revision=doc.revision, batch_id=batch.batch_id, requirements=reqs_new,
                    params=params, deleted=deleted, blocks=snap, status="待审核")
    lib.save_prd_version(pv)
    batch.params["extract_s"], batch.params["chars"] = round(time.time() - t0, 1), len(text)
    lib.save_batch(batch)
    await say("casegen.reqs", {k: v for k, v in reqs_msg(batch, params).items() if k != "type"})
    if not batch.requirements:
        batch.status = "失败"
        lib.save_batch(batch)
        return batch

    # 上一基线里的用例：未改的继承，修改的沿用编号出新修订，删除的退出
    old_by_req: dict[str, list[TestCase]] = {}
    if prev:
        for c in lib.baseline_cases(prev.baseline_id):
            old_by_req.setdefault(c.req_id, []).append(c)
    reqs = {r.req_id: r for r in batch.requirements}
    for r in batch.requirements:
        if r.change == "未改":
            for c in old_by_req.get(r.req_id, []):
                batch.inherited[c.case_id] = c.case_rev
    batch.retired = [c.case_id for r in deleted for c in old_by_req.get(r.req_id, [])]
    todo = [r for r in batch.requirements if r.change in ("新增", "修改")]

    # 2) 生成用例
    # 每组只带这几个需求点的出处章节（不再每组带整份 PRD）；哪组先完成就先自检、先推给页面
    plist = "\n".join(f"- {p.name}（{p.desc}）：默认 {p.default}" +
                      "".join(f"，{vehicles.name(k)} {v}" for k, v in p.values.items()) for p in params)
    groups = [todo[i:i + REQ_PER_CALL] for i in range(0, len(todo), REQ_PER_CALL)]
    scope_blocks = [b for b in doc.blocks if not section or _in_section(b.section, section)]
    sem = asyncio.Semaphore(GEN_CONCURRENCY)
    timings: list[dict] = []
    skill = skills.for_module(code)  # 模块的用例编写规范包（领域规则、示例、额外自检）
    sys_gen = SYS_GENERATE.replace("{max_per_req}", str(max_per_req)) + skills.prompt_extra(skill, "write")
    batch.params["skill"] = skill.meta() if skill else None
    batch.params["extract_skill"] = pre_skill.meta() if pre_skill else None

    def context(g: list[Requirement]) -> str:
        """出处块所在的章节（同章节的上下文一起给）+ 上级标题；没记出处的需求点取它所在章节。"""
        keys = {k for r in g for k in r.blocks}
        secs = {b.section for b in scope_blocks if b.key in keys} | {r.section for r in g if not r.blocks}
        picked = [b for b in scope_blocks if b.section in secs or any(_in_section(b.section, x) for x in secs if not keys)
                  or (b.kind == "heading" and any(_in_section(x, b.section) for x in secs))]
        body = render(picked).strip()
        return (body or text)[:CTX_CHARS]

    async def gen_group(g: list[Requirement]) -> tuple[list[Requirement], list[dict]]:
        listing = "\n".join(f"{r.req_id}（§{r.section}）{r.summary}：{r.quote}" for r in g)
        ctx = context(g)
        prompt = (f"模块：{batch.module}\n需求点：\n{listing}\n\n" + (f"车型参数：\n{plist}\n\n" if plist else "") +
                  f"PRD 相关原文：\n{ctx}\n\n只为上面列出的需求点写用例。")
        async with sem:
            t = time.time()
            res = await _llm_json(sys_gen, prompt, "casegen.generate", session, max_tokens=16000)
        timings.append({"reqs": [r.req_id for r in g], "chars": len(prompt), "s": round(time.time() - t, 1)})
        return g, res.get("cases") or []

    t1 = time.time()
    errors: list[str] = []
    per_req: dict[str, int] = {}
    cases: list[TestCase] = []
    taken = set(get_cases()._local)  # 本地用例缓存里的编号也不能撞
    todo_ids = {r.req_id for r in todo}
    reuse = {rid: list(old_by_req.get(rid, [])) for rid in todo_ids if reqs[rid].change == "修改"}
    reused_ids = {o.case_id for olds in reuse.values() for o in olds}
    lib_cases = [c for c in lib.library() if c.case_id not in reused_ids]  # 自检比对的已入库用例
    pnames = {p.name for p in params}

    async def accept(raw_cases: list[dict]) -> None:
        """一组的结果：规整、编号、自检（与已入库和本批已收下的用例比重复）、保存、推给页面。"""
        got: list[TestCase] = []
        raw_cases.sort(key=lambda x: order.get(str(x.get("req_id", "")).upper(), 999))
        for raw in raw_cases:
            c = _norm_case(raw, reqs)
            if not c or c.req_id not in todo_ids or per_req.get(c.req_id, 0) >= max_per_req:
                continue
            per_req[c.req_id] = per_req.get(c.req_id, 0) + 1
            c.module, c.batch_id, c.prd_id = batch.module, batch.batch_id, prd_id
            old = reuse.get(c.req_id) or []
            if old:  # 修改：沿用旧编号，修订号 +1（只数已入基线的修订，被作废批次的修订号复用）
                o = old.pop(0)
                latest = max((x.case_rev for x in lib.revisions(o.case_id) if x.baselines), default=o.case_rev)
                c.case_id, c.case_rev, c.model_overrides = o.case_id, latest + 1, dict(o.model_overrides)
            else:
                c.case_id = lib.next_id(code, taken)
            got.append(c)
        self_check(got, lib_cases + cases, pnames, skill)
        for c in got:
            lib.save_case(c)
            cases.append(c)
            await say("casegen.case", {"batch_id": batch.batch_id, "case": c.model_dump()})
        batch.case_ids = [c.case_id for c in cases]
        lib.save_batch(batch)  # 页面刷新或断线重连时能恢复已生成的部分

    for fut in asyncio.as_completed([gen_group(g) for g in groups]):
        try:
            g, raw = await fut
        except Exception as e:  # 一组失败不影响其它组
            errors.append(f"{type(e).__name__}: {e}")
            continue
        await accept(raw)
    batch.retired += [o.case_id for left in reuse.values() for o in left]
    batch.params["generate_s"] = round(time.time() - t1, 1)
    batch.params["generate_groups"] = timings
    if errors:
        batch.params["errors"] = errors

    # 3) 写入批次（待审核）；没有新增 / 修改时直接发布基线
    batch.case_ids = [c.case_id for c in cases]
    if cases:
        batch.status = "待审核"
    elif batch.inherited:
        batch.status = "已审核"
    else:
        batch.status = "失败"
    lib.save_batch(batch)
    if batch.status == "已审核":
        lib.publish_baseline(batch)
    await say("casegen.done", {"batch": batch.model_dump(), "coverage": coverage(batch, cases)})
    return batch


async def draft_cases(reqs: list[Requirement], module: str, skill: "skills.Skill | None", *, max_per_req: int = 3,
                      session: str = "skills.trial") -> list[TestCase]:
    """只生成、不入库：给规范包试生成对比用。用同样的提示词（含规范包）生成并自检，不编号、不保存、不进审核。"""
    listing = "\n".join(f"{r.req_id}（§{r.section}）{r.summary}：{r.quote}" for r in reqs)
    prompt = (f"模块：{module}\n需求点：\n{listing}\n\nPRD 相关原文：\n" + "\n".join(r.quote for r in reqs) +
              "\n\n只为上面列出的需求点写用例。")
    sys_gen = SYS_GENERATE.replace("{max_per_req}", str(max_per_req)) + skills.prompt_extra(skill, "write")
    res = await _llm_json(sys_gen, prompt, "skills.trial", session, max_tokens=8000)
    by_id = {r.req_id: r for r in reqs}
    out: list[TestCase] = []
    for i, raw in enumerate(res.get("cases") or [], 1):
        c = _norm_case(raw, by_id)
        if c:
            c.case_id = f"试-{i:02d}"
            out.append(c)
    self_check(out, get_caselib().library(), None, skill)
    return out


def coverage(batch: CaseBatch, cases: list[TestCase]) -> dict[str, Any]:
    by_req = {r.req_id: 0 for r in batch.requirements}
    by_type = {"正向": 0, "边界": 0, "异常": 0}
    for c in cases:
        by_req[c.req_id] = by_req.get(c.req_id, 0) + 1
        by_type[c.type] += 1
    flagged = [c.case_id for c in cases if c.suggestion]
    # 继承的用例也算覆盖
    inherited_by_req: dict[str, int] = {}
    lib = get_caselib()
    for cid, rev in batch.inherited.items():
        c = lib.get_case(cid, rev)
        if c:
            inherited_by_req[c.req_id] = inherited_by_req.get(c.req_id, 0) + 1
    for rid, n in inherited_by_req.items():
        by_req[rid] = by_req.get(rid, 0) + n
    return {"by_req": by_req, "by_type": by_type, "covered": sum(1 for v in by_req.values() if v),
            "total_reqs": len(by_req), "n_cases": len(cases), "flagged": flagged,
            "n_voice": sum(1 for c in cases if c.is_voice), "n_inherited": len(batch.inherited),
            "diff": batch.diff, "version": batch.prd_version, "prev_version": batch.prev_version}
