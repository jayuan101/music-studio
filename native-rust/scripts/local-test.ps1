param([string]$LogName = 'local-test', [Parameter(ValueFromRemainingArguments = $true)][string[]]$CargoArguments)
# Runs Cargo at low priority with few jobs: this PC also hosts other live services.
if (-not $CargoArguments) { $CargoArguments = @('test', '--workspace', '--', '--nocapture') }
. (Join-Path $PSScriptRoot 'environment.ps1')
[Diagnostics.Process]::GetCurrentProcess().PriorityClass = 'BelowNormal'
if (-not $env:CARGO_BUILD_JOBS) { $env:CARGO_BUILD_JOBS = '4' }
$logDirectory = Join-Path $rustWorkspace 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ($LogName + '.txt')
# cmd.exe keeps Cargo's UTF-8 bytes; PowerShell 5.1 redirection would rewrite them as UTF-16.
$commandLine = 'cargo ' + (($CargoArguments | ForEach-Object { '"' + $_ + '"' }) -join ' ') + ' > "' + $logPath + '" 2>&1'
& cmd.exe /d /c $commandLine
$code = $LASTEXITCODE
Get-Content -LiteralPath $logPath -Encoding UTF8 -Tail 40
exit $code
