param([int]$WaitSeconds = 45, [switch]$LeaveRunning)
# Launches the selected build through START.cmd and proves the window belongs to that exact file.
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$app = Get-Content -LiteralPath (Join-Path $project 'app.json') -Raw | ConvertFrom-Json
$selection = Get-Content -LiteralPath (Join-Path $project 'dist\current-build.json') -Raw | ConvertFrom-Json
$expected = [IO.Path]::GetFullPath((Join-Path $project ('dist\' + $selection.folder + '\' + $app.executable)))
if (-not (Test-Path -LiteralPath $expected -PathType Leaf)) { throw "Selected executable is missing: $expected" }
$name = [IO.Path]::GetFileNameWithoutExtension($app.executable)
if (Get-Process -Name $name -ErrorAction SilentlyContinue) { throw "$name is already running. Close it before verifying." }

Add-Type -AssemblyName System.Drawing
Add-Type -Namespace LaunchCheck -Name Native -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
'@
[void][LaunchCheck.Native]::SetProcessDPIAware()

Start-Process -FilePath (Join-Path $project 'START.cmd') -WorkingDirectory $project -WindowStyle Hidden
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$process = $null
while ((Get-Date) -lt $deadline) {
    $process = Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | Select-Object -First 1
    if ($process) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $process) { throw "No window appeared for $name within $WaitSeconds seconds." }
if ([IO.Path]::GetFullPath($process.Path) -ine $expected) { throw "Running file differs: $($process.Path)" }
$null = $process.Handle  # Keeps the handle open so the exit code stays readable.
Start-Sleep -Seconds 4
$process.Refresh()
if ($process.HasExited) { throw "$name exited during startup with code $($process.ExitCode)." }

[void][LaunchCheck.Native]::SetForegroundWindow($process.MainWindowHandle)
Start-Sleep -Milliseconds 700
$rect = New-Object LaunchCheck.Native+RECT
[void][LaunchCheck.Native]::GetWindowRect($process.MainWindowHandle, [ref]$rect)
$width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top
$shots = Join-Path $project '.tmp\launch-checks'
New-Item -ItemType Directory -Path $shots -Force | Out-Null
$shot = Join-Path $shots ($selection.folder + '.png')
$bitmap = New-Object Drawing.Bitmap $width, $height
$graphics = [Drawing.Graphics]::FromImage($bitmap)
try { $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size); $bitmap.Save($shot, [Drawing.Imaging.ImageFormat]::Png) }
finally { $graphics.Dispose(); $bitmap.Dispose() }

$result = [ordered]@{
    app = $app.name; title = $process.MainWindowTitle; path = $process.Path
    sha256 = (Get-FileHash -LiteralPath $expected -Algorithm SHA256).Hash
    sourceHead = $selection.sourceHead; runId = $selection.runId
    workingSetMB = [math]::Round($process.WorkingSet64 / 1MB, 1); screenshot = $shot
}
if (-not $LeaveRunning) {
    [void]$process.CloseMainWindow()
    if (-not $process.WaitForExit(15000)) { $result.closed = 'did not close within 15 seconds'; }
    else { $result.closed = "clean exit code $($process.ExitCode)" }
}
$json = $result | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $shots 'latest.json'), $json, (New-Object Text.UTF8Encoding $false))
$json
