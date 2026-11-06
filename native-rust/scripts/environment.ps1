$ErrorActionPreference = 'Stop'
$rustWorkspace = Split-Path -Parent $PSScriptRoot
if ([IO.Path]::GetPathRoot($rustWorkspace) -ine 'D:\') { throw 'Keep this workspace on D:.' }
$env:CARGO_HOME = (Join-Path $rustWorkspace '.tools\cargo')
$env:RUSTUP_HOME = (Join-Path $rustWorkspace '.tools\rustup')
$env:CARGO_TARGET_DIR = Join-Path $rustWorkspace 'target'
$env:TEMP = (Join-Path $rustWorkspace '.tmp')
$env:TMP = $env:TEMP
foreach ($directory in @($env:CARGO_HOME,$env:RUSTUP_HOME,$env:CARGO_TARGET_DIR,$env:TEMP)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$env:PATH = (Join-Path $env:CARGO_HOME 'bin') + ';' + $env:PATH
Set-Location -LiteralPath $rustWorkspace
