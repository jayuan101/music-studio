"""Execute helper control flow with fake copying/launching inside test folders."""

import json
import subprocess
import sys

import pytest

from musicstudio.core.update_install import HELPER


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update helper")
@pytest.mark.parametrize("failure", ["none", "copy", "launch", "backup"])
def test_helper_rolls_back_failed_install(tmp_path, failure):
    target, work = tmp_path / "installed", tmp_path / "update"
    target.mkdir()
    source = work / "extracted"
    source.mkdir(parents=True)
    (target / "MusicStudio.exe").write_text("old app")
    (source / "MusicStudio.exe").write_text("new app")
    config = work / "config.json"
    config.write_text(json.dumps({"target": str(target), "work": str(work), "process_id": -1}))
    helper = work / "helper.ps1"
    helper.write_text(HELPER, encoding="utf-8-sig")
    wrapper = work / "test-helper.ps1"
    # No processes are launched or real installations changed: all entry points
    # which have external effects are replaced in this script's scope.
    wrapper.write_text(r'''
param([string]$Helper, [string]$Configuration, [string]$Failure)
$global:copies = 0
$global:launches = 0
function Get-Process { return $null }
function Start-Sleep { }
function robocopy([string]$from, [string]$to) {
    $global:copies++
    New-Item -ItemType Directory -Path $to -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $from 'MusicStudio.exe') -Destination (Join-Path $to 'MusicStudio.exe') -Force
    $global:LASTEXITCODE = 0
    if (($Failure -eq 'copy' -and $global:copies -eq 2) -or ($Failure -eq 'backup' -and $global:copies -eq 1)) { $global:LASTEXITCODE = 8 }
}
function Start-Process {
    $global:launches++
    $process = [pscustomobject]@{}
    $process | Add-Member ScriptMethod WaitForExit { param($milliseconds) return ($Failure -eq 'launch') }
    return $process
}
& $Helper -ConfigPath $Configuration
if ($global:launches -eq 0) { throw 'App was not relaunched' }
''', encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper), str(helper), str(config), failure],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "MusicStudio.exe").read_text() == ("new app" if failure == "none" else "old app")
    assert (work / "backup" / "MusicStudio.exe").read_text() == "old app"
