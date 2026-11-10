# CabinPilot 台架 Agent

每台台架 PC 一份：领取执行单元、回放 TaskPlan、离线缓存与补传，并提供只在本机访问的页面。

```bash
cp apps/agent/.env.example apps/agent/.env
```

```bash
uv sync --package cabin-agent
```

```bash
cd apps/agent/web && npm ci && npm run build
```

```bash
uv run --package cabin-agent cabin-agent
```

本地页面：http://127.0.0.1:8810 。令牌、离线缓存和证据在 `apps/agent/data/`（`CABIN_DATA_DIR` 可改）。
