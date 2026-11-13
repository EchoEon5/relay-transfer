
$root = Split-Path -Parent $PSScriptRoot
$files = Get-ChildItem -LiteralPath $root -Recurse -Filter *.ps1 -File |
Where-Object { $_.FullName -notmatch '\\_diagnose\\' }
$fixed = 0
foreach ($f in $files) {
    $bytes = [IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if (-not $hasBom) {
        $text = [IO.File]::ReadAllText($f.FullName, [Text.Encoding]::UTF8)
        [IO.File]::WriteAllText($f.FullName, $text, (New-Object Text.UTF8Encoding($true)))
        Write-Host "已补 BOM: $($f.Name)"
        $fixed++
    }
}
Write-Host "检查 $($files.Count) 个文件，修复 $fixed 个"
