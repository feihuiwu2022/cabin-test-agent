# 公开演示镜像：云端 + 模拟车机台架 Agent + 演示主页，一个容器跑完（Hugging Face Spaces 默认端口 7860）。
# 本地试：docker build -t cabinpilot-demo . && docker run -p 7860:7860 -e CABIN_LLM_API_KEY=sk-... cabinpilot-demo
FROM node:22-slim AS web
WORKDIR /src
COPY apps/cloud/web apps/cloud/web
COPY apps/agent/web apps/agent/web
RUN cd apps/cloud/web && npm ci && npm run build && cd ../../agent/web && npm ci && npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl bzip2 ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv && useradd -m -u 1000 user
WORKDIR /app
COPY . .
COPY --from=web /src/apps/cloud/web/dist apps/cloud/web/dist
COPY --from=web /src/apps/agent/web/dist apps/agent/web/dist
ENV UV_PROJECT_ENVIRONMENT=/app/.venv PLAYWRIGHT_BROWSERS_PATH=/app/.pw
RUN uv sync --frozen --no-dev --all-packages \
 && uv run --no-sync playwright install --with-deps chromium
# 离线语音识别模型（约 230MB）：下不到也能用，只是不能语音输入
RUN mkdir -p apps/cloud/models && (curl -fsSL https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 \
    | tar -xj -C apps/cloud/models || echo "ASR 模型下载失败，跳过")
RUN chown -R user:user /app && chmod +x deploy/demo-start.sh
USER user
ENV CABIN_DEMO=1 CABIN_AGENT_PUBLIC=1 CABIN_AUTH_MODE=dev CABIN_DEV_USER=演示 CABIN_DEVICE=simcar \
    CABIN_HOST=127.0.0.1 CABIN_PORT=8800 CABIN_CLOUD_URL=http://127.0.0.1:8800 CABIN_PUBLIC_URL=http://127.0.0.1:8800 \
    CABIN_UI_PORT=7860 CABIN_RIG_ID=B1 CABIN_RIG_MODEL=A01-CN CABIN_DATA_DIR=/app/apps/cloud/data
EXPOSE 7860
CMD ["deploy/demo-start.sh"]
