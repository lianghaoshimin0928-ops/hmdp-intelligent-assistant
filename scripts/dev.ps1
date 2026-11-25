<#
.SYNOPSIS
    hmdp 本地开发环境管理：一键启动 / 停止 / 查看依赖与服务。

.DESCRIPTION
    启动顺序严格按依赖关系：
        虚拟机 MySQL → 本机 Redis → hmdp 业务后端(8081) → ai-service(Python 8000)

    每个服务在后台运行，输出重定向到 logs\ 下的日志文件，PID 记录在 logs\*.pid。

.EXAMPLE
    .\scripts\dev.ps1                 # 默认查看状态
    .\scripts\dev.ps1 -Action start   # 启动全部
    .\scripts\dev.ps1 -Action stop    # 停止全部
    .\scripts\dev.ps1 -Action restart # 重启全部
    .\scripts\dev.ps1 -Action start -SkipVm   # 虚拟机已在跑，跳过检测
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'status',

    # 跳过去启动 VMware 虚拟机（当你在别处已经把它开好了）
    [switch]$SkipVm
)

$ErrorActionPreference = 'Stop'

# ============================================================================
# 配置：机器相关的路径都在这里，换电脑只改这一段
# ============================================================================
$RepoRoot = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $RepoRoot 'logs'
$AiDir = Join-Path $RepoRoot 'ai-service'

$Config = @{
    # ---- Redis（本机）----
    RedisDir  = 'D:\develpo\redis-5.0.14.1'
    RedisExe  = 'redis-server.exe'
    RedisConf = 'redis-hmdp.conf'
    RedisPort = 6379
    RedisPass = '123456'

    # ---- Java 业务后端 ----
    JavaHome = 'C:\Program Files\Eclipse Adoptium\jdk-8.0.512.9-hotspot'
    Mvn      = 'D:\develpo\idea\IntelliJ IDEA 2026.1.3\plugins\maven\lib\maven3\bin\mvn.cmd'
    HmdpPort = 8081

    # ---- AI 服务 ----
    AiPort = 8000

    # ---- 虚拟机 MySQL（CentOS + VMware）----
    VmIp    = '192.168.188.128'
    VmPort  = 3306
    VmxPath = 'C:\Users\34886\Documents\Virtual Machines\CentOS 7 64 位\CentOS 7 64 位.vmx'
    Vmrun   = 'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
}

# ============================================================================
# 工具函数
# ============================================================================
function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "    [OK]   $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "    [FAIL] $msg" -ForegroundColor Red }

<#
    调用原生命令的安全包装。
    坑：$ErrorActionPreference='Stop' 时，原生命令只要往 stderr 写一行，
    PowerShell 就会抛 NativeCommandError 终止整个脚本（redis-cli 的密码告警就会触发）。
    这里局部降级为 Continue，并把 stderr 合并进输出返回。
#>
function Invoke-Native {
    param([string]$FilePath, [string[]]$Arguments = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $FilePath @Arguments 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($out) }
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Output = @("$($_.Exception.Message)") }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Initialize-LogDir {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
}

function Test-Port {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 1500)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        $ready = $async.AsyncWaitHandle.WaitOne($TimeoutMs)
        if ($ready) { $client.EndConnect($async); $client.Close(); return $true }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

