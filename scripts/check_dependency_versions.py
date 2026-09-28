#!/usr/bin/env python3
"""
Reports the installed versions of Ddreportcards' direct runtime
dependencies against the exact pins in requirements.txt, so a future
deployment drifting from the known-good baseline (Phase 4B) is easy to
spot -- a `pip install` that silently picked up something different, a
base image change, or a manual dependency bump that forgot to update
requirements.txt.

Two, and only two, requirement forms are accepted (Stage 2C-6):

  name[optional-extras]==exact-version        ordinary direct dependency
  dynasty-core @ git+https://github.com/mcroney531-ctrl/dynasty-core.git@<40 lowercase hex>

The Git form is a single, deliberate exception for the shared package: one
distribution, one repository, git+https, full commit SHA. Branches, tags,
short SHAs, other repositories, other transports, embedded credentials, and
VCS dependencies for any other package are all rejected, and dynasty-core
is not accepted as an ordinary ==-pin either, since it isn't published to
any package index. Any non-comment line that isn't one of the two forms is
an error, never silently skipped.

For the Git dependency the installed *version* proves nothing; the source
commit is the immutability contract. The installed package's PEP 610
direct_url.json must say vcs == "git", commit_id == the pinned SHA, and
url == the expected repository.

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
import json
import pathlib
import re
import sys

REQUIREMENTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

DYNASTY_CORE_DIST = "dynasty-core"
DYNASTY_CORE_REPO_URL = "https://github.com/mcroney531-ctrl/dynasty-core"

_PIN_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)(\[[^\]]+\])?==([A-Za-z0-9_.\-+]+)$")
_DYNASTY_CORE_GIT_PATTERN = re.compile(
    r"^dynasty-core @ git\+https://github\.com/mcroney531-ctrl/dynasty-core\.git@([0-9a-f]{40})$"
)


class RequirementsError(ValueError):
    """requirements.txt contains a line that isn't one of the allowed forms."""


