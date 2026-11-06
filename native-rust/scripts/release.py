"""Keep this project, its repository clone and its build records in step.

  release.py sync              Mirror this folder into .repo/native-rust for review and commit.
  release.py pin RUN [PR]      Record a successful Windows run as the build UPDATE.cmd installs.
  release.py record            Record the launch check of the installed build.

Records are written from GitHub and local evidence, so they cannot claim more than happened.
"""
import datetime
import filecmp
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / ".repo"
SOURCE = REPO / "native-rust"
LOCAL_ONLY = {".repo", ".tmp", ".tools", "target", "dist", "logs", "installers", "__pycache__"}


def tracked(path):
    relative = path.relative_to(ROOT)
    if relative.parts[0] in LOCAL_ONLY or path.name == ".env" or path.suffix == ".tmp":
        return False
    return not (relative.parts[:2] == ("tools", "ffmpeg") and path.suffix == ".exe")


def sync():
    wanted = {path.relative_to(ROOT) for path in ROOT.rglob("*") if path.is_file() and tracked(path)}
    existing = {path.relative_to(SOURCE) for path in SOURCE.rglob("*") if path.is_file()}
    for relative in sorted(existing - wanted):
        (SOURCE / relative).unlink()
    for relative in sorted(wanted):
        target = SOURCE / relative
        if not target.exists() or not filecmp.cmp(ROOT / relative, target, shallow=False):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
    workflow = REPO / ".github/workflows/rust-native.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "ci/rust-native.yml", workflow)
    print(subprocess.run(["git", "-C", str(REPO), "status", "--short"], capture_output=True, text=True).stdout)


def gh_json(*args):
    return json.loads(subprocess.run(["gh", *args], check=True, capture_output=True, encoding="utf-8").stdout)


def write_text(name, text):
    (ROOT / name).write_text(text, encoding="utf-8", newline="\n")


def load_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8-sig"))


def repository_note(app, build, pr, installed):
    pull = f"- Pull request: https://github.com/{app['repository']}/pull/{pr}\n" if pr else ""
    state = (f"The installed build was launched through START.cmd on {installed['date']} and its running file "
             f"matched this source and checksum ({installed['sha256']}). Launching is not full acceptance; "
             "see VALIDATION.json and FEATURE-PARITY.md."
             if installed else "This build has not been launched on this PC yet.")
    return (f"# Repository and Windows build\n\n- Repository: https://github.com/{app['repository']}\n"
            f"- Source: main, native-rust/\n{pull}- Tested source: {build['sourceHead']}\n"
            f"- Windows build: {build['runUrl']}\n- Download: {build['artifactUrl']}\n"
            f"- ZIP SHA-256: {build['sha256']}\n- Expires: {build['expiresAt']}\n\n"
            "Run UPDATE.cmd to download and install this pinned executable, then close the running Rust app and "
            "use START.cmd. INSTALL.cmd also accepts a manually downloaded ZIP. Both verify checksums and build "
            f"identity. Original apps remain available.\n\n{state}\n\nFull feature parity remains incomplete; "
            "see FEATURE-PARITY.md.\n\nMetadata and documentation commits can follow the source revision used "
            "for the executable without changing its Rust runtime.\n")


def pin(run_id, pr):
    app = load_json("app.json")
    repository = app["repository"]
    run = gh_json("api", f"repos/{repository}/actions/runs/{run_id}")
    if run["conclusion"] != "success":
        sys.exit(f"Run {run_id} concluded {run['conclusion']}; refusing to pin it.")
    artifacts = [a for a in gh_json("api", f"repos/{repository}/actions/runs/{run_id}/artifacts")["artifacts"] if not a["expired"]]
    if len(artifacts) != 1:
        sys.exit(f"Expected one unexpired artifact, found {len(artifacts)}.")
    artifact = artifacts[0]
    log = subprocess.run(["gh", "run", "view", str(run_id), "-R", repository, "--log"], check=True, capture_output=True, encoding="utf-8", errors="replace").stdout
    counts = re.findall(r"test result: ok\. (\d+) passed; 0 failed", log)
    if "test result: FAILED" in log or not counts:
        sys.exit("Could not confirm passing tests in the run log.")
    tests = sum(int(count) for count in counts)
    build = {"repository": repository, "sourceHead": run["head_sha"], "runId": str(run_id), "artifactId": str(artifact["id"]),
             "sha256": artifact["digest"].removeprefix("sha256:"), "bytes": artifact["size_in_bytes"], "binaryNames": [app["executable"]],
             "runUrl": run["html_url"], "artifactUrl": f"{run['html_url']}/artifacts/{artifact['id']}", "expiresAt": artifact["expires_at"], "pullRequest": pr}
    write_text("ci/latest-build.json", json.dumps(build, indent=2) + "\n")
    validation = {"state": "windows-build-passed-not-yet-installed", "sourceHead": build["sourceHead"], "runId": build["runId"],
                  "testsPassed": tests, "releaseBuild": "passed; exactly one app executable", "installedLocally": False,
                  "launchVerified": False, "originalAppModified": False, "featureParity": "Incomplete. See FEATURE-PARITY.md."}
    write_text("VALIDATION.json", json.dumps(validation, indent=2) + "\n")
    write_text("REPOSITORY.md", repository_note(app, build, pr, None))
    print(f"Pinned run {run_id}: {build['sourceHead']}, {tests} tests, artifact {artifact['id']}.")


def record():
    app = load_json("app.json")
    build = load_json("ci/latest-build.json")
    selection = load_json("dist/current-build.json")
    if selection["sourceHead"] != build["sourceHead"] or str(selection["runId"]) != build["runId"]:
        sys.exit("The installed build differs from ci/latest-build.json. Run UPDATE.cmd first.")
    check = load_json(".tmp/launch-checks/latest.json")
    if check["sourceHead"] != build["sourceHead"] or str(check["runId"]) != build["runId"]:
        sys.exit("The latest launch check is for a different build. Run verify-launch.ps1 first.")
    validation = load_json("VALIDATION.json")
    today = datetime.date.today().isoformat()
    validation.update(state="installed-and-launched", installedLocally=True, launchVerified=True, launchDate=today,
                      launchedFile=check["path"], launchedSha256=check["sha256"], windowTitle=check["title"])
    write_text("VALIDATION.json", json.dumps(validation, indent=2) + "\n")
    write_text("REPOSITORY.md", repository_note(app, build, build.get("pullRequest"), {"date": today, "sha256": check["sha256"]}))
    print(f"Recorded the launch of {check['path']}.")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "sync":
        sync()
    elif action == "pin" and len(sys.argv) >= 3:
        pin(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif action == "record":
        record()
    else:
        sys.exit(__doc__)
