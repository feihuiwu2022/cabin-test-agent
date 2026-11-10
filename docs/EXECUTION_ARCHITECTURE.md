# CabinPilot 用例执行架构：TaskPlan Runner、动作通道、证据与评估器

> 状态：**R0、R1 已实现**（2026-11-04，代码在 `apps/agent/cabin_agent/runner/`，验证记录见第 13 节）；R2 及之后仍是设计。
> 接续：[ARCHITECTURE.md](ARCHITECTURE.md) 的 P0 闭环。

## 1. 结论

| 问题 | 决定 | 理由 |
|---|---|---|
| TaskPlan 是什么 | 声明式测试代码（YAML），描述动作和断言，本身不运行 | 已由 `planner.py` 生成并通过静态校验 |
| 用什么执行 | 确定性的 Runner 解释执行，**不用 Skill 加载器** | 回归必须可复现；回放不调用大模型（R-1、PF-RUN-10） |
| 视觉和语音怎么分 | **只有一套 Runner**，在动作通道、证据、评估器三层按类型区分 | 新增通道或判定方式时不改 Runner |
| 截图还是 GUI Agent | 不是二选一：截图 / 控件树负责感知，GUI Agent 负责决策；**GUI Agent 只用于首次试跑和探索** | 回放不依赖模型，结果稳定、成本低 |

Skill 或 Agent 可以放在 Runner 外面，用于解析对话指令、分析失败原因、编排任务，但不进入执行主链路。

## 2. 现状与缺口

| 能力 | 现状 | 缺口 |
|---|---|---|
| TaskPlan YAML | `planner.py` 生成，存 `data/plans` | 没有执行器 |
| 探索执行 | `execsvc/runner.py` 的 `ai_explore` / `replay` | 只认 `schema.py` 的 Pydantic `TaskPlan`，读不了 YAML |
| 设备抽象 | `DeviceAdapter`：截图、控件树、点击、滑动、按键、日志 | 没有音频、相机、机械臂、CAN 通道 |
| 判定 | `judge.py`：崩溃 / ANR 规则 + VLM 比对 | 入参绑定旧 `TaskPlan`，没有断言注册表，没有 `timing` / `voice` |
| 测试音频 | 只有 `assets` 清单，`sha256: null` | 未合成，语音用例目前无法真正执行 |
| 前置条件 | `cmd_ref` 占位 | 车型命令库为空 |

**命名冲突（R0 已解决）：** 旧的探索任务和新的 YAML 曾都声明 `cabin/taskplan/v1`。接口约定定义的是 YAML，所以 YAML 保留该名称；旧模型改名 `ExploreTask`（`cabin/exploretask/v1`），旧执行器改名 `ExploreRunner`。

> 上表是 R0 之前的状态，保留作设计背景。

## 3. 总体分层

```text
          TaskPlan YAML（A-11 下载，校验版本与哈希）
                         │
┌────────────────────────▼───────────────────────────┐
│ Runner（通用）                                       │
│ 装载校验 · setup · 逐步执行 · 重试 · 超时 · teardown │
│ 事件上报 · 结论合成。不含任何视觉 / 语音判断逻辑      │
└───────┬───────────────────────────────┬────────────┘
        │ 动作名 → 处理函数              │ 断言 type → 评估器
┌───────▼──────────────┐        ┌───────▼──────────────┐
│ 动作层 ActionRegistry │        │ 评估层 EvaluatorRegistry│
│ Audio / Adb / Camera │ 写证据  │ rule / text / timing │
│ Arm / Can / Host     ├───────►│ vision / voice / can │
└───────┬──────────────┘ 读证据  └───────┬──────────────┘
        │                               │
   车机与外设                    本地规则 或 云端 A-10
                         │
       Evidence：截图、录音、时间点、日志（manifest.json）
                         │
       结果 → 缺陷候选 → 查重 → 人工审核 → 建单
```

