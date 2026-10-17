# 知遇 · 多场景多难度自适应 AI 虚拟面试智能体 —— 一键启动器
#
# 作用：找 Python -> 查依赖(缺就装) -> 起 Streamlit -> 自动打开浏览器
# 关闭这个黑窗口 = 停止应用

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$AppFile     = Join-Path $Root 'app.py'
$ReqFile     = Join-Path $Root 'requirements.txt'
$DefaultPort = 8501
$PortMax     = 8520

function Say([string]$Text, [string]$Color = 'Gray') {
    Write-Host "  $Text" -ForegroundColor $Color
}

function Stop-WithError([string]$Message) {
    Write-Host ''
    Write-Host "  [启动失败] $Message" -ForegroundColor Red
    Write-Host ''
    Read-Host '  按回车键关闭'
    exit 1
}

Write-Host ''
Write-Host '  知遇 · 多场景多难度自适应 AI 虚拟面试智能体' -ForegroundColor White
Write-Host '  ---------------------------------------------------' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path -LiteralPath $AppFile)) {
    Stop-WithError '当前目录下找不到 app.py。请把启动器和项目文件放在同一个文件夹里。'
}

# ---------- 1. 找 Python ----------
function Resolve-Python {
    $candidates = @(
        @{ Exe = 'py';     Args = @('-3.12') },
        @{ Exe = 'py';     Args = @('-3')    },
        @{ Exe = 'python'; Args = @()        }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $probe = @($c.Args) + @('-c', 'import sys;print(sys.version.split()[0])')
        try {
            $ver = & $c.Exe @probe 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                return @{ Exe = $c.Exe; Args = $c.Args; Version = ([string]$ver).Trim() }
            }
        } catch { }
    }
    return $null
}

Say '正在查找 Python ...'
$py = Resolve-Python
if (-not $py) {
    Stop-WithError '没有找到可用的 Python。请先安装 Python 3.12（安装时勾选 Add python.exe to PATH），再双击本启动器。'
}
Say ('已找到 Python ' + $py.Version) 'DarkGray'

# ---------- 2. 检查依赖 ----------
Say '正在检查依赖 ...'
$checkArgs = @($py.Args) + @('-c', 'import streamlit, openai, httpx, fitz, docx, edge_tts')
& $py.Exe @checkArgs 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say '依赖不完整，开始安装（首次约 1-3 分钟，请勿关闭窗口）...' 'Yellow'
    $installArgs = @($py.Args) + @('-m', 'pip', 'install', '-r', $ReqFile)
    & $py.Exe @installArgs
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError '依赖安装失败。请检查网络，或换国内镜像：pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple'
    }
    Say '依赖安装完成' 'Green'
} else {
    Say '依赖已就绪' 'DarkGray'
}

# ---------- 3. 选端口 ----------
function Test-AppHealthy([int]$Port) {
    try {
        $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $Port + '/_stcore/health') `
                               -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-PortFree([int]$Port) {
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch { return $false }
    finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

if (Test-AppHealthy $DefaultPort) {
    $existingUrl = 'http://127.0.0.1:' + $DefaultPort
    Say ('应用已经在运行，直接打开：' + $existingUrl) 'Green'
    Start-Process $existingUrl
    Start-Sleep -Seconds 2
    exit 0
}

$port = 0
for ($p = $DefaultPort; $p -le $PortMax; $p++) {
    if (Test-PortFree $p) { $port = $p; break }
}
if ($port -eq 0) {
    Stop-WithError ('端口 ' + $DefaultPort + '-' + $PortMax + ' 都被占用了。请关掉占用端口的程序后重试。')
}

# ---------- 4. 启动服务 ----------
$url = 'http://127.0.0.1:' + $port
Say ('正在启动服务（端口 ' + $port + '）...')

$portArg = '--server.port=' + $port
$streamlitArgs = @($py.Args) + @(
    '-m', 'streamlit', 'run', $AppFile,
    '--server.headless=true',
    '--server.address=127.0.0.1',
    $portArg,
    '--server.fileWatcherType=none',
    '--browser.gatherUsageStats=false'
)

$proc = Start-Process -FilePath $py.Exe -ArgumentList $streamlitArgs `
                      -WorkingDirectory $Root -NoNewWindow -PassThru

$deadline = (Get-Date).AddSeconds(90)
$healthy  = $false
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        Stop-WithError ('服务进程提前退出（退出码 ' + $proc.ExitCode + '），请看上方报错信息。')
    }
    if (Test-AppHealthy $port) { $healthy = $true; break }
    Start-Sleep -Milliseconds 400
}

if (-not $healthy) {
    try { $proc.Kill() } catch { }
    Stop-WithError '服务在 90 秒内没有就绪。可能是首次加载较慢，也可能是依赖损坏，请重试一次。'
}

# ---------- 5. 打开浏览器 ----------
Start-Process $url
Write-Host ''
Write-Host '  ===================================================' -ForegroundColor DarkGray
Write-Host ('   应用已启动： ' + $url) -ForegroundColor Green
Write-Host '   关闭本窗口即停止应用。' -ForegroundColor Yellow
Write-Host '  ===================================================' -ForegroundColor DarkGray
Write-Host ''

try { $proc.WaitForExit() } catch { }
Write-Host ''
Write-Host '  服务已停止。' -ForegroundColor DarkGray
