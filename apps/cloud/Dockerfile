# 云端镜像。构建上下文是仓库根目录：docker build -f apps/cloud/Dockerfile -t cabin-cloud .
FROM node:22-slim AS web
WORKDIR /web
COPY apps/cloud/web/ ./
RUN npm ci && npm run build

FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/common packages/common
COPY apps/cloud apps/cloud
COPY --from=web /web/dist apps/cloud/web/dist
# 工作区要求成员目录存在，Agent 不进镜像
RUN mkdir -p apps/agent/cabin_agent && cp -n /dev/null apps/agent/cabin_agent/__init__.py
COPY apps/agent/pyproject.toml apps/agent/pyproject.toml
RUN uv sync --frozen --no-dev --package cabin-cloud
ENV CABIN_HOST=0.0.0.0 CABIN_PORT=8800
EXPOSE 8800
CMD ["uv", "run", "--no-sync", "--package", "cabin-cloud", "cabin-cloud"]
