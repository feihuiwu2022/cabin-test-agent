#!/usr/bin/env bash
# 公开演示：先起云端（只监听容器内 127.0.0.1:8800），再起模拟车机台架 Agent，演示主页对外开在 7860。
# 模型密钥只从环境变量 CABIN_LLM_API_KEY 读（Hugging Face Spaces 里配在 Settings → Secrets），不进镜像、不进仓库。
set -euo pipefail
cd /app
uv run --no-sync python apps/cloud/scripts/seed_demo.py
export CABIN_RIG_REGISTER_CODE="${CABIN_RIG_REGISTER_CODE:-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
uv run --no-sync cabin-cloud &
for _ in $(seq 60); do curl -fs http://127.0.0.1:8800/api/health >/dev/null 2>&1 && break; sleep 1; done
CABIN_DATA_DIR=/app/.agent exec uv run --no-sync cabin-agent --no-browser
