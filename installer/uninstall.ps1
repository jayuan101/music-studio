# Removes Music Studio for the current user. Settings and your music are kept.
$ErrorActionPreference = 'Stop'

$AppName    = 'Music Studio'
$InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\MusicStudio'
$StartMenu  = Join-Path ([Environment]::GetFolderPath('Programs')) "$AppName.lnk"
$Desktop    = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
$UninstKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicStudio'
$SetupDir   = Join-Path $env:LOCALAPPDATA 'Programs\MusicStudio-Setup'

Get-Process MusicStudio -ErrorAction SilentlyContinue | ForEach-Object {
    $_.CloseMainWindow() | Out-Null
    if (-not $_.WaitForExit(10000)) { $_.Kill() }
}

foreach ($p in @($StartMenu, $Desktop)) { Remove-Item $p -Force -ErrorAction SilentlyContinue }
Remove-Item $UninstKey -Recurse -Force -ErrorAction SilentlyContinue

# This script may be running from $SetupDir, so delete from a detached cmd
# once this process has exited.
Start-Process cmd.exe -WindowStyle Hidden -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$InstallDir`" & rmdir /s /q `"$SetupDir`""

Write-Host "$AppName has been uninstalled."
