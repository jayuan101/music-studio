<#
.SYNOPSIS
    Install Music Studio for the current user.

.DESCRIPTION
    Takes a PyInstaller bundle (dist\MusicStudio) and turns it into a real
    installed Windows application: a copy under %LOCALAPPDATA%\Programs, a
    Start Menu entry, an optional desktop shortcut, and a registration in
    Add or Remove Programs so it can be uninstalled the ordinary way.

    Per-user on purpose. Installing under %LOCALAPPDATA% rather than
    "Program Files" means no administrator prompt, and it is what VS Code,
    Teams and other modern per-user apps do. The trade is that it installs
    for this user account only, which is what a single-user machine wants.

.PARAMETER Source
    The built bundle to install. Defaults to dist\MusicStudio beside this
    repository.

.PARAMETER InstallDir
    Where to install. Defaults to %LOCALAPPDATA%\Programs\Music Studio.

.PARAMETER NoDesktopShortcut
    Skip the desktop shortcut; the Start Menu entry is always created.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Music Studio"),
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = "Stop"

$AppName    = "Music Studio"
$ExeName    = "MusicStudio.exe"
$RegKey     = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicStudio"
$Publisher  = "jayuan101"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Source) { $Source = Join-Path $repoRoot "dist\MusicStudio" }

function Write-Step { param([string]$m) Write-Host "`n>>> $m" -ForegroundColor Cyan }
function Write-OK   { param([string]$m) Write-Host "    [OK]  $m" -ForegroundColor Green }
function Write-Fail { param([string]$m) Write-Host "    [X]   $m" -ForegroundColor Red; exit 1 }

# -- checks ----------------------------------------------------------------
$sourceExe = Join-Path $Source $ExeName
if (-not (Test-Path $sourceExe)) {
    Write-Fail "No build found at $Source. Run: .venv\Scripts\python.exe -m PyInstaller MusicStudio.spec"
}

# Read the version out of the source tree so the Add/Remove Programs entry
# matches what was actually built, rather than a number pasted in here that
# would quietly drift.
$version = "0.0.0"
$initPy = Join-Path $repoRoot "musicstudio\__init__.py"
if (Test-Path $initPy) {
    $m = Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"'
    if ($m) { $version = $m.Matches[0].Groups[1].Value }
}
Write-Step "Installing $AppName $version"

# -- stop any running copy -------------------------------------------------
# A running instance holds its own files open, so the copy below would fail
# partway and leave a half-updated install.
$running = Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($ExeName)) -ErrorAction SilentlyContinue
if ($running) {
    Write-Step "Closing the running copy..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
    Write-OK "Closed"
}

# -- copy ------------------------------------------------------------------
Write-Step "Copying files to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
# /MIR so an upgrade drops files the new build no longer ships, instead of
# leaving stale DLLs behind for the loader to find.
$null = robocopy $Source $InstallDir /MIR /R:2 /W:2 /NFL /NDL /NP /NJH /NJS
if ($LASTEXITCODE -ge 8) { Write-Fail "Copy failed (robocopy $LASTEXITCODE)" }
Write-OK "Files installed"

$installedExe = Join-Path $InstallDir $ExeName
$iconPath = Join-Path $InstallDir "_internal\assets\icon.ico"
if (-not (Test-Path $iconPath)) { $iconPath = $installedExe }

# -- shortcuts -------------------------------------------------------------
function New-Shortcut {
    param([string]$LinkPath, [string]$Target, [string]$Icon, [string]$WorkDir, [string]$Description)
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($LinkPath)
    $sc.TargetPath       = $Target
    $sc.WorkingDirectory = $WorkDir
    $sc.IconLocation     = $Icon
    $sc.Description      = $Description
    $sc.Save()
}

Write-Step "Creating shortcuts..."
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
New-Shortcut -LinkPath (Join-Path $startMenu "$AppName.lnk") -Target $installedExe `
             -Icon $iconPath -WorkDir $InstallDir -Description $AppName
Write-OK "Start Menu"

if (-not $NoDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    New-Shortcut -LinkPath (Join-Path $desktop "$AppName.lnk") -Target $installedExe `
                 -Icon $iconPath -WorkDir $InstallDir -Description $AppName
    Write-OK "Desktop"
}

# -- uninstaller -----------------------------------------------------------
Write-Step "Registering with Add or Remove Programs..."
Copy-Item (Join-Path $PSScriptRoot "uninstall.ps1") (Join-Path $InstallDir "uninstall.ps1") -Force

$sizeKB = [int]((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
New-Item -Path $RegKey -Force | Out-Null
$uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`""
@{
    DisplayName     = $AppName
    DisplayVersion  = $version
    Publisher       = $Publisher
    DisplayIcon     = $iconPath
    InstallLocation = $InstallDir
    UninstallString = $uninstallCmd
    EstimatedSize   = $sizeKB
    NoModify        = 1
    NoRepair        = 1
}.GetEnumerator() | ForEach-Object {
    $type = if ($_.Value -is [int]) { "DWord" } else { "String" }
    New-ItemProperty -Path $RegKey -Name $_.Key -Value $_.Value -PropertyType $type -Force | Out-Null
}
Write-OK "Registered (uninstall from Settings > Apps)"

Write-Host "`n$AppName $version is installed." -ForegroundColor Green
Write-Host "  $installedExe" -ForegroundColor DarkGray
