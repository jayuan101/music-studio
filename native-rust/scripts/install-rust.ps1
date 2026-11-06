. (Join-Path $PSScriptRoot 'environment.ps1')
$installerDirectory = (Join-Path $rustWorkspace 'installers')
New-Item -ItemType Directory -Path $installerDirectory -Force | Out-Null
$installer = Join-Path $installerDirectory 'rustup-init.exe'
$checksumPath = Join-Path $installerDirectory 'rustup-init.exe.sha256'
$downloadBase = 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe'
Invoke-WebRequest -Uri $downloadBase -OutFile $installer -UseBasicParsing
Invoke-WebRequest -Uri ($downloadBase + '.sha256') -OutFile $checksumPath -UseBasicParsing
$expected = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0]
if ($expected -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ine $expected) {
    throw 'Rust installer checksum verification failed. Installer was not executed.'
}
Write-Host 'Rust and Cargo will use this app''s .tools folder on D:. The system PATH will not be changed.'
Write-Host 'Windows MSVC C++ Build Tools and the Windows SDK must already be available to compile Windows applications.'
& $installer -y --profile minimal --default-host x86_64-pc-windows-msvc --default-toolchain stable --no-modify-path
if ($LASTEXITCODE -ne 0) { throw 'Rust installation did not complete.' }
& (Join-Path $env:CARGO_HOME 'bin\rustup.exe') component add rustfmt clippy
if ($LASTEXITCODE -ne 0) { throw 'Could not install rustfmt and clippy.' }
