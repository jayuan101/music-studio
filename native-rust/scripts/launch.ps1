$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$executableName = 'MusicStudio-Rust.exe'
$candidates = @()
$selectionPath = Join-Path $workspaceRoot 'dist\current-build.json'
if (Test-Path -LiteralPath $selectionPath) {
    $selection = Get-Content -LiteralPath $selectionPath -Raw | ConvertFrom-Json
    if ($selection.folder -notmatch '^[0-9a-f]{40}-run-[0-9]+$') { throw 'The selected build folder is invalid.' }
    $candidates += Join-Path $workspaceRoot ("dist\" + $selection.folder + '\' + $executableName)
}
$candidates += Join-Path $workspaceRoot "target\release\$executableName"
$executable = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $executable) {
    throw 'No built desktop executable is installed here yet. Build with scripts\build.ps1 or import the private Windows CI artifact with scripts\import-build.ps1.'
}
Start-Process -FilePath $executable -WorkingDirectory $workspaceRoot
