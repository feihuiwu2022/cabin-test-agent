"""台架 Agent 配置。这里不放模型凭证，判定与模型调用都走云端。

优先级：环境变量 > .env（源码目录或 exe 旁边）> 数据目录里的 agent.json（首次启动在本地页面填写）> 默认值。
打包成 exe 后：资源（本地页面、模拟车机页）在解包目录，数据默认放 %LOCALAPPDATA%\\CabinPilot\\agent。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from cabin_common.env import load_env_file

FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller 打包后运行
APP_DIR = Path(__file__).resolve().parent.parent  # 源码运行时的 apps/agent
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else APP_DIR  # .env 放这里
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # web/dist、simcar 所在目录


def _default_data_dir() -> Path:
    if not FROZEN:
        return APP_DIR / "data"
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "CabinPilot" / "agent"


DATA_DIR = Path(os.getenv("CABIN_DATA_DIR") or _default_data_dir())
CONFIG_KEYS = ("cloud_url", "rig_id", "rig_model", "register_code", "device", "adb_serial", "log_dir", "ui_port")
ENV_NAMES = {"cloud_url": "CABIN_CLOUD_URL", "rig_id": "CABIN_RIG_ID", "rig_model": "CABIN_RIG_MODEL",
             "register_code": "CABIN_RIG_REGISTER_CODE", "device": "CABIN_DEVICE", "adb_serial": "CABIN_ADB_SERIAL",
             "log_dir": "CABIN_LOG_DIR", "ui_port": "CABIN_UI_PORT"}


@dataclass
class Settings:
    cloud_url: str = ""
    rig_id: str = "B1"
    rig_model: str = "A01-CN"
    register_code: str = ""  # 只有首次注册需要，之后用令牌
    device: str = "adb"  # adb | simcar
    adb_serial: str = ""
    log_dir: str = ""  # 车机上存放日志包的目录，失败时 adb pull 最新的一个
    ui_port: int = 8810  # 本地页面，只监听 127.0.0.1
    data_dir: Path = DATA_DIR

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def configured(self) -> bool:
        return bool(self.cloud_url)


def config_file(data_dir: Path | None = None) -> Path:
    return (data_dir or DATA_DIR) / "agent.json"


def load_local(data_dir: Path | None = None) -> dict:
    f = config_file(data_dir)
    try:
        raw = json.loads(f.read_text("utf8")) if f.is_file() else {}
    except (OSError, ValueError):
        raw = {}
    return {k: v for k, v in raw.items() if k in CONFIG_KEYS}


def save_local(values: dict, data_dir: Path | None = None) -> Path:
    """首次启动配置页保存的配置。注册码在注册成功后清掉（之后用令牌）。"""
    f = config_file(data_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    cur = load_local(data_dir)
    cur.update({k: v for k, v in values.items() if k in CONFIG_KEYS})
    f.write_text(json.dumps(cur, ensure_ascii=False, indent=1), "utf8")
    return f


@lru_cache
def get_settings() -> Settings:
    load_env_file(EXE_DIR / ".env")
    s = Settings()
    base = asdict(s)
    merged = {**{k: base[k] for k in CONFIG_KEYS}, **load_local()}
    for k, env in ENV_NAMES.items():
        if os.getenv(env):
            merged[k] = os.getenv(env)
    merged["ui_port"] = int(merged.get("ui_port") or Settings.ui_port)
    s = Settings(**merged, data_dir=DATA_DIR)
    s.evidence_dir.mkdir(parents=True, exist_ok=True)
    return s
