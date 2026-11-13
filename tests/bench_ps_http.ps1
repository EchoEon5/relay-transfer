
$ErrorActionPreference = 'Stop'
$port = 8097
$sizeMB = 256
$tmp = Join-Path $env:TEMP ('bench_' + (Get-Date -Format 'HHmmss'))
$null = New-Item -ItemType Directory -Path $tmp -Force
$file = Join-Path $tmp 'payload.bin'

Write-Host ("准备 {0} MB 测试文件…" -f $sizeMB) -ForegroundColor Cyan
$buf = New-Object byte[] (1MB)
$rnd = [System.Random]::new(42)
$rnd.NextBytes($buf)
$fs = [IO.File]::Create($file)
for ($i = 0; $i -lt $sizeMB; $i++) { $fs.Write($buf, 0, $buf.Length) }
$fs.Close()
$len = (Get-Item $file).Length
Write-Host ("文件大小: {0:N0} 字节" -f $len)

$serverScript = {
    param($Port, $FilePath, $Length, $MaxReq)
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    $listener.Server.SetSocketOption([System.Net.Sockets.SocketOptionLevel]::Socket,
                                     [System.Net.Sockets.SocketOptionName]::ReuseAddress, $true)
    $listener.Start()
    for ($n = 0; $n -lt $MaxReq; $n++) {
        try {
            $client = $listener.AcceptTcpClient()
        } catch { break }
        try {
            $client.NoDelay = $true
            $stream = $client.GetStream()
           
            $sr = New-Object System.IO.StreamReader($stream, [Text.Encoding]::ASCII, $false, 8192, $true)
            while ($true) {
                $line = $sr.ReadLine()
                if ($null -eq $line -or $line -eq '') { break }
            }
            $header = "HTTP/1.1 200 OK`r`nContent-Length: $Length`r`nContent-Type: application/octet-stream`r`nConnection: close`r`n`r`n"
            $hb = [Text.Encoding]::ASCII.GetBytes($header)
            $stream.Write($hb, 0, $hb.Length)
            $src = [IO.File]::Open($FilePath, 'Open', 'Read', 'ReadWrite')
            $src.CopyTo($stream, 1048576)
            $src.Close()
            $stream.Flush()
        } catch {
        } finally {
            $client.Close()
        }
    }
    $listener.Stop()
}

$job = Start-Job -ScriptBlock $serverScript -ArgumentList $port, $file, $len, 6

$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 150
    try {
        $t = [System.Net.Sockets.TcpClient]::new()
        $t.Connect('127.0.0.1', $port)
        $t.Close()
        $ready = $true
        break
    } catch {}
}
if (-not $ready) { Write-Host '服务端没起来' -ForegroundColor Red; Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue; Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue; exit 1 }
Write-Host '服务端就绪' -ForegroundColor Green
Write-Host ''

$times = @()
for ($i = 1; $i -le 3; $i++) {
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$port/x")
    $req.Timeout = 300000
    $req.ReadWriteTimeout = 300000
    $req.Proxy = $null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = $req.GetResponse()
    $st = $resp.GetResponseStream()
    $sink = New-Object byte[] (1MB)
    $total = 0L
    while ($true) {
        $n = $st.Read($sink, 0, $sink.Length)
        if ($n -le 0) { break }
        $total += $n
    }
    $sw.Stop()
    $st.Close(); $resp.Close()
    $sec = $sw.Elapsed.TotalSeconds
    $mbs = ($total / 1MB) / $sec
    $times += $mbs
    Write-Host ("  第 {0} 次: {1:N0} 字节 / {2:N2} 秒 = {3:N1} MB/s" -f $i, $total, $sec, $mbs)
}

Stop-Job $job -ErrorAction SilentlyContinue
Remove-Job $job -Force -ErrorAction SilentlyContinue
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

$avg = ($times | Measure-Object -Average).Average
Write-Host ''
Write-Host ("平均单连接吞吐: {0:N1} MB/s  (约 {1:N0} Mbps)" -f $avg, ($avg * 8)) -ForegroundColor Yellow
Write-Host ''
Write-Host '参照：' -ForegroundColor DarkGray
Write-Host '  100 Mbps 网卡 ≈ 12 MB/s' -ForegroundColor DarkGray
Write-Host '  1 Gbps 网卡   ≈ 125 MB/s' -ForegroundColor DarkGray
