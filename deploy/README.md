# 部署公开演示

演示镜像（仓库根目录的 `Dockerfile`）把云端、模拟车机台架 Agent 和演示主页放在一个容器里，对外只开 7860 端口：

- 云端只监听容器内 `127.0.0.1:8800`，不对外；
- 演示主页（台架本地页面）对外开放，访客自动以“访客”身份使用；
- 启动时写入两条示例用例（`apps/cloud/scripts/seed_demo.py`）：系统设置调光、语音开启空调。

演示模式（`CABIN_DEMO=1`）的保护：

| 保护 | 说明 |
|---|---|
| 密钥不外泄 | 模型密钥只从环境变量读；改模型地址 / 密钥、测试连接的接口一律只读，访客没法把密钥发到别的地址 |
| 额度不被刷光 | 模型、搜索、语音合成每天调用总数上限 `CABIN_DEMO_DAILY_CALLS`（默认 300）；每个会话每小时对话轮数上限 `CABIN_DEMO_TURNS_PER_HOUR`（默认 20） |
| 不能改配置 | 规范包保存 / 回滚、台架用例集 / 排程、Jira 配置都只读；台架页面不能登录、不能改台架信息 |

## Hugging Face Spaces（免费）

1. 在 https://huggingface.co/new-space 新建 Space，SDK 选 **Docker**，可见性选 Public。
2. Space 的 Settings → **Variables and secrets** → New secret：名字 `CABIN_LLM_API_KEY`，值填模型密钥。
   用别的兼容接口时再加变量 `CABIN_LLM_BASE_URL`、`CABIN_CHAT_MODEL`、`CABIN_VISION_MODEL`。
3. 把本仓库推到 Space 的 git 仓库（Space 的 README 需要带下面的头部，指定端口）：

```bash
git remote add space https://huggingface.co/spaces/<你的用户名>/cabinpilot
```

```bash
git push space main
```

Space 的 `README.md` 开头需要这段配置（可以只在推到 Space 的分支里加）：

```yaml
---
title: CabinPilot
emoji: 🚗
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
---
```

免费 CPU 空间一段时间没人访问会休眠，下次打开时自动启动（要等一会儿）。国内网络访问 Hugging Face 可能不稳定。

## 本地 Docker

```bash
docker build -t cabinpilot-demo .
```

```bash
docker run --env-file .env -p 7860:7860 cabinpilot-demo
```

## 静态交互演示（GitHub Pages）

`docs/demo/` 是演示主页的静态版本：真实界面 + 一次真实运行的录像，不连服务器。重新录制：

1. 按上文在本地跑起演示环境（http://127.0.0.1:7860）。
2. 构建静态页面（会清空 `docs/demo`）：

```bash
cd apps/agent/web && VITE_DEMO=1 npx vite build --base ./ --outDir ../../../docs/demo --emptyOutDir
```

3. 录制并打包进 `docs/demo`：

```bash
node apps/agent/web/demo-tools/record.cjs /tmp/rec
```

```bash
python3 apps/agent/web/demo-tools/pack.py /tmp/rec/rec.json docs/demo
```

另外两个静态演示（`docs/demo-cases/` 用例生成、`docs/demo-board/` 多台架看板）用云端页面，录制和打包见 `apps/cloud/web/demo-tools/pack.py` 开头的说明；构建：

```bash
cd apps/cloud/web && VITE_DEMO=1 VITE_DEMO_PAGE=cases npx vite build --base ./ --outDir ../../../docs/demo-cases --emptyOutDir
```

```bash
cd apps/cloud/web && VITE_DEMO=1 VITE_DEMO_PAGE=board npx vite build --base ./ --outDir ../../../docs/demo-board --emptyOutDir
```
