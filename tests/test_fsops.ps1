
$ErrorActionPreference = 'Stop'
$agent = Join-Path (Split-Path -Parent $PSScriptRoot) 'client\agent.ps1'
$text = [IO.File]::ReadAllText($agent, [Text.Encoding]::UTF8)

$startMark = '#region 设备信息与枚举'
$endMark = '#region OSS 上传'
$i = $text.IndexOf($startMark)
$j = $text.IndexOf($endMark)
if ($i -lt 0 -or $j -lt 0 -or $j -le $i) { throw '抽取失败' }
$block = $text.Substring($i, $j - $i)

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:FsCount = 0
$script:StatusFile = Join-Path $env:TEMP 'fstest_status.txt'
$script:LastStatusText = ''
function Set-Status { param([string]$Line1 = '', [string]$Line2 = '', [string]$Line3 = '', [string]$Line4 = '') }

. ([scriptblock]::Create($block))

$root = Join-Path $env:TEMP ('fstest_' + (Get-Date -Format 'HHmmss'))
$null = New-Item -ItemType Directory -Path $root -Force
Write-Host ("测试根目录: {0}" -f $root) -ForegroundColor Cyan
Write-Host ''

$pass = 0; $fail = 0
function Check([string]$name, [scriptblock]$body) {
    try {
        $r = & $body
        if ($r -eq $true) { Write-Host ("  [PASS] {0}" -f $name) -ForegroundColor Green; $script:pass++ }
        else { Write-Host ("  [FAIL] {0} -> {1}" -f $name, $r) -ForegroundColor Red; $script:fail++ }
    } catch {
        Write-Host ("  [FAIL] {0} -> 异常 {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
        $script:fail++
    }
}

$d1 = Join-Path $root '新建目录'
Check 'mkdir 新建文件夹' {
    $r = New-DirItem ([pscustomobject]@{ path = $d1 })
    ($r.path -eq $d1) -and [IO.Directory]::Exists($d1)
}
Check 'mkdir 重名应报错' {
    try { New-DirItem ([pscustomobject]@{ path = $d1 }) | Out-Null; '没有报错' }
    catch { if ($_.Exception.Message -like '*同名*') { $true } else { $_.Exception.Message } }
}
Check 'mkdir 上级不存在应报错' {
    try { New-DirItem ([pscustomobject]@{ path = (Join-Path $root 'no\such\x') }) | Out-Null; '没有报错' }
    catch { if ($_.Exception.Message -like '*上级*') { $true } else { $_.Exception.Message } }
}

$f1 = Join-Path $root '说明.txt'
Check 'newfile 带内容' {
    $r = New-FileItem ([pscustomobject]@{ path = $f1; text = '你好，信息传输' })
    ([IO.File]::ReadAllText($f1, [Text.Encoding]::UTF8) -eq '你好，信息传输')
}
Check 'newfile 空文件' {
    $f = Join-Path $root '空.bin'
    $null = New-FileItem ([pscustomobject]@{ path = $f })
    ([IO.File]::Exists($f)) -and ((Get-Item $f).Length -eq 0)
}

Check 'rename 文件' {
    $r = Rename-FsItem ([pscustomobject]@{ path = $f1; name = '改名后.txt' })
    ([IO.File]::Exists((Join-Path $root '改名后.txt'))) -and (-not [IO.File]::Exists($f1))
}
Check 'rename 非法字符应报错' {
    try { Rename-FsItem ([pscustomobject]@{ path = (Join-Path $root '改名后.txt'); name = 'a*b' }) | Out-Null; '没有报错' }
    catch { if ($_.Exception.Message -like '*不能包含*') { $true } else { $_.Exception.Message } }
}
Check 'rename 目录' {
    $r = Rename-FsItem ([pscustomobject]@{ path = $d1; name = '改名后目录' })
    [IO.Directory]::Exists((Join-Path $root '改名后目录'))
}

$src = Join-Path $root '源目录'
$null = New-Item -ItemType Directory -Path (Join-Path $src 'sub') -Force
[IO.File]::WriteAllText((Join-Path $src 'a.txt'), 'AAA')
[IO.File]::WriteAllText((Join-Path $src 'sub\b.txt'), 'BBB')
$null = New-Item -ItemType Directory -Path (Join-Path $root '目标') -Force
Check 'copy 目录（含子目录）' {
    $r = Copy-FsItems ([pscustomobject]@{ paths = @($src); dest = (Join-Path $root '目标') })
    $t = Join-Path $root '目标\源目录'
    ([IO.File]::Exists((Join-Path $t 'a.txt'))) -and
    ([IO.File]::Exists((Join-Path $t 'sub\b.txt'))) -and
    ($r.failed.Count -eq 0)
}
Check 'copy 到自身子目录应报错' {
    try {
        Copy-FsItems ([pscustomobject]@{ paths = @($src); dest = (Join-Path $src 'sub') }) | Out-Null
        '没有报错'
    } catch { 'ok' }
   
    $r = Copy-FsItems ([pscustomobject]@{ paths = @($src); dest = (Join-Path $src 'sub') })
    (@($r.failed).Count -gt 0) -and (@($r.failed)[0] -like '*自身*')
}

Check 'move 文件跨目录' {
    $m = Join-Path $root '待移动.txt'
    [IO.File]::WriteAllText($m, 'MOVE')
    $r = Move-FsItems ([pscustomobject]@{ paths = @($m); dest = (Join-Path $root '目标') })
    ([IO.File]::Exists((Join-Path $root '目标\待移动.txt'))) -and (-not [IO.File]::Exists($m))
}

Check 'delete 文件' {
    $x = Join-Path $root '待删除.txt'
    [IO.File]::WriteAllText($x, 'DEL')
    $r = Remove-FsItems ([pscustomobject]@{ paths = @($x) })
    (-not [IO.File]::Exists($x)) -and ($r.done -eq 1)
}
Check 'delete 含只读文件的目录' {
    $dd = Join-Path $root '只读目录'
    $null = New-Item -ItemType Directory -Path $dd -Force
    $ro = Join-Path $dd 'ro.txt'
    [IO.File]::WriteAllText($ro, 'RO')
    [IO.File]::SetAttributes($ro, [IO.FileAttributes]::ReadOnly)
    $r = Remove-FsItems ([pscustomobject]@{ paths = @($dd) })
    (-not [IO.Directory]::Exists($dd)) -and (@($r.failed).Count -eq 0)
}
Check 'delete 盘符根应被拒绝' {
    $r = Remove-FsItems ([pscustomobject]@{ paths = @('C:\') })
    (@($r.failed).Count -eq 1) -and (@($r.failed)[0] -like '*不允许*')
}
Check 'delete 系统目录应被拒绝' {
    $r = Remove-FsItems ([pscustomobject]@{ paths = @($env:SystemRoot) })
    (@($r.failed).Count -eq 1) -and (@($r.failed)[0] -like '*不允许*')
}

Check 'exists 判断文件/目录' {
    $a = Get-FsPathInfo ([pscustomobject]@{ path = $src })
    $b = Get-FsPathInfo ([pscustomobject]@{ path = (Join-Path $root '不存在的') })
    ($a.exists -and $a.is_dir) -and (-not $b.exists)
}

Check 'list 普通目录' {
    $r = Get-DirList $root
    ($r.denied -eq $false) -and (@($r.entries).Count -gt 0)
}
Check 'list 受限目录返回 denied 而不是抛错' {
    $r = Get-DirList 'C:\System Volume Information'
    ($r.denied -eq $true) -and ($r.denied_reason -ne '') -and ($r.path -ne '')
}
Check 'list 盘符根保留尾反斜杠' {
    $r = Get-DirList 'C:'
    ($r.path -eq 'C:\')
}
Check 'list 不存在路径应报错' {
    try { Get-DirList 'C:\这个绝对不存在的目录_zzz' | Out-Null; '没有报错' }
    catch { if ($_.Exception.Message -like '*不存在*') { $true } else { $_.Exception.Message } }
}

Write-Host ''
Write-Host ("结果：通过 {0} / 失败 {1}" -f $pass, $fail) -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Red' })
Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
exit $(if ($fail -eq 0) { 0 } else { 1 })