三条依赖规则：

1. **动作不判断对错。** `listen_tts` 只记录录音和起止时间。例外：唤醒、无播报等检测结果作为事实写入证据，是否算失败由评估器决定。
2. **评估器不碰设备。** 只读证据，所以判定算法改了可以离线重判，不用重跑台架。
3. **Runner 不认识具体动作和断言类型。** 只通过两个注册表分发。

## 4. Runner

### 4.1 执行模式（对应 run.kind）

| kind | 含义 | 调模型 | 产物 |
|---|---|---|---|
| `dry` | 干跑：校验动作、参数、音频，不操作车机（PF-RUN-11） | 否 | 校验报告 |
| `trial` | 首次试跑：允许 GUI Agent 补全定位 | 是 | 定位、坐标、基线，经 A-12 固化成新版本 |
| `replay` | 严格按步骤回放 | 操作层否 | 结果 + 证据 |
| `retest` | 失败复测，行为同 replay | 操作层否 | 结果 + 证据 |

`replay` / `retest` 不注入模型客户端，由结构保证“不调用”，而不是靠约定。`vision`、`voice` 断言要判定时走评估层的 A-10，属于云端判定，不算 Runner 调模型。

### 4.2 流程

```text
load      校验 schema、requires ⊆ 台架能力、动作都已注册、音频存在且哈希一致
setup     执行前置条件；失败 → blocked
steps     逐步执行，到 after_step 时触发对应断言
assert    无 after_step 的断言（如 no_crash）在全部步骤后执行
teardown  无论成败都执行（语音用例必须恢复讲解）
finalize  合成结论，生成 manifest，上报
```

### 4.3 失败语义

| 情况 | 处理 | 结论影响 |
|---|---|---|
| 定位类动作失败 | 重试 1 次，间隔 1 秒（PF-RUN-04） | 仍失败则动作失败 |
| 动作失败 | 立即停止后续步骤，执行 teardown | blocked |
| 断言失败 | `on_fail: continue` 继续收集证据；`stop` 立即停止 | failed |
| 等待条件超时 | 按 TaskPlan 配置 | failed 或 blocked |
| 执行单元超时 | 停止（PF-RUN-07） | blocked |
| 能力不足 / 计划无效 | 不执行 | blocked（capability / plan_invalid） |
| 唤醒失败 | 不继续判温度（PF-AUD-04） | blocked |
| 无播报 | 继续收尾（PF-AUD-05） | failed |

结论合成（PF-JDG-02）：任一断言失败则 failed；动作失败或超时为 blocked；否则 passed。

## 5. 动作层

动作名与参数以台架端 PRD 表 15 为准，`planner.ACTIONS` 是同一份定义，后续应合并成一处，避免生成和执行两边不一致。

```python
class Channel(Protocol):
    name: str                         # adb / audio / camera / arm / can / host
    capabilities: set[str]            # 上报给云端的能力标签
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> ChannelHealth: ...

@action("play_utterance", channel="audio", required=("audio",))
async def play_utterance(ctx: RunContext, step: dict) -> None:
    await ctx.audio.play(ctx.asset(step["audio"]))
    ctx.evidence.mark("t0")           # 播放结束时刻，与录音同一时钟
```

| 通道 | 动作 | 实现 | 阶段 |
|---|---|---|---|
| Host | `mute_host` `unmute_host` `wait` | 控制本机讲解和等待条件 | 一期 |
| Adb | `screenshot` `shell` `tap` `swipe` `drag` `key` `input_text` | 包装现有 `DeviceAdapter`；SimCar 走同一接口 | 截图一期，操作类二期 |
| Audio | `record_start/stop` `wake` `play_utterance` `listen_tts` | 新建，音箱 + 麦克风 | 一期 B |
| Camera | `camera_shot` | 新建 | 一期 B |
| Arm / Can / Power | `tap`（物理）`can_send` `power_cycle` | 后续 | 三期 |

