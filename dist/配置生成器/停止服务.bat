@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
title Stop File Transfer Service

powershell -NoProfile -ExecutionPolicy Bypass -Command "$l=[IO.File]::ReadAllLines('%~f0',[Text.Encoding]::UTF8);$i=[Array]::IndexOf($l,'#PSSTART');if($i -lt 0){Write-Host 'package broken';exit 2};Invoke-Expression (($l[($i+1)..($l.Length-1)]) -join [Environment]::NewLine)"
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo   [!] Failed. See the messages above.
echo.
pause
exit /b 1

#PSSTART

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$work = Join-Path $env:LOCALAPPDATA 'WinUpdSvc'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'WinUpdSvc'

Write-Host ''
Write-Host '  停止信息传输服务' -ForegroundColor Cyan
Write-Host '  ' + ('=' * 60) -ForegroundColor DarkGray
Write-Host ''

# 1. 结束后台进程
$killed = 0
try {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.CommandLine -and $_.CommandLine -match 'WinUpdSvc' })
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host ('  已结束进程 PID ' + $p.ProcessId) -ForegroundColor Green
            $killed++
        } catch {
            Write-Host ('  结束进程 ' + $p.ProcessId + ' 失败: ' + $_.Exception.Message) -ForegroundColor Yellow
        }
    }
} catch {}
if ($killed -eq 0) {
    Write-Host '  没有发现正在运行的后台进程' -ForegroundColor DarkGray
}

# 2. 删除开机自启
try {
    $has = Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue
    if ($has) {
        Remove-ItemProperty -Path $runKey -Name $runName -ErrorAction Stop
        Write-Host '  已删除开机自启项' -ForegroundColor Green
    } else {
        Write-Host '  本来就没有开机自启项' -ForegroundColor DarkGray
    }
} catch {
    Write-Host ('  删除自启项失败: ' + $_.Exception.Message) -ForegroundColor Red
}

# 3. 清掉运行状态文件（保留 agent.log 方便排查）
foreach ($f in 'worker.pid', 'worker.lock', 'status.txt', 'cursor.json', 'state.json') {
    $p = Join-Path $work $f
    if (Test-Path -LiteralPath $p) {
        try { Remove-Item -LiteralPath $p -Force -ErrorAction Stop } catch {}
    }
}

Write-Host ''
Write-Host '  检查结果' -ForegroundColor DarkCyan
Write-Host '  ' + ('-' * 60) -ForegroundColor DarkGray

$stillRun = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -and $_.CommandLine -match 'WinUpdSvc' }).Count
$stillAuto = [bool](Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue)

Write-Host ('  后台进程    : ' + $(if ($stillRun -eq 0) { '已全部结束' } else { '' + $stillRun + ' 个仍在运行' })) `
    -ForegroundColor $(if ($stillRun -eq 0) { 'Green' } else { 'Yellow' })
Write-Host ('  开机自启    : ' + $(if ($stillAuto) { '仍存在' } else { '已关闭' })) `
    -ForegroundColor $(if ($stillAuto) { 'Yellow' } else { 'Green' })

if (Test-Path -LiteralPath (Join-Path $work 'agent.log')) {
    Write-Host ('  运行日志    : ' + (Join-Path $work 'agent.log')) -ForegroundColor DarkGray
}

Write-Host ''
if ($stillRun -eq 0 -and -not $stillAuto) {
    Write-Host '  已彻底停止，下次开机不会再自动连接。' -ForegroundColor Green
} else {
    Write-Host '  还有残留，请重跑一次，或手动检查上面列出的位置。' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  按回车键退出…' -ForegroundColor DarkGray
$null = Read-Host
