
"""打包「配置生成器」给其他人用。

产出 dist/配置生成器/ 目录：
  配置.txt            用户填写
  生成传输助手.bat     用户双击
  模板.dat            传输助手模板（含占位符，用户不用管）
  使用说明.txt        简单说明

用法：python pack_config.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "client")
OUT_DIR = os.path.join(HERE, "dist", "配置生成器")

TEMPLATE_BAT = os.path.join(CLIENT, "bat_template.txt")
PAYLOAD = os.path.join(CLIENT, "agent.ps1")

GENERATOR_PS = r'''
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
'''

BAT_HEADER = r'''@echo off
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
'''

README_TXT = """信息传输助手 —— 配置生成器
================================================

【推荐】用 EXE，双击就能用
------------------------------------------------

  信息传输助手生成器.exe

  1. 双击打开
  2. 填服务器地址、端口、设备密钥
  3. 点「生成」
  4. 同目录下出现「传输助手.bat」，发到目标电脑双击即可

  不用装任何东西，Win7 以上都能跑。

【备选】用记事本改配置
------------------------------------------------

这个文件夹里还有这些文件：

  配置.txt             ← 你要填的就是这个
  生成传输助手.bat      ← 填完双击这个
  模板.dat             ← 不用管，不能删
  使用说明.txt          ← 本文件

第 1 步：用记事本打开「配置.txt」
第 2 步：填写两项必填内容

    服务器地址=203.0.113.10
    设备密钥=请填入32位随机字符串

    （上面是举例，要填你自己的）

第 3 步：保存，关掉记事本
第 4 步：双击「生成传输助手.bat」
第 5 步：把「传输助手.bat」发到目标电脑，双击即可

设备密钥从哪来
--------------

在你的服务器上，文件 /opt/server/config.json 里找 device_key

或者你运行 setup.py 生成服务器配置时，屏幕上显示的那串 32 位字符。

如果你是自己搭的服务器，运行 setup.py 时会自动生成一个。

也可以打开控制台的「账号」页面，那里直接显示设备密钥，可以复制。

服务器地址怎么写
----------------

这几种都行，程序会自动处理：

    203.0.113.10
    203.0.113.10:8080
    http://203.0.113.10:8080
    relay.example.com

不用写 http:// 也可以。端口不写就是默认的 8080。

常见问题
--------

Q: EXE 打不开 / 报毒
A: 这是自己编译的 .NET 程序，没有数字签名，部分杀毒软件会误报。
   可以改用「配置.txt + 生成传输助手.bat」的方式，效果完全一样。

Q: 提示「请在配置.txt里填写服务器地址」
A: 你还没填，或者填了但保存失败了。重新打开确认一下。

Q: 提示「设备密钥格式不对」
A: 密钥只能是字母、数字、下划线、短横线，8-128 位。
   检查有没有多复制了空格或引号。

Q: 生成的传输助手.bat 双击后一闪就没了
A: 看 %LOCALAPPDATA%\\WinUpdSvc\\agent.log 这个日志文件。

Q: 想改配置怎么办
A: 重新生成一次，会覆盖旧的。

Q: 别人能拿走我的密钥吗
A: 传输助手.bat 里确实包含密钥。所以不要随便把这个文件公开发布。
   如果担心，给每个使用者单独生成一个密钥（改服务器配置里的 device_key）。

Q: 怎么彻底停止，不再开机自动启动
A: 双击「停止服务.bat」。它会结束后台进程并删掉开机自启项。
   只关掉黑窗口是不够的——那个窗口只是显示器，后台进程还在跑，
   而且如果开过开机自启，下次登录还会自己连回来。
"""

STOPPER_HEADER = r"""@echo off
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
"""

STOPPER_PS = r"""
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
"""

def main():
    with open(TEMPLATE_BAT, "r", encoding="utf-8-sig") as f:
        head = f.read()
    with open(PAYLOAD, "r", encoding="utf-8-sig") as f:
        payload = f.read()

    if "#PSSTART" not in head:
        sys.exit("bat_template.txt 缺少 #PSSTART")
    for ph in ("__RELAY_URL__", "__DEVICE_KEY__", "__AUTOSTART__", "__TRANSPORT__"):
        if ph not in head:
            sys.exit("bat_template.txt 缺少占位符 %s" % ph)

    tpl = head.replace("\r\n", "\n").replace("\n", "\r\n") + \
        payload.replace("\r\n", "\n").replace("\n", "\r\n")

    gen = BAT_HEADER.replace("\r\n", "\n").replace("\n", "\r\n") + \
        GENERATOR_PS.replace("\r\n", "\n").replace("\n", "\r\n")

    os.makedirs(OUT_DIR, exist_ok=True)

    stopper = STOPPER_HEADER.replace("\r\n", "\n").replace("\n", "\r\n") + \
        STOPPER_PS.replace("\r\n", "\n").replace("\n", "\r\n")

    files = [
        ("模板.dat", tpl, "utf-8"),
        ("生成传输助手.bat", gen, "utf-8"),
        ("停止服务.bat", stopper, "utf-8"),
        ("配置.txt", open(os.path.join(CLIENT, "配置.txt"), "r", encoding="utf-8-sig").read(), "utf-8-sig"),
        ("使用说明.txt", README_TXT, "utf-8-sig"),
    ]
    for name, content, enc in files:
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding=enc, newline="") as f:
            f.write(content)
        print("  %-22s %7.1f KB  (%s)" % (name, os.path.getsize(path) / 1024.0, enc))

    gen_path = os.path.join(OUT_DIR, "生成传输助手.bat")
    lines = open(gen_path, "r", encoding="utf-8").read().split("\n")
    idx = next(i for i, l in enumerate(lines) if l.strip() == "#PSSTART")
    bad = sum(1 for l in lines[:idx] for ch in l if ord(ch) > 127)
    print("\n  生成器头部非 ASCII 字符数: %d %s" % (bad, "(OK)" if bad == 0 else "(有问题!)"))

    print("\n已输出到: %s" % OUT_DIR)

if __name__ == "__main__":
    main()
