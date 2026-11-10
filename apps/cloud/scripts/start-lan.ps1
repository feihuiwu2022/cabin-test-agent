# CabinPilot 云端：在本机启动，给同一 Wi-Fi / 局域网里的同事用。
# 用法（在 apps\cloud\scripts 目录，或右键“使用 PowerShell 运行”）：
#   第一次：以管理员身份打开 PowerShell 运行一次（放行防火墙端口）
#   .\start-lan.ps1            # 默认端口 8800
#   .\start-lan.ps1 -Port 8900
# 配置写在 apps\cloud\.env（复制仓库根目录的 .env.example）；局域网部署见 docs\DEPLOY_LAN.md
param([int]$Port = 8800)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")   # apps\cloud

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "没有找到 uv。先安装：powershell -ExecutionPolicy ByPass -c ""irm https://astral.sh/uv/install.ps1 | iex""" -ForegroundColor Red
    exit 1
}

# 本机局域网地址：有默认网关、处于连接状态的那块网卡
$cfg = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -ne $null -and $_.NetAdapter.Status -eq "Up" } | Select-Object -First 1
if (-not $cfg) { Write-Host "没有找到连接中的网卡，确认电脑已经连上公司 Wi-Fi" -ForegroundColor Red; exit 1 }
$ip = $cfg.IPv4Address.IPAddress

# 公用网络会挡住别人访问：建议把公司 Wi-Fi 设成“专用网络”
$netProfile = Get-NetConnectionProfile -InterfaceIndex $cfg.InterfaceIndex -ErrorAction SilentlyContinue
if ($netProfile -and $netProfile.NetworkCategory -eq "Public") {
    Write-Warning "当前网络是“公用网络”，Windows 防火墙会挡住同事访问。到 设置 → 网络和 Internet → WLAN → 当前网络，改成“专用网络”。"
}

# 防火墙放行端口（只需一次，要管理员权限）
$rule = "CabinPilot 云端 $Port"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) {
        New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Domain,Private | Out-Null
        Write-Host "已放行防火墙端口 $Port（域网络、专用网络）"
    } else {
        Write-Warning "防火墙还没放行端口 $Port：第一次请用“以管理员身份运行”的 PowerShell 执行本脚本。"
    }
}

# 网页（用例工作台、看板）没构建时先构建，需要 Node.js
if (-not (Test-Path "web\dist\index.html")) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Write-Host "网页还没构建，需要先安装 Node.js（https://nodejs.org）" -ForegroundColor Red; exit 1 }
    Push-Location web; npm ci; npm run build; Pop-Location
}

$env:CABIN_HOST = "0.0.0.0"
$env:CABIN_PORT = "$Port"
# .env 里没写局域网地址时，用本机地址（飞书登录回跳、台架连接都用它）
$envFile = Join-Path (Get-Location) ".env"
$fixed = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*CABIN_PUBLIC_URL\s*=\s*https?://(?!127\.|localhost)' -Quiet)
if (-not $env:CABIN_PUBLIC_URL -and -not $fixed) { $env:CABIN_PUBLIC_URL = "http://${ip}:$Port" }

Write-Host ""
Write-Host "同事在浏览器打开： http://${ip}:$Port/cases" -ForegroundColor Green
Write-Host "台架 Agent 的云端地址填： http://${ip}:$Port" -ForegroundColor Green
Write-Host "关掉这个窗口就停止服务。电脑不要睡眠。" 
Write-Host ""
uv run --package cabin-cloud cabin-cloud