同一动作可由不同通道实现：`tap` 优先 ADB，量产版关闭 ADB 时由机械臂实现（PF-ADB-07）。选择由 `requires` 和台架能力决定，TaskPlan 不改。

`DeviceAdapter` 保留为 Adb 通道的底层，Runner 不直接依赖它。

## 6. 证据

```python
class Evidence:
    def add(self, name: str, kind: str, step: int | None = None, **meta) -> str: ...
    def mark(self, event: str, at: float | None = None) -> None: ...  # t0 / tts_start / tts_end
    def find(self, kind: str, step: int | None = None) -> EvidenceFile | None: ...
    def fact(self, key: str, value: Any) -> None: ...                 # wake=ok / tts=none
    def manifest(self) -> dict: ...                                   # 接口约定里的格式
```

- 时间点用同一时钟，播放结束时刻与录音误差 ≤ 50 ms（PF-AUD-01）。
- 语音用例无论成败都保留录音、播报片段、截图和时间点（PF-EVD-06）。
- 失败以断言步骤结束时刻为准截取证据，不用云端返回时刻（PF-EVD-08）。
- 落盘时用 `ctx` 提供的目录，打包前做脱敏（PF-EVD-05）。

## 7. 评估层

```python
class Evaluator(Protocol):
    type: str
    where: Literal["local", "cloud"]
    async def evaluate(self, a: dict, ev: Evidence, ctx: EvalContext) -> AssertResult: ...

class AssertResult(BaseModel):
    assertion_id: str
    result: Literal["pass", "fail", "error"]
    confidence: float
    detail: dict
    judge_ref: str = ""
```

| type | 位置 | 输入 | 判定 | 复用 |
|---|---|---|---|---|
| `rule` | 本地 | 日志增量 | 崩溃、ANR，置信度 1 | `judge.rule_judge` 的规则，改为读证据 |
| `text` | 本地 | 控件树 | 目标文本匹配 | 新建（依赖 dump_ui） |
| `timing` | 本地 | `t0` → `tts_start` | 与 `max_ms` 比较，失败类型“响应时延” | 新建 |
| `vision` | 云端 A-10 | 截图 + 基线 / 期望文本 | 视觉模型比对 | `judge.py` 的 VLM 比对 |
| `voice` | 云端 A-10 | 截图 + 播报片段 + 时间点 | 看、听分别判定后合成 | 新建 |
| `can` | 本地 | CAN 记录 | 信号值和时延比较 | 三期 |

判定成本从低到高依次是 `rule` / `text` / `timing` → `vision` → `voice`。生成 TaskPlan 时，能用文本判定的不用视觉：比如开关状态优先读控件树。文案截断仍要用视觉，控件树里的文本是数据层原文，看不出屏幕上被截断。

**语音不是独立系统。** 一条语音用例就是：音频动作 + ADB 截图动作 + `voice` / `timing` / `rule` 断言。

| 步骤 | 动作 | 写入证据 |
|---|---|---|
| 1–2 | `mute_host` `record_start` | 录音开始时刻 |
| 3 | `wake` | 事实 `wake=ok/no_response` |
| 4 | `play_utterance` | 时间点 `t0` |
| 5 | `listen_tts` | 播报片段、`tts_start`、`tts_end` 或 `tts=none` |
| 6–7 | `record_stop` `screenshot` | 录音、截图 |
| 8 | `unmute_host` | — |
| 断言 | `voice` → A-10；`timing`、`no_crash` → 本地 | 看 / 听 / 时延结论 |

期望值已由测试语句参数推出并写进 TaskPlan。屏幕或播报任一不符即失败，一期不依赖车机意图日志。

**实现中补充的两条规则（R1 实测得出）：**

