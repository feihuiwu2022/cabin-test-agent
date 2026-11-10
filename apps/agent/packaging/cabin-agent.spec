# -*- mode: python ; coding: utf-8 -*-
# 台架 Agent 打包（台架端 PRD PF-CFG-05）：目录形式，含本地页面、adb、ffmpeg；不含 playwright 和任何密钥。
# 在 apps/agent 下执行：uv run --with pyinstaller pyinstaller packaging/cabin-agent.spec --noconfirm
# 打包前先构建本地页面：cd web && npm ci && npm run build
# 装了 vision 可选依赖（uv sync --extra vision）时一并打进 OCR / 模板匹配；没装时这两层定位跳过。
import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # apps/agent
web = ROOT / "web" / "dist"
if not (web / "index.html").is_file():
    raise SystemExit(f"本地页面还没构建：{web}（cd web && npm ci && npm run build）")

datas = [(str(web), "web/dist")]
datas += collect_data_files("adbutils")         # adb / adb.exe 与 Windows 的 AdbWinApi.dll
datas += collect_data_files("imageio_ffmpeg")   # ffmpeg 可执行文件（录屏拼接）
hidden = collect_submodules("uvicorn") + collect_submodules("cabin_agent", filter=lambda n: "simcar" not in n) + collect_submodules("cabin_common")
if importlib.util.find_spec("rapidocr_onnxruntime"):
    datas += collect_data_files("rapidocr_onnxruntime")  # OCR 模型与配置
    hidden += ["rapidocr_onnxruntime"]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["playwright", "cabin_agent.device.simcar", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="cabin-agent", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="cabin-agent", upx=False)
