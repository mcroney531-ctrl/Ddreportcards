#!/usr/bin/env python3
"""
Reports the installed versions of Ddreportcards' direct runtime
dependencies against the exact pins in requirements.txt, so a future
deployment drifting from the known-good baseline (Phase 4B) is easy to
spot -- a `pip install` that silently picked up something different, a
base image change, or a manual dependency bump that forgot to update
requirements.txt.

Deliberately NOT part of the offline test suite: it depends on which
packages happen to be installed in whatever environment runs it, and that
varies between a bare dev sandbox (which may not have every dependency
installed at all) and the actual deployed Render container -- the offline
suite has to pass in either, so a hard version assertion doesn't belong
there. Run this after any dependency-affecting deploy, or locally after
`pip install -r requirements.txt`, to confirm what's pinned is what's
actually there.

Usage:
  python scripts/check_dependency_versions.py
"""
import importlib.metadata
import pathlib
import re
import sys

REQUIREMENTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"
_PIN_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)(\[[^\]]+\])?==([A-Za-z0-9_.\-+]+)\s*$")


def parse_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in REQUIREMENTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_PATTERN.match(line)
        if not m:
            print(f"warning: could not parse pin from line: {line!r}", file=sys.stderr)
            continue
        name, _extras, version = m.groups()
        pins[name] = version
    return pins


def main() -> int:
    pins = parse_pins()
    if not pins:
        print(f"No pinned dependencies found in {REQUIREMENTS_PATH}", file=sys.stderr)
        return 2

    mismatches = 0
    for dist_name, pinned_version in sorted(pins.items()):
        try:
            installed_version = importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            print(f"[MISSING]   {dist_name}: pinned {pinned_version}, not installed")
            mismatches += 1
            continue
        if installed_version == pinned_version:
            print(f"[OK]        {dist_name}: {installed_version}")
        else:
            print(f"[MISMATCH]  {dist_name}: pinned {pinned_version}, installed {installed_version}")
            mismatches += 1

    print()
    if mismatches:
        print(f"{mismatches} of {len(pins)} pinned dependencies do not match what's installed.")
        return 1
    print(f"All {len(pins)} pinned dependencies match what's installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
