
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$agent = Join-Path (Split-Path -Parent $here) 'client\agent.ps1'

. $agent     

$tmp = Join-Path $env:TEMP ('v6test_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

$pass = 0
$fail = 0
function Check($name, $cond, $extra) {
    if ($cond) { $script:pass++; Write-Host ('  [PASS] ' + $name) -ForegroundColor Green }
    else { $script:fail++; Write-Host ('  [FAIL] ' + $name + '  ' + $extra) -ForegroundColor Red }
}

Write-Host ''
Write-Host ('=' * 70)
Write-Host '  IPv6 直连文件服务测试'
Write-Host ('=' * 70)

$f1 = Join-Path $tmp 'small.txt'
[IO.File]::WriteAllText($f1, 'hello 中文内容 0123456789', [Text.UTF8Encoding]::new($false))
$f1len = (Get-Item $f1).Length

$f2 = Join-Path $tmp 'big.bin'
$rb = New-Object byte[] 5242880     
$rnd = New-Object Random 12345
$rnd.NextBytes($rb)
[IO.File]::WriteAllBytes($f2, $rb)

$f3 = Join-Path $tmp '中文名字 文件.dat'
[IO.File]::WriteAllBytes($f3, [byte[]](1..200))

$tok = 'tok123abc'
$res = Start-V6Serve -Files @(
    @{ id = 'a1'; token = $tok; path = $f1; name = 'small.txt' },
    @{ id = 'b2'; token = $tok; path = $f2; name = 'big.bin' },
    @{ id = 'c3'; token = $tok; path = $f3; name = '中文名字 文件.dat' }
) -Port 0

Check '服务启动成功' $res.ok $res.error
Check '分配到端口' ($res.port -gt 0) $res.port
Check '登记 3 个文件' ($res.files -eq 3) $res.files
Write-Host ('     端口: ' + $res.port + '   报告地址: ' + (@($res.addrs) -join ', '))

$base = 'http://[::1]:' + $res.port

try {
    $r = Invoke-WebRequest -Uri ($base + '/ping') -UseBasicParsing -TimeoutSec 10
    Check 'ping 返回 200' ($r.StatusCode -eq 200) $r.StatusCode
    Check 'ping 内容为 pong' ($r.Content -match 'pong') $r.Content
    Check 'ping 带 CORS 头' ($r.Headers['Access-Control-Allow-Origin'] -eq '*') $r.Headers['Access-Control-Allow-Origin']
} catch { Check 'ping 返回 200' $false $_.Exception.Message }

try {
    $out1 = Join-Path $tmp 'dl1.txt'
    Invoke-WebRequest -Uri ($base + '/f/' + $tok + '/a1') -OutFile $out1 -UseBasicParsing -TimeoutSec 20
    $h1 = (Get-FileHash $f1 -Algorithm SHA256).Hash
    $h2 = (Get-FileHash $out1 -Algorithm SHA256).Hash
    Check '完整下载字节一致' ($h1 -eq $h2) "源 $h1 / 下载 $h2"
} catch { Check '完整下载字节一致' $false $_.Exception.Message }

try {
    $out2 = Join-Path $tmp 'dl2.bin'
    Invoke-WebRequest -Uri ($base + '/f/' + $tok + '/b2') -OutFile $out2 -UseBasicParsing -TimeoutSec 120
    Check '5MB 下载字节一致' ((Get-FileHash $f2).Hash -eq (Get-FileHash $out2).Hash) ''
} catch { Check '5MB 下载字节一致' $false $_.Exception.Message }

try {
    $req = [Net.HttpWebRequest]::Create($base + '/f/' + $tok + '/b2')
    $req.AddRange(1000000, 1999999)
    $req.Timeout = 30000
    $resp = $req.GetResponse()
    $ms = New-Object IO.MemoryStream
    $resp.GetResponseStream().CopyTo($ms)
    $resp.Close()
    $got = $ms.ToArray()
    $expect = $rb[1000000..1999999]
    $same = ($got.Length -eq $expect.Length)
    if ($same) {
        for ($i = 0; $i -lt $got.Length; $i++) { if ($got[$i] -ne $expect[$i]) { $same = $false; break } }
    }
    Check 'Range 返回 206' ($resp.StatusCode -eq [Net.HttpStatusCode]::PartialContent) $resp.StatusCode
    Check 'Range 长度正确 (1000000 字节)' ($got.Length -eq 1000000) $got.Length
    Check 'Range 内容正确' $same ''
    Check 'Content-Range 头正确' ($resp.Headers['Content-Range'] -eq ('bytes 1000000-1999999/' + $rb.Length)) $resp.Headers['Content-Range']
} catch { Write-Host ('     ' + $_.Exception.Message); Check 'Range 返回 206' $false $_.Exception.Message }

try {
    $code = & curl.exe -s -o NUL -w '%{http_code}' -r '999999-1000000' ($base + '/f/' + $tok + '/a1') 2>$null
    Check '越界 Range 返回 416' ($code -eq '416') ('实际 ' + $code)
} catch { Check '越界 Range 返回 416' $false $_.Exception.Message }

try {
    $r = Invoke-WebRequest -Uri ($base + '/f/wrongtoken/a1') -UseBasicParsing -TimeoutSec 10
    Check '错误令牌返回 403' $false ('返回 ' + $r.StatusCode)
} catch {
    $sc = 0
    if ($_.Exception.Response) { $sc = [int]$_.Exception.Response.StatusCode }
    Check '错误令牌返回 403' ($sc -eq 403) ('实际 ' + $sc)
}

try {
    $r = Invoke-WebRequest -Uri ($base + '/f/' + $tok + '/nope') -UseBasicParsing -TimeoutSec 10
    Check '未知 id 返回 404' $false ('返回 ' + $r.StatusCode)
} catch {
    $sc = 0
    if ($_.Exception.Response) { $sc = [int]$_.Exception.Response.StatusCode }
    Check '未知 id 返回 404' ($sc -eq 404) ('实际 ' + $sc)
}

try {
    $r = Invoke-WebRequest -Uri ($base + '/f/' + $tok + '/c3') -UseBasicParsing -TimeoutSec 20
    $cd = $r.Headers['Content-Disposition']
    Check '中文名走 RFC5987' ($cd -match "filename\*=UTF-8''") $cd
} catch { Check '中文名走 RFC5987' $false $_.Exception.Message }

try {
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { throw '找不到 curl.exe' }
    $procs = @()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    for ($i = 0; $i -lt 8; $i++) {
        $outf = Join-Path $tmp ("p_$i.bin")
        $procs += Start-Process -FilePath 'curl.exe' -PassThru -NoNewWindow -ArgumentList @(
            '-s', '-o', $outf, '-r', '0-1048575', ($base + '/f/' + $tok + '/b2'))
    }
    $procs | ForEach-Object { $_.WaitForExit(120000) | Out-Null }
    $sw.Stop()
    $exp = $rb[0..1048575]
    $okCount = 0
    for ($i = 0; $i -lt 8; $i++) {
        $outf = Join-Path $tmp ("p_$i.bin")
        if (-not (Test-Path $outf)) { continue }
        $got = [IO.File]::ReadAllBytes($outf)
        $same = ($got.Length -eq $exp.Length)
        if ($same) { for ($j = 0; $j -lt $got.Length; $j++) { if ($got[$j] -ne $exp[$j]) { $same = $false; break } } }
        if ($same) { $okCount++ }
    }
    Check '8 路并发内容全部正确' ($okCount -eq 8) ("正确 $okCount / 8")
    Write-Host ('     8 × 1MB 并发耗时: ' + [math]::Round($sw.Elapsed.TotalSeconds, 2) + ' 秒')
} catch { Check '8 路并发内容全部正确' $false $_.Exception.Message }

$st = Get-V6Status
Check '状态显示运行中' $st.running ''
Check '状态里登记 3 个文件' ($st.files -eq 3) $st.files
Check '已统计命中次数' ($st.hits -ge 10) $st.hits
Write-Host ('     累计下发: ' + [math]::Round($st.served / 1MB, 2) + ' MB, 命中 ' + $st.hits + ' 次')

Stop-V6Serve
$st2 = Get-V6Status
Check '停止后不再监听' (-not $st2.running) ''
try {
    Invoke-WebRequest -Uri ($base + '/ping') -UseBasicParsing -TimeoutSec 3 | Out-Null
    Check '停止后连接被拒' $false '仍能连上'
} catch { Check '停止后连接被拒' $true '' }

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host ('=' * 70)
Write-Host ('结果：通过 ' + $pass + ' / 失败 ' + $fail) -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Red' })
Write-Host ('=' * 70)
if ($fail -gt 0) { exit 1 }
