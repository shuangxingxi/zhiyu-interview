# 知遇 · 让别人打开你这台机器上的面试页
#
# 两种方式：
#   局域网（同一 WiFi / 同一路由器）   .\分享给别人.ps1 -Mode lan
#   公网临时地址（任何人、任何网络）   .\分享给别人.ps1 -Mode public
#
# 关闭本窗口 = 停止分享（服务与隧道一起结束）

param(
    [ValidateSet('lan', 'public')]
    [string]$Mode = 'lan',
    [int]$Port = 8501
)

$ErrorActionPreference = 'Continue'
# 不要动 [Console]::OutputEncoding：本文件是 UTF-8 with BOM，PowerShell 读源码没问题，
# 输出时按控制台代码页（936）转码才正常。强行设成 UTF8 会让中文全变乱码。
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$AppFile = Join-Path $Root 'app.py'

function Say([string]$Text, [string]$Color = 'Gray') { Write-Host "  $Text" -ForegroundColor $Color }
function Stop-WithError([string]$Message) {
    Write-Host ''
    Write-Host "  [失败] $Message" -ForegroundColor Red
    Write-Host ''
    if (-not [Console]::IsInputRedirected) { Read-Host '  按回车键关闭' }
    exit 1
}

Write-Host ''
Write-Host '  知遇 · 分享面试页' -ForegroundColor White
Write-Host '  ---------------------------------------------------' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path -LiteralPath $AppFile)) { Stop-WithError '当前目录下找不到 app.py。' }

# ---------- 1. 找 Python ----------
$py = $null
foreach ($c in @(@{Exe='py';Args=@('-3.12')}, @{Exe='py';Args=@('-3')}, @{Exe='python';Args=@()})) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    # 注意：探测命令里不能出现双引号——py 启动器会把引号吃掉，print("ok") 变成 print(ok) 直接报错
    $probe = @($c.Args) + @('-c', 'import streamlit')
    & $c.Exe @probe *> $null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) { Stop-WithError '没有可用的 Python（或依赖未装）。先双击一次「启动知遇面试.bat」把依赖装好。' }

# ---------- 2. 端口 ----------
function Test-PortFree([int]$P) {
    $l = $null
    try { $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $P); $l.Start(); return $true }
    catch { return $false }
    finally { if ($l) { try { $l.Stop() } catch { } } }
}
function Test-Healthy([int]$P) {
    try { $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $P + '/_stcore/health') -TimeoutSec 2 -UseBasicParsing; return ($r.StatusCode -eq 200) }
    catch { return $false }
}
function Test-ListeningOnAll([int]$P) {
    # 判断端口是不是绑在 0.0.0.0 / :: 上（也就是对外可达）
    try {
        $lines = @(netstat -ano | Select-String (':' + $P + '\s') | Select-String 'LISTENING')
        foreach ($l in $lines) {
            $t = $l.ToString().Trim()
            if ($t -match '^(TCP|TCPv6)\s+(0\.0\.0\.0|\[::\]):' + $P + '\s') { return $true }
        }
    } catch { }
    return $false
}

if (Test-Healthy $Port) {
    # 复用的前提：这份服务确实对外可达。只挂 127.0.0.1 的话别人连不上，
    # 必须让用户先关掉原来那个黑窗口，由本脚本用 0.0.0.0 重新起。
    if ($Mode -eq 'lan' -and -not (Test-ListeningOnAll $Port)) {
        Stop-WithError ('端口 ' + $Port + ' 上那份服务只监听本机（127.0.0.1），别人连不上。' + "`n" +
                        '  先把原来那个「知遇」黑窗口关掉，再运行本脚本。')
    }
    Say ('端口 ' + $Port + ' 上已经有一份服务在跑了，直接复用。') 'Green'
} else {
    if (-not (Test-PortFree $Port)) { Stop-WithError ('端口 ' + $Port + ' 被别的程序占用。换一个：.\分享给别人.ps1 -Mode ' + $Mode + ' -Port 8600') }

    # 对外分享必须听 0.0.0.0（局域网）或 127.0.0.1（隧道自己会连本机）
    $bind = if ($Mode -eq 'lan') { '0.0.0.0' } else { '127.0.0.1' }
    Say ('正在启动服务（监听 ' + $bind + ':' + $Port + '）...')
    # 注意：数组字面量里 '--k=' + $v 会被拆成两个元素，必须先算好再放进数组
    $argAddr = '--server.address=' + $bind
    $argPort = '--server.port=' + $Port
    $streamlitArgs = @($py.Args) + @(
        '-m', 'streamlit', 'run', $AppFile,
        '--server.headless=true',
        $argAddr,
        $argPort,
        '--server.fileWatcherType=none',
        '--browser.gatherUsageStats=false'
    )
    $proc = Start-Process -FilePath $py.Exe -ArgumentList $streamlitArgs -WorkingDirectory $Root -NoNewWindow -PassThru
}

$deadline = (Get-Date).AddSeconds(90)
while (-not (Test-Healthy $Port)) {
    if ($proc -and $proc.HasExited) { Stop-WithError ('服务进程提前退出（退出码 ' + $proc.ExitCode + '），看上面的报错。') }
    if ((Get-Date) -gt $deadline) { Stop-WithError '服务 90 秒内没就绪。' }
    Start-Sleep -Milliseconds 400
}

