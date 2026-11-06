"""Offline end-to-end updater test. Fake gh emits a ZIP; no network or app execution."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '.tmp' / 'update-tests'
BASE.mkdir(parents=True, exist_ok=True)
app = json.loads((ROOT / 'app.json').read_text(encoding='utf-8-sig'))
with tempfile.TemporaryDirectory(dir=BASE) as temporary:
    root = Path(temporary)
    assert root.resolve().is_relative_to(BASE.resolve())
    for directory in ['scripts', 'ci', 'bin', 'dist']:
        (root / directory).mkdir()
    for name in ['update-app.ps1', 'import-build.ps1']:
        shutil.copyfile(ROOT / 'scripts' / name, root / 'scripts' / name)
    (root / 'app.json').write_text(json.dumps(app))
    marker = root / 'dist/current-build.json'
    marker.write_text('{"existing":"keep this build"}')
    prior = marker.read_bytes()
    payload = b'Never executed synthetic binary\x00\xff\xfe\r\n'
    archive = root / 'fixture.zip'
    manifest = {'formatVersion': 1, 'sourceHead': 'a' * 40, 'runId': '123', 'binaries': [
        {'name': app['executable'], 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}]}
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('build-evidence/artifacts.json', json.dumps(manifest))
        out.writestr('target/release/' + app['executable'], payload)
    metadata = {'repository': app['repository'], 'artifactId': '123', 'runId': '123',
                'sourceHead': 'a' * 40, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'binaryNames': [app['executable']]}
    metadata_file = root / 'ci/latest-build.json'
    metadata_file.write_text(json.dumps(metadata))
    # Only this test process sees the stub. No changes to the real gh executable.
    stub = root / 'bin/gh.exe'
    source = root / 'stub.cs'
    source.write_text('''using System; using System.IO;
public class Stub { public static int Main(string[] args) {
 if(Environment.GetEnvironmentVariable("UPDATER_TEST_FAIL")=="1") {
 Console.Error.WriteLine("synthetic authentication failure"); return 2; }
 using(var input=File.OpenRead(Environment.GetEnvironmentVariable("UPDATER_TEST_ZIP")))
 using(var output=Console.OpenStandardOutput()) { input.CopyTo(output); }
 return 0;
} }''')
    compile_script = root / 'compile.ps1'
    compile_script.write_text("$ErrorActionPreference='Stop'\nAdd-Type -Path '" + str(source) +
                              "' -OutputAssembly '" + str(stub) + "' -OutputType ConsoleApplication\n")
    env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'],
               UPDATER_TEST_ZIP=str(archive), TEMP=str(root), TMP=str(root))
    subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(compile_script)], check=True, capture_output=True, timeout=30, env=env)
    def run(expected=None):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(root / 'scripts/update-app.ps1'), '-VerifyOnly'],
            env=env, capture_output=True, text=True, timeout=40)
        if expected:
            assert result.returncode != 0 and expected in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0 and 'Nothing installed' in result.stdout, result.stdout + result.stderr
        assert marker.read_bytes() == prior
    run()
    print('PASS: raw binary ZIP reaches checksum-verifying importer without text corruption')
    env['UPDATER_TEST_FAIL'] = '1'
    run('Build download failed')
    del env['UPDATER_TEST_FAIL']
    metadata['sha256'] = '0' * 64
    metadata_file.write_text(json.dumps(metadata))
    run('ZIP checksum does not match')
    metadata['artifactId'] = '123 --hostname evil.example'
    metadata_file.write_text(json.dumps(metadata))
    run('Invalid app build metadata')
    print('PASS: failed download, bad checksum and invalid artifact ID retain existing build')
print('PASS: four offline updater checks; no real binary installed or executed')
