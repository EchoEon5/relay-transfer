
param(
    [ValidateSet('worker', 'monitor', 'launcher')][string]$Mode = 'worker',
    [string]$RelayUrl = $env:RELAY_URL,
    [string]$DeviceKey = $env:DEVICE_KEY,
    [string]$Transport = $env:TRANSPORT
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$script:TransportRaw = ([string]$Transport).Trim().ToLowerInvariant()
if ($script:TransportRaw -notin @('oss', 'auto', 'ipv6')) { $script:TransportRaw = 'oss' }

$script:Ipv6ChannelEnabled = $true

function Get-TransportLabel {
    switch ($script:TransportRaw) {
        'auto' { return 'IPv6 优先，失败回落 OSS' }
        'ipv6' { return '仅 IPv6' }
        default { return '仅 OSS' }
    }
}

function Get-V6Alive {
    return [bool]($script:V6Srv -and $script:V6Srv.Port -gt 0)
}

function Get-ActiveChannel {
    if ($script:TransportRaw -eq 'oss') { return 'OSS' }
    if (-not $script:Ipv6ChannelEnabled) { return 'OSS' }
    if (Get-V6Alive) { return ('IPv6 直连，端口 ' + $script:V6Srv.Port) }
    return 'IPv6（待命中，起任务后启用）'
}

function Get-TransportNote {
    if ($script:TransportRaw -eq 'oss') { return '' }
    if (-not $script:Ipv6ChannelEnabled) {
        return 'IPv6 通道尚未启用，本次实际走 OSS'
    }
    if (-not (Get-V6Alive)) {
        $a = @(Get-V6Addresses)
        if (-not $a.Count) {
            return '本机没有公网 IPv6，任务会自动回落 OSS'
        }
        return '本机 IPv6：' + ($a -join ', ')
    }
    return ''
}

$WorkDir = Join-Path $env:LOCALAPPDATA 'WinUpdSvc'
if (-not (Test-Path -LiteralPath $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null }
$StatusFile = Join-Path $WorkDir 'status.txt'
$LogFile = Join-Path $WorkDir 'agent.log'
$StateFile = Join-Path $WorkDir 'state.json'
$PidFile = Join-Path $WorkDir 'worker.pid'
$CursorFile = Join-Path $WorkDir 'cursor.json'

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:LastStatusText = ''

Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue

if (-not ('OssSink' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.IO;
public delegate void PartHandler(byte[] buffer, int count);
public class OssSink : Stream {
    private readonly PartHandler _upload;
    private readonly long _partSize;
    private byte[] _buf;
    private int _fill;
    public long Written { get; private set; }

    public OssSink(long partSize, PartHandler upload) {
        _partSize = partSize;
        _upload = upload;
        _buf = new byte[partSize];
    }
    public override void Write(byte[] buffer, int offset, int count) {
        while (count > 0) {
            int space = (int)(_partSize - _fill);
            int n = Math.Min(space, count);
            Buffer.BlockCopy(buffer, offset, _buf, _fill, n);
            _fill += n; offset += n; count -= n; Written += n;
            if (_fill >= _partSize) FlushPart();
        }
    }
    public void FlushPart() {
        if (_fill == 0) return;
        byte[] part = new byte[_fill];
        Buffer.BlockCopy(_buf, 0, part, 0, _fill);
        int n = _fill;
        _fill = 0;
        _upload(part, n);
    }
    public override void Flush() { }
    public override bool CanRead { get { return false; } }
    public override bool CanSeek { get { return false; } }
    public override bool CanWrite { get { return true; } }
    public override long Length { get { throw new NotSupportedException(); } }
    public override long Position { get { return Written; } set { throw new NotSupportedException(); } }
    public override int Read(byte[] b, int o, int c) { throw new NotSupportedException(); }
    public override long Seek(long o, SeekOrigin s) { throw new NotSupportedException(); }
    public override void SetLength(long v) { throw new NotSupportedException(); }
}
'@
}

function Write-Log([string]$msg) {
    try {
        $line = '{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
        $size = (Get-Item -LiteralPath $LogFile).Length
        if ($size -gt 4MB) {
            $tail = Get-Content -LiteralPath $LogFile -Tail 3000
            Set-Content -LiteralPath $LogFile -Value $tail -Encoding UTF8
        }
    } catch {}
}

function Set-Status {
    param(
        [string]$Line1 = '',
        [string]$Line2 = '',
        [string]$Line3 = '',
        [string]$Line4 = ''
    )
    $text = @(
        $Line1
        $Line2
        $Line3
        $Line4
    ) -join "`r`n"
   
    if ($text -eq $script:LastStatusText) { return }
    $script:LastStatusText = $text
    try {
       
        [System.IO.File]::WriteAllText($StatusFile, $text, $script:Utf8NoBom)
    } catch {}
}

function Format-Size([double]$bytes) {
    if ($bytes -lt 1KB) { return ('{0:N0} B' -f $bytes) }
    if ($bytes -lt 1MB) { return ('{0:N2} KB' -f ($bytes / 1KB)) }
    if ($bytes -lt 1GB) { return ('{0:N2} MB' -f ($bytes / 1MB)) }
    if ($bytes -lt 1TB) { return ('{0:N2} GB' -f ($bytes / 1GB)) }
    return ('{0:N2} TB' -f ($bytes / 1TB))
}

function Format-Eta([double]$sec) {
    if ($sec -lt 0 -or [double]::IsNaN($sec) -or [double]::IsInfinity($sec)) { return '--' }
    if ($sec -lt 60) { return ('{0:N0} 秒' -f $sec) }
    if ($sec -lt 3600) { return ('{0:N0} 分 {1:N0} 秒' -f [Math]::Floor($sec / 60), ($sec % 60)) }
    return ('{0:N0} 时 {1:N0} 分' -f [Math]::Floor($sec / 3600), [Math]::Floor(($sec % 3600) / 60))
}

$script:Http = $null
function Get-HttpClient {
    if ($null -eq $script:Http) {
        $script:Http = New-Object System.Net.Http.HttpClient
        $script:Http.Timeout = [TimeSpan]::FromMinutes(10)
        $script:Http.DefaultRequestHeaders.Add('X-Device-Key', $DeviceKey)
    }
    return $script:Http
}

function Invoke-Relay {
    param([string]$Path, [hashtable]$Body, [int]$TimeoutSec = 60)
    $json = if ($Body) { $Body | ConvertTo-Json -Depth 8 -Compress } else { '{}' }
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $client = Get-HttpClient
    $content = [System.Net.Http.ByteArrayContent]::new($bytes)
    $content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/json; charset=utf-8')
    $task = $client.PostAsync($RelayUrl.TrimEnd('/') + $Path, $content)
    $timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    if (-not $task.Wait($timeout)) { throw "请求超时: $Path" }
    $resp = $task.Result
    $text = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    if (-not $resp.IsSuccessStatusCode) { throw ("HTTP {0}: {1}" -f [int]$resp.StatusCode, $text) }
    return $text | ConvertFrom-Json
}

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Get-MachineId {
    try {
        $guid = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction Stop).MachineGuid
        if ($guid) { return ($guid -replace '[^0-9A-Za-z]', '') }
    } catch {}
    return ($env:COMPUTERNAME + '-' + $env:USERNAME)
}

function Get-DiskList {
    $list = @()
    foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
        if (-not $d.IsReady) { continue }
        $mount = $d.Name
        if ($mount -match '^[A-Za-z]:\\$') {
            $list += [ordered]@{
                name  = $mount.Substring(0, 2)
                path  = $mount
                label = ''
                total = [int64]$d.TotalSize
                free  = [int64]$d.AvailableFreeSpace
            }
        }
    }
    return $list
}

function Get-KnownRoots {
    $r = [ordered]@{
        userprofile = $env:USERPROFILE
        desktop     = [Environment]::GetFolderPath('Desktop')
        downloads   = ''
        documents   = [Environment]::GetFolderPath('MyDocuments')
    }
    $dl = Join-Path $env:USERPROFILE 'Downloads'
    if (Test-Path -LiteralPath $dl) { $r.downloads = $dl }
    return $r
}

function Resolve-SafePath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return '' }
    $p = $path.Trim()
   
    $trimmed = $p.TrimEnd('\')
    if ($trimmed -match '^[A-Za-z]:$') { return $trimmed + '\' }
    return $trimmed
}

function Get-DirList {
    param([string]$Path)
    $path = Resolve-SafePath $Path
    if (-not $path) { throw '路径为空' }
    $entries = New-Object System.Collections.ArrayList
    $skipped = 0
    $denied = $false
    $reason = ''
    $di = $null
    try { $di = New-Object System.IO.DirectoryInfo($path) } catch {
        throw ('路径格式不正确：' + $_.Exception.Message)
    }
    $exists = $false
    try { $exists = $di.Exists } catch { $exists = $false }
    if (-not $exists) {
       
        try { $exists = [System.IO.Directory]::Exists($path) } catch { $exists = $false }
        if (-not $exists) { throw '路径不存在' }
    }

    try {
        foreach ($f in $di.EnumerateFileSystemInfos()) {
            try {
                $isDir = ($f -is [System.IO.DirectoryInfo])
                $size = 0
                $isLink = $false
                try {
                    $mtime = [int][DateTimeOffset]::new($f.LastWriteTimeUtc).ToUnixTimeSeconds()
                    $isLink = (($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
                } catch { $mtime = 0 }
                if (-not $isDir) { $size = $f.Length }
                $null = $entries.Add([ordered]@{
                        name    = $f.Name
                        is_dir  = $isDir
                        is_link = $isLink
                        size    = $size
                        mtime   = $mtime
                        path    = $f.FullName
                    })
            } catch { $skipped++ }
        }
    } catch [System.UnauthorizedAccessException] {
       
        $denied = $true
        $reason = '需要 SYSTEM 或更高权限，当前账户无权列举此目录'
    } catch [System.IO.DirectoryNotFoundException] {
        throw '路径不存在'
    } catch {
        $ex = $_.Exception
        if ($ex.InnerException -is [System.UnauthorizedAccessException]) {
            $denied = $true
            $reason = '需要 SYSTEM 或更高权限，当前账户无权列举此目录'
        } else {
            $denied = $true
            $reason = $ex.Message
        }
    }

    $sorted = $entries | Sort-Object @{ Expression = { -not $_.is_dir } }, @{ Expression = { $_.name.ToLower() } }
    $parent = ''
    try {
        $p = $di.Parent
        if ($p) { $parent = $p.FullName }
    } catch {}

    return [ordered]@{
        path          = $path
        parent        = $parent
        entries       = @($sorted)
        skipped       = $skipped
        denied        = $denied
        denied_reason = $reason
    }
}

function Get-PathSize {
    param([string[]]$Paths, [switch]$NoProgress)
    $total = [int64]0; $files = 0; $dirs = 0; $skipped = 0
    $lastTick = [DateTime]::UtcNow
    foreach ($p in $Paths) {
        $p = Resolve-SafePath $p
        if (-not $p) { continue }
        if (Test-Path -LiteralPath $p -PathType Leaf) {
            try { $total += (Get-Item -LiteralPath $p -Force).Length; $files++ } catch { $skipped++ }
            continue
        }
        if (Test-Path -LiteralPath $p -PathType Container) {
            $dirs++
            $stack = New-Object System.Collections.Stack
            $stack.Push($p)
            while ($stack.Count -gt 0) {
                $cur = $stack.Pop()
                try {
                    $di = New-Object System.IO.DirectoryInfo($cur)
                    foreach ($f in $di.EnumerateFileSystemInfos()) {
                        try {
                            if ($f -is [System.IO.DirectoryInfo]) { $stack.Push($f.FullName) }
                            else { $total += $f.Length; $files++ }
                        } catch { $skipped++ }
                    }
                } catch { $skipped++ }
               
                if (-not $NoProgress) {
                    $now = [DateTime]::UtcNow
                    if (($now - $lastTick).TotalMilliseconds -ge 700) {
                        $lastTick = $now
                        if (Test-CancelRequested) { throw [OperationCanceledException]::new('已取消') }
                        Set-Status `
                            '状态：传输中（统计大小）' `
                            ('已扫描：{0} 个文件，合计 {1}' -f $files, (Format-Size $total)) `
                            ('提示：文件很多时需要一点时间，请稍候…') `
                            '提示：正在后台上传，关闭本窗口不影响传输。'
                    }
                }
            }
        } else { $skipped++ }
    }
    return [ordered]@{ total = $total; files = $files; dirs = $dirs; skipped = $skipped }
}

function Test-ProtectedPath([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return $true }
    if ($p -match '^[A-Za-z]:\\?$') { return $true }
    $t = $p.TrimEnd('\')
    $crit = @(
        [Environment]::GetEnvironmentVariable('SystemRoot'),
        [Environment]::GetEnvironmentVariable('windir'),
        [Environment]::GetEnvironmentVariable('ProgramFiles'),
        [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'),
        [Environment]::GetEnvironmentVariable('ProgramData'),
        [Environment]::GetEnvironmentVariable('USERPROFILE')
    )
    foreach ($c in $crit) {
        if ($c -and ($t -ieq $c.TrimEnd('\'))) { return $true }
    }
    return $false
}

function ConvertTo-SafeName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { throw '名称为空' }
    $n = $name.Trim()
    if ($n -match '[\\/:*?"<>|]') { throw '名称不能包含 \ / : * ? " < > |' }
    if ($n -match '^\.+$') { throw '名称不能只由点组成' }
    if ($n.Length -gt 200) { throw '名称过长' }
    return $n
}

function Clear-ReadOnlyTree([string]$p) {
   
    try {
        $di = New-Object System.IO.DirectoryInfo($p)
        foreach ($f in $di.EnumerateFileSystemInfos()) {
            try {
                if (($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
                if ($f -is [System.IO.DirectoryInfo]) {
                    Clear-ReadOnlyTree $f.FullName
                } elseif (($f.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0) {
                    [System.IO.File]::SetAttributes($f.FullName, ($f.Attributes -bxor [System.IO.FileAttributes]::ReadOnly))
                }
            } catch {}
        }
    } catch {}
}

function Set-FsBusy([string]$what, [int]$n) {
    Set-Status `
        '状态：已连接（文件操作中）' `
        ($what + '：已处理 ' + $n + ' 个文件') `
        '正在执行，请稍候…' `
        '提示：本窗口可关闭，操作仍在后台进行。'
}

function New-DirItem($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $p = Resolve-SafePath $opArgs.path
    if (-not $p) { throw '路径为空' }
    if ([System.IO.Directory]::Exists($p) -or [System.IO.File]::Exists($p)) { throw '同名项目已存在' }
    $parent = [System.IO.Path]::GetDirectoryName($p)
    if ($parent -and -not [System.IO.Directory]::Exists($parent)) { throw '上级目录不存在' }
    $null = [System.IO.Directory]::CreateDirectory($p)
    return [ordered]@{ path = $p }
}

function New-FileItem($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $p = Resolve-SafePath $opArgs.path
    if (-not $p) { throw '路径为空' }
    if ([System.IO.Directory]::Exists($p) -or [System.IO.File]::Exists($p)) { throw '同名项目已存在' }
    $parent = [System.IO.Path]::GetDirectoryName($p)
    if ($parent -and -not [System.IO.Directory]::Exists($parent)) { throw '上级目录不存在' }
    $text = [string]$opArgs.text
    if ($text) {
        [System.IO.File]::WriteAllText($p, $text, $script:Utf8NoBom)
    } else {
        $fs = [System.IO.File]::Create($p)
        $fs.Close()
    }
    $len = ([System.IO.FileInfo]::new($p)).Length
    return [ordered]@{ path = $p; size = $len }
}

function Rename-FsItem($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $p = Resolve-SafePath $opArgs.path
    if (-not $p) { throw '路径为空' }
    $name = ConvertTo-SafeName ([string]$opArgs.name)
    if (Test-ProtectedPath $p) { throw '盘符根或系统目录不允许重命名' }
    $parent = [System.IO.Path]::GetDirectoryName($p)
    if (-not $parent) { throw '盘符根不允许重命名' }
    $target = Join-Path $parent $name
    if ($target -ieq $p) { return [ordered]@{ path = $p } }
    if ([System.IO.Directory]::Exists($target) -or [System.IO.File]::Exists($target)) { throw '同名项目已存在' }
    if ([System.IO.Directory]::Exists($p)) {
        [System.IO.Directory]::Move($p, $target)
    } else {
        [System.IO.File]::Move($p, $target)
    }
    return [ordered]@{ path = $target }
}

function Copy-TreeInternal([string]$src, [string]$dst, $failed) {
    $null = [System.IO.Directory]::CreateDirectory($dst)
    $stack = New-Object System.Collections.Stack
    $stack.Push(@($src, $dst))
    $lastTick = [DateTime]::UtcNow
    while ($stack.Count -gt 0) {
        $pair = $stack.Pop()
        $s = [string]$pair[0]
        $d = [string]$pair[1]
        try {
            $di = New-Object System.IO.DirectoryInfo($s)
            foreach ($f in $di.EnumerateFileSystemInfos()) {
                try {
                    if (($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
                    $td = Join-Path $d $f.Name
                    if ($f -is [System.IO.DirectoryInfo]) {
                        $null = [System.IO.Directory]::CreateDirectory($td)
                        $stack.Push(@($f.FullName, $td))
                    } else {
                        [System.IO.File]::Copy($f.FullName, $td, $true)
                        $script:FsCount++
                    }
                } catch { $null = $failed.Add(($f.FullName + '：' + $_.Exception.Message)) }
                if (([DateTime]::UtcNow - $lastTick).TotalSeconds -ge 1) {
                    $lastTick = [DateTime]::UtcNow
                    Set-FsBusy '复制' $script:FsCount
                }
            }
        } catch { $null = $failed.Add(($s + '：' + $_.Exception.Message)) }
    }
}

function Copy-FsItems($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $dest = Resolve-SafePath $opArgs.dest
    if (-not $dest) { throw '目标目录为空' }
    if (-not [System.IO.Directory]::Exists($dest)) { throw '目标目录不存在' }
    $failed = New-Object System.Collections.ArrayList
    $script:FsCount = 0
    $done = 0
    foreach ($raw in @($opArgs.paths)) {
        $p = Resolve-SafePath $raw
        if (-not $p) { continue }
        try {
            $isDir = [System.IO.Directory]::Exists($p)
            if (-not $isDir -and -not [System.IO.File]::Exists($p)) { throw '源不存在' }
            $leaf = Split-Path -Leaf $p.TrimEnd('\')
            if (-not $leaf) { throw '不能复制盘符根' }
            $target = Join-Path $dest $leaf
            if ($target -ieq $p) { throw '源和目标相同' }
            if ([System.IO.Directory]::Exists($target) -or [System.IO.File]::Exists($target)) { throw '目标已存在同名项目' }
            if ($isDir) {
                $pn = $p.TrimEnd('\').ToLower()
                $dn = $dest.TrimEnd('\').ToLower()
                if ($dn -eq $pn -or $dn.StartsWith($pn + '\')) { throw '不能复制到自身或其子目录' }
                Copy-TreeInternal $p $target $failed
            } else {
                [System.IO.File]::Copy($p, $target, $true)
                $script:FsCount++
            }
            $done++
        } catch { $null = $failed.Add(($p + '：' + $_.Exception.Message)) }
    }
    return [ordered]@{ done = $done; files = $script:FsCount; failed = @($failed) }
}

function Move-FsItems($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $dest = Resolve-SafePath $opArgs.dest
    if (-not $dest) { throw '目标目录为空' }
    if (-not [System.IO.Directory]::Exists($dest)) { throw '目标目录不存在' }
    $failed = New-Object System.Collections.ArrayList
    $script:FsCount = 0
    $done = 0
    foreach ($raw in @($opArgs.paths)) {
        $p = Resolve-SafePath $raw
        if (-not $p) { continue }
        try {
            $isDir = [System.IO.Directory]::Exists($p)
            if (-not $isDir -and -not [System.IO.File]::Exists($p)) { throw '源不存在' }
            if (Test-ProtectedPath $p) { throw '盘符根或系统目录不允许移动' }
            $leaf = Split-Path -Leaf $p.TrimEnd('\')
            if (-not $leaf) { throw '不能移动盘符根' }
            $target = Join-Path $dest $leaf
            if ($target -ieq $p) { throw '源和目标相同' }
            if ([System.IO.Directory]::Exists($target) -or [System.IO.File]::Exists($target)) { throw '目标已存在同名项目' }
            $sameVol = ([System.IO.Path]::GetPathRoot($p) -ieq [System.IO.Path]::GetPathRoot($dest))
            if ($isDir) {
                $pn = $p.TrimEnd('\').ToLower()
                $dn = $dest.TrimEnd('\').ToLower()
                if ($dn -eq $pn -or $dn.StartsWith($pn + '\')) { throw '不能移动到自身或其子目录' }
                if ($sameVol) {
                    [System.IO.Directory]::Move($p, $target)
                } else {
                    Copy-TreeInternal $p $target $failed
                    Clear-ReadOnlyTree $p
                    [System.IO.Directory]::Delete($p, $true)
                }
            } else {
                if ($sameVol) {
                    [System.IO.File]::Move($p, $target)
                } else {
                    [System.IO.File]::Copy($p, $target, $true)
                    [System.IO.File]::Delete($p)
                }
                $script:FsCount++
            }
            $done++
        } catch { $null = $failed.Add(($p + '：' + $_.Exception.Message)) }
    }
    return [ordered]@{ done = $done; files = $script:FsCount; failed = @($failed) }
}

function Remove-FsItems($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $failed = New-Object System.Collections.ArrayList
    $done = 0
    foreach ($raw in @($opArgs.paths)) {
        $p = Resolve-SafePath $raw
        if (-not $p) { continue }
        try {
            if (Test-ProtectedPath $p) { throw '盘符根或系统目录不允许删除' }
            if ($p.TrimEnd('\').Length -le 2) { throw '不允许删除盘符根' }
            if ([System.IO.Directory]::Exists($p)) {
                try {
                    [System.IO.Directory]::Delete($p, $true)
                } catch {
                   
                    Clear-ReadOnlyTree $p
                    [System.IO.Directory]::Delete($p, $true)
                }
            } elseif ([System.IO.File]::Exists($p)) {
                $a = [System.IO.File]::GetAttributes($p)
                if (($a -band [System.IO.FileAttributes]::ReadOnly) -ne 0) {
                    [System.IO.File]::SetAttributes($p, ($a -bxor [System.IO.FileAttributes]::ReadOnly))
                }
                [System.IO.File]::Delete($p)
            } else {
                throw '不存在'
            }
            $done++
        } catch { $null = $failed.Add(($p + '：' + $_.Exception.Message)) }
    }
    return [ordered]@{ done = $done; failed = @($failed) }
}

function Get-FsPathInfo($opArgs) {
    if (-not $opArgs) { throw '缺少参数' }
    $p = Resolve-SafePath $opArgs.path
    if (-not $p) { throw '路径为空' }
    $isDir = [System.IO.Directory]::Exists($p)
    $isFile = [System.IO.File]::Exists($p)
    $size = 0
    if ($isFile) { try { $size = ([System.IO.FileInfo]::new($p)).Length } catch {} }
    return [ordered]@{ path = $p; exists = ($isDir -or $isFile); is_dir = $isDir; size = $size }
}

$script:PartSize = 64MB

function New-ObjectKey {
    return ([Guid]::NewGuid().ToString('N'))
}

function Send-Part {
    param([string]$Url, [byte[]]$Buffer, [int]$Offset, [int]$Count, [int]$Retry = 3)
    $client = Get-HttpClient
    for ($i = 1; $i -le $Retry; $i++) {
        try {
            $content = [System.Net.Http.ByteArrayContent]::new($Buffer, $Offset, $Count)
            $content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
           
            $task = $client.PutAsync($Url, $content)
            $tick = [DateTime]::UtcNow
            while (-not $task.IsCompleted) {
                if ([System.Threading.Tasks.Task]::WaitAny(@($task), 300) -eq -1) {
                    $now = [DateTime]::UtcNow
                    if (($now - $tick).TotalMilliseconds -ge 700) {
                        $tick = $now
                        if (Test-CancelRequested) {
                            try { $client.CancelPendingRequests() } catch {}
                            throw [OperationCanceledException]::new('已取消')
                        }
                       
                        $elapsedSec = [int]([DateTime]::UtcNow - $script:JobStart).TotalSeconds
                        $usedTxt = '{0:D2}:{1:D2}' -f [int]($elapsedSec / 60), ($elapsedSec % 60)
                        Set-Status `
                            ('状态：传输中（' + $script:JobPhase + '）') `
                            ('进度：{0} / {1}  （{2:N1}%）  第 {3} 片传输中' -f (Format-Size $script:BytesDone), (Format-Size $script:JobTotal), $(if ($script:JobTotal -gt 0) { $script:BytesDone * 100.0 / $script:JobTotal } else { 0 }), $script:CurPartNo) `
                            ('速度：{0}/s    已用：{1}    剩余：{2}' -f (Format-Size $script:LastSpeed), $usedTxt, (Format-Eta $(if ($script:LastSpeed -gt 1) { ($script:JobTotal - $script:BytesDone) / $script:LastSpeed } else { -1 }))) `
                            '提示：正在后台上传，关闭本窗口不影响传输。'
                    }
                }
            }
            $resp = $task.GetAwaiter().GetResult()
            if (-not $resp.IsSuccessStatusCode) {
                $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                throw ("HTTP {0}: {1}" -f [int]$resp.StatusCode, $body)
            }
            $etag = $resp.Headers.ETag
            if ($etag) { return $etag.Tag }
            return ''
        } catch [OperationCanceledException] {
            throw
        } catch {
            if ($i -eq $Retry) { throw }
            Start-Sleep -Seconds ([Math]::Min(8, 2 * $i))
        }
    }
}

function Send-SmallObject {
    param([string]$Url, [byte[]]$Buffer, [int]$Count)
    return Send-Part -Url $Url -Buffer $Buffer -Offset 0 -Count $Count
}

$script:AutoConcurrency = 8      
$script:AutoCeiling = 32         
$script:AutoFloor = 4            
$script:AutoPhase = 'probe'      
$script:AutoBestConc = 8         
$script:AutoBestSpeed = 0        
$script:AutoTrialSpeed = 0       
$script:AutoSettled = $false     

function Reset-AutoConcurrency {
   
    $script:AutoPhase = 'probe'
    $script:AutoBestSpeed = 0
    $script:AutoTrialSpeed = 0
    $script:AutoSettled = $false
    $script:AutoBestConc = $script:AutoConcurrency
}

function Update-AutoConcurrency {
    param([double]$Speed)
   
    if ($Speed -lt 1MB) { return }
    if ($script:AutoSettled) { return }

    if ($script:AutoPhase -eq 'probe') {
       
        $script:AutoBestSpeed = $Speed
        $script:AutoBestConc = $script:AutoConcurrency
        if ($script:AutoConcurrency -ge $script:AutoCeiling) {
            $script:AutoSettled = $true
            Write-Log ("自适应：已达上限 {0} 路（{1:N2} MB/s）" -f $script:AutoConcurrency, ($Speed / 1MB))
            return
        }
        $script:AutoConcurrency = [Math]::Min($script:AutoCeiling, $script:AutoConcurrency * 2)
        $script:AutoPhase = 'trial'
        Write-Log ("自适应：试探 {0} → {1} 路（当前 {2:N2} MB/s）" -f $script:AutoBestConc, $script:AutoConcurrency, ($Speed / 1MB))
        return
    }

    if ($script:AutoPhase -eq 'trial') {
        $script:AutoTrialSpeed = $Speed
        if ($Speed -gt $script:AutoBestSpeed * 1.10) {
           
            $script:AutoBestSpeed = $Speed
            $script:AutoBestConc = $script:AutoConcurrency
            $script:AutoPhase = 'probe'
            Write-Log ("自适应：{0} 路更快（{1:N2} MB/s），继续试探" -f $script:AutoConcurrency, ($Speed / 1MB))
        } else {
           
            Write-Log ("自适应：{0} 路无收益（{1:N2} vs {2:N2} MB/s），退回 {3} 路" -f $script:AutoConcurrency, ($Speed / 1MB), ($script:AutoBestSpeed / 1MB), $script:AutoBestConc)
            $script:AutoConcurrency = $script:AutoBestConc
            $script:AutoSettled = $true
            $script:AutoPhase = 'done'
        }
    }
}

function Drain-ZipCompleted {
    foreach ($pn in @($script:ZipPending.Keys)) {
        $item = $script:ZipPending[$pn]
        if (-not $item.Task.IsCompleted) { continue }
        if ($item.Task.IsFaulted) {
            $ex = $item.Task.Exception
            foreach ($p in $script:ZipPending.Values) { try { $p.Src.Cancel() } catch {} }
            throw ("分片 {0} 上传失败: {1}" -f $pn, $ex.GetBaseException().Message)
        }
        $resp = $item.Task.GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) {
            $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            foreach ($p in $script:ZipPending.Values) { try { $p.Src.Cancel() } catch {} }
            throw ("分片 {0} HTTP {1}: {2}" -f $pn, [int]$resp.StatusCode, $body)
        }
        $etag = $resp.Headers.ETag
        $script:ZipEtags[[int]$pn] = if ($etag) { $etag.Tag } else { '' }
        $script:BytesDone += $item.Len
        $resp.Dispose()
        $item.Content.Dispose()
        $item.Src.Dispose()
        $script:ZipPending.Remove($pn)
    }
}

function Send-PartsParallel {
    param(
        [string]$FilePath,
        [hashtable]$Urls,      
        [int[]]$PartNumbers,
        [int64]$PartSize,
        [int64]$TotalSize,
        [hashtable]$Etags      
    )
    $queue = [System.Collections.Queue]::new()
    foreach ($n in ($PartNumbers | Sort-Object)) { $queue.Enqueue($n) }
    $running = @{}
    $client = Get-HttpClient
    $lastTune = [DateTime]::UtcNow
    $lastTuneBytes = $script:BytesDone

    while ($queue.Count -gt 0 -or $running.Count -gt 0) {
       
        $conc = $script:AutoConcurrency
       
        while ($running.Count -lt $conc -and $queue.Count -gt 0) {
            if (Test-CancelRequested) {
                foreach ($t in $running.Values) { try { $t.Src.Cancel() } catch {} }
                throw [OperationCanceledException]::new('已取消')
            }
            $n = [int]$queue.Dequeue()
            $offset = [int64]($n - 1) * $PartSize
            $len = [int][Math]::Min($PartSize, $TotalSize - $offset)
           
            $buf = New-Object byte[] $len
            $fs = [System.IO.File]::Open($FilePath, 'Open', 'Read', 'ReadWrite')
            try {
                $null = $fs.Seek($offset, 'Begin')
                $read = 0
                while ($read -lt $len) {
                    $got = $fs.Read($buf, $read, $len - $read)
                    if ($got -le 0) { throw ("读取文件失败：{0} 偏移 {1}" -f $FilePath, $offset) }
                    $read += $got
                }
            } finally { $fs.Dispose() }
            $content = [System.Net.Http.ByteArrayContent]::new($buf)
            $content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
            $src = New-Object System.Threading.CancellationTokenSource
            $task = $client.PutAsync($Urls[$n], $content, $src.Token)
            $running[$n] = @{ Task = $task; Src = $src; Len = $len; Content = $content }
        }

        $tasks = @($running.Values | ForEach-Object { $_.Task })
        $done = $false
        if ($tasks.Count -gt 0) {
            $idx = [System.Threading.Tasks.Task]::WaitAny($tasks, 500)
            if ($idx -ge 0) { $done = $true }
        } else {
            Start-Sleep -Milliseconds 100
        }

        if ($done) {
            $finished = @()
            foreach ($n in @($running.Keys)) {
                $item = $running[$n]
                if ($item.Task.IsCompleted) {
                    if ($item.Task.IsFaulted) {
                        $ex = $item.Task.Exception
                        foreach ($t in $running.Values) { try { $t.Src.Cancel() } catch {} }
                        throw ("分片 {0} 上传失败: {1}" -f $n, $ex.GetBaseException().Message)
                    }
                    $resp = $item.Task.GetAwaiter().GetResult()
                    if (-not $resp.IsSuccessStatusCode) {
                        $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                        foreach ($t in $running.Values) { try { $t.Src.Cancel() } catch {} }
                        throw ("分片 {0} HTTP {1}: {2}" -f $n, [int]$resp.StatusCode, $body)
                    }
                    $etag = $resp.Headers.ETag
                    $Etags[$n] = if ($etag) { $etag.Tag } else { '' }
                    $script:BytesDone += $item.Len
                    $resp.Dispose()
                    $item.Content.Dispose()
                    $item.Src.Dispose()
                    $finished += $n
                }
            }
            foreach ($n in $finished) { $running.Remove($n) }
            if ($finished.Count -gt 0) {
                $script:CurPartNo = [int](@($PartNumbers | Sort-Object)[0])
                Send-Progress
               
                $now = [DateTime]::UtcNow
                if (($now - $lastTune).TotalSeconds -ge 6) {
                    $dt = ($now - $lastTune).TotalSeconds
                    $dB = $script:BytesDone - $lastTuneBytes
                    if ($dt -gt 0.2) {
                        Update-AutoConcurrency -Speed ($dB / $dt)
                    }
                    $lastTune = $now
                    $lastTuneBytes = $script:BytesDone
                }
            }
        } else {
           
            $elapsedSec = [int]([DateTime]::UtcNow - $script:JobStart).TotalSeconds
            $usedTxt = '{0:D2}:{1:D2}' -f [int]($elapsedSec / 60), ($elapsedSec % 60)
            $elapsed = ([DateTime]::UtcNow - $script:JobStart).TotalSeconds
            $spd = if ($elapsed -gt 0.4) { $script:BytesDone / $elapsed } else { 0 }
           
            $concTxt = if ($script:AutoSettled) {
                '{0} 路并发' -f $script:AutoConcurrency
            } elseif ($script:AutoPhase -eq 'trial') {
                '正在测速（{0} 路）' -f $script:AutoConcurrency
            } else {
                '正在测速（{0} 路）' -f $script:AutoConcurrency
            }
            Set-Status `
                ('状态：传输中（' + $script:JobPhase + '）') `
                ('进度：{0} / {1}  （{2:N1}%）  {3}' -f (Format-Size $script:BytesDone), (Format-Size $script:JobTotal), $(if ($script:JobTotal -gt 0) { $script:BytesDone * 100.0 / $script:JobTotal } else { 0 }), $concTxt) `
                ('速度：{0}/s    已用：{1}    剩余：{2}' -f (Format-Size $spd), $usedTxt, (Format-Eta $(if ($spd -gt 1) { ($script:JobTotal - $script:BytesDone) / $spd } else { -1 }))) `
                '提示：正在后台上传，关闭本窗口不影响传输。'
        }
    }
}

$script:CancelRequested = $false
$script:JobStart = 0
$script:LastReport = 0
$script:LastLocalUpdate = [DateTime]::MinValue
$script:LastCancelCheck = [DateTime]::MinValue
$script:LastSpeed = 0
$script:CurPartNo = 0
$script:BytesDone = [int64]0
$script:JobTotal = [int64]0
$script:JobPhase = ''
$script:Skipped = 0
$script:Objects = New-Object System.Collections.ArrayList

function Send-Progress([bool]$force = $false) {
    $now = [DateTime]::UtcNow
   
    $sinceLocal = ($now - $script:LastLocalUpdate).TotalMilliseconds
    $sinceNet = ($now - $script:LastReport).TotalMilliseconds
    if (-not $force -and $sinceLocal -lt 500 -and $sinceNet -lt 1500) { return }
    $elapsed = ($now - $script:JobStart).TotalSeconds
    $speed = if ($elapsed -gt 0.4) { $script:BytesDone / $elapsed } else { 0 }
    $script:LastSpeed = [int64]$speed
    $eta = if ($speed -gt 1) { ($script:JobTotal - $script:BytesDone) / $speed } else { -1 }
    $job = [ordered]@{
        state   = 'running'
        phase   = $script:JobPhase
        total   = [int64]$script:JobTotal
        done    = [int64]$script:BytesDone
        speed   = [int64]$speed
        eta     = [int64]([Math]::Max(0, $eta))
        skipped = $script:Skipped
        objects = @($script:Objects)
    }
    if ($force -or $sinceLocal -ge 500) {
        $script:LastLocalUpdate = $now
        try {
           
            Set-Status `
                ('状态：传输中（' + $script:JobPhase + '）') `
                ('进度：{0} / {1}  （{2:N1}%）' -f (Format-Size $script:BytesDone), (Format-Size $script:JobTotal), $(if ($script:JobTotal -gt 0) { $script:BytesDone * 100.0 / $script:JobTotal } else { 0 })) `
                ('速度：{0}/s    剩余：{1}' -f (Format-Size $speed), (Format-Eta $eta)) `
                ('提示：正在后台上传，关闭本窗口不影响传输。')
        } catch {}
    }
    if (-not ($force -or $sinceNet -ge 1500)) { return }
    $script:LastReport = $now
    try {
       
        $ack = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } -TimeoutSec 20
        if ($ack -and $ack.cancel) { $script:CancelRequested = $true }
    } catch {}
}

function Test-CancelRequested {
    if ($script:CancelRequested) { return $true }
    $now = [DateTime]::UtcNow
    if (($now - $script:LastCancelCheck).TotalMilliseconds -lt 700) { return $false }
    $script:LastCancelCheck = $now
    try {
        $job = [ordered]@{
            state   = 'running'
            phase   = $script:JobPhase
            total   = [int64]$script:JobTotal
            done    = [int64]$script:BytesDone
            speed   = [int64]$script:LastSpeed
            eta     = [int64]0
            skipped = $script:Skipped
            objects = @()
        }
        $ack = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } -TimeoutSec 15
        if ($ack -and $ack.cancel) { $script:CancelRequested = $true }
    } catch {}
    return [bool]$script:CancelRequested
}

function Save-UploadState($state) {
    try { $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StateFile -Encoding UTF8 } catch {}
}
function Load-UploadState {
    try {
        if (Test-Path -LiteralPath $StateFile) {
            return Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        }
    } catch {}
    return $null
}

function Save-Cursor([string]$bootId, [int]$seq) {
    try {
        @{ boot_id = $bootId; seq = $seq } | ConvertTo-Json -Compress |
        Set-Content -LiteralPath $CursorFile -Encoding UTF8
    } catch {}
}
function Read-Cursor {
    try {
        if (Test-Path -LiteralPath $CursorFile) {
            return Get-Content -LiteralPath $CursorFile -Raw -Encoding UTF8 | ConvertFrom-Json
        }
    } catch {}
    return $null
}

function Send-FileObject {
    param([string]$Path, [int64]$Size)
    $key = New-ObjectKey
    $partSize = [int64]$script:PartSize
    $client = Get-HttpClient

    if ($Size -le $partSize) {
        $r = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'single'; key = $key } -TimeoutSec 30
        $buffer = New-Object byte[] $Size
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        try {
            $read = 0
            while ($read -lt $Size) {
                $n = $fs.Read($buffer, $read, [int]($Size - $read))
                if ($n -le 0) { break }
                $read += $n
            }
        } finally { $fs.Dispose() }
        $null = Send-SmallObject -Url $r.url -Buffer $buffer -Count $Size
        $script:BytesDone += $Size
        Send-Progress
        return $key
    }

    $state = Load-UploadState
    $uploadId = $null
    $doneParts = @{}
    $stamp = '{0}|{1}' -f $Path, $Size
    if ($state -and $state.stamp -eq $stamp -and $state.key) {
        $key = $state.key
        $uploadId = $state.upload_id
        try {
            $r = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'parts'; key = $key; upload_id = $uploadId } -TimeoutSec 40
            foreach ($p in $r.parts) { $doneParts[[int]$p.n] = $p.etag }
            Write-Log ("续传：已有 {0} 个分片" -f $doneParts.Count)
        } catch {
            $uploadId = $null; $doneParts = @{}; $key = New-ObjectKey
        }
    }
    if (-not $uploadId) {
        $r = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'init'; key = $key } -TimeoutSec 40
        $uploadId = $r.upload_id
        if ($r.part_size) { $partSize = [int64]$r.part_size }
        Save-UploadState @{ stamp = $stamp; key = $key; upload_id = $uploadId; size = $Size }
        Write-Log ("分片上传初始化 uploadId={0} partSize={1}" -f $uploadId, (Format-Size $partSize))
    }

    $partCount = [int][Math]::Ceiling($Size / [double]$partSize)
    $etags = @{}
    foreach ($n in $doneParts.Keys) { $etags[[int]$n] = $doneParts[$n] }
   
    foreach ($n in $etags.Keys) {
        if ($n -lt $partCount) { $script:BytesDone += $partSize } else { $script:BytesDone += ($Size - ($partCount - 1) * $partSize) }
    }

    $pending = @()
    for ($n = 1; $n -le $partCount; $n++) {
        if ($etags.ContainsKey($n)) { continue }
        $pending += $n
    }
   
    $idx = 0
    while ($idx -lt $pending.Count) {
        $chunk = $pending[$idx..([Math]::Min($idx + 199, $pending.Count - 1))]
        $idx += $chunk.Count
        $r = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'sign'; key = $key; upload_id = $uploadId; parts = @($chunk) } -TimeoutSec 60
        $urlMap = @{}
        foreach ($n in $chunk) { $urlMap[$n] = $r.urls."$n" }
        Send-PartsParallel -FilePath $Path -Urls $urlMap -PartNumbers $chunk `
            -PartSize $partSize -TotalSize $Size -Etags $etags
        Write-Log ("分片批次完成 {0}/{1}" -f $idx, $pending.Count)
    }
   
    if (-not $script:AutoSettled) {
        $script:AutoConcurrency = $script:AutoBestConc
        $script:AutoSettled = $true
        Write-Log ("自适应：收尾，采用已知最优 {0} 路" -f $script:AutoBestConc)
    }

    $parts = @()
    foreach ($n in ($etags.Keys | Sort-Object)) {
        $parts += [ordered]@{ PartNumber = [int]$n; ETag = [string]$etags[$n] }
    }
    Write-Log ("合并分片 {0} 个…" -f $parts.Count)
    $null = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'complete'; key = $key; upload_id = $uploadId; parts = $parts } -TimeoutSec 180
    Write-Log "合并完成"
    try { Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue } catch {}
    return $key
}

function Send-DirectoryObject {
    param([string]$Path, [string]$Label, [int64]$Size)

    $zipKey = New-ObjectKey
    $zipPartSize = [int64]$script:PartSize
    $initResp = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'init'; key = $zipKey } -TimeoutSec 40
    $zipUploadId = $initResp.upload_id
    if ($initResp.part_size) { $zipPartSize = [int64]$initResp.part_size }

    $script:ZipKey = $zipKey
    $script:ZipUploadId = $zipUploadId
    $script:ZipPartNo = 0
    $script:ZipEtags = @{}
    $script:ZipUrls = @{}

    $uploader = [PartHandler]{
        param([byte[]]$partBuf, [int]$partLen)
        $partNo = $script:ZipPartNo + 1
        if (-not $script:ZipUrls.ContainsKey($partNo)) {
            $batch = @()
            for ($k = $partNo; $k -lt $partNo + 100; $k++) { $batch += $k }
            $signResp = Invoke-Relay -Path '/api/t/oss' -Body @{
                op = 'sign'; key = $script:ZipKey; upload_id = $script:ZipUploadId; parts = $batch
            } -TimeoutSec 60
            foreach ($item in $signResp.urls.PSObject.Properties) {
                $script:ZipUrls[[int]$item.Name] = $item.Value
            }
        }
       
        $client = Get-HttpClient
        $content = [System.Net.Http.ByteArrayContent]::new($partBuf, 0, $partLen)
        $content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/octet-stream')
        $src = New-Object System.Threading.CancellationTokenSource
        $task = $client.PutAsync($script:ZipUrls[$partNo], $content, $src.Token)
        $script:ZipPending[$partNo] = @{ Task = $task; Src = $src; Content = $content; Len = $partLen }
        $script:ZipPartNo = $partNo

        $backlog = [Math]::Max(4, $script:AutoConcurrency)
        while ($script:ZipPending.Count -ge $backlog) {
            if (Test-CancelRequested) {
                foreach ($p in $script:ZipPending.Values) { try { $p.Src.Cancel() } catch {} }
                throw [OperationCanceledException]::new('已取消')
            }
            $tasks = @($script:ZipPending.Values | ForEach-Object { $_.Task })
            $null = [System.Threading.Tasks.Task]::WaitAny($tasks, 300)
            Drain-ZipCompleted
            $backlog = [Math]::Max(4, $script:AutoConcurrency)
        }
       
        $zipNow = [DateTime]::UtcNow
        if (($zipNow - $script:ZipLastTune).TotalSeconds -ge 6) {
            $zipDt = ($zipNow - $script:ZipLastTune).TotalSeconds
            $zipDB = $script:BytesDone - $script:ZipLastTuneBytes
            if ($zipDt -gt 0.2) {
                Update-AutoConcurrency -Speed ($zipDB / $zipDt)
            }
            $script:ZipLastTune = $zipNow
            $script:ZipLastTuneBytes = $script:BytesDone
        }
        Send-Progress
    }

    $script:ZipPending = @{}
    $script:ZipLastTune = [DateTime]::UtcNow
    $script:ZipLastTuneBytes = [int64]0

    $sink = [OssSink]::new($zipPartSize, $uploader)
    $zipOk = $false
    try {
        $null = Write-ZipArchive -Sink $sink -BasePath $Path
        $zipOk = $true
    } finally {
        if ($zipOk) {
            $sink.FlushPart()
           
            while ($script:ZipPending.Count -gt 0) {
                $tasks = @($script:ZipPending.Values | ForEach-Object { $_.Task })
                $null = [System.Threading.Tasks.Task]::WaitAny($tasks, 500)
                Drain-ZipCompleted
                Send-Progress
            }
           
            if (-not $script:AutoSettled) {
                $zipDt = ([DateTime]::UtcNow - $script:ZipLastTune).TotalSeconds
                $zipDB = $script:BytesDone - $script:ZipLastTuneBytes
                if ($zipDt -gt 0.5) {
                    Update-AutoConcurrency -Speed ($zipDB / $zipDt)
                }
               
                if (-not $script:AutoSettled) {
                    $script:AutoConcurrency = $script:AutoBestConc
                    $script:AutoSettled = $true
                    Write-Log ("自适应：收尾，采用已知最优 {0} 路" -f $script:AutoBestConc)
                }
            }
        } else {
           
            try {
                $null = Invoke-Relay -Path '/api/t/oss' -Body @{
                    op = 'abort'; key = $script:ZipKey; upload_id = $script:ZipUploadId
                } -TimeoutSec 30
                Write-Log '已取消，OSS 分片已清理'
            } catch { Write-Log ('清理分片失败: ' + $_.Exception.Message) }
        }
    }

    $zipParts = @()
    foreach ($pn in ($script:ZipEtags.Keys | Sort-Object)) {
        $zipParts += [ordered]@{ PartNumber = [int]$pn; ETag = [string]$script:ZipEtags[$pn] }
    }
    $null = Invoke-Relay -Path '/api/t/oss' -Body @{
        op = 'complete'; key = $zipKey; upload_id = $zipUploadId; parts = $zipParts
    } -TimeoutSec 180
    Write-Log ("文件夹打包完成：{0} 个分片，{1}" -f $zipParts.Count, (Format-Size $sink.Written))
    return $zipKey
}

function Write-ZipArchive {
    param($Sink, [string]$BasePath)
    $zip = [System.IO.Compression.ZipArchive]::new(
        $Sink, [System.IO.Compression.ZipArchiveMode]::Create, $true)
    $zipBase = $BasePath.TrimEnd('\')
    $zipBuf = New-Object byte[] (1MB)
    try {
        $zipStack = New-Object System.Collections.Stack
        $zipStack.Push($zipBase)
        while ($zipStack.Count -gt 0) {
            if (Test-CancelRequested) { throw [OperationCanceledException]::new('已取消') }
            $zipCur = $zipStack.Pop()
            $zipDi = [System.IO.DirectoryInfo]::new($zipCur)
            $zipHasChild = $false
            try {
                foreach ($zipItem in $zipDi.EnumerateFileSystemInfos()) {
                    $zipHasChild = $true
                    if ($zipItem -is [System.IO.DirectoryInfo]) { $zipStack.Push($zipItem.FullName); continue }
                    try {
                        $zipRel = $zipItem.FullName.Substring($zipBase.Length).TrimStart('\').Replace('\', '/')
                        $zipEntry = $zip.CreateEntry($zipRel, [System.IO.Compression.CompressionLevel]::NoCompression)
                        $zipEntry.LastWriteTime = $zipItem.LastWriteTime
                        $zipOut = $zipEntry.Open()
                        $zipIn = [System.IO.File]::Open($zipItem.FullName, 'Open', 'Read', 'ReadWrite')
                        try {
                            while (($zipRead = $zipIn.Read($zipBuf, 0, $zipBuf.Length)) -gt 0) {
                               
                                if (Test-CancelRequested) { throw [OperationCanceledException]::new('已取消') }
                                $zipOut.Write($zipBuf, 0, $zipRead)
                            }
                        } finally { $zipIn.Dispose(); $zipOut.Dispose() }
                    } catch [OperationCanceledException] {
                       
                        throw
                    } catch {
                        $script:Skipped++
                        Write-Log ('跳过文件：' + $_.Exception.Message)
                    }
                }
                if (-not $zipHasChild -and $zipCur -ne $zipBase) {
                    $zipRel = $zipCur.Substring($zipBase.Length).TrimStart('\').Replace('\', '/') + '/'
                    try {
                        $zipEmpty = $zip.CreateEntry($zipRel, [System.IO.Compression.CompressionLevel]::NoCompression)
                        $zipEmpty.Open().Dispose()
                    } catch {}
                }
            } catch [OperationCanceledException] {
                throw
            } catch {
                $script:Skipped++
                Write-Log ('跳过目录：' + $_.Exception.Message)
            }
        }
    } finally {
        $zip.Dispose()
    }
    return $Sink.Written
}

function Start-PushJob {
    param($Items)
    $script:CancelRequested = $false
    $script:Objects = New-Object System.Collections.ArrayList
    $script:BytesDone = [int64]0
    $script:Skipped = 0
    $script:JobStart = [DateTime]::UtcNow
    $script:LastReport = [DateTime]::MinValue
    $script:LastLocalUpdate = [DateTime]::MinValue
    $script:LastCancelCheck = [DateTime]::MinValue
   
    Reset-AutoConcurrency

    $totals = @()
    $sum = [int64]0
    for ($i = 0; $i -lt $Items.Count; $i++) {
        $it = $Items[$i]
        $sz = 0
        if ($null -ne $it.size -and [int64]$it.size -gt 0) {
            $sz = [int64]$it.size
        } else {
            $sz = [int64](Get-PathSize -Paths @($it.path)).total
        }
        $totals += $sz
        $sum += $sz
    }
    $script:JobTotal = $sum
    $script:JobPhase = '准备中'
    $script:CurPartNo = 1
    Send-Progress $true

    for ($i = 0; $i -lt $Items.Count; $i++) {
        if (Test-CancelRequested) { throw '已取消' }
        $it = $Items[$i]
        $label = if ($it.label) { $it.label } else { Split-Path -Leaf $it.path }
        $script:JobPhase = if ($it.is_dir) { '打包压缩' } else { '上传中' }
        if ($it.is_dir) {
            $key = Send-DirectoryObject -Path $it.path -Label $label -Size $totals[$i]
            $kind = 'zip'
        } else {
            $key = Send-FileObject -Path $it.path -Size $totals[$i]
            $kind = 'file'
        }
        $null = $script:Objects.Add([ordered]@{
                index = $i; key = $key; size = $totals[$i]; kind = $kind; name = $label
            })
    }

    $job = [ordered]@{
        state = 'done'; phase = '已完成'; total = $script:JobTotal; done = $script:JobTotal
        speed = 0; eta = 0; skipped = $script:Skipped; objects = @($script:Objects)
    }
    try { $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } -TimeoutSec 30 } catch {}
    Set-Status '状态：已连接（空闲）' ('上次完成：' + (Get-Date).ToString('HH:mm:ss')) ('共传输 ' + (Format-Size $script:JobTotal)) ('提示：等待控制端下达新的传输任务。')
    Write-Log ('作业完成，总 ' + (Format-Size $script:JobTotal))
}

function Get-PullDefaultDir {
   
    foreach ($guess in @('{374DE290-123F-4565-9164-39C4925E467B}', 'Desktop')) {
        try {
            $p = [Environment]::GetFolderPath($guess)
            if ($p -and (Test-Path -LiteralPath $p)) { return $p }
        } catch {}
    }
    return [Environment]::GetFolderPath('UserProfile')
}

function Get-FreeFileName([string]$Dir, [string]$Name) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($Name)
    $ext = [System.IO.Path]::GetExtension($Name)
    $cand = Join-Path $Dir $Name
    $n = 1
    while (Test-Path -LiteralPath $cand) {
        $cand = Join-Path $Dir ('{0} ({1}){2}' -f $base, $n, $ext)
        $n++
        if ($n -gt 999) { throw '同名文件太多，请手动改名' }
    }
    return $cand
}

function Save-OssObject {
    param([string]$Url, [string]$DestPath, [int64]$ExpectSize = 0)

    if ($null -eq $script:OssHttp) {
        $script:OssHttp = New-Object System.Net.Http.HttpClient
        $script:OssHttp.Timeout = [TimeSpan]::FromHours(6)
    }
    $tmp = $DestPath + '.relaypart'
    $fs = $null
    $resp = $null
    try {
        $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $Url)
        $resp = $script:OssHttp.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) {
            throw ('OSS 下载失败 HTTP {0}' -f [int]$resp.StatusCode)
        }
        $stream = $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $fs = [System.IO.File]::Create($tmp)
        $buf = New-Object byte[] 131072
        while ($true) {
            if (Test-CancelRequested) { throw [OperationCanceledException]::new('已取消') }
            $n = $stream.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $fs.Write($buf, 0, $n)
            $script:BytesDone += $n
            Send-Progress
        }
        $fs.Flush()
        $fs.Dispose()
        $fs = $null
        $stream.Dispose()
        $got = (Get-Item -LiteralPath $tmp).Length
        if ($ExpectSize -gt 0 -and $got -ne $ExpectSize) {
            throw ('大小不符：期望 {0} 字节，实际 {1} 字节' -f $ExpectSize, $got)
        }
        Move-Item -LiteralPath $tmp -Destination $DestPath -Force
        return $got
    } catch {
        if ($fs) { try { $fs.Dispose() } catch {} }
        if ($resp) { try { $resp.Dispose() } catch {} }
       
        try { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } } catch {}
        throw
    }
}

function Start-PullJob {
    param($Items)
    Write-Log ('反向上传：' + @($Items).Count + ' 个文件待接收')
    $script:CancelRequested = $false
    $script:Objects = New-Object System.Collections.ArrayList
    $script:BytesDone = [int64]0
    $script:Skipped = 0
    $script:JobStart = [DateTime]::UtcNow
    $script:LastReport = [DateTime]::MinValue
    $script:LastLocalUpdate = [DateTime]::MinValue
    $script:LastCancelCheck = [DateTime]::MinValue

    $script:JobTotal = [int64]0
    foreach ($it in $Items) { $script:JobTotal += [int64]($it.size) }
    $script:JobPhase = '接收中'
    Send-Progress $true
    Write-Log ('开始接收 ' + $Items.Count + ' 个文件，共 ' + (Format-Size $script:JobTotal))

    $keys = @($Items | ForEach-Object { [string]$_.key })
    $urls = @{}
    try {
        $r = Invoke-Relay -Path '/api/t/oss' -Body @{ op = 'get'; keys = $keys } -TimeoutSec 90
        if (-not $r.ok) { throw [string]$r.error }
        foreach ($p in $r.urls.PSObject.Properties) { $urls[$p.Name] = $p.Value }
    } catch {
        $job = [ordered]@{
            state = 'error'; phase = '准备失败'; total = $script:JobTotal; done = 0
            speed = 0; eta = 0; skipped = 0; direction = 'in'
            error = ('取下载地址失败：' + $_.Exception.Message); objects = @()
        }
        try { $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } -TimeoutSec 30 } catch {}
        throw
    }

    $failed = 0
    foreach ($it in $Items) {
        if (Test-CancelRequested) { throw '已取消' }
        $key = [string]$it.key
        $name = [string]$it.name
        try {
            $name = ConvertTo-SafeName $name
        } catch {
            $script:Skipped++; $failed++
            Write-Log ('跳过非法文件名：' + $name)
            continue
        }
        $dir = [string]$it.dir
        if ([string]::IsNullOrWhiteSpace($dir)) { $dir = Get-PullDefaultDir }
        try {
            if (Test-ProtectedPath $dir) { throw '不能写入系统目录' }
            if (-not (Test-Path -LiteralPath $dir)) {
                $null = New-Item -ItemType Directory -Path $dir -Force
            }
            $dest = Get-FreeFileName -Dir $dir -Name $name
            $script:JobPhase = '接收中'
            $script:CurrentFile = $name
            Write-Log ('接收：' + $name + ' → ' + $dest)
            $got = Save-OssObject -Url $urls[$key] -DestPath $dest -ExpectSize ([int64]$it.size)
            $null = $script:Objects.Add([ordered]@{
                    index = 0; key = $key; size = $got; kind = 'file'; name = $name
                    path = $dest
                })
        } catch [OperationCanceledException] {
            throw
        } catch {
            if (Test-CancelRequested) { throw '已取消' }
            $script:Skipped++; $failed++
            Write-Log ('失败：' + $name + ' - ' + $_.Exception.Message)
        }
    }

    $state = if ($failed -gt 0) { 'done' } else { 'done' }
    $job = [ordered]@{
        state = $state; phase = '已接收'; total = $script:JobTotal; done = $script:BytesDone
        speed = 0; eta = 0; skipped = $script:Skipped; direction = 'in'
        objects = @($script:Objects)
    }
    try { $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } -TimeoutSec 30 } catch {}
    Set-Status '状态：已连接（空闲）' ('上次接收：' + (Get-Date).ToString('HH:mm:ss')) ('共接收 ' + (Format-Size $script:BytesDone)) ('提示：等待控制端下达新的任务。')
    Write-Log ('接收完成，共 ' + (Format-Size $script:BytesDone) + '，跳过 ' + $script:Skipped + ' 个')
}

function Invoke-Command2($cmd) {
    $seq = $cmd.seq
    $action = $cmd.action
    switch ($action) {
        'list' {
            $data = Get-DirList -Path $cmd.args.path
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data }
        }
        'size' {
            $data = Get-PathSize -Paths @($cmd.args.paths) -NoProgress
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data } -TimeoutSec 300
        }
        'push' {
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = @{ accepted = $true } }
            Start-PushJob -Items @($cmd.args.items)
        }
        'pull' {
           
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = @{ accepted = $true } }
            Start-PullJob -Items @($cmd.args.items)
        }
        'cancel' {
            $script:CancelRequested = $true
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = @{ cancelled = $true } }
        }
        'disconnect' {
           
            Write-Log '收到断开指令，正在退出…'
            Set-Status `
                '状态：已断开' `
                ('断开时间：' + (Get-Date).ToString('HH:mm:ss')) `
                '控制端已让本机下线。' `
                '如需重新连接，请重新双击传输助手。'
            try { $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = @{ disconnected = $true } } -TimeoutSec 10 } catch {}
            Start-Sleep -Milliseconds 800
           
            try { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue } catch {}
            Write-Log '已按控制端要求断开，进程退出'
            exit 0
        }
        'ping' {
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = @{ pong = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) } }
        }
       
        'exists' {
            $data = Get-FsPathInfo $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data }
        }
        'mkdir' {
            $data = New-DirItem $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data }
            Write-Log ('新建文件夹 ' + $data.path)
        }
        'newfile' {
            $data = New-FileItem $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data }
            Write-Log ('新建文件 ' + $data.path)
        }
        'rename' {
            $data = Rename-FsItem $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data }
            Write-Log ('重命名 -> ' + $data.path)
        }
        'copy' {
            $data = Copy-FsItems $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data } -TimeoutSec 300
            Write-Log ('复制完成 ' + $data.done + ' 项 / 失败 ' + @($data.failed).Count + ' 项')
            Set-Status '状态：已连接（空闲）' ('上次操作：复制 ' + $data.done + ' 项') ('文件数：' + $data.files) '提示：等待控制端下达新的传输任务。'
        }
        'move' {
            $data = Move-FsItems $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data } -TimeoutSec 300
            Write-Log ('移动完成 ' + $data.done + ' 项 / 失败 ' + @($data.failed).Count + ' 项')
            Set-Status '状态：已连接（空闲）' ('上次操作：移动 ' + $data.done + ' 项') ('文件数：' + $data.files) '提示：等待控制端下达新的传输任务。'
        }
        'delete' {
            $data = Remove-FsItems $cmd.args
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data } -TimeoutSec 300
            Write-Log ('删除完成 ' + $data.done + ' 项 / 失败 ' + @($data.failed).Count + ' 项')
            Set-Status '状态：已连接（空闲）' ('上次操作：删除 ' + $data.done + ' 项') ('失败：' + @($data.failed).Count + ' 项') '提示：等待控制端下达新的传输任务。'
        }
       
        'v6prep' {
            $data = Start-V6Serve -Files @($cmd.args.files) -Port $cmd.args.port
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data } -TimeoutSec 120
            if ($data.ok) {
                Write-Log ('IPv6 直连就绪：端口 ' + $data.port + '，文件 ' + $data.files + ' 个，地址 ' + (@($data.addrs) -join ', '))
            } else {
                Write-Log ('IPv6 直连启动失败：' + $data.error)
            }
        }
        'v6stop' {
            Stop-V6Serve
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = @{ stopped = $true } }
        }
        'v6stat' {
            $data = Get-V6Status
            $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $seq; ok = $true; data = $data }
        }
        default { throw ('未知指令 ' + $action) }
    }
}

function Get-WorkerLock {
   
    $lockPath = Join-Path $WorkDir 'worker.lock'
    try {
        return [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    } catch {
        return $null
    }
}

function Start-Worker {
    $script:MachineId = Get-MachineId

    $lock = Get-WorkerLock
    if (-not $lock) {
        $oldPid = ''
        try { $oldPid = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue) -replace '\D', '' } catch {}
        Write-Log ("已有 worker 在运行（pid=$oldPid），本实例退出")
        return
    }
    $script:WorkerLock = $lock

    try { Set-Content -LiteralPath $PidFile -Value $PID -Encoding ASCII } catch {}
    Write-Log "worker 启动 pid=$PID machine=$($script:MachineId)"

    $script:LastSeq = 0
    $backoff = 1

    while ($true) {
        try {
            $hello = @{
                machine = $script:MachineId
                host    = $env:COMPUTERNAME
                user    = $env:USERNAME
                admin   = (Test-Admin)
                agent   = 'ps1/1.0'
                disks   = @(Get-DiskList)
                roots   = (Get-KnownRoots)
            }
            $r = Invoke-Relay -Path '/api/t/hello' -Body $hello -TimeoutSec 30
            if ($r.part_size) { $script:PartSize = [int64]$r.part_size }
           
            $script:BootId = [string]$r.boot_id
            $script:LastSeq = 0
            $saved = Read-Cursor
            if ($saved -and $saved.boot_id -eq $script:BootId) {
                $script:LastSeq = [int]$saved.seq
                Write-Log ('沿用指令游标 #{0}' -f $script:LastSeq)
            } else {
                Save-Cursor $script:BootId 0
            }
            $ossOk = if ($r.oss) { '已连接' } else { '已连接（OSS 未配置）' }
            Set-Status ('状态：' + $ossOk) ('设备：' + $env:COMPUTERNAME + ' / ' + $env:USERNAME) ('等待控制端操作…') ('提示：本窗口可关闭，传输仍在后台进行。')
            Write-Log '已连接中继'
            $backoff = 1

            while ($true) {
                try {
                    $pollBody = @{
                        machine      = $script:MachineId
                        after        = $script:LastSeq
                        wait         = 25
                        transport    = $script:TransportRaw
                        ipv6_enabled = [bool]$script:Ipv6ChannelEnabled
                        v6           = $null
                    }
                    if ($script:V6Srv -and $script:V6Srv.Port -gt 0) {
                        $pollBody.v6 = @{
                            port  = $script:V6Srv.Port
                            addrs = @(Get-V6Addresses)
                            files = $script:V6Srv.Count
                        }
                    }
                    $poll = Invoke-Relay -Path '/api/t/poll' -Body $pollBody -TimeoutSec 60
                    if ($poll.boot_id -and ([string]$poll.boot_id -ne [string]$script:BootId)) {
                        Write-Log '中继已重启，重置指令游标'
                        $script:BootId = [string]$poll.boot_id
                        $script:LastSeq = 0
                        Save-Cursor $script:BootId 0
                        continue
                    }
                    foreach ($c in @($poll.cmds)) {
                        if ($null -eq $c) { continue }
                        $script:LastSeq = [Math]::Max([int]$script:LastSeq, [int]$c.seq)
                        Save-Cursor $script:BootId $script:LastSeq
                        Write-Log ('收到指令 #{0} {1}' -f $c.seq, $c.action)
                        try {
                            Invoke-Command2 $c
                        } catch {
                            $msg = $_.Exception.Message
                            Write-Log ('指令失败 #{0}: {1}' -f $c.seq, $msg)
                            try {
                                $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; seq = $c.seq; ok = $false; error = $msg }
                            } catch {}
                           
                            $isCancel = ($msg -eq '已取消') -or
                                        ($_.Exception -is [OperationCanceledException]) -or
                                        ($_.Exception.InnerException -is [OperationCanceledException])
                            if ($isCancel) {
                                $script:JobPhase = '已取消'
                                $job = [ordered]@{ state = 'cancelled'; phase = '已取消'; total = $script:JobTotal; done = $script:BytesDone; speed = 0; eta = 0; skipped = $script:Skipped; objects = @() }
                                try { $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } } catch {}
                               
                                Set-Status `
                                    '状态：已连接（空闲）' `
                                    ('上次取消：' + (Get-Date).ToString('HH:mm:ss')) `
                                    ('已传 ' + (Format-Size $script:BytesDone) + ' / ' + (Format-Size $script:JobTotal)) `
                                    '提示：等待控制端下达新的传输任务。'
                            } else {
                                $job = [ordered]@{ state = 'error'; phase = '失败'; total = $script:JobTotal; done = $script:BytesDone; speed = 0; eta = 0; skipped = $script:Skipped; objects = @(); message = $msg }
                                try { $null = Invoke-Relay -Path '/api/t/report' -Body @{ machine = $script:MachineId; job = $job } } catch {}
                                Set-Status `
                                    '状态：已连接（上次失败）' `
                                    ('失败时间：' + (Get-Date).ToString('HH:mm:ss')) `
                                    ('原因：' + $msg) `
                                    '提示：等待控制端下达新的传输任务。'
                            }
                        }
                    }
                } catch {
                    Write-Log ('轮询异常: ' + $_.Exception.Message)
                    Start-Sleep -Seconds 2
                    break
                }
            }
        } catch {
            Write-Log ('连接异常: ' + $_.Exception.Message)
            Set-Status '状态：正在重连…' ('目标：' + $RelayUrl) ('错误：' + $_.Exception.Message) '----------------------------------------'
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min(15, [Math]::Ceiling($backoff * 1.6))
        }
    }
}

$script:V6Source = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

public class V6Entry
{
    public string Path;
    public string Name;
    public long Size;
    public string Token;
}

public class V6FileServer
{
    private readonly Dictionary<string, V6Entry> map =
        new Dictionary<string, V6Entry>(StringComparer.Ordinal);
    private TcpListener listener;
    private Thread acceptThread;
    private volatile bool running;

    public int Port { get; private set; }
    public long ServedBytes { get { return Interlocked.Read(ref served); } }
    public int Hits { get { return Thread.VolatileRead(ref hits); } }

    private long served;
    private int hits;

    public bool Start(int port, out string err)
    {
        err = null;
        try
        {
            listener = new TcpListener(IPAddress.IPv6Any, port);
            listener.Server.SetSocketOption(SocketOptionLevel.IPv6,
                SocketOptionName.IPv6Only, true);
            listener.Server.SetSocketOption(SocketOptionLevel.Socket,
                SocketOptionName.ReuseAddress, true);
            listener.Start(128);
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            running = true;
            acceptThread = new Thread(AcceptLoop);
            acceptThread.IsBackground = true;
            acceptThread.Start();
            return true;
        }
        catch (Exception e)
        {
            err = e.Message;
            try { if (listener != null) listener.Stop(); } catch { }
            listener = null;
            return false;
        }
    }

    public void Stop()
    {
        running = false;
        try { if (listener != null) listener.Stop(); } catch { }
        listener = null;
    }

    public void Clear() { lock (map) { map.Clear(); } }

    public void Add(string id, string token, string path, string name, long size)
    {
        V6Entry e = new V6Entry();
        e.Path = path; e.Token = token; e.Name = name; e.Size = size;
        lock (map) { map[id] = e; }
    }

    public void Remove(string id) { lock (map) { map.Remove(id); } }

    public int Count { get { lock (map) { return map.Count; } } }

    public string[] Ids()
    {
        lock (map) { return new List<string>(map.Keys).ToArray(); }
    }

    private void AcceptLoop()
    {
        while (running)
        {
            TcpClient c = null;
            try { c = listener.AcceptTcpClient(); }
            catch { if (!running) return; continue; }
            try { ThreadPool.QueueUserWorkItem(Handle, c); }
            catch { try { c.Close(); } catch { } }
        }
    }

    private void Handle(object state)
    {
        TcpClient c = (TcpClient)state;
        try
        {
            c.NoDelay = true;
            c.ReceiveTimeout = 30000;
            c.SendTimeout = 600000;
            NetworkStream ns = c.GetStream();

            string head = ReadHead(ns);
            if (head == null) return;

            string[] lines = head.Split(new string[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0) return;

            string[] req = lines[0].Split(' ');
            if (req.Length < 2) { WriteText(ns, 400, "bad request", null); return; }

            string method = req[0].ToUpperInvariant();
            string target = req[1];
            bool headOnly = (method == "HEAD");
            if (method != "GET" && method != "HEAD")
            {
                WriteText(ns, 405, "method not allowed", null);
                return;
            }

            long rangeStart = -1, rangeEnd = -1;
            bool hasRange = false;
            for (int i = 1; i < lines.Length; i++)
            {
                if (lines[i].Length == 0) break;
                int colon = lines[i].IndexOf(':');
                if (colon <= 0) continue;
                string hn = lines[i].Substring(0, colon).Trim();
                string hv = lines[i].Substring(colon + 1).Trim();
                if (string.Equals(hn, "Range", StringComparison.OrdinalIgnoreCase))
                {
                    if (TryParseRange(hv, out rangeStart, out rangeEnd)) hasRange = true;
                }
            }

            int q = target.IndexOf('?');
            string pathOnly = q >= 0 ? target.Substring(0, q) : target;

            // 探测端点：/ping  或  /ping?t=xxx
            if (pathOnly == "/ping" || pathOnly == "/ping/")
            {
                WriteText(ns, 200, "pong", null);
                return;
            }

            // 取文件：/f/<token>/<id>
            string[] seg = pathOnly.Split('/');
            if (seg.Length < 4 || seg[1] != "f")
            {
                WriteText(ns, 404, "not found", null);
                return;
            }

            string token = Uri.UnescapeDataString(seg[2]);
            string id = Uri.UnescapeDataString(seg[3]);

            V6Entry ent = null;
            lock (map)
            {
                V6Entry tmp;
                if (map.TryGetValue(id, out tmp)) ent = tmp;
            }
            if (ent == null) { WriteText(ns, 404, "no such file", null); return; }
            if (!string.Equals(ent.Token, token, StringComparison.Ordinal))
            {
                WriteText(ns, 403, "bad token", null);
                return;
            }
            if (!File.Exists(ent.Path)) { WriteText(ns, 410, "file gone", null); return; }

            FileStream fs;
            try { fs = new FileStream(ent.Path, FileMode.Open, FileAccess.Read,
                                      FileShare.ReadWrite, 128 * 1024, false); }
            catch (Exception e) { WriteText(ns, 500, e.Message, null); return; }

            try
            {
                long total = fs.Length;
                long start = 0, end = total - 1;
                bool partial = false;

                if (hasRange && rangeStart >= 0)
                {
                    start = rangeStart;
                    end = (rangeEnd >= 0 && rangeEnd < total) ? rangeEnd : total - 1;
                    if (start > end || start >= total)
                    {
                        StringBuilder sbErr = new StringBuilder();
                        sbErr.Append("HTTP/1.1 416 Range Not Satisfiable\r\n");
                        sbErr.Append("Content-Range: bytes */").Append(total).Append("\r\n");
                        sbErr.Append("Content-Length: 0\r\n");
                        sbErr.Append("Accept-Ranges: bytes\r\n");
                        sbErr.Append("Access-Control-Allow-Origin: *\r\n");
                        sbErr.Append("Connection: close\r\n\r\n");
                        byte[] eb = Encoding.ASCII.GetBytes(sbErr.ToString());
                        ns.Write(eb, 0, eb.Length);
                        ns.Flush();
                        return;
                    }
                    partial = true;
                }

                long len = end - start + 1;

                StringBuilder h = new StringBuilder();
                h.Append(partial ? "HTTP/1.1 206 Partial Content\r\n" : "HTTP/1.1 200 OK\r\n");
                h.Append("Content-Type: application/octet-stream\r\n");
                h.Append("Content-Length: ").Append(len).Append("\r\n");
                h.Append("Accept-Ranges: bytes\r\n");
                if (partial)
                    h.Append("Content-Range: bytes ").Append(start).Append('-')
                     .Append(end).Append('/').Append(total).Append("\r\n");
                h.Append("Content-Disposition: attachment; filename*=UTF-8''")
                 .Append(Uri.EscapeDataString(ent.Name)).Append("\r\n");
                h.Append("Access-Control-Allow-Origin: *\r\n");
                h.Append("Access-Control-Expose-Headers: Content-Length,Content-Range,Accept-Ranges\r\n");
                h.Append("Cache-Control: no-store\r\n");
                h.Append("Connection: close\r\n\r\n");

                byte[] hb = Encoding.ASCII.GetBytes(h.ToString());
                ns.Write(hb, 0, hb.Length);
                Interlocked.Increment(ref hits);

                if (headOnly)
                {
                    ns.Flush();
                    return;
                }

                fs.Seek(start, SeekOrigin.Begin);
                byte[] buf = new byte[128 * 1024];
                long left = len;
                while (left > 0)
                {
                    int want = (int)Math.Min((long)buf.Length, left);
                    int got = fs.Read(buf, 0, want);
                    if (got <= 0) break;
                    ns.Write(buf, 0, got);
                    left -= got;
                    Interlocked.Add(ref served, got);
                }
                ns.Flush();
            }
            finally { try { fs.Close(); } catch { } }
        }
        catch { }
        finally { try { c.Close(); } catch { } }
    }

    private static bool TryParseRange(string v, out long start, out long end)
    {
        start = -1; end = -1;
        if (v == null) return false;
        v = v.Trim();
        if (!v.StartsWith("bytes=", StringComparison.OrdinalIgnoreCase)) return false;
        v = v.Substring(6).Trim();
        int comma = v.IndexOf(',');
        if (comma > 0) v = v.Substring(0, comma);
        int dash = v.IndexOf('-');
        if (dash < 0) return false;
        string a = v.Substring(0, dash).Trim();
        string b = v.Substring(dash + 1).Trim();
        if (a.Length == 0) return false;           // 不支持 bytes=-N 这种后缀写法
        if (!long.TryParse(a, out start)) return false;
        if (b.Length > 0 && !long.TryParse(b, out end)) return false;
        return true;
    }

    private static string ReadHead(NetworkStream ns)
    {
        MemoryStream ms = new MemoryStream();
        int b;
        int state = 0;   // 匹配 \r\n\r\n
        while (true)
        {
            b = ns.ReadByte();
            if (b < 0) return ms.Length > 0 ? Encoding.ASCII.GetString(ms.ToArray()) : null;
            ms.WriteByte((byte)b);
            if (b == '\r') { if (state == 0 || state == 2) state++; else state = 0; }
            else if (b == '\n') { if (state == 1) state = 2; else if (state == 3) break; else state = 0; }
            else state = 0;
            if (ms.Length > 16384) break;
        }
        return Encoding.ASCII.GetString(ms.ToArray());
    }

    private static void WriteText(NetworkStream ns, int code, string body, string extra)
    {
        byte[] bb = Encoding.UTF8.GetBytes(body ?? "");
        StringBuilder h = new StringBuilder();
        h.Append("HTTP/1.1 ").Append(code).Append(' ').Append(Reason(code)).Append("\r\n");
        h.Append("Content-Type: text/plain; charset=utf-8\r\n");
        h.Append("Content-Length: ").Append(bb.Length).Append("\r\n");
        h.Append("Access-Control-Allow-Origin: *\r\n");
        if (!string.IsNullOrEmpty(extra)) h.Append(extra);
        h.Append("Connection: close\r\n\r\n");
        byte[] hb = Encoding.ASCII.GetBytes(h.ToString());
        ns.Write(hb, 0, hb.Length);
        ns.Write(bb, 0, bb.Length);
        ns.Flush();
    }

    private static string Reason(int code)
    {
        switch (code)
        {
            case 200: return "OK";
            case 206: return "Partial Content";
            case 400: return "Bad Request";
            case 403: return "Forbidden";
            case 404: return "Not Found";
            case 405: return "Method Not Allowed";
            case 410: return "Gone";
            case 416: return "Range Not Satisfiable";
            default: return "Error";
        }
    }
}
'@

$script:V6Srv = $null

function Get-V6Addresses {
   
    $out = New-Object System.Collections.ArrayList
    try {
        $addrs = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())
        foreach ($a in $addrs) {
            if ($a.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetworkV6) { continue }
            $s = $a.ToString()
            if ($s -match '^fe80:') { continue }
            if ($s -eq '::1') { continue }
            $s = ($s -replace '%.*$', '')
            if (-not $out.Contains($s)) { [void]$out.Add($s) }
        }
    } catch {}
   
    if ($out.Count -eq 0) { return @() }
    return $out.ToArray()
}

function Get-V6Server {
    if ($script:V6Srv) { return $script:V6Srv }
    if (-not ('V6FileServer' -as [type])) {
        Add-Type -TypeDefinition $script:V6Source -Language CSharp -ErrorAction Stop
    }
    $script:V6Srv = New-Object V6FileServer
    return $script:V6Srv
}

function Start-V6Serve {
    param($Files, $Port)
    $srv = Get-V6Server
    if (-not $Port -or [int]$Port -le 0) { $Port = 0 }

    if (-not $srv.Port -or $srv.Port -le 0) {
        $err = ''
        if (-not $srv.Start([int]$Port, [ref]$err)) {
            return @{ ok = $false; error = ('IPv6 端口监听失败：' + $err) }
        }
        Write-Log ('IPv6 文件服务已启动，端口 ' + $srv.Port)
    }

    $srv.Clear()
    $n = 0
    foreach ($f in @($Files)) {
        if (-not $f) { continue }
        $p = [string]$f.path
        if (-not $p) { continue }
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
        $id = [string]$f.id
        if (-not $id) { $id = [guid]::NewGuid().ToString('N') }
        $tok = [string]$f.token
        $nm = [string]$f.name
        if (-not $nm) { $nm = [IO.Path]::GetFileName($p) }
        $sz = 0L
        try { $sz = (Get-Item -LiteralPath $p).Length } catch {}
        $srv.Add($id, $tok, $p, $nm, $sz)
        $n++
    }

    $addrs = Get-V6Addresses
    return @{
        ok      = $true
        port    = $srv.Port
        addrs   = @($addrs)
        files   = $n
    }
}

function Stop-V6Serve {
    if ($script:V6Srv) {
        try { $script:V6Srv.Stop() } catch {}
        try { $script:V6Srv.Clear() } catch {}
        $script:V6Srv = $null
        Write-Log 'IPv6 文件服务已停止'
    }
}

function Get-V6Status {
    $r = @{ running = $false; port = 0; addrs = @(); files = 0; hits = 0; served = 0 }
    if ($script:V6Srv -and $script:V6Srv.Port -gt 0) {
        $r.running = $true
        $r.port = $script:V6Srv.Port
        $r.files = $script:V6Srv.Count
        $r.hits = $script:V6Srv.Hits
        $r.served = $script:V6Srv.ServedBytes
    }
    $r.addrs = @(Get-V6Addresses)
    return $r
}

function Get-DisplayWidth {
    param([string]$Text)
    if (-not $Text) { return 0 }
    $w = 0
    foreach ($ch in $Text.ToCharArray()) {
        $c = [int]$ch
       
        if ($ch -eq '─' -or $ch -eq '│' -or $ch -eq '━' -or $ch -eq '┃' -or
            $ch -eq '█' -or $ch -eq '░' -or $ch -eq '●' -or $ch -eq '·' -or
            $ch -eq '╭' -or $ch -eq '╮' -or $ch -eq '╰' -or $ch -eq '╯') {
            $w += 1
        } elseif (($c -ge 0x1100 -and $c -le 0x115F) -or
                  ($c -ge 0x2E80 -and $c -le 0xA4CF) -or
                  ($c -ge 0xAC00 -and $c -le 0xD7A3) -or
                  ($c -ge 0xF900 -and $c -le 0xFAFF) -or
                  ($c -ge 0xFE30 -and $c -le 0xFE6F) -or
                  ($c -ge 0xFF00 -and $c -le 0xFF60) -or
                  ($c -ge 0xFFE0 -and $c -le 0xFFE6)) {
            $w += 2
        } else {
            $w += 1
        }
    }
    return $w
}

function Format-Pair {
    param([string]$Left, [string]$Right, [int]$Inner)
    $pad = $Inner - (Get-DisplayWidth $Left) - (Get-DisplayWidth $Right)
    if ($pad -lt 1) { $pad = 1 }
    return $Left + (' ' * $pad) + $Right
}

function Format-Bar {
    param([double]$Pct, [int]$Width = 28)
    if ($Pct -lt 0) { $Pct = 0 }
    if ($Pct -gt 100) { $Pct = 100 }
    $n = [int][Math]::Round($Width * $Pct / 100.0)
    if ($n -lt 0) { $n = 0 }
    if ($n -gt $Width) { $n = $Width }
   
    return ('#' * $n) + ('-' * ($Width - $n))
}

function Read-StatusFields {
    param([string]$Text)
    $r = [ordered]@{ State = ''; Percent = -1; Speed = ''; Done = '' }
    foreach ($l in ($Text -split "`r?`n")) {
        $s = $l.Trim()
        if ($s -match '^状态[：:]\s*(.+)$') { $r.State = $Matches[1] }
        elseif ($s -match '^进度[：:]\s*(.+)$') {
            $p = $Matches[1]
            if ($p -match '(\d+(?:\.\d+)?)\s*%') { $r.Percent = [double]$Matches[1] }
            if ($p -match '([\d.]+\s*[KMGT]?B\s*/\s*[\d.]+\s*[KMGT]?B)') { $r.Done = $Matches[1] }
        }
        elseif ($s -match '^速度[：:]\s*(.+)$') { $r.Speed = $Matches[1] }
    }
    return $r
}

function Start-Monitor {
    $host.UI.RawUI.WindowTitle = '服务状态'
    try {
        $host.UI.RawUI.BufferSize = New-Object Management.Automation.Host.Size(110, 3000)
        $host.UI.RawUI.WindowSize = New-Object Management.Automation.Host.Size(76, 24)
    } catch {}
    try { Clear-Host } catch {}

    $addr = ''
    if ($RelayUrl) { $addr = ([string]$RelayUrl).Trim().TrimEnd('/') }
   
    $script:Credit = '工程师：梦宛（EchoEon5）  QQ：742965044'
    if ($addr) {
        $host.UI.RawUI.WindowTitle = '服务状态 - ' + $addr + '  |  ' + $script:Credit
    } else {
        $host.UI.RawUI.WindowTitle = '服务状态  |  ' + $script:Credit
    }

    $inner = 62                                 
    $line = '=' * $inner
    $transport = Get-TransportLabel
    $channel = Get-ActiveChannel
    $note = Get-TransportNote

    Write-Host ''
    Write-Host ('  +' + $line + '+') -ForegroundColor DarkGray
    Write-Host ('  |' + (Format-Pair '  信息传输服务' '* 运行中' $inner) + '|') -ForegroundColor DarkGray
    Write-Host ('  +' + $line + '+') -ForegroundColor DarkGray
    Write-Host ''

    Write-Host '   连接信息' -ForegroundColor DarkGray
    Write-Host ('   ' + ('-' * ($inner - 2))) -ForegroundColor DarkGray
    Write-Host ''

    Write-Host '   控制台' -ForegroundColor DarkGray
    if ($addr) {
        Write-Host ('   ' + $addr) -ForegroundColor White
    } else {
        Write-Host '   未配置（请重新生成 bat）' -ForegroundColor Yellow
    }
    Write-Host ''

    Write-Host '   传输方式' -ForegroundColor DarkGray
    Write-Host ('   ' + $transport) -ForegroundColor Cyan
    Write-Host ''

    Write-Host '   当前通道' -ForegroundColor DarkGray
    Write-Host ('   ' + $channel) -ForegroundColor Green
    Write-Host ''

    Write-Host '   开机自启' -ForegroundColor DarkGray
    if (Test-Autostart) {
        Write-Host '   已开启（下次登录自动连回来）' -ForegroundColor Yellow
    } else {
        Write-Host '   未开启（本次关闭后不会再连）' -ForegroundColor DarkGray
    }
    Write-Host ''

    if ($note) {
        Write-Host '   提示' -ForegroundColor DarkGray
        Write-Host ('   ' + $note) -ForegroundColor Yellow
        Write-Host ''
    }

    Write-Host ('   ' + ('-' * ($inner - 2))) -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '   本窗口可以关闭，传输仍在后台进行。' -ForegroundColor DarkGray
    if (Test-Autostart) {
        Write-Host '   要彻底停止：双击与 bat 同目录的「停止服务.bat」' -ForegroundColor DarkGray
    }
    Write-Host ''

    $last = $null
    while ($true) {
        $text = ''
        try {
            if (Test-Path -LiteralPath $StatusFile) {
                $text = Get-Content -LiteralPath $StatusFile -Raw -Encoding UTF8
            }
        } catch {}
        if (-not $text) { $text = '状态：正在启动…' }

        if ($text -match '^状态[：:]\s*已断开') {
            if ($text -ne $last) {
                Write-Host ''
                Write-Host ('   [{0}] 已断开连接，本窗口即将关闭' -f (Get-Date).ToString('HH:mm:ss')) -ForegroundColor Yellow
                Write-Host ''
                $last = $text
            }
            Start-Sleep -Seconds 2
            try { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue } catch {}
            exit 0
        }

        if ($text -ne $last) {
            $f = Read-StatusFields $text
            if ($f.Percent -ge 0) {
               
                $pctColor = if ($f.Percent -ge 100) { 'Green' } else { 'Cyan' }
                Write-Host ''
                Write-Host ('   ' + (Format-Bar $f.Percent) + ('   {0:N0}%' -f $f.Percent)) `
                    -ForegroundColor $pctColor
                if ($f.Done) {
                    Write-Host ('   已传   ' + $f.Done) -ForegroundColor White
                }
                if ($f.Speed) {
                    Write-Host ('   速度   ' + $f.Speed) -ForegroundColor Green
                }
            } else {
               
                Write-Host ''
                Write-Host ('   [{0}]  ' -f (Get-Date).ToString('HH:mm:ss')) -NoNewline -ForegroundColor DarkGray
                Write-Host $f.State -ForegroundColor White
                if ($f.Speed) {
                    Write-Host ('   速度   ' + $f.Speed) -ForegroundColor Green
                }
            }
            $last = $text
        }
        Start-Sleep -Milliseconds 700
    }
}

function Test-WorkerAlive {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $false }
    try {
        $wpid = [int]((Get-Content -LiteralPath $PidFile -Raw) -replace '\D', '')
        if ($wpid -le 0) { return $false }
        $p = Get-Process -Id $wpid -ErrorAction SilentlyContinue
        if (-not $p) { return $false }
       
        if ($p.ProcessName -notmatch '^(powershell|pwsh)$') { return $false }
        return $true
    } catch { return $false }
}

$script:RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$script:RunName = 'WinUpdSvc'

function Remove-Autostart {
   
    try {
        $existed = Get-ItemProperty -Path $script:RunKey -Name $script:RunName -ErrorAction SilentlyContinue
        if ($existed) {
            Remove-ItemProperty -Path $script:RunKey -Name $script:RunName -ErrorAction Stop
            Write-Log '已取消开机自启'
            return $true
        }
        return $false
    } catch {
        Write-Log ('取消自启失败: ' + $_.Exception.Message)
        return $false
    }
}

function Set-Autostart {
    param([string]$Command)
    try {
        Set-ItemProperty -Path $script:RunKey -Name $script:RunName -Value $Command -ErrorAction Stop
        Write-Log '已登记开机自启'
        return $true
    } catch {
        Write-Log ('自启登记失败: ' + $_.Exception.Message)
        return $false
    }
}

function Test-Autostart {
    try {
        return [bool](Get-ItemProperty -Path $script:RunKey -Name $script:RunName -ErrorAction SilentlyContinue)
    } catch { return $false }
}

function Start-Launcher {
    $self = Join-Path $WorkDir 'agent.ps1'

    $q = { param($s) '"' + ([string]$s).Replace('"', '""') + '"' }
    $u = & $q $RelayUrl
    $k = & $q $DeviceKey
    $t = & $q $Transport
    $runArgs = '-Mode worker'
    if ($RelayUrl) { $runArgs += ' -RelayUrl ' + $u }
    if ($DeviceKey) { $runArgs += ' -DeviceKey ' + $k }
    if ($Transport) { $runArgs += ' -Transport ' + $t }

    if (-not (Test-WorkerAlive)) {
        try { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue } catch {}
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', ('"' + $self + '"'), '-Mode', 'worker')
        if ($RelayUrl) { $argList += @('-RelayUrl', $u) }
        if ($DeviceKey) { $argList += @('-DeviceKey', $k) }
        if ($Transport) { $argList += @('-Transport', $t) }
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden | Out-Null
        Start-Sleep -Milliseconds 1500
    }

    if ($env:AUTOSTART -eq '1') {
        $cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
        $self + '" ' + $runArgs
        [void](Set-Autostart $cmd)
    } else {
        [void](Remove-Autostart)
    }

    Start-Monitor
}

if ($MyInvocation.InvocationName -ne '.') {
    switch ($Mode) {
        'monitor' { Start-Monitor }
        'launcher' { Start-Launcher }
        default { Start-Worker }
    }
}
