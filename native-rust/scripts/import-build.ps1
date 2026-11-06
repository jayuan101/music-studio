param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [string]$MetadataPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'ci\latest-build.json'),
    [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
if ([IO.Path]::GetPathRoot($workspaceRoot) -ine 'D:\') { throw 'Keep the Rust workspace on D:.' }
$expected = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
if ($expected.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or $expected.sourceHead -notmatch '^[0-9a-f]{40}$' -or [string]$expected.runId -notmatch '^[0-9]+$') { throw 'Build metadata is invalid.' }
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ine $expected.sha256) { throw 'ZIP checksum does not match the verified GitHub artifact. Nothing was installed.' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stageBase = Join-Path $workspaceRoot '.tmp\imports'
$stage = Join-Path $stageBase ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$stagePrefix = [IO.Path]::GetFullPath($stage).TrimEnd('\') + '\'
try {
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $totalBytes = 0L
        if ($zip.Entries.Count -gt 200) { throw 'Unexpected number of files in the build archive.' }
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $stage $entry.FullName.Replace('/','\')))
            if (-not $target.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive contains a path outside its staging folder.' }
            $totalBytes += $entry.Length
        }
        if ($totalBytes -gt 256MB) { throw 'Unexpected expanded size for this build archive.' }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $stage)
    $manifest = Get-Content -LiteralPath (Join-Path $stage 'build-evidence\artifacts.json') -Raw | ConvertFrom-Json
    if ($manifest.formatVersion -ne 1 -or $manifest.sourceHead -cne $expected.sourceHead -or [string]$manifest.runId -ne [string]$expected.runId) { throw 'The archive is for a different build.' }
    $allowedNames = @('MusicStudio-Rust.exe')
    $names = if ($expected.binaryNames) { @($expected.binaryNames) } else { @($allowedNames) }
    if ($names.Count -lt 1 -or @($names | Select-Object -Unique).Count -ne $names.Count -or @($names | Where-Object { $_ -cnotin $allowedNames }).Count) { throw 'Build metadata contains unsupported executable names.' }
    foreach ($name in $names) {
        $record = @($manifest.binaries | Where-Object name -CEQ $name)
        if ($record.Count -ne 1) { throw "Missing or duplicate manifest entry: $name" }
        $binary = Join-Path $stage ("target\release\$name")
        if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "Missing binary: $name" }
        if ((Get-Item -LiteralPath $binary).Length -ne $record[0].bytes -or (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash -ine $record[0].sha256) { throw "Binary validation failed: $name" }
    }
    if ($VerifyOnly) { Write-Output "Verified ZIP, source revision, and $($names.Count) executables. Nothing installed."; return }
    $folderName = $expected.sourceHead + '-run-' + $expected.runId
    $destination = Join-Path $workspaceRoot ('dist\' + $folderName)
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    foreach ($name in $names) {
        $sourceFile = Join-Path $stage ("target\release\$name")
        $destinationFile = Join-Path $destination $name
        if (Test-Path -LiteralPath $destinationFile) {
            if ((Get-FileHash -LiteralPath $destinationFile -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash) { throw "An existing build file differs: $destinationFile" }
        } else { Copy-Item -LiteralPath $sourceFile -Destination $destinationFile }
    }
    $selection = Join-Path $workspaceRoot 'dist\current-build.json'
    if (Test-Path -LiteralPath $selection) { Copy-Item -LiteralPath $selection -Destination ($selection + '.' + [guid]::NewGuid().ToString('N') + '.backup') }
    @{folder=$folderName; sourceHead=$expected.sourceHead; runId=$expected.runId} | ConvertTo-Json | Set-Content -LiteralPath $selection -Encoding UTF8
    Write-Output "Installed Rust previews to $destination. Use START.cmd. Original apps are unchanged."
} finally {
    $resolvedStage = (Resolve-Path -LiteralPath $stage).Path
    if (-not $resolvedStage.StartsWith($stageBase + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Staging cleanup path is outside the build imports folder.' }
    if ((Get-Item -LiteralPath $resolvedStage -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Staging folder became a link; refusing cleanup.' }
    Remove-Item -LiteralPath $resolvedStage -Recurse -Force
}
