# Installs Music Studio for the current user -- no admin rights needed.
# Per-user location so the in-app updater can write over the install.
$ErrorActionPreference = 'Stop'

$AppName    = 'Music Studio'
$Source     = Join-Path $PSScriptRoot 'app'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\MusicStudio'
$Exe        = Join-Path $InstallDir 'MusicStudio.exe'
$Icon       = Join-Path $InstallDir '_internal\assets\icon.ico'
$StartMenu  = Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"
$Desktop    = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
$UninstKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicStudio'
# Kept outside $InstallDir: the in-app updater mirrors each new build over
# $InstallDir and would delete anything the release zip doesn't contain.
$SetupDir   = Join-Path $env:LOCALAPPDATA 'Programs\MusicStudio-Setup'

if (-not (Test-Path (Join-Path $Source 'MusicStudio.exe'))) {
    throw "Can't find app\MusicStudio.exe next to this script. Copy the whole MusicStudio-Installer folder."
}

Write-Host "Installing $AppName to $InstallDir ..."

# Files in use can't be replaced, so close a running copy first.
Get-Process MusicStudio -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host 'Closing running Music Studio...'
    $_.CloseMainWindow() | Out-Null
    if (-not $_.WaitForExit(10000)) { $_.Kill() }
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
# /MIR so files dropped from a newer build don't linger from an older one.
robocopy $Source $InstallDir /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying files failed (robocopy code $LASTEXITCODE)." }

# Keep the uninstaller where Settings -> Apps can run it.
New-Item -ItemType Directory -Force -Path $SetupDir | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') $SetupDir -Force

$shell = New-Object -ComObject WScript.Shell
foreach ($lnkPath in @($StartMenu, $Desktop)) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $Exe
    $lnk.WorkingDirectory = $InstallDir
    if (Test-Path $Icon) { $lnk.IconLocation = $Icon } else { $lnk.IconLocation = $Exe }
    $lnk.Save()
}

$size = [int]((Get-ChildItem $InstallDir -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
New-Item -Path $UninstKey -Force | Out-Null
$uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$SetupDir\uninstall.ps1`""
$values = @{
    DisplayName     = $AppName
    Publisher       = 'Music Studio'
    InstallLocation = $InstallDir
    DisplayIcon     = $Exe
    UninstallString = $uninstallCmd
}
foreach ($k in $values.Keys) { Set-ItemProperty -Path $UninstKey -Name $k -Value $values[$k] }
Set-ItemProperty -Path $UninstKey -Name EstimatedSize -Value $size -Type DWord
Set-ItemProperty -Path $UninstKey -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $UninstKey -Name NoRepair -Value 1 -Type DWord

Write-Host ''
Write-Host "$AppName installed. Shortcuts were added to the Start Menu and Desktop."
