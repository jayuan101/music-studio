. (Join-Path $PSScriptRoot 'environment.ps1')
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw 'Rust is not available. Run scripts\install-rust.ps1 in a network-enabled terminal, then run this build again.'
}
$logDirectory = Join-Path $rustWorkspace 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ('build-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.txt')
Start-Transcript -LiteralPath $logPath | Out-Null
try {
    & cargo --version
    if ($LASTEXITCODE -ne 0) { throw 'Cargo could not start.' }
    & cargo fmt --all
    if ($LASTEXITCODE -ne 0) { throw 'Rust formatting failed.' }
    & cargo test --workspace
    if ($LASTEXITCODE -ne 0) { throw 'Rust tests failed.' }
    & cargo clippy --workspace --all-targets
    if ($LASTEXITCODE -ne 0) { throw 'Clippy checks failed.' }
    & cargo build --workspace --release
    if ($LASTEXITCODE -ne 0) { throw 'Rust release build failed.' }
    Write-Host 'Native desktop previews and core utilities built in target\release. Feature parity with the original apps is still in progress.' -ForegroundColor Green
} finally { Stop-Transcript | Out-Null }