function Wait-Port {
    param([string]$Name, [string]$TargetHost, [int]$Port, [int]$TimeoutSec = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastReport = 0
    while ((Get-Date) -lt $deadline) {
        if (Test-Port -TargetHost $TargetHost -Port $Port) {
            Write-Ok "$Name 已就绪 ($TargetHost`:$Port)"
            return $true
        }
        $elapsed = [int]((Get-Date) - (($deadline.AddSeconds(-$TimeoutSec)))).TotalSeconds
        if ($elapsed - $lastReport -ge 10) {
            Write-Host "    等待 $Name ... ${elapsed}s / ${TimeoutSec}s" -ForegroundColor DarkGray
            $lastReport = $elapsed
        }
        Start-Sleep -Milliseconds 1000
    }
    Write-Err "$Name 启动超时 ($TargetHost`:$Port)，请查看 $LogDir 下的日志"
    return $false
}

function Get-PidFile($name) { Join-Path $LogDir "$name.pid" }

function Save-Pid($name, $processId) {
    Set-Content -Path (Get-PidFile $name) -Value $processId -Encoding ascii
}

function Get-SavedPid($name) {
    $file = Get-PidFile $name
    if (-not (Test-Path $file)) { return $null }
    $raw = (Get-Content $file -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    if ($raw -match '^\d+$') { return [int]$raw }
    return $null
}

function Get-PortOwner {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch { }
    return $null
}

function Stop-ProcessSafely {
    param([string]$Name, [int]$ProcessId)
    if (-not $ProcessId) { return $false }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "    $Name 的进程 $ProcessId 已不存在" -ForegroundColor DarkGray
        Remove-Item (Get-PidFile $Name) -Force -ErrorAction SilentlyContinue
        return $false
    }
    # 连子进程一起杀（mvn 会 fork 出 java 进程）
    try {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
        foreach ($c in $children) { Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 400
    Remove-Item (Get-PidFile $Name) -Force -ErrorAction SilentlyContinue
    Write-Ok "$Name 已停止 (pid=$ProcessId)"
    return $true
}

# ============================================================================
# 各组件
# ============================================================================
function Get-MySqlStatus {
    $up = Test-Port -TargetHost $Config.VmIp -Port $Config.VmPort
    return [pscustomobject]@{
        Name    = 'MySQL (虚拟机)'
        Host    = $Config.VmIp
        Port    = $Config.VmPort
        IsUp    = $up
        Note    = 'VMware 里的 CentOS'
    }
}

function Start-MySql {
    Write-Step "MySQL（虚拟机 $($Config.VmIp)）"
    if (Test-Port -TargetHost $Config.VmIp -Port $Config.VmPort) {
        Write-Ok 'MySQL 已可连接，跳过'
        return $true
    }

    if ($SkipVm) {
        Write-Err 'MySQL 不可达，且已指定 -SkipVm。请手动确认虚拟机与 MySQL 服务状态。'
        return $false
    }

    if (-not (Test-Path $Config.Vmrun)) {
        Write-Err "找不到 vmrun.exe：$($Config.Vmrun)"
        return $false
    }
    if (-not (Test-Path $Config.VmxPath)) {
        Write-Err "找不到虚拟机配置文件：$($Config.VmxPath)"
        return $false
    }

    $list = Invoke-Native $Config.Vmrun @('list')
    if (($list.Output -join "`n") -match [regex]::Escape($Config.VmxPath)) {
        Write-Ok '虚拟机进程已在运行，等待 MySQL 端口...'
    } else {
        Write-Host '    正在启动虚拟机（headless）...' -ForegroundColor DarkGray
        $startVm = Invoke-Native $Config.Vmrun @('-T', 'ws', 'start', $Config.VmxPath, 'nogui')
        foreach ($line in $startVm.Output) { Write-Host "    $line" -ForegroundColor DarkGray }
    }

    # 虚拟机开机 + 系统内 MySQL 自启，通常 30~90 秒
    if (Wait-Port -Name 'MySQL' -TargetHost $Config.VmIp -Port $Config.VmPort -TimeoutSec 150) {
        return $true
    }

    Write-Err '虚拟机起来了但 3306 仍不通。可能原因：'
    Write-Host '      · CentOS 里的 MySQL 没设开机自启 → 进虚拟机执行 systemctl start mysqld' -ForegroundColor Yellow
    Write-Host '      · 虚拟机 IP 变了 → 检查 ip addr，并同步修改 application.yaml' -ForegroundColor Yellow
    return $false
}

function Start-Redis {
    Write-Step "Redis（本机 :$($Config.RedisPort)）"
    if (Test-Port -TargetHost '127.0.0.1' -Port $Config.RedisPort) {
        Write-Ok 'Redis 已在运行，跳过'
        return $true
    }

    $exePath = Join-Path $Config.RedisDir $Config.RedisExe
    if (-not (Test-Path $exePath)) {
        Write-Err "找不到 redis-server.exe：$exePath"
        return $false
    }
    if (-not (Test-Path (Join-Path $Config.RedisDir $Config.RedisConf))) {
        Write-Err "找不到配置文件：$($Config.RedisConf)"
        return $false
    }

    $proc = Start-Process -FilePath $exePath `
        -ArgumentList $Config.RedisConf `
        -WorkingDirectory $Config.RedisDir `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'redis.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'redis.err.log')

    Save-Pid 'redis' $proc.Id

    if (Wait-Port -Name 'Redis' -TargetHost '127.0.0.1' -Port $Config.RedisPort -TimeoutSec 20) {
        # 顺便验证口令（--no-auth-warning 可避免 redis-cli 往 stderr 写密码告警）
        $cli = Join-Path $Config.RedisDir 'redis-cli.exe'
        if (Test-Path $cli) {
            $r = Invoke-Native $cli @('-h', '127.0.0.1', '-p', "$($Config.RedisPort)", '-a', $Config.RedisPass, '--no-auth-warning', 'ping')
            if (($r.Output -join ' ') -match 'PONG') { Write-Ok '口令验证通过 (PONG)' }
            else { Write-Warn2 "口令验证未通过：$($r.Output -join ' ')" }
        }
        return $true
    }
    return $false
}

function Start-Hmdp {
    Write-Step "hmdp 业务后端（:8081）"
    if (Test-Port -TargetHost '127.0.0.1' -Port $Config.HmdpPort) {
        Write-Ok '8081 已占用，跳过启动'
        return $true
    }
    if (-not (Test-Path $Config.Mvn)) { Write-Err "找不到 mvn.cmd：$($Config.Mvn)"; return $false }
    if (-not (Test-Path $Config.JavaHome)) { Write-Err "找不到 JDK 8：$($Config.JavaHome)"; return $false }

    $env:JAVA_HOME = $Config.JavaHome
    $cmd = "set `"JAVA_HOME=$($Config.JavaHome)`" && `"$($Config.Mvn)`" -B -DskipTests spring-boot:run"

    $proc = Start-Process -FilePath 'cmd.exe' `
        -ArgumentList '/c', $cmd `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'hmdp.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'hmdp.err.log')

    Save-Pid 'hmdp' $proc.Id
    # Spring Boot 首次启动较慢，给 180 秒
    if (Wait-Port -Name 'hmdp' -TargetHost '127.0.0.1' -Port $Config.HmdpPort -TimeoutSec 180) {
        Write-Ok "接口地址 http://127.0.0.1:$($Config.HmdpPort)/shop/1"
        return $true
    }
    Write-Warn2 "查看日志：Get-Content '$LogDir\hmdp.out.log' -Tail 50"
    return $false
}

function Start-AiService {
    Write-Step "ai-service AI 服务（:8000）"
    if (Test-Port -TargetHost '127.0.0.1' -Port $Config.AiPort) {
        Write-Ok '8000 已占用，跳过启动'
        return $true
    }

    $python = Join-Path $AiDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        Write-Err "找不到虚拟环境：$python"
        Write-Host "    先执行：cd ai-service; python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt" -ForegroundColor Yellow
        return $false
    }
    if (-not (Test-Path (Join-Path $AiDir '.env'))) {
        Write-Warn2 '.env 不存在（AI 相关接口会报未配置 key），可先 Copy-Item .env.example .env'
    }

    $proc = Start-Process -FilePath $python `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$($Config.AiPort)" `
        -WorkingDirectory $AiDir `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'ai-service.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'ai-service.err.log')

    Save-Pid 'ai-service' $proc.Id

    if (Wait-Port -Name 'ai-service' -TargetHost '127.0.0.1' -Port $Config.AiPort -TimeoutSec 60) {
        Write-Ok "接口文档 http://127.0.0.1:$($Config.AiPort)/docs"
        return $true
    }
    return $false
}

# ============================================================================
# 动作
# ============================================================================
function Show-Status {
    Write-Step '服务状态'
    $rows = @(
        (Get-MySqlStatus),
        [pscustomobject]@{ Name = 'Redis'; Host = '127.0.0.1'; Port = $Config.RedisPort; IsUp = (Test-Port -TargetHost '127.0.0.1' -Port $Config.RedisPort); Note = 'requirepass 123456' },
        [pscustomobject]@{ Name = 'hmdp'; Host = '127.0.0.1'; Port = $Config.HmdpPort; IsUp = (Test-Port -TargetHost '127.0.0.1' -Port $Config.HmdpPort); Note = 'Java 业务后端' },
        [pscustomobject]@{ Name = 'ai-service'; Host = '127.0.0.1'; Port = $Config.AiPort; IsUp = (Test-Port -TargetHost '127.0.0.1' -Port $Config.AiPort); Note = 'Python AI 服务' }
    )

    Write-Host ''
    foreach ($r in $rows) {
        $flag = if ($r.IsUp) { 'UP  ' } else { 'DOWN' }
        $color = if ($r.IsUp) { 'Green' } else { 'Red' }
        $addr = "$($r.Host):$($r.Port)"
        Write-Host ("    {0,-12} {1,-22} {2,-6} {3}" -f $r.Name, $addr, $flag, $r.Note) -ForegroundColor $color
    }

    $allUp = ($rows | Where-Object { -not $_.IsUp }).Count -eq 0
    Write-Host ''
    if ($allUp) { Write-Ok '全部就绪，可以开始开发' }
    else { Write-Warn2 "有服务未启动，执行：.\scripts\dev.ps1 -Action start" }
    return $allUp
}

function Invoke-Start {
    Initialize-LogDir
    Write-Host '启动顺序：MySQL → Redis → hmdp(8081) → ai-service(8000)' -ForegroundColor DarkCyan

    if (-not (Start-MySql)) {
        Write-Err 'MySQL 未就绪，后续服务即使启动也无法工作，已中止。'
        Write-Host '    如虚拟机已在别处跑好，可加 -SkipVm 跳过检测。' -ForegroundColor Yellow
        return $false
    }
    $redisOk = Start-Redis
    $hmdpOk = Start-Hmdp
    $aiOk = Start-AiService

    Show-Status | Out-Null

    Write-Step '常用命令'
    Write-Host "    业务接口    curl.exe http://127.0.0.1:$($Config.HmdpPort)/shop/1" -ForegroundColor Gray
    Write-Host "    AI 自检     cd ai-service; .\.venv\Scripts\python.exe scripts\verify_all.py" -ForegroundColor Gray
    Write-Host "    AI 对话     curl.exe -X POST http://127.0.0.1:$($Config.AiPort)/api/chat -H `"Content-Type: application/json`" -d `"{\`"message\`":\`"帮我找火锅店\`",\`"thread_id\`":\`"u1\`"}`"" -ForegroundColor Gray
    Write-Host "    查看日志    Get-Content .\logs\hmdp.out.log -Tail 50 -Wait" -ForegroundColor Gray
    Write-Host "    停止全部    .\scripts\dev.ps1 -Action stop" -ForegroundColor Gray

    return ($redisOk -and $hmdpOk -and $aiOk)
}

function Invoke-Stop {
    Write-Step '停止服务（倒序）'
    Stop-ProcessSafely 'ai-service' (Get-SavedPid 'ai-service') | Out-Null
    # 没有 pid 文件时按端口兜底
    if (Test-Port -TargetHost '127.0.0.1' -Port $Config.AiPort) {
        $owner = Get-PortOwner $Config.AiPort
        if ($owner) { Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue; Write-Ok "按端口停止 ai-service (pid=$owner)" }
    }

    Stop-ProcessSafely 'hmdp' (Get-SavedPid 'hmdp') | Out-Null
    if (Test-Port -TargetHost '127.0.0.1' -Port $Config.HmdpPort) {
        $owner = Get-PortOwner $Config.HmdpPort
        if ($owner) { Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue; Write-Ok "按端口停止 hmdp (pid=$owner)" }
    }

    Stop-ProcessSafely 'redis' (Get-SavedPid 'redis') | Out-Null
    if (Test-Port -TargetHost '127.0.0.1' -Port $Config.RedisPort) {
        $owner = Get-PortOwner $Config.RedisPort
        if ($owner) { Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue; Write-Ok "按端口停止 Redis (pid=$owner)" }
    }

    Write-Warn2 '虚拟机 MySQL 不会被脚本关闭（里面可能还有别的东西在跑）'
    Write-Host ''
    Show-Status | Out-Null
}

# ============================================================================
# 入口
# ============================================================================
switch ($Action) {
    'start' { Invoke-Start | Out-Null }
    'stop' { Invoke-Stop }
    'restart' { Invoke-Stop; Start-Sleep -Seconds 2; Invoke-Start | Out-Null }
    'status' { Show-Status | Out-Null }
}
