"""Fail the build when the build environment doesn't satisfy requirements.txt.

v1.4.9 shipped yt-dlp 2026.7.4 while requirements.txt already asked for
>=2026.8.19. YouTube rejects 2026.7.4 with "HTTP Error 403: Forbidden", which
the download panel surfaced only as "The download produced no audio files", so
every download failed and the reason was invisible. The requirement had been
bumped; the venv the build ran from had not, and nothing compared the two.

MusicStudio.spec calls this before PyInstaller does any work, so a stale
environment stops the build instead of producing an app that looks fine and
fails at runtime. Run it directly to check the current environment:

    python scripts/check_requirements.py
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ModuleNotFoundError:  # pragma: no cover
    print("WARNING: `packaging` is not installed; skipping the requirements check.")
    sys.exit(0)

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def _parse(path: Path) -> list[Requirement]:
    reqs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            reqs.append(Requirement(line))
        except Exception:
            print(f"WARNING: could not parse requirement {line!r}; skipping it.")
    return reqs


def check() -> list[str]:
    """Human-readable problems, empty when the environment is good."""
    if not REQUIREMENTS.is_file():
        return [f"{REQUIREMENTS} not found"]

    problems = []
    for req in _parse(REQUIREMENTS):
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            problems.append(f"{req.name}: required {req.specifier or 'any'}, NOT INSTALLED")
            continue
        # An empty specifier means any version is fine.
        if req.specifier and installed not in req.specifier:
            problems.append(
                f"{req.name}: required {req.specifier}, installed {installed}"
            )
    return problems


def main() -> int:
    problems = check()
    if not problems:
        print("requirements check: build environment satisfies requirements.txt")
        return 0

    print("=" * 72, file=sys.stderr)
    print("BUILD STOPPED: this environment does not satisfy requirements.txt", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(file=sys.stderr)
    print("Building anyway would freeze these versions into the app, where the", file=sys.stderr)
    print("failure shows up as a runtime bug rather than a build error -- this is", file=sys.stderr)
    print("exactly how v1.4.9 shipped a yt-dlp that YouTube 403s.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"Fix it with:  {Path(sys.executable).name} -m pip install -U -r requirements.txt", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