# ---------- 3. 生成地址 ----------
$tunnel = $null
if ($Mode -eq 'lan') {
    # 只报「真正通外网」那块网卡的地址。单纯按网卡别名排除不够：
    # WSL / VPN / 小米互联这类虚拟网卡也会带个 172.x 或 192.168.x.1 的地址，
    # 报出去对方连不上。有默认路由的接口才算数。
    $virtualPattern = 'VMware|VirtualBox|Hyper-V|WSL|vEthernet|Loopback|TAP|TUN|Docker|Bluetooth|Tailscale|Npcap'
    $gatewayIdx = @()
    try {
        $gatewayIdx = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
                        Sort-Object RouteMetric |
                        Select-Object -ExpandProperty InterfaceIndex -Unique)
    } catch { }

    $ips = @()
    try {
        $candidates = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
                        Where-Object {
                            $_.IPAddress -notlike '127.*' -and
                            $_.IPAddress -notlike '169.254.*' -and
                            $_.PrefixOrigin -ne 'WellKnown' -and
                            $_.InterfaceAlias -notmatch $virtualPattern
                        })
        $withGw = @($candidates | Where-Object { $gatewayIdx -contains $_.InterfaceIndex })
        $picked = if ($withGw.Count -gt 0) { $withGw } else { $candidates }
        $ips = @($picked | Select-Object -ExpandProperty IPAddress)
    } catch { }
    if (-not $ips) {
        # 兜底：老的写法，至少能拿到一个
        $ips = @(ipconfig | Select-String 'IPv4' | ForEach-Object { ($_ -split ':')[-1].Trim() } | Where-Object { $_ -and $_ -notlike '127.*' })
    }
    $ips = @($ips | Where-Object { $_ } | Select-Object -Unique)
    if (-not $ips) { Stop-WithError '没读到本机内网 IP，检查网络连接。' }

    Write-Host ''
    Write-Host '  ===================================================' -ForegroundColor DarkGray
    Write-Host '   同一 WiFi 下，把下面地址发给对方：' -ForegroundColor Green
    foreach ($ip in $ips) { Write-Host ('     http://' + $ip + ':' + $Port) -ForegroundColor Cyan }
    Write-Host '  ===================================================' -ForegroundColor DarkGray
    Write-Host ''
    Say '手机连同一个 WiFi，浏览器直接输这个地址就能开工。' 'DarkGray'
    Say '对方打不开？多半是 Windows 防火墙拦了。管理员 PowerShell 里跑一次：' 'Yellow'
    Write-Host ('     netsh advfirewall firewall add rule name="ZhiYu ' + $Port + '" dir=in action=allow protocol=TCP localport=' + $Port) -ForegroundColor DarkGray
    Say '校园网 / 公司网常开了「客户端隔离」，同一 WiFi 也互相访问不到——这种情况改用公网模式。' 'DarkGray'
} else {
    $cf = Join-Path $Root '.tools\cloudflared.exe'
    if (-not (Test-Path -LiteralPath $cf)) { Stop-WithError '.tools\cloudflared.exe 不存在，无法开公网地址。' }

    $log = Join-Path $env:TEMP ('zhiyu-tunnel-' + [guid]::NewGuid().ToString('N') + '.log')
    Say '正在开公网隧道（约 10 秒）...'
    # --protocol http2 是必须的：默认优先 QUIC(UDP)，校园网 / 公司网常把 UDP 限速或丢包，
    # 表现就是隧道地址建出来了、拿到的却一直是 530。退回 TCP 才稳。
    $tunnel = Start-Process -FilePath $cf -ArgumentList @('tunnel', '--url', ('http://127.0.0.1:' + $Port), '--protocol', 'http2', '--no-autoupdate') `
                            -RedirectStandardError $log -RedirectStandardOutput ($log + '.out') -WindowStyle Hidden -PassThru

    $url = $null
    $deadline = (Get-Date).AddSeconds(40)
    while (-not $url -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $log) {
            $m = [regex]::Match((Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue), 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($m.Success) { $url = $m.Value }
        }
    }
    if (-not $url) {
        try { $tunnel.Kill() } catch { }
        Stop-WithError '隧道没拿到地址，检查能否访问外网（cloudflare）。'
    }

    # 地址建出来不等于能访问：DNS 还没生效、边缘节点没连上时会是 530。
    # 必须真的等到健康检查通过再把链接给出去，否则对方看到的是 530 页面。
    Say '等公网地址生效...'
    $ready = $false
    $deadline = (Get-Date).AddSeconds(60)
    while (-not $ready -and (Get-Date) -lt $deadline) {
        if ($tunnel.HasExited) { Stop-WithError ('隧道进程退出了（退出码 ' + $tunnel.ExitCode + '），日志：' + $log) }
        try {
            $r = Invoke-WebRequest -Uri ($url + '/_stcore/health') -TimeoutSec 8 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        try { $tunnel.Kill() } catch { }
        Stop-WithError ('公网地址 60 秒内没生效（对方会看到 530）。' + "`n" +
                        '  常见原因：这个网络封了 cloudflare 的出站。改用局域网模式，或换手机热点再试。')
    }

    Write-Host ''
    Write-Host '  ===================================================' -ForegroundColor DarkGray
    Write-Host '   把这个链接发给任何人（手机、4G、外地都行）：' -ForegroundColor Green
    Write-Host ('     ' + $url) -ForegroundColor Cyan
    Write-Host '   临时地址，本窗口一关就失效；下次启动会换一个新的。' -ForegroundColor Yellow
    Write-Host '  ===================================================' -ForegroundColor DarkGray
}

Write-Host ''
Say '关闭本窗口 = 停止服务（和隧道）。' 'Yellow'
Write-Host ''

try {
    if ($proc) { $proc.WaitForExit() }
    else { while ($true) { Start-Sleep -Seconds 5 } }
} finally {
    if ($tunnel -and -not $tunnel.HasExited) { try { $tunnel.Kill() } catch { } }
}
Say '已停止。' 'DarkGray'
