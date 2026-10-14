# TcpQuality-P2P | Windows PowerShell 5.1+ / PowerShell 7+
# GPL-3.0-only. No Python, administrator permission or third-party dependency
# required for the default TCP connect probe.
[CmdletBinding()]
param(
    [Parameter(Position = 0)] [string]$Target,
    [ValidateRange(1, 65535)] [int]$Port = 22,
    [ValidateRange(1, 10000)] [int]$Count = 30,
    [ValidateRange(0.0, 120.0)] [double]$Interval = 0.3,
    [ValidateRange(0.05, 120.0)] [double]$Timeout = 1.5,
    [switch]$IPv4,
    [switch]$IPv6,
    [switch]$Route,
    [switch]$Speed,
    [switch]$Full,
    [switch]$Quick,
    [ValidateRange(1, 65535)] [int]$IperfPort = 5201,
    [ValidateRange(1, 120)] [int]$Seconds = 8,
    [string]$MaxRate,
    [string]$Json,
    [string]$Csv,
    [switch]$Serve,
    [string]$Bind = '127.0.0.1',
    [ValidateRange(1, 65535)] [int]$ListenPort = 49222
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$Version = '0.1.1'

function Get-ResolvedAddress([string]$Name, [bool]$Only4, [bool]$Only6) {
    $addresses = [System.Net.Dns]::GetHostAddresses($Name)
    foreach ($address in $addresses) {
        if ($Only4 -and $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
        if ($Only6 -and $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetworkV6) { continue }
        if ($address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -or
            $address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) { return $address }
    }
    throw '找不到符合条件的 IPv4 / IPv6 地址。'
}

function Get-RootException([System.Exception]$Ex) {
    while ($Ex.InnerException) { $Ex = $Ex.InnerException }
    return $Ex
}

function Invoke-TcpProbe([System.Net.IPAddress]$Address, [int]$RemotePort, [double]$TimeoutSec) {
    $client = [System.Net.Sockets.TcpClient]::new($Address.AddressFamily)
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $handle = $null
    try {
        $async = $client.BeginConnect($Address, $RemotePort, $null, $null)
        $handle = $async.AsyncWaitHandle
        if (-not $handle.WaitOne([int][Math]::Ceiling($TimeoutSec * 1000))) {
            return [pscustomobject]@{ status = 'timeout'; rtt_ms = $null; details = '连接超时（可能是防火墙）' }
        }
        try {
            $client.EndConnect($async)
            $clock.Stop()
            return [pscustomobject]@{ status = 'ok'; rtt_ms = [Math]::Round($clock.Elapsed.TotalMilliseconds, 3); details = '' }
        }
        catch {
            $clock.Stop()
            $ex = Get-RootException $_.Exception
            if ($ex -is [System.Net.Sockets.SocketException] -and $ex.SocketErrorCode -eq [System.Net.Sockets.SocketError]::ConnectionRefused) {
                return [pscustomobject]@{ status = 'refused'; rtt_ms = [Math]::Round($clock.Elapsed.TotalMilliseconds, 3); details = '目标拒绝连接（TCP RST），不视作网络丢包' }
            }
            return [pscustomobject]@{ status = 'error'; rtt_ms = $null; details = $ex.Message }
        }
    }
    catch {
        $ex = Get-RootException $_.Exception
        return [pscustomobject]@{ status = 'error'; rtt_ms = $null; details = $ex.Message }
    }
    finally {
        if ($null -ne $handle) { $handle.Close() }
        $client.Close()
        $clock.Stop()
    }
}

function Get-Percentile([double[]]$Values, [double]$Fraction) {
    if ($Values.Count -eq 0) { return $null }
    $sorted = @($Values | Sort-Object)
    $index = ($sorted.Count - 1) * $Fraction
    $lo = [int][Math]::Floor($index)
    $hi = [int][Math]::Ceiling($index)
    $result = [double]$sorted[$lo] + ([double]$sorted[$hi] - [double]$sorted[$lo]) * ($index - $lo)
    return [Math]::Round($result, 3)
}

function Get-Summary([object[]]$Samples) {
    $values = @($Samples | Where-Object { $_.status -eq 'ok' -and $null -ne $_.rtt_ms } | ForEach-Object { [double]$_.rtt_ms })
    $refused = @($Samples | Where-Object { $_.status -eq 'refused' }).Count
    $timeouts = @($Samples | Where-Object { $_.status -eq 'timeout' }).Count
    $errors = @($Samples | Where-Object { $_.status -eq 'error' }).Count
    $output = [ordered]@{
        sent = $Samples.Count; received = $values.Count; refused = $refused
        timed_out = $timeouts; errors = $errors
        response_missing_pct = [Math]::Round(100.0 * ($Samples.Count - $values.Count) / $Samples.Count, 2)
        min_ms = $null; avg_ms = $null; p50_ms = $null; p95_ms = $null; max_ms = $null; jitter_ms = $null
    }
    if ($values.Count) {
        $output.min_ms = [Math]::Round([double](($values | Measure-Object -Minimum).Minimum), 3)
        $output.avg_ms = [Math]::Round([double](($values | Measure-Object -Average).Average), 3)
        $output.p50_ms = Get-Percentile $values 0.5
        $output.p95_ms = Get-Percentile $values 0.95
        $output.max_ms = [Math]::Round([double](($values | Measure-Object -Maximum).Maximum), 3)
        if ($values.Count -gt 1) {
            $delta = @()
            for ($j = 1; $j -lt $values.Count; $j++) { $delta += [Math]::Abs($values[$j] - $values[$j-1]) }
            $output.jitter_ms = [Math]::Round([double](($delta | Measure-Object -Average).Average), 3)
        }
    }
    return [pscustomobject]$output
}

function Invoke-Route([string]$Remote) {
    Write-Host "`n[2] 路由追踪（Windows ICMP tracert，非 TCP）"
    $rows = @(& tracert.exe -d -h 16 -w 500 $Remote 2>&1 | ForEach-Object { $_.ToString() })
    foreach ($row in $rows) { Write-Host $row }
    return [pscustomobject]@{ status = 'completed'; kind = 'ICMP'; output = ($rows -join "`n") }
}

function Invoke-Bandwidth([string]$Remote, [int]$PortNumber, [int]$TimeSeconds, [string]$Limit, [bool]$V6) {
    Write-Host "`n[3] iperf3 双向单线程带宽（目标需启动 iperf3 -s）"
    if (-not $Limit) { Write-Host '  注意：默认不限速，可能消耗较多 VPS 出站流量。可使用 -MaxRate 30M。' }
    $executable = Get-Command iperf3 -ErrorAction SilentlyContinue
    if (-not $executable) {
        Write-Host '  未发现 iperf3，已跳过。默认 TCP 测试不依赖 iperf3。'
        return [pscustomobject]@{ status = 'skipped'; reason = 'iperf3 not installed' }
    }
    $directions = [ordered]@{}
    foreach ($reverse in @($false, $true)) {
        $direction = if ($reverse) { '目标 -> 本机' } else { '本机 -> 目标' }
        $familyArg = if ($V6) { '-6' } else { '-4' }
        $arguments = @($familyArg, '-c', $Remote, '-p', [string]$PortNumber,
                       '-t', [string]$TimeSeconds, '-P', '1', '-J', '--connect-timeout', '3000')
        if ($reverse) { $arguments += '-R' }
        if ($Limit) { $arguments += @('-b', $Limit) }
        try {
            $output = & $executable.Source @arguments 2>&1 | Out-String
            $data = $output | ConvertFrom-Json
            $iperfError = $null
            if ($data.PSObject.Properties['error']) { $iperfError = $data.error }
            if ($LASTEXITCODE -ne 0 -or $iperfError) { throw "iperf3 无法完成连接/测试: $iperfError" }
            $bps = $data.end.sum_received.bits_per_second
            if ($null -eq $bps) { $bps = $data.end.sum_sent.bits_per_second }
            $retransmits = $null
            if ($data.end.sum_sent -and $data.end.sum_sent.PSObject.Properties['retransmits']) { $retransmits = $data.end.sum_sent.retransmits }
            $directionResult = [pscustomobject]@{
                status = 'ok'; mbps = if ($null -ne $bps) { [Math]::Round([double]$bps / 1000000, 3) } else { $null }
                retransmits_reported = $retransmits; rate_limit = $Limit
            }
            $directions[$direction] = $directionResult
            Write-Host "  ${direction}: $($directionResult.mbps) Mbps | 重传 $retransmits"
        }
        catch {
            $directions[$direction] = [pscustomobject]@{ status = 'error'; reason = $_.Exception.Message }
            Write-Host "  ${direction}: 跳过/失败 - $($_.Exception.Message)"
        }
    }
    return [pscustomobject]@{ status = 'completed'; directions = $directions; rate_limit = $Limit }
}

function Start-ProbeServer([string]$Binding, [int]$ListeningPort) {
    $address = [System.Net.IPAddress]::Parse($Binding)
    $listener = [System.Net.Sockets.TcpListener]::new($address, $ListeningPort)
    $listener.Start()
    Write-Host "TCP 探测服务已启动：${Binding}:${ListeningPort}。按 Ctrl+C 停止。"
    Write-Host '默认只绑定 127.0.0.1。要让其他机器访问，请显式指定 -Bind 为 Tailscale IP。'
    Write-Host '带宽测试仍需另外启动 iperf3 -s。'
    try {
        while ($true) {
            $client = $listener.AcceptTcpClient()
            try { $client.Close() } catch { }
        }
    }
    finally { $listener.Stop() }
}

if ($Serve) {
    Start-ProbeServer $Bind $ListenPort
    return
}
if ([string]::IsNullOrWhiteSpace($Target)) { throw '请指定目标 IP。例如：& ([scriptblock]::Create((irm URL))) -Target 1.2.3.4' }
if ($IPv4 -and $IPv6) { throw '-IPv4 与 -IPv6 不能同时指定。' }
if ($Full -and $Quick) { throw '-Full 与 -Quick 不能同时指定。' }
if ($Full) { $Route = $true; $Speed = $true }
elseif (-not $Quick) { $Route = $true }
$addr = Get-ResolvedAddress $Target $IPv4.IsPresent $IPv6.IsPresent
$ip = $addr.ToString()
$familyName = if ($addr.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) { 'IPv6' } else { 'IPv4' }
Write-Host "TcpQuality-P2P v$Version | 本机 -> $Target ($ip) | $familyName"
Write-Host "`n[1] TCP connect 测试 | ${ip}:${Port} | 共 $Count 次"
$samples = @()
for ($i = 1; $i -le $Count; $i++) {
    $iteration = [System.Diagnostics.Stopwatch]::StartNew()
    $probe = Invoke-TcpProbe $addr $Port $Timeout
    $samples += [pscustomobject]@{
        index = $i; timestamp = [DateTime]::UtcNow.ToString('o')
        status = $probe.status; rtt_ms = $probe.rtt_ms; details = $probe.details
    }
    if ($i -le 5 -or $i -eq $Count -or $i % 10 -eq 0 -or $probe.status -ne 'ok') {
        $ms = if ($null -ne $probe.rtt_ms) { '{0:N2} ms' -f $probe.rtt_ms } else { '-' }
        Write-Host ('  #{0,3} {1,-8} {2}' -f $i, $probe.status, $ms)
    }
    $iteration.Stop()
    $remaining = [Math]::Max(0.0, $Interval * 1000 - $iteration.Elapsed.TotalMilliseconds)
    if ($i -lt $Count -and $remaining -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Ceiling($remaining)) }
}
$stats = Get-Summary $samples
Write-Host "  响应 $($stats.received)/$($stats.sent) | 无有效响应 $($stats.response_missing_pct)% | 端口拒绝 $($stats.refused) | 超时 $($stats.timed_out)"
Write-Host "  RTT min/avg/p50/p95/max: $($stats.min_ms)/$($stats.avg_ms)/$($stats.p50_ms)/$($stats.p95_ms)/$($stats.max_ms) ms | 抖动 $($stats.jitter_ms) ms"
Write-Host '  注：此处是 TCP 连接成功率，不能直接解释为 IP 层丢包率。'

$report = [ordered]@{
    tool = 'TcpQuality-P2P'; version = $Version
    time_utc = [DateTime]::UtcNow.ToString('o'); source = $env:COMPUTERNAME
    target = $Target; resolved_ip = $ip; family = [string]$addr.AddressFamily
    tcp_port = $Port; probe_mode = 'connect'; stats = $stats; samples = $samples
}
if ($Route) {
    try { $report.route = Invoke-Route $ip }
    catch { $report.route = [pscustomobject]@{ status = 'error'; reason = $_.Exception.Message } }
}
if ($Speed) {
    $report.speed = Invoke-Bandwidth $ip $IperfPort $Seconds $MaxRate ($addr.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6)
}
else {
    Write-Host "`n[3] 双向带宽：未执行（为避免意外耗费流量，需显式添加 -Full 或 -Speed）"
    Write-Host '  需要本机安装 iperf3，目标运行 iperf3 -s，并允许连接目标 TCP 5201。'
}
if ($Json) {
    $jsonText = $report | ConvertTo-Json -Depth 10
    $jsonFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Json)
    $parent = Split-Path -Parent $jsonFile
    if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [System.IO.File]::WriteAllText($jsonFile, $jsonText, [System.Text.UTF8Encoding]::new($false))
    Write-Host "JSON: $jsonFile"
}
if ($Csv) {
    $csvFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Csv)
    $parent = Split-Path -Parent $csvFile
    if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    $samples | Export-Csv -LiteralPath $csvFile -NoTypeInformation -Encoding UTF8
    Write-Host "CSV: $csvFile"
}