"""Validate update packages and generate a recoverable Windows install helper."""

import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile


def extract_package(zip_path: Path, destination: Path) -> Path:
    """Reject incomplete or unsafe archives before touching the installation."""
    with zipfile.ZipFile(zip_path) as archive:
        names = set()
        total = 0
        for entry in archive.infolist():
            name = entry.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or ":" in name
                    or any(part.endswith((".", " ")) for part in path.parts)
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Update archive contains an unsafe path")
            if name.casefold() in names:
                raise ValueError("Update archive contains duplicate paths")
            names.add(name.casefold())
            total += entry.file_size
        if "musicstudio.exe" not in names or not any(n.startswith("_internal/") for n in names):
            raise ValueError("Update package is missing MusicStudio.exe or its runtime")
        if total > shutil.disk_usage(zip_path.parent).free:
            raise OSError("Not enough disk space to extract the update")
        if archive.testzip() is not None:
            raise ValueError("Update archive failed its integrity check")
        destination.mkdir(parents=True, exist_ok=False)
        archive.extractall(destination)
    executable = destination / "MusicStudio.exe"
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Update executable is invalid")
    return destination


# Paths are passed in JSON, never interpolated as PowerShell code. Both mirror
# operations use the same validated installation directory. Keep backups and
# logs on disk so a failed rollback can be recovered manually.
HELPER = r'''
param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$update = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($update.target)
$work = [IO.Path]::GetFullPath($update.work)
if ($target -eq [IO.Path]::GetPathRoot($target)) { throw 'Invalid install directory' }
if (-not (Test-Path -LiteralPath (Join-Path $target 'MusicStudio.exe'))) { throw 'Installation missing' }
$source = [IO.Path]::GetFullPath((Join-Path $work 'extracted'))
$backup = [IO.Path]::GetFullPath((Join-Path $work 'backup'))
if ($source -eq $target -or $target.StartsWith($work + '\', [StringComparison]::OrdinalIgnoreCase) -or $work.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Overlapping update directories' }
$log = Join-Path $work 'apply-update.log'
function Mirror([string]$from, [string]$to) {
    & robocopy $from $to /MIR /R:3 /W:1 /XJ /NP /NFL /NDL | Out-File -LiteralPath $log -Append
    if ($LASTEXITCODE -ge 8) { throw "Copy failed: $LASTEXITCODE" }
}
$old = Get-Process -Id $update.process_id -ErrorAction SilentlyContinue
if ($old -and $old.Path -eq (Join-Path $target 'MusicStudio.exe')) { $old.WaitForExit() }
Start-Sleep -Seconds 2
$backupComplete = $false
try {
    # Complete backup before any replacement begins.
    Mirror $target $backup
    $backupComplete = $true
    Mirror $source $target
    $started = Start-Process -FilePath (Join-Path $target 'MusicStudio.exe') -WorkingDirectory $target -WindowStyle Hidden -PassThru
    if ($started.WaitForExit(5000)) { throw 'Updated app exited during startup' }
    'Update installed. Backup retained in this directory.' | Out-File -LiteralPath $log -Append
} catch {
    $_ | Out-File -LiteralPath $log -Append
    if ($backupComplete) { Mirror $backup $target }
    Start-Process -FilePath (Join-Path $target 'MusicStudio.exe') -WorkingDirectory $target -WindowStyle Hidden
    'Previous version restored.' | Out-File -LiteralPath $log -Append
}
'''


def launch(zip_path: Path, target: Path) -> None:
    zip_path, target = zip_path.resolve(), target.resolve()
    work = zip_path.parent
    if target == Path(target.anchor) or target in work.parents or work in target.parents or work == target:
        raise ValueError("Update staging directory must be outside the installation")
    if not (target / "MusicStudio.exe").is_file():
        raise ValueError("Installation executable is missing")
    extract_package(zip_path, work / "extracted")
    install_size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    if shutil.disk_usage(work).free < install_size:
        raise OSError("Not enough space to back up the installed app")
    helper = work / "apply-update.ps1"
    helper.write_text(HELPER, encoding="utf-8-sig")
    config = work / "apply-update.json"
    config.write_text(json.dumps({"target": str(target), "work": str(work), "process_id": os.getpid()}), encoding="utf-8")
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(helper), "-ConfigPath", str(config)],
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
    )
