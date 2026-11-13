@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions DisableDelayedExpansion
title Generate Transfer Assistant

rem ============================================================
rem  Reads the config text file and builds the transfer helper.
rem  Pure PowerShell, no Python or other dependency needed.
rem ============================================================

set "GENPS=%TEMP%\gen_transfer_%RANDOM%.ps1"
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

echo.
echo   ============================================================
echo     File Transfer Assistant - Builder
echo   ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$l=[IO.File]::ReadAllLines('%~f0',[Text.Encoding]::UTF8);$i=[Array]::IndexOf($l,'#PSSTART');if($i -lt 0){Write-Host 'package broken';exit 2};[IO.File]::WriteAllText('%GENPS%',($l[($i+1)..($l.Length-1)] -join [Environment]::NewLine),(New-Object Text.UTF8Encoding($true)))"
if errorlevel 1 goto broken

powershell -NoProfile -ExecutionPolicy Bypass -File "%GENPS%" -WorkDir "%HERE%"
set "RC=%ERRORLEVEL%"
del "%GENPS%" >nul 2>&1

if not "%RC%"=="0" (
    echo   Please fix the config file and try again.
    echo.
    pause
    exit /b 1
)

echo.
pause
exit /b 0

:broken
echo.
echo   [!] Package is corrupted. Please extract the zip again.
echo.
pause
exit /b 2

#PSSTART

param([string]$WorkDir = '')

$ErrorActionPreference = 'Stop'

# bat 会把这段脚本解压到 %TEMP% 再执行，所以不能靠 $PSScriptRoot
# （那会指向 %TEMP%）。目录由 bat 作为参数传进来。
$here = $WorkDir
if (-not $here) {
    try { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition } catch {}
}
if (-not $here -or -not (Test-Path -LiteralPath $here)) { $here = (Get-Location).Path }
$here = $here.TrimEnd('\')

function Fail($msg) {
    Write-Host ''
    Write-Host ('  [X] ' + $msg) -ForegroundColor Red
    Write-Host ''
    exit 1
}

try {
    $cfgPath = Join-Path $here '配置.txt'
    $tplPath = Join-Path $here '模板.dat'

    if (-not (Test-Path -LiteralPath $cfgPath)) {
        Fail ('找不到「配置.txt」。当前查找位置: ' + $cfgPath)
    }
    if (-not (Test-Path -LiteralPath $tplPath)) { Fail ('找不到「模板.dat」，这个文件不能删。位置: ' + $tplPath) }

    # ---- 读配置：逐行 key=value，忽略注释和空行
    $raw = [IO.File]::ReadAllText($cfgPath, [Text.Encoding]::UTF8)
    $cfg = @{}
    foreach ($line in ($raw -split "`r?`n")) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($k) { $cfg[$k] = $v }
    }

    # ---- 校验
    $host_ = if ($cfg.ContainsKey('服务器地址')) { $cfg['服务器地址'] } else { '' }
    if (-not $host_) { Fail '请在「配置.txt」里填写「服务器地址」，例如 203.0.113.10' }

    $key = if ($cfg.ContainsKey('设备密钥')) { $cfg['设备密钥'] } else { '' }
    if (-not $key) { Fail '请在「配置.txt」里填写「设备密钥」' }
    if ($key -notmatch '^[0-9A-Za-z_\-]{16,64}$') {
        Fail ('设备密钥格式不对，应该是 16-64 位字母数字，你填的是: ' + $key)
    }

    $port = if ($cfg.ContainsKey('端口') -and $cfg['端口']) { $cfg['端口'] } else { '8080' }
    if ($port -notmatch '^\d{1,5}$') { Fail ('端口只能是数字，你填的是: ' + $port) }

    $auto = if ($cfg.ContainsKey('开机自启') -and $cfg['开机自启']) { $cfg['开机自启'] } else { '1' }
    if ($auto -ne '0' -and $auto -ne '1') { Fail '「开机自启」只能填 0 或 1' }

    # ---- 传输方式：仅 OSS / IPv6 优先回落 OSS / 仅 IPv6
    $xport = if ($cfg.ContainsKey('传输方式') -and $cfg['传输方式']) { $cfg['传输方式'].Trim().ToLower() } else { 'oss' }
    if ($xport -in @('1', 'oss', '仅oss', '仅 oss')) { $xport = 'oss' }
    elseif ($xport -in @('2', 'auto', 'ipv6优先', 'ipv6 优先')) { $xport = 'auto' }
    elseif ($xport -in @('3', 'ipv6', '仅ipv6', '仅 ipv6')) { $xport = 'ipv6' }
    else { Fail ('「传输方式」只能填 1 / 2 / 3（或 oss / auto / ipv6），你填的是: ' + $xport) }
    $xportLabel = switch ($xport) {
        'auto' { 'IPv6 优先，失败回落 OSS' }
        'ipv6' { '仅 IPv6' }
        default { '仅 OSS' }
    }

    # ---- 拼地址
    $url = $host_
    if ($url -notmatch '^https?://') { $url = 'http://' + $url }
    if ($url -notmatch ':\d+$') { $url = $url.TrimEnd('/') + ':' + $port }
    $url = $url.TrimEnd('/')

    # ---- 替换模板
    $tpl = [IO.File]::ReadAllText($tplPath, [Text.Encoding]::UTF8)
    if ($tpl -notmatch '__RELAY_URL__') { Fail '模板.dat 已损坏（缺少占位符），请重新解压' }

    $out = $tpl.Replace('__RELAY_URL__', $url)
    $out = $out.Replace('__DEVICE_KEY__', $key)
    $out = $out.Replace('__AUTOSTART__', $auto)
    $out = $out.Replace('__TRANSPORT__', $xport)
    $out = $out.Replace("`r`n", "`n").Replace("`n", "`r`n")

    $outPath = Join-Path $here '传输助手.bat'
    [IO.File]::WriteAllText($outPath, $out, (New-Object Text.UTF8Encoding($false)))

    Write-Host ''
    Write-Host '  ============================================================' -ForegroundColor Green
    Write-Host '    生成成功' -ForegroundColor Green
    Write-Host '  ============================================================' -ForegroundColor Green
    Write-Host ''
    Write-Host ('     服务器   : ' + $url)
    Write-Host ('     设备密钥 : ' + $key)
    Write-Host ('     传输方式 : ' + $xportLabel)
    Write-Host ('     开机自启 : ' + $(if ($auto -eq '1') { '是' } else { '否' }))
    Write-Host ('     文件大小 : ' + [math]::Round((Get-Item -LiteralPath $outPath).Length / 1KB, 1) + ' KB')
    Write-Host ''
    Write-Host ('     位置: ' + $outPath) -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  把这个「传输助手.bat」发到目标电脑，双击就能用。' -ForegroundColor Cyan
    Write-Host ''
    exit 0
} catch {
    Fail $_.Exception.Message
}
