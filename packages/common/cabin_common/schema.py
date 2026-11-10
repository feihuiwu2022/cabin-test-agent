"""cabin-schema：跨模块数据契约。"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_ts() -> float:
    return time.time()


def short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


# ---------- 台架档案 ----------
class Rig(BaseModel):
    id: str
    model: str = "A01-CN"  # 车型代码（cabin/vehicles.py）
    domain: Literal["android", "qnx", "linux"] = "android"
    connection: str = "simcar"  # simcar | adb-usb | adb-tcp | ssh
    serial: str = ""
    packages: list[str] = Field(default_factory=list)  # 被测包白名单
    capabilities: list[str] = Field(default_factory=lambda: ["android_adb"])
    owner: str = ""


# ---------- 用例 ----------
class Case(BaseModel):
    id: str
    title: str
    module: str = ""
    package: str = ""
    page: str = ""
    precondition: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: str
    priority: str = "P1"
    type: str = "功能"
    source: str = "local-cache"  # feishu:<doc> | local-cache
    keywords: list[str] = Field(default_factory=list)


# ---------- ExploreTask（cabin/exploretask/v1，AI 探索任务；测试代码 cabin/taskplan/v1 见 cabin/runner） ----------
class Origin(BaseModel):
    utterance: str
    session: str
    user: str = "engineer"
    model: str = ""


class CaseRef(BaseModel):
    source: str
    case_id: str
    expected: str


class Target(BaseModel):
    rig: str
    domain: str = "android"
    package: str


class PlanStep(BaseModel):
    type: Literal["ai_explore", "replay", "monkey", "taskplan"]  # taskplan：回放用例库里的测试代码
    goal: str = ""
    case_id: str = ""  # taskplan 用
    kind: Literal["replay", "trial", "retest"] = "replay"  # trial：首次试跑，失败不进缺陷审核；retest：回归失败后的复测
    regression_id: str = ""  # 所属回归批次，现场下发的任务没有
    retest_of: str = ""  # 复测的是哪个任务
    max_steps: int = 12
    collect: list[str] = Field(default_factory=lambda: ["screenshot_each_step", "logcat", "dumpsys"])
    script: list[dict[str, Any]] | None = None  # replay 用


class JudgeCfg(BaseModel):
    mode: str = "expect_vs_actual"
    vlm: bool = True
    rule: str = "android-crash"


class ReportCfg(BaseModel):
    feishu: bool = True
    jira: str = "sandbox_tiered"


class Limits(BaseModel):
    max_duration_s: int = 300
    stop_on: list[str] = Field(default_factory=lambda: ["device_offline"])


class ExploreTask(BaseModel):
    schema_: str = Field("cabin/exploretask/v1", alias="schema")
    id: str
    origin: Origin
    case_ref: CaseRef | None = None
    target: Target
    steps: list[PlanStep]
    judge: JudgeCfg = Field(default_factory=JudgeCfg)
    report: ReportCfg = Field(default_factory=ReportCfg)
    limits: Limits = Field(default_factory=Limits)
    summary: list[str] = Field(default_factory=list)  # 复述给人看的步骤要点

    model_config = {"populate_by_name": True}


class TaskStatus(str, Enum):
    draft = "草稿"
    pending_confirm = "待确认"
    queued = "排队"
    preparing = "准备"
    running = "执行中"
    judging = "判定中"
    reviewing = "候选待审核"
    done = "已完成"
    cancelled = "已取消"
    env_error = "环境异常"
    interrupted = "执行中断"


# ---------- 执行记录 ----------
class StepRecord(BaseModel):
    idx: int
    action: str  # tap / swipe / back / home / done / look
    label: str
    thought: str = ""
    element_id: int | None = None
    x: int | None = None
    y: int | None = None
    bbox: list[int] | None = None
    screen: str = ""  # 页面名
    shot: str = ""  # 证据截图 URL
    llm_s: float = 0
    exe_s: float = 0
    wait_s: float = 0


class RunResult(BaseModel):
    task_id: str
    ota: str = ""
    package_version: str = ""
    steps: list[StepRecord] = Field(default_factory=list)
    final_shot: str = ""
    final_shot_path: str = ""
    final_elements: list[dict[str, Any]] = Field(default_factory=list)
    final_screen: str = ""
    logcat: str = ""
    replay_script: list[dict[str, Any]] = Field(default_factory=list)
    finished: bool = False
    error: str = ""


# ---------- 缺陷候选 ----------
class DupResult(BaseModel):
    category: Literal["duplicate", "continuation", "regression", "new"] = "new"
    label: str = "未命中，新缺陷"
    issue_key: str = ""
    issue_summary: str = ""
    issue_version: str = ""
    suggestion: Literal["link", "create", "create_regression"] = "create"


class Review(BaseModel):
    action: Literal["confirm", "reject", "link"]
    by: str = "测试工程师"
    reason: str = ""
    severity: str = ""
    summary: str = ""
    at: float = Field(default_factory=now_ts)


class JiraResult(BaseModel):
    tier: Literal["rest", "sandbox-local", "prefill-link", "draft"]
    key: str = ""
    url: str = ""
    linked_to: str = ""
    draft: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    attached: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # 超过附件上限没传的文件


class DefectCandidate(BaseModel):
    id: str
    task_id: str
    rule: str  # J-01..J-06
    kind: str  # 显示异常 / Java 崩溃 / ANR …
    title: str
    summary: str
    reason: str = ""
    confidence: float = 0.0
    level: Literal["高", "中", "低"] = "中"
    judged_by: Literal["规则判定", "AI 判定", "低置信"] = "AI 判定"
    package: str = ""
    page: str = ""
    case_id: str = ""
    expected: str = ""
    actual_shot: str = ""
    expected_shot: str = ""
    diff_bbox: list[int] | None = None
    log_excerpt: str = ""
    ota: str = ""
    rig: str = ""
    fingerprint: str = ""  # 车型 + 用例 + 断言 + 失败类型；OTA 版本在查重时比较
    model: str = ""
    module: str = ""
    session: str = ""
    run_id: str = ""  # 证据在云端的目录
    steps: list[str] = Field(default_factory=list)  # 复现步骤
    finder: str = ""  # 发现人，KPI 算在这里
    assignees: list[str] = Field(default_factory=list)  # 待审核归谁
    rigs: list[str] = Field(default_factory=list)  # 还有哪些台架复现了同一问题
    jira_status: str = ""
    dup: DupResult = Field(default_factory=DupResult)
    severity: str = "B"
    status: Literal["retest", "pending", "confirmed", "rejected", "linked", "suppressed", "expired", "flaky"] = "pending"
    review: Review | None = None
    jira: JiraResult | None = None
    created_at: float = Field(default_factory=now_ts)


REJECT_REASONS = ["判定误报", "用例预期有误", "环境问题", "已知问题"]


# ---------- 用例生成 / 用例库 ----------
CaseType = Literal["正向", "边界", "异常"]
ReviewStatus = Literal["待审核", "通过", "修改后通过", "驳回", "转需求方"]
CASE_REJECT_REASONS = ["与已有用例重复", "预期不可判定", "PRD 理解有误", "不属于本模块", "不适合自动化"]


ChangeType = Literal["新增", "修改", "未改", "删除"]
PointSource = Literal["原文", "推断", "默认"]


class Requirement(BaseModel):
    req_id: str  # REQ-nn；升版本时配对成功的沿用上一版本编号
    section: str = ""  # PRD 章节，如 4.1.2
    quote: str = ""  # 原文片段
    summary: str = ""
    points: dict[str, str] = Field(default_factory=dict)  # 车型打点：{车型代码: "●" | "—"}
    point_source: PointSource = "默认"
    fingerprint: str = ""  # 原文 + 打点的指纹，跨版本比对用
    blocks: list[str] = Field(default_factory=list)  # 出处：PRD 块的身份（飞书块 ID / 内容哈希），块级增量用
    change: ChangeType = "新增"  # 相对上一 PRD 版本

    @property
    def models(self) -> list[str]:
        return [m for m, v in self.points.items() if v == "●"]


class ModelParam(BaseModel):
    """车型参数：用例与测试代码里以 ${name} 引用。"""
    name: str
    desc: str = ""
    default: str = ""
    values: dict[str, str] = Field(default_factory=dict)  # {车型代码: 值}，缺省用 default
    req_ids: list[str] = Field(default_factory=list)

    def value_for(self, model: str) -> str:
        return self.values.get(model) or self.default


class PrdVersion(BaseModel):
    """PRD 的一个版本：需求点、打点、参数的快照；批次审完后发布为基线 <prd_id>@<version>。"""
    prd_id: str
    name: str = ""
    version: str
    version_source: Literal["原文", "测试预填"] = "原文"
    prev_version: str = ""
    module: str = ""
    module_code: str = ""
    doc_source: str = ""
    doc_sha: str = ""
    doc_revision: str = ""  # 飞书文档 revision_id（导入时的快照）
    batch_id: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    params: list[ModelParam] = Field(default_factory=list)
    deleted: list[Requirement] = Field(default_factory=list)  # 相对上一版本删除的需求点
    blocks: list[dict[str, str]] = Field(default_factory=list)  # 块快照 [{k: 身份, h: 内容哈希, s: 章节}]，块级增量用
    archive_path: str = ""  # PRD 原文存档：<模块文件夹>/prd/<文件名>（测试代码仓库内）
    status: Literal["生成中", "待审核", "已发布"] = "生成中"
    created_at: float = Field(default_factory=now_ts)

    @property
    def baseline_id(self) -> str:
        return f"{self.prd_id}@{self.version}"


class Ambiguity(BaseModel):
    id: str  # Q-nn
    section: str = ""
    quote: str = ""
    question: str


class CheckItem(BaseModel):
    kind: str  # 重复 / 缺前置条件 / 预期不可判定 / PRD 未定义 / 通道不符 / 语音字段
    msg: str
    suggest: Literal["修改", "驳回", "确认", "转需求方"] = "修改"


class TestCase(BaseModel):
    __test__ = False  # 不是 pytest 测试类
    case_id: str
    case_rev: int = 1  # 用例修订：需求修改才 +1（设计文档 4.3）
    prd_id: str = ""
    baselines: list[str] = Field(default_factory=list)  # 所属基线 <prd_id>@<version>
    model_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)  # 车型覆盖：{车型: {skip: True} | {字段: 值}}
    title: str
    module: str = ""  # 模块路径，如 语音助手/空调
    req_id: str = ""
    prd_section: str = ""
    precondition: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: str = ""
    type: CaseType = "正向"
    priority: Literal["P0", "P1", "P2"] = "P1"
    channel: list[str] = Field(default_factory=list)  # audio / adb / ssh_qnx / arm / can
    utterance: str = ""  # 语音用例：测试语句
    expect_intent: str = ""
    expect_screen: str = ""
    expect_tts: str = ""
    latency_max_ms: int | None = None
    voice_profile: str = ""
    page: str = ""
    package: str = ""
    models: list[str] = Field(default_factory=list)
    source: str = "AI生成"
    batch_id: str = ""
    checks: list[CheckItem] = Field(default_factory=list)
    suggestion: str = ""  # 自检汇总建议：修改 / 驳回 / ""
    review_status: ReviewStatus = "待审核"
    reviewer: str = ""
    review_note: str = ""
    auto_status: Literal["未生成", "已生成", "试跑中", "试跑未通过", "已验证", "不适合自动化", "需重新生成"] = "未生成"
    trial: dict[str, Any] = Field(default_factory=dict)  # 最近一次首次试跑：结论、原因、台架、测试代码版本
    plan_status: Literal["", "generating", "ready", "invalid", "unsuitable"] = ""
    plan_path: str = ""
    plan_version: str = ""
    bitable_record_id: str = ""  # 飞书多维表格记录 ID
    bitable_url: str = ""  # 记录所在的表（模块改表后据此重新建行）
    synced_at: float = 0  # 最近一次与多维表格同步的时间
    updated_at: float = Field(default_factory=now_ts)

    @property
    def is_voice(self) -> bool:
        return "audio" in self.channel


class CaseBatch(BaseModel):
    batch_id: str  # B-yyyymmdd-nn
    prd_id: str = ""
    prd_version: str = ""
    version_source: str = ""
    prev_version: str = ""
    diff: dict[str, int] = Field(default_factory=dict)  # 需求点四类计数
    inherited: dict[str, int] = Field(default_factory=dict)  # 未改需求继承的用例 {case_id: rev}，不送审
    retired: list[str] = Field(default_factory=list)  # 不进新基线的用例（需求删除或修改后不再需要）
    session: str = ""
    created_by: str = ""  # 发起生成的登录用户，重新打开页面时据此恢复进度
    doc_name: str = ""
    doc_source: str = ""
    doc_sha: str = ""
    content_sha: str = ""  # 本批所用 PRD 正文（按章节取）的哈希：同内容再导入时据此去重
    doc_revision: str = ""  # 飞书文档 revision_id
    section: str = ""
    models: list[str] = Field(default_factory=list)
    module: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    status: Literal["生成中", "待审核", "已审核", "失败", "已作废"] = "生成中"  # 已作废：被同版本的新导入取代
    params: dict[str, Any] = Field(default_factory=dict)  # 生成参数、模型、提示词版本
    created_at: float = Field(default_factory=now_ts)
