# 局域网部署：云端装在自己的电脑上，同一 Wi-Fi 的同事都能用

适用：团队不用外部服务器，CabinPilot 云端跑在一台 Windows 电脑上，同事和台架 PC 在同一个 Wi-Fi / 局域网里访问。

## 1. 准备这台电脑

- **固定 IP**：别人靠 IP 访问，IP 变了大家就连不上。请网管在路由器上给这台电脑做 DHCP 地址保留，或在网卡里设静态 IP。
- **不睡眠**：设置 → 系统 → 电源 → 接通电源时“从不”睡眠。
- **网络类型设成“专用网络”**：设置 → 网络和 Internet → WLAN → 当前网络。“公用网络”下防火墙会挡住别人。
- **要能上外网**：调大模型（用例生成、判定）要访问模型服务；用飞书登录要访问飞书。
- 安装 [uv](https://docs.astral.sh/uv/)、Git、[Node.js](https://nodejs.org)（构建网页用，只需一次）。

## 2. 配置 `apps\cloud\.env`

复制仓库根目录的 `.env.example` 到 `apps\cloud\.env`，至少填这些：

```ini
CABIN_HOST=0.0.0.0
CABIN_PORT=8800
CABIN_PUBLIC_URL=http://192.168.1.23:8800      # 换成这台电脑的局域网地址
CABIN_ADMINS=张三,李四                           # 管理员：能改默认规范包、Jira 和模型配置
CABIN_RIG_REGISTER_CODE=换成一串随机字符          # 台架注册码
```

登录二选一：

| 方式 | 配置 | 说明 |
|---|---|---|
| 飞书登录 | `CABIN_AUTH_MODE=feishu`，填 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 在飞书开放平台把 `http://192.168.1.23:8800/api/v1/auth/feishu/callback` 加到应用的重定向地址。审核人、编辑人是飞书实名 |
| 局域网登录 | `CABIN_AUTH_MODE=lan`，`CABIN_LAN_PASSCODE=团队口令`，可选 `CABIN_LAN_USERS=张三,李四,王五` | 没有飞书应用时用。填姓名 + 口令；配了名单时只能选名单里的姓名。连续输错 10 次锁 10 分钟 |

## 3. 启动

在 `apps\cloud\scripts` 里运行 `start-lan.ps1`。**第一次要“以管理员身份运行”**，脚本会放行防火墙端口；以后普通权限运行即可。

脚本会自动找到本机局域网地址、在需要时构建网页，然后启动，窗口里打印出：

```
同事在浏览器打开： http://192.168.1.23:8800/cases
台架 Agent 的云端地址填： http://192.168.1.23:8800
```

启动日志开头的 `[CabinPilot]` 几行会提示常见的配置问题（只监听本机、`CABIN_PUBLIC_URL` 还是 127.0.0.1、没配登录口令等）。

开机自启：用“任务计划程序”新建任务，触发器“登录时”，操作运行 `powershell -ExecutionPolicy Bypass -File <路径>\start-lan.ps1`。

## 4. 同事和台架

- 同事：浏览器打开 `http://192.168.1.23:8800/cases`（用例工作台），`/board`（调度看板），`/skills`（用例编写规范），`/touch`（触屏执行）。
- 台架 PC：运行 `cabin-agent.exe`，首次配置页的“云端地址”填 `http://192.168.1.23:8800`，注册码填上面的 `CABIN_RIG_REGISTER_CODE`。

## 5. 数据与备份

所有数据在 `apps\cloud\data\`：结果库（SQLite）、测试代码与规范包（`plans\`，是一个 git 仓库）、证据、测试音频。定期把整个 `data` 文件夹拷到别处备份。

## 6. 安全

- 同一 Wi-Fi 的人都能打开登录页，所以一定要开登录（飞书或局域网口令），口令只告诉团队成员。
- 不要把这个端口映射到公网，也不要在访客 Wi-Fi 上开放。
- 局域网口令只能区分“谁说自己是谁”，比飞书登录弱；有条件时用飞书登录。
