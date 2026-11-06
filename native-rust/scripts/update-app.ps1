param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
if ([IO.Path]::GetPathRoot($project) -ine 'D:\') { throw 'Keep the application on D:.' }
$expected = Get-Content -LiteralPath (Join-Path $project 'ci\latest-build.json') -Raw | ConvertFrom-Json
$app = Get-Content -LiteralPath (Join-Path $project 'app.json') -Raw | ConvertFrom-Json
if ($expected.repository -cne $app.repository -or $expected.repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or [string]$expected.artifactId -notmatch '^\d+$') { throw 'Invalid app build metadata.' }
if ($expected.expiresAt -and [DateTimeOffset]::Parse($expected.expiresAt) -le [DateTimeOffset]::UtcNow) { throw 'This build archive expired. A new verified build and ci/latest-build.json are required.' }
$gh = Get-Command gh.exe -ErrorAction SilentlyContinue
if (-not $gh) { throw 'GitHub CLI is required. Install gh and sign in with gh auth login, then run UPDATE.cmd again.' }
$downloads = Join-Path $project '.tmp\downloads'
New-Item -ItemType Directory -Path $downloads -Force | Out-Null
$env:TEMP = Join-Path $project '.tmp'
$env:TMP = $env:TEMP
$archive = Join-Path $downloads ($app.name + '-' + [guid]::NewGuid().ToString('N') + '.zip')
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = $gh.Source
$start.Arguments = 'api repos/' + $expected.repository + '/actions/artifacts/' + $expected.artifactId + '/zip'
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.WorkingDirectory = $project
$process = New-Object Diagnostics.Process
$process.StartInfo = $start
$stream = $null
try {
    Write-Host ('Downloading verified build for ' + $app.name + ' to D:...')
    if (-not $process.Start()) { throw 'GitHub CLI did not start.' }
    # Copy raw stdout bytes. PowerShell text redirection would corrupt a ZIP.
    $stream = [IO.File]::Open($archive, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    $copy = $process.StandardOutput.BaseStream.CopyToAsync($stream)
    $errors = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(180000)) { $process.Kill(); $process.WaitForExit(); throw 'Download timed out. The installed app was not changed.' }
    $copy.GetAwaiter().GetResult()
    $errorText = $errors.GetAwaiter().GetResult()
    $stream.Dispose(); $stream = $null
    if ($process.ExitCode -ne 0) { throw ('Build download failed. Check gh authentication and network access. ' + $errorText) }
    & (Join-Path $PSScriptRoot 'import-build.ps1') -Archive $archive -VerifyOnly:$VerifyOnly
    if (-not $VerifyOnly) { Write-Host 'Installed the verified build on D:. Close the running Rust app, then use START.cmd to open the updated version.' -ForegroundColor Green }
} finally {
    if ($stream) { $stream.Dispose() }
    $process.Dispose()
    # Keep the ZIP for troubleshooting/reinstall; do not remove user files.
}