- `vision` 断言把到该步为止的截图按先后都交给判定（最多 6 张）。用例期望常描述多个状态（“第一次点击后开启，第二次点击后关闭”），只看最后一张会判错。
- 前置条件没有自动设置时，起始状态不确定，判定可能是误报。证据里记 `setup_unmet`，视觉 / 文本候选降为低置信并写明原因，交人工核对；崩溃规则不受影响。

## 8. 视觉：感知、定位与 GUI Agent

ADB 和 GUI Agent 不在同一层：

```text
感知层：拿画面     adb screencap / uiautomator dump / 相机 / cluster_shot
决策层：下一步     TaskPlan 写死（回放） 或 GUI Agent（试跑、探索）
执行层：做动作     adb input / 机械臂
判定层：对不对     rule / text / vision
```

| 阶段 | 感知 | 决策 | 产物 |
|---|---|---|---|
| 首次试跑 | 截图 + 控件树 | GUI Agent 按步骤找控件 | 选择器、坐标、基线写回 TaskPlan（A-12） |
| 回放 | 截图 + 控件树 | TaskPlan | 结果 |
| 定位失效 | 同上 | 回放只重试，不自愈，判阻塞（locator）；台架只上传失败现场，云端多模态模型给出新控件建议（`heal.py`） | 人工确认后写回新版本 |
| 探索测试 | 截图 + 控件树 | GUI Agent 自主 | 缺陷候选，不作为回归基线 |

**定位顺序（`runner/locate.py`）：** 控件选择器 → 本地 OCR → 图标模板 → 相对坐标 `ratio` → `fallback_xy` 坐标 → 机械臂坐标，按 target 里写了的层依次试，先命中先用。OCR 和模板匹配都在台架本地跑（可选依赖 `uv sync --extra vision`，没装时这两层跳过并写明原因）。命中层记入步骤结果；没用上首选层时告警“界面可能改版”。回放时不用视觉模型找控件，否则一次回放的结果会随模型变化。

**回放不自愈是有意的：** 自动绕过定位失败，可能把真实的界面缺陷掩盖掉。

**ADB 不适用的场景：**

| 场景 | 方案 |
|---|---|
| 仪表 / QNX | `cluster_shot` 或 SSH，声明对应能力 |
| 要证明屏幕真实显示（黑屏、花屏） | 相机拍屏与截图对照（PF-JDG-03） |
| 量产版关闭 ADB | 机械臂 + 相机（PF-ADB-07） |
| 物理按键 | 机械臂 |

现有 `ai_explore` 不废弃，作为 GUI Agent 的基础，继续服务 `trial` 和探索测试。

## 9. 部署边界

Runner 和通道属于台架端；模型判定属于云端。一期 Runner 先内嵌在现有后端，但评估器通过接口调用：

```python
class JudgeClient(Protocol):
    async def vision(self, req: VisionReq) -> VisionResp: ...   # A-10 /judge/vision
    async def voice(self, req: VoiceReq) -> VoiceResp: ...      # A-10 /judge/voice
```

先用进程内实现（调 `modelgw`），台架 PC 独立部署时换成 HTTP 实现，Runner 和评估器不改。计划获取同理，先读本地 `data/plans`，之后走 A-11。

## 10. 目录规划

```text
apps/agent/cabin_agent/runner/
  loader.py        装载与校验 YAML（复用 planner.validate）
  engine.py        Runner 主流程
  context.py       RunContext / 结论合成
  evidence.py      Evidence / manifest
  actions/         host.py adb.py audio.py camera.py + 注册表
  evaluators/      rule.py text.py timing.py vision.py voice.py + 注册表
  judge_client.py  JudgeClient 进程内实现
```

现有 `execsvc/service.py` 继续负责调度、台架锁、状态机和缺陷闭环，新增一条分支：按 `case_id` 取 YAML 交给新 Runner。判定结果映射到现有 `DefectCandidate`，复用查重、审核、建单。

## 11. 实施顺序

