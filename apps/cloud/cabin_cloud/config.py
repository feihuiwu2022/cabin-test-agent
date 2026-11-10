"""运行配置：环境变量优先，模型凭证从本地 .env 读取（不进前端、不进日志）。

优先级：进程环境变量 > 根目录 .env。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from cabin_common.env import load_env_file

BACKEND_DIR = Path(__file__).resolve().parent.parent  # apps/cloud，部署时是一个独立目录
ROOT_DIR = BACKEND_DIR.parent.parent
DATA_DIR = Path(os.getenv("CABIN_DATA_DIR", BACKEND_DIR / "data"))


def _load_dotenv() -> None:
    """载入 .env：部署目录优先，其次仓库根目录（已存在的环境变量优先，不覆盖）。"""
    load_env_file(BACKEND_DIR / ".env")
    load_env_file(ROOT_DIR / ".env")


@dataclass
class Settings:
    # 模型网关
    llm_base_url: str = "https://api.minimaxi.com/anthropic"
    llm_api_key: str = ""
    chat_model: str = "MiniMax-M3"
    vision_model: str = "MiniMax-M3"
    minimax_api_base: str = "https://api.minimaxi.com"
    strict_data: bool = False  # True：internal/sensitive 数据禁止走外网模型
    version_label: str = "开源演示"  # 顶栏版本标识
    # 语音
    tts_model: str = "speech-2.8-turbo"
    tts_voice: str = "female-shaonv"
    asr_model_dir: Path = BACKEND_DIR / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    wake_words: tuple[str, ...] = ("你好小禾", "hello xiaohe")  # 机器人唤醒词（浏览器免提监听时用）
    # 鉴权
    # 设备
    device: str = "simcar"  # 台架类型：simcar 时前置条件可用示例车型命令
    rig_id: str = "B2"
    rig_model: str = "A01-CN"  # 台架车型代码（车型字典），拉取用例与装载测试代码按它代入参数
    app_packages: dict = field(default_factory=dict)  # 被测应用包名：{“设置”: 包名}，生成触屏测试代码时作起点
    rig_register_code: str = ""  # 台架注册码；云端用它校验，台架 Agent 首次注册时提交
    secret_key: str = ""  # 加密云端保管的凭证
    admins: tuple[str, ...] = ()  # 能改 Jira 公共配置的人（飞书姓名）
    auth_mode: str = "feishu"  # feishu | lan（局域网：姓名 + 共享口令）| dev（只用于本地调试，须显式开启）
    lan_passcode: str = ""  # lan 登录口令（必填）
    lan_users: tuple[str, ...] = ()  # lan 登录允许的姓名；为空时任何姓名都行（凭口令）
    demo: bool = False  # 公开演示：没登录的访客按“访客”处理，改配置类接口只读，模型调用有上限
    demo_daily_calls: int = 300  # 公开演示：每天模型 / 搜索 / 语音合成调用总数上限（防止密钥额度被刷光）
    demo_turns_per_hour: int = 20  # 公开演示：每个访客 IP 每小时最多对话轮数
    dev_user: str = "测试工程师"
    session_ttl_s: int = 7 * 24 * 3600
    # 集成
    jira_url: str = ""
    jira_user: str = ""
    jira_password: str = ""
    jira_project: str = "CTRTS"
    feishu_webhook: str = ""
    digest_at: str = "08:30"  # 每日飞书汇总的时间
    tz_hours: int = 8  # 定时和汇总按这个时区算，容器里的系统时区通常是 UTC
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_case_doc: str = ""
    feishu_prd_folder: str = ""  # Word PRD 转成飞书文档后放的云空间文件夹（token 或链接）；为空时不转、本地解析
    feishu_bitable_url: str = ""  # 用例库多维表格链接（含 ?table=），配置后为用例主数据
    bitable_sync_s: int = 300  # 多维表格 → 云端反向同步周期
    modules_file: Path = BACKEND_DIR / "modules.yaml"  # 模块路由：模块 → 文件夹 + 多维表格
    preconditions_file: Path = BACKEND_DIR / "preconditions.yaml"  # 前置条件 → 设置命令
    # 服务
    host: str = "127.0.0.1"
    port: int = 8800
    data_dir: Path = DATA_DIR
    public_base_url: str = "http://127.0.0.1:8800"
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cabin.sqlite3"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"


SIMCAR_APPS = {"设置": "com.cabin.sim.settings", "空调": "com.cabin.sim.hvac"}


def _apps(text: str, device: str) -> dict:
    """“设置=com.android.car.settings;空调=…” → dict；SimCar 默认用模拟车机的包名。"""
    out = {k.strip(): v.strip() for k, _, v in (x.partition("=") for x in text.replace("；", ";").split(";"))
           if k.strip() and v.strip()}
    return out or (dict(SIMCAR_APPS) if device == "simcar" else {})


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    e = os.getenv
    s = Settings(
        llm_base_url=e("CABIN_LLM_BASE_URL", e("ANTHROPIC_BASE_URL", Settings.llm_base_url)),
        llm_api_key=e("CABIN_LLM_API_KEY", e("ANTHROPIC_AUTH_TOKEN", "")),
        chat_model=e("CABIN_CHAT_MODEL", Settings.chat_model),
        vision_model=e("CABIN_VISION_MODEL", Settings.vision_model),
        strict_data=e("CABIN_STRICT_DATA", "0") == "1",
        version_label=e("CABIN_VERSION_LABEL", Settings.version_label),
        tts_model=e("CABIN_TTS_MODEL", Settings.tts_model),
        tts_voice=e("CABIN_TTS_VOICE", Settings.tts_voice),
        device=e("CABIN_DEVICE", Settings.device),
        rig_id=e("CABIN_RIG_ID", Settings.rig_id),
        rig_model=e("CABIN_RIG_MODEL", Settings.rig_model),
        app_packages=_apps(e("CABIN_APP_PACKAGES", ""), e("CABIN_DEVICE", Settings.device)),
        rig_register_code=e("CABIN_RIG_REGISTER_CODE", ""),
        secret_key=e("CABIN_SECRET_KEY", ""),
        admins=tuple(x.strip() for x in e("CABIN_ADMINS", "").split(",") if x.strip()),
        auth_mode=e("CABIN_AUTH_MODE", Settings.auth_mode),
        dev_user=e("CABIN_DEV_USER", Settings.dev_user),
        lan_passcode=e("CABIN_LAN_PASSCODE", ""),
        demo=e("CABIN_DEMO", "0") == "1",
        demo_daily_calls=int(e("CABIN_DEMO_DAILY_CALLS", "300")),
        demo_turns_per_hour=int(e("CABIN_DEMO_TURNS_PER_HOUR", "20")),
        lan_users=tuple(x.strip() for x in e("CABIN_LAN_USERS", "").replace("，", ",").split(",") if x.strip()),
        jira_url=e("JIRA_URL", ""),
        jira_user=e("JIRA_USER", ""),
        jira_password=e("JIRA_PASSWORD", ""),
        jira_project=e("JIRA_PROJECT", Settings.jira_project),
        feishu_webhook=e("FEISHU_WEBHOOK", ""),
        digest_at=e("CABIN_DIGEST_AT", Settings.digest_at),
        tz_hours=int(e("CABIN_TZ_HOURS", str(Settings.tz_hours))),
        feishu_app_id=e("FEISHU_APP_ID", ""),
        feishu_app_secret=e("FEISHU_APP_SECRET", ""),
        feishu_case_doc=e("FEISHU_CASE_DOC", ""),
        feishu_prd_folder=e("FEISHU_PRD_FOLDER", ""),
        feishu_bitable_url=e("FEISHU_BITABLE_URL", ""),
        bitable_sync_s=int(e("CABIN_BITABLE_SYNC_S", "300")),
        modules_file=Path(e("CABIN_MODULES_FILE", str(Settings.modules_file))),
        host=e("CABIN_HOST", Settings.host),
        port=int(e("CABIN_PORT", str(Settings.port))),
    )
    s.public_base_url = e("CABIN_PUBLIC_URL", f"http://{s.host}:{s.port}")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.evidence_dir.mkdir(parents=True, exist_ok=True)
    return s