def normalize_name(name: str) -> str:
    """PEP 503 name normalization, for duplicate detection."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement_line(line: str) -> tuple[str, dict]:
    """Parse one non-comment requirement line into (name, spec).

    spec is {"kind": "version", "value": "<version>", "extras": "[...]" or ""}
    or {"kind": "git", "value": "<40-hex sha>", "repo": DYNASTY_CORE_REPO_URL}.
    Raises RequirementsError for anything else.
    """
    git = _DYNASTY_CORE_GIT_PATTERN.match(line)
    if git:
        return DYNASTY_CORE_DIST, {"kind": "git", "value": git.group(1), "repo": DYNASTY_CORE_REPO_URL}

    pin = _PIN_PATTERN.match(line)
    if pin:
        name, extras, version = pin.groups()
        if normalize_name(name) == DYNASTY_CORE_DIST:
            raise RequirementsError(
                f"{line!r}: dynasty-core may only be pinned as "
                f"'dynasty-core @ git+{DYNASTY_CORE_REPO_URL}.git@<40-char lowercase commit sha>'"
            )
        return name, {"kind": "version", "value": version, "extras": extras or ""}

    if normalize_name(re.split(r"[\s@\[=<>~!;]", line, maxsplit=1)[0]) == DYNASTY_CORE_DIST:
        raise RequirementsError(
            f"{line!r}: dynasty-core must be exactly "
            f"'dynasty-core @ git+{DYNASTY_CORE_REPO_URL}.git@<40-char lowercase commit sha>' "
            "(no branches, tags, short SHAs, other repos, other transports, or credentials)"
        )
    raise RequirementsError(
        f"{line!r}: not an allowed requirement form -- expected 'name[extras]==exact-version' "
        "(VCS/URL dependencies are only allowed for dynasty-core in its immutable form)"
    )


def parse_requirements_text(text: str) -> dict[str, dict]:
    """Parse every non-comment line. Raises RequirementsError listing every
    invalid line and every duplicate (normalized) name; never skips a line."""
    parsed: dict[str, dict] = {}
    seen: dict[str, str] = {}
    errors: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name, spec = parse_requirement_line(line)
        except RequirementsError as exc:
            errors.append(str(exc))
            continue
        key = normalize_name(name)
        if key in seen:
            errors.append(f"{line!r}: duplicate dependency (already declared as {seen[key]!r})")
            continue
        seen[key] = line
        parsed[name] = spec
    if errors:
        raise RequirementsError("invalid requirements.txt:\n  " + "\n  ".join(errors))
    return parsed


def parse_requirements(path: pathlib.Path = REQUIREMENTS_PATH) -> dict[str, dict]:
    return parse_requirements_text(path.read_text())


def parse_pins(path: pathlib.Path = REQUIREMENTS_PATH) -> dict[str, str]:
    """Compatibility helper: {name: exact version} for the ordinary ==-pins.
    Raises (rather than skipping) if any line is invalid."""
    return {name: spec["value"] for name, spec in parse_requirements(path).items() if spec["kind"] == "version"}


def _normalize_repo_url(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url.rstrip("/")


def check_version_install(dist_name: str, pinned: str, metadata=importlib.metadata) -> tuple[str, str]:
    try:
        installed = metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return "MISSING", f"{dist_name}: pinned {pinned}, not installed"
    if installed == pinned:
        return "OK", f"{dist_name}: {installed}"
    return "MISMATCH", f"{dist_name}: pinned {pinned}, installed {installed}"


def check_git_install(dist_name: str, expected_sha: str, expected_repo: str = DYNASTY_CORE_REPO_URL,
                      metadata=importlib.metadata) -> tuple[str, str]:
    """Verify a Git-pinned install from its PEP 610 direct_url.json.
    The resolved vcs_info.commit_id is authoritative; requested_revision is
    diagnostic only."""
    try:
        dist = metadata.distribution(dist_name)
    except metadata.PackageNotFoundError:
        return "MISSING", f"{dist_name}: pinned commit {expected_sha}, not installed"
    raw = dist.read_text("direct_url.json")
    if raw is None:
        return "MISMATCH", f"{dist_name}: no direct_url.json -- not installed from the pinned Git source"
    try:
        info = json.loads(raw)
        vcs_info = info["vcs_info"]
        vcs, commit, url = vcs_info["vcs"], vcs_info["commit_id"], info["url"]
    except (ValueError, TypeError, KeyError) as exc:
        return "MISMATCH", f"{dist_name}: malformed direct_url.json ({exc.__class__.__name__}: {exc})"
    requested = vcs_info.get("requested_revision")
    if vcs != "git":
        return "MISMATCH", f"{dist_name}: installed from vcs {vcs!r}, expected 'git'"
    if _normalize_repo_url(url) != _normalize_repo_url(expected_repo):
        return "MISMATCH", f"{dist_name}: installed from {url!r}, expected {expected_repo!r}"
    if commit != expected_sha:
        return "MISMATCH", (f"{dist_name}: installed commit {commit}, pinned {expected_sha}"
                            + (f" (requested {requested!r})" if requested else ""))
    return "OK", f"{dist_name}: git {commit}" + (f" (requested {requested!r})" if requested else "")


def main() -> int:
    try:
        specs = parse_requirements()
    except RequirementsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not specs:
        print(f"No pinned dependencies found in {REQUIREMENTS_PATH}", file=sys.stderr)
        return 2

    problems = 0
    for name, spec in sorted(specs.items()):
        if spec["kind"] == "git":
            status, message = check_git_install(name, spec["value"], spec["repo"])
        else:
            status, message = check_version_install(name, spec["value"])
        print(f"{'[' + status + ']':<11} {message}")
        problems += status != "OK"

    print()
    if problems:
        print(f"{problems} of {len(specs)} pinned dependencies do not match what's installed.")
        return 1
    print(f"All {len(specs)} pinned dependencies match what's installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