| 里程碑 | 内容 | 验收 | 依赖 |
|---|---|---|---|
| R0 底座 | 解决命名冲突；loader、engine、注册表、Evidence；`dry` 模式 | 已生成的 TaskPlan 都能干跑并出校验报告 | 无 |
| R1 SimCar 回放 | Adb 通道；`rule`、`vision`、`text` 评估器；接入 service | TC-DISP-017 在 SimCar 上发现文案截断，并走到缺陷审核 | 无 |
| R2 试跑固化 | `trial` 模式复用 GUI Agent；定位与基线写回 TaskPlan 新版本 | 同一用例回放 10 次结论一致，回放阶段无操作类模型调用 | R1 |
| R3 语音闭环 | Audio 通道；测试音频合成；`timing`、`voice` 评估器 | 语音空调用例从下达到沙箱建单跑通 | 语音服务、音箱、麦克风 |
| R4 台架部署 | Runner 独立部署；A-05 / A-06 / A-07 / A-10 / A-11 / A-12 走 HTTP | 真实台架领取、执行、上报 | 台架 PC、云端接口 |

**与 PRD 分期的差异：** PRD 把语音放在一期 B，ADB 操作放在二期。这里先做 R1，是因为 SimCar 不依赖硬件，能先验证 Runner 框架；语音动作仍按一期 B 交付，不推迟。

R3 之前可以做一个模拟音频通道：用预录车机回应走通时序和 `voice` 判定，用于开发测试，不算真实语音验收。

## 12. 待确认

已按建议实现，可再调整：

1. **结论优先级：** 同一次执行既有断言失败又有动作失败时按 failed，因为已有明确缺陷证据。崩溃后定位失败就是这种情况。
2. **等待超时：** 步骤支持 `on_timeout`，默认 blocked。planner 还不会生成这个字段。

仍待决定：

3. **SimCar 的语音能力：** 是否给 SimCar 增加模拟空调页和播报，用来演示语音用例。
4. **前置条件：** 车型命令库由谁提供。目前没有命令时告警并继续，候选降为低置信；是否要改成阻塞或人工确认后继续。
5. **基线归属：** 基线截图存对象存储还是 git。接口示例用 `plan://` 引用，存储位置未定。
6. **定位失效阈值：** PRD 定为连续 3 次，是否按车型或模块调整。

## 13. R0、R1 验证记录（2026-11-04）

| 验收项 | 判据 | 结果 | 证据 |
|---|---|---|---|
| R0 干跑 | 已生成的 TaskPlan 都能干跑并出报告 | 通过 | `plan_dry_run.py`：25 条，4 条触屏可执行，21 条语音报缺 audio 能力、动作和评估器 |
| R1 发现缺陷 | TC-DISP-017 发现文案截断并走到审核 | 通过 | `replay_demo.py`：SimCar + 真实视觉模型，置信度 0.99，查重 → 审核 → 沙箱建单 → 通知，17.7 s |
| R1 反例 | 无缺陷时不误报 | 通过 | `CABIN_SIMCAR_BUGS=` 无缺陷车机，同一 TaskPlan 判定 passed |
| 对话链路 | 话术 → 确认门 → 回放 → 缺陷卡 → 驳回 | 通过 | 经 WebSocket + Agent：未登录被拦下；确认后才执行；驳回不建单 |
| 单元测试 | 失败语义、证据、评估器、集成 | 通过 | `test_runner.py` 等共 56 个 |

**其余触屏用例的结论不能当作车机缺陷：**

| 用例 | 结论 | 原因 |
|---|---|---|
| TC-DISP-016 | failed（低置信） | 用例假设开关初始为关，SimCar 初始为开，前置条件没有自动设置，属于环境问题 |
| TC-DISP-018 | failed | SimCar 的亮度滑条不响应拖动，也不显示百分比 |
| TC-DISP-019 | failed（低置信） | SimCar 没有实现“自动亮度开启时滑条置灰” |

