<#
.SYNOPSIS
    Remove Music Studio for the current user.

.DESCRIPTION
    Undoes scripts\install.ps1: shortcuts, the Add or Remove Programs entry,
    and the installed files.

    Your music, library index and settings are deliberately left alone. They
    live outside the install directory (under %APPDATA% and your Music
    folder), so reinstalling picks up exactly where you left off, and
    uninstalling never costs you a library.
#>
[CmdletBinding()]
param(
    [switch]$Silent
)

$ErrorActionPreference = "Continue"

$AppName = "Music Studio"
$ExeName = "MusicStudio.exe"
$RegKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MusicStudio"

$installDir = Split-Path -Parent $PSCommandPath

if (-not $Silent) {
    $answer = $null
    try {
        $answer = [Microsoft.VisualBasic.Interaction]::MsgBox(
            "Remove $AppName?`n`nYour music, library and settings are kept.",
            4 + 32, "Uninstall $AppName")
    } catch {
        # No WinForms available (or a non-interactive host): fall through and
        # proceed, since the user reached this only by choosing Uninstall.
    }
    if ($answer -eq 7) { return }   # 7 = No
}

# -- stop the app ----------------------------------------------------------
Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($ExeName)) -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# -- shortcuts -------------------------------------------------------------
$links = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk")
)
foreach ($link in $links) {
    if (Test-Path $link) { Remove-Item $link -Force -ErrorAction SilentlyContinue }
}

# -- registry --------------------------------------------------------------
if (Test-Path $RegKey) { Remove-Item $RegKey -Recurse -Force -ErrorAction SilentlyContinue }

# -- files -----------------------------------------------------------------
# This script lives inside the directory it is deleting, so the removal is
# handed to a detached process that waits for this one to exit first.
$cmd = "Start-Sleep -Seconds 3; Remove-Item -LiteralPath '$installDir' -Recurse -Force -ErrorAction SilentlyContinue"
Start-Process powershell.exe -ArgumentList @(
    "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", $cmd
) -WindowStyle Hidden

Write-Host "$AppName has been removed. Your music and settings were kept."
