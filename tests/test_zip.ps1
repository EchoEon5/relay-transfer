
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'client\agent.ps1') -Mode worker

$zipFile = Join-Path $env:TEMP 'zip_selftest.zip'
$srcDir = Join-Path $env:TEMP 'zip_selftest_src'
$outDir = Join-Path $env:TEMP 'zip_selftest_out'
Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $srcDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $outDir -Recurse -Force -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Path $srcDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $srcDir 'sub\deeper') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $srcDir '空目录') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $srcDir 'a.txt') -Value 'hello 中文 内容' -Encoding UTF8
Set-Content -LiteralPath (Join-Path $srcDir 'sub\b.txt') -Value ('x' * 5000) -Encoding UTF8
Set-Content -LiteralPath (Join-Path $srcDir 'sub\deeper\c.txt') -Value 'deep' -Encoding UTF8
Set-Content -LiteralPath (Join-Path $srcDir '中文文件名.txt') -Value 'name test' -Encoding UTF8
[IO.File]::WriteAllBytes((Join-Path $srcDir 'empty.dat'), @())
$rnd = New-Object Random 42
$blob = New-Object byte[] (1024 * 1024)
$rnd.NextBytes($blob)
[IO.File]::WriteAllBytes((Join-Path $srcDir 'sub\big.bin'), $blob)

$outStream = [IO.File]::Open($zipFile, 'Create', 'Write')
$script:partCount = 0
$script:partBytes = [int64]0
$uploader = [PartHandler]{
    param([byte[]]$partBuf, [int]$partLen)
    $script:partCount++
    $script:partBytes += $partLen
    $outStream.Write($partBuf, 0, $partLen)
    $outStream.Flush()
}

$sink = [OssSink]::new(64KB, $uploader)  
$written = Write-ZipArchive -Sink $sink -BasePath $srcDir
$sink.FlushPart()
$outStream.Dispose()
$sink.Dispose()

$onDisk = (Get-Item $zipFile).Length
"写出 {0} 字节 / 分片 {1} 片 / 落盘 {2} 字节" -f $written, $script:partCount, $onDisk
if ($written -ne $onDisk) { throw '分片拼接后长度不一致' }
if ($script:partBytes -ne $onDisk) { throw '回调累计字节与落盘不一致' }

Expand-Archive -LiteralPath $zipFile -DestinationPath $outDir -Force

$bad = 0
foreach ($f in (Get-ChildItem -LiteralPath $srcDir -Recurse -File)) {
    $rel = $f.FullName.Substring($srcDir.Length).TrimStart('\')
    $target = Join-Path $outDir $rel
    if (-not (Test-Path -LiteralPath $target)) { "缺失: $rel"; $bad++; continue }
    $h1 = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
    $h2 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($h1 -ne $h2) { "内容不一致: $rel"; $bad++ }
}
$srcFiles = (Get-ChildItem -LiteralPath $srcDir -Recurse -File).Count
$srcDirs = (Get-ChildItem -LiteralPath $srcDir -Recurse -Directory).Count
$dstDirs = (Get-ChildItem -LiteralPath $outDir -Recurse -Directory).Count
"文件 {0} 个（坏 {1} 个），目录 源 {2} / 解压 {3}" -f $srcFiles, $bad, $srcDirs, $dstDirs

if ($bad -eq 0) { "ZIP 自测通过" } else { throw "ZIP 自测失败：$bad 项异常" }