**未验证：** 真实安卓台架；R2 的 `trial` 固化。`drag` / `swipe` 只验证了动作下发，SimCar 滑条不响应，效果未确认。

**实测发现并修复：** 强制思考的模型会用完小额度导致正文为空（模型网关统一抬高预算，AI 探索的 600 token 调用同样受影响）；多状态期望只看最后一张截图会判错；前置条件未设置时会产生误报。

## 14. 语音模拟与飞书闭环（2026-11-04）

R3 的模拟版已实现，真实语音未开始：

- **用例来源：** PRD 拆解，或 `.xlsx` 用例表导入（`casexls.py`）。两者都先过审核门，表里的审核状态不继承。
- **多维表格：** 写入 → 表格审核 → 读回 → 生成 YAML（`feishu_e2e.py`）。
- **模拟语音：** SimCar 新增空调页和车机语音规则；`SimAudioChannel` 走真实时序并写入 t0、播报起止和播报片段（静音占位）。
- **评估器：** `voice`、`timing` 已实现。看线走真实视觉模型；听线在模拟通道里比对车机文案，**不是语音识别**。接入真实音频但没有语音服务时，判定会报错，不会误判通过。
- **前置条件：** 空调前置条件翻译成 SimCar 示例命令 `cabin-hvac`，真实车型命令库仍为空。

| 验收项 | 结果 | 证据 |
|---|---|---|
| xlsx → YAML → Runner → 判定 | 8/8 passed | `voice_e2e.py`，无缺陷车机 |
| PRD → 多维表格 → 读回 → YAML → Runner | 9/9 passed | `feishu_e2e.py --auto-approve`，真实模型 + 真实飞书 |
| 判定分类与失败语义 | 通过 | `test_voice.py`，用假设备覆盖 |

**未验证：** `CABIN_SIMCAR_BUGS` 注入缺陷后在 SimCar 上的整轮运行；真实音箱、麦克风和语音服务。

## 15. 测试音频（2026-11-05）

`audioasset.py` 实现：生成 TaskPlan 时合成音频并把哈希写回 YAML，回放只播放缓存。

- **缓存键：** 文字 + 音色 + 语速 + 语音模型。模型升级后自动重新合成，不会串用旧音频。
- **存放：** `data/assets/<sha256>.wav`，16 kHz 单声道。相同内容只存一份，所有用例共用同一个唤醒词。
- **下发：** `GET /api/v1/plans/{case_id}/assets/{name}`（A-11），响应头 `X-Asset-Sha256`。
- **执行前校验：** 音频未合成或缺失时干跑和执行都报 `plan_invalid`；哈希不符时不操作车机。
- **合成失败：** 用例置为 `invalid`，不下发，下次拉取自动重试。旧版本没有音频的计划在拉取时自动重新生成。
- **语音服务：** 当前用 MiniMax。切到私有云服务时改 `VOICES` 和模型网关；模型名变化后会全部重新合成一次。

| 验收项 | 结果 |
|---|---|
| 缓存效果 | 18 条用例引用 36 次音频，实际合成 14 次；再次拉取 0 次 |
| 音频内容 | 本地 ASR 复核 14 个音频，文字与数字全部一致 |
| 下载校验 | A-11 下载哈希一致，16 kHz 单声道 |
| 回写 | 飞书 18/18 行更新了测试代码版本 |
| 执行 | 用真实音频时长占用时序，18/18 passed（SimCar 模拟语音） |

**实测数据：** 单次合成约 1.1 秒；14 个音频共 724 KB、22.7 秒；MiniMax 对 4 个汉字计费 8 个字符。

**尚未验证：** 音频真正从音箱播放，以及车机能否听懂。SimCar 只用音频时长模拟播放，接收的仍是文字。

**已知问题：** “空调调到 24 度”和“空调调到24度”是不同文字，会各合成一份，时长也不同；可以在生成时统一格式。
