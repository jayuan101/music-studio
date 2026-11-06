. (Join-Path $PSScriptRoot 'environment.ps1')
$installerDirectory = (Join-Path $rustWorkspace 'installers')
New-Item -ItemType Directory -Path $installerDirectory -Force | Out-Null

# The GNU host links with MinGW tools, so a PC without Visual Studio or a Windows
# SDK can still test locally. Shipped executables always come from Windows CI (MSVC).
$hasMsvc = $false
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path -LiteralPath $vswhere) {
    $found = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    $hasMsvc = [bool]$found
}
$rustHost = if ($hasMsvc) { 'x86_64-pc-windows-msvc' } else { 'x86_64-pc-windows-gnu' }

$installer = Join-Path $installerDirectory 'rustup-init.exe'
$checksumPath = Join-Path $installerDirectory 'rustup-init.exe.sha256'
$downloadBase = "https://static.rust-lang.org/rustup/dist/$rustHost/rustup-init.exe"
Invoke-WebRequest -Uri $downloadBase -OutFile $installer -UseBasicParsing
Invoke-WebRequest -Uri ($downloadBase + '.sha256') -OutFile $checksumPath -UseBasicParsing
$expected = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0]
if ($expected -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ine $expected) {
    throw 'Rust installer checksum verification failed. Installer was not executed.'
}
Write-Host "Installing $rustHost Rust into this app's .tools folder on D:. The system PATH will not be changed."
& $installer -y --profile minimal --default-host $rustHost --default-toolchain "stable-$rustHost" --no-modify-path
if ($LASTEXITCODE -ne 0) { throw 'Rust installation did not complete.' }
& (Join-Path $env:CARGO_HOME 'bin\rustup.exe') component add rustfmt clippy
if ($LASTEXITCODE -ne 0) { throw 'Could not install rustfmt and clippy.' }

if ($rustHost -like '*-gnu') {
    # Rust's minimal MinGW bundle has dlltool but not the assembler dlltool runs.
    $kit = Join-Path $rustWorkspace '.tools\w64devkit'
    if (-not (Test-Path -LiteralPath (Join-Path $kit 'bin\as.exe'))) {
        $sevenZip = Join-Path $env:ProgramFiles '7-Zip\7z.exe'
        if (-not (Test-Path -LiteralPath $sevenZip)) { throw 'Install 7-Zip so the MinGW kit can be extracted without running it.' }
        $archive = Join-Path $installerDirectory 'w64devkit-x64-2.11.0.7z.exe'
        Invoke-WebRequest -Uri 'https://github.com/skeeto/w64devkit/releases/download/v2.11.0/w64devkit-x64-2.11.0.7z.exe' -OutFile $archive -UseBasicParsing
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine '54c2bb9962e8a30c8270e34c5efc3d5fee579f53087fa7d435b95fbba2af7b2b') {
            throw 'MinGW kit checksum verification failed. It was not extracted.'
        }
        & $sevenZip x $archive ('-o' + (Join-Path $rustWorkspace '.tools')) -y | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $kit 'bin\as.exe'))) { throw 'MinGW kit extraction failed.' }
    }
}
& (Join-Path $env:CARGO_HOME 'bin\rustc.exe') --version
