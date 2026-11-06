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
# Local GNU builds need the MinGW kit's assembler and linker. CI uses MSVC instead.
$mingw = Join-Path $rustWorkspace '.tools\w64devkit\bin'
if (Test-Path -LiteralPath $mingw) {
    $env:PATH = $mingw + ';' + $env:PATH
    # The kit supplies the assembler; Rust's own bundled linker has the runtime libraries.
    $env:RUSTFLAGS = '-Clink-self-contained=yes'
}
Set-Location -LiteralPath $rustWorkspace
