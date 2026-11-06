"""Exercise the real PowerShell importer with synthetic, never-executed files."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".tmp" / "import-tests"
BASE.mkdir(parents=True, exist_ok=True)
NAMES = ["MusicStudio-Rust.exe"]


def check(directory, label, *, traversal=False, tamper=False, wrong_hash=False):
    archive = directory / f"{label}.zip"
    binaries = {name: f"Synthetic test data: {name}".encode() for name in NAMES}
    manifest = {"formatVersion": 1, "sourceHead": "a" * 40, "runId": "123",
                "binaries": [{"name": name, "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()}
                             for name, data in binaries.items()]}
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("build-evidence/artifacts.json", json.dumps(manifest))
        for name, data in binaries.items():
            output.writestr(f"target/release/{name}", data + (b"tampered" if tamper else b""))
        if traversal:
            output.writestr("../escaped-import-test.txt", "must not be extracted")
    metadata = directory / f"{label}.json"
    metadata.write_text(json.dumps({"sourceHead": "a" * 40, "runId": "123", "binaryNames": NAMES,
        "sha256": "0" * 64 if wrong_hash else hashlib.sha256(archive.read_bytes()).hexdigest()}))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "scripts/import-build.ps1"), "-Archive", str(archive),
        "-MetadataPath", str(metadata), "-VerifyOnly"], capture_output=True, text=True, timeout=30)
    expected_error = ("outside its staging folder" if traversal else
                      "Binary validation failed" if tamper else
                      "ZIP checksum does not match" if wrong_hash else None)
    if expected_error:
        assert result.returncode != 0 and expected_error in result.stderr, result.stdout + result.stderr
    else:
        assert result.returncode == 0 and "Nothing installed" in result.stdout, result.stdout + result.stderr
    print(f"PASS: {label}")


with tempfile.TemporaryDirectory(prefix="verify-", dir=BASE) as temporary:
    directory = Path(temporary)
    assert directory.resolve().is_relative_to(BASE.resolve())
    check(directory, "valid-build-verifies-without-installing")
    check(directory, "wrong-archive-hash-rejected", wrong_hash=True)
    check(directory, "archive-path-traversal-rejected", traversal=True)
    check(directory, "modified-binary-rejected", tamper=True)
print("PASS: four importer checks; no executable was installed or run.")
