"""首次配置：生成根目录 .env 并校验凭证可用。

用法：
    uv run python scripts/setup_env.py            # 交互式生成 .env
    uv run python scripts/setup_env.py --check    # 只校验现有 .env，不修改
    uv run python scripts/setup_env.py --non-interactive

行为：
    - .env 已存在时默认不覆盖（避免冲掉已有凭证），加 --force 才重建。
    - 生成的 .env 权限设为 600，且已在 .gitignore 中排除。
    - --check 会实际调用一次模型网关，确认 token 有效。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

DEFAULTS = {
    "CABIN_LLM_BASE_URL": "https://api.minimaxi.com/anthropic",
    "CABIN_CHAT_MODEL": "MiniMax-M3",
    "CABIN_VISION_MODEL": "MiniMax-M3",
}


def _mask(secret: str) -> str:
    if not secret:
        return "(空)"
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 12 else "***"


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _write_env(key: str, base_url: str) -> None:
    lines = [
        "# 本地开发凭证（已被 .gitignore 排除，不要提交）",
        "# 由 scripts/setup_env.py 生成；字段说明见 .env.example",
        "",
        f"CABIN_LLM_API_KEY={key}",
        f"CABIN_LLM_BASE_URL={base_url}",
        f"CABIN_CHAT_MODEL={DEFAULTS['CABIN_CHAT_MODEL']}",
        f"CABIN_VISION_MODEL={DEFAULTS['CABIN_VISION_MODEL']}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf8")
    os.chmod(ENV_PATH, 0o600)


def _read_key() -> str:
    """优先取环境变量，其次取已有 .env，最后交互输入。"""
    env_key = os.getenv("CABIN_LLM_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if env_key:
        return env_key.strip()
    if ENV_PATH.exists():
        old = _parse_env(ENV_PATH.read_text("utf8")).get("CABIN_LLM_API_KEY", "")
        if old:
            return old
    try:
        import getpass

        return getpass.getpass("请输入 MiniMax API Key（输入不回显）: ").strip()
    except Exception:
        return input("请输入 MiniMax API Key: ").strip()


def check() -> int:
    """校验 .env 是否存在、token 是否非空、模型网关能否连通。"""
    if not ENV_PATH.exists():
        print("✗ 未找到 .env —— 先运行：uv run python scripts/setup_env.py")
        return 1
    vals = _parse_env(ENV_PATH.read_text("utf8"))
    key = vals.get("CABIN_LLM_API_KEY", "")
    if not key:
        print("✗ .env 中缺少 CABIN_LLM_API_KEY")
        return 1
    print(f"· .env 路径     : {ENV_PATH}")
    print(f"· API Key       : {_mask(key)}")
    print(f"· 接入地址      : {vals.get('CABIN_LLM_BASE_URL', DEFAULTS['CABIN_LLM_BASE_URL'])}")

    sys.path.insert(0, str(ROOT / "apps" / "cloud"))
    try:
        import asyncio
    except Exception as e:  # pragma: no cover
        print(f"✗ 无法导入 asyncio：{e}")
        return 1

    try:
        from cabin_cloud.config import get_settings
        from cabin_cloud.modelgw import get_gateway
    except ImportError as e:
        print(f"✗ 无法导入 cabin 包：{e}")
        print("  请在 backend/ 目录下运行，或先执行 cd backend && uv sync")
        return 1

    try:
        s = get_settings()
        if not s.llm_api_key:
            print("✗ config 未取到 token")
            return 1

        async def _ping() -> str:
            gw = get_gateway()
            return await gw.complete(
                system="你是连通性测试助手，只回复两个字。",
                prompt="回复：正常",
                label="public",
                source="setup-env-check",
                max_tokens=16,
            )

        reply = asyncio.run(_ping())
        print(f"· 模型连通性    : OK（回复：{reply[:40]}）")
        print("✓ 全部检查通过")
        return 0
    except Exception as e:
        print(f"✗ 模型网关调用失败：{type(e).__name__}: {e}")
        print("  排查：token 是否有效 / CABIN_LLM_BASE_URL 是否正确 / 网络是否可达")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="生成或校验 CabinPilot 的 .env 配置")
    ap.add_argument("--check", action="store_true", help="只校验现有 .env，不修改")
    ap.add_argument("--force", action="store_true", help=".env 已存在时也重建")
    ap.add_argument("--non-interactive", action="store_true", help="不交互读取 key（从环境变量取）")
    args = ap.parse_args()

    if args.check:
        return check()

    if ENV_PATH.exists() and not args.force:
        print(f"· .env 已存在：{ENV_PATH}")
        print("  保持不变。如需重建请加 --force，或用 --check 校验现有配置。")
        return 0

    if not args.non_interactive and not sys.stdin.isatty():
        print("✗ 非交互环境：请用 --non-interactive（从 CABIN_LLM_API_KEY 环境变量读取）")
        return 1

    key = _read_key()
    if not key:
        print("✗ 未提供 API Key")
        return 1

    base_url = os.getenv("CABIN_LLM_BASE_URL") or DEFAULTS["CABIN_LLM_BASE_URL"]
    _write_env(key, base_url)
    print(f"✓ 已生成 {ENV_PATH}（权限 600，已被 .gitignore 排除）")
    print(f"  CABIN_LLM_API_KEY  = {_mask(key)}")
    print(f"  CABIN_LLM_BASE_URL = {base_url}")

    if EXAMPLE_PATH.exists():
        print(f"  完整字段说明见 {EXAMPLE_PATH}")

    print("\n下一步：")
    print("  cd backend && uv run pytest -q            # 自检")
    print("  cd backend && uv run python scripts/setup_env.py --check   # 验通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
