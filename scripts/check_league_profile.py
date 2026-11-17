#!/usr/bin/env python3
"""
Compare GM Command's dynasty_config.json against Ddreportcards' canonical
league profile (config/dynasty_config.py LEAGUE).

Run deliberately whenever league configuration changes, from sibling clones:

  python scripts/check_league_profile.py --gm-config ../gm-command/dynasty_config.json

Local files only: deterministic and offline. The comparison is exact:
the mirror must carry every profile field, with the same value and the same
JSON type (an int is not a float, a bool is not an int), and nothing else
except the documented metadata keys. Values are public league facts; no
secret is read or compared.

Exit codes:
  0  the mirror matches
  1  the mirror disagrees (each difference printed)
  2  the mirror is missing, unreadable, not JSON, or not a JSON object
     (GM's _redirects serves index.html with a 200 for a missing path, so
     HTML in place of JSON is an error, never a "mismatch")
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.dynasty_config import LEAGUE  # noqa: E402

PROFILE_FIELDS = tuple(LEAGUE)
METADATA_KEYS = frozenset({"_note", "_schema"})


class ProfileFileError(ValueError):
    """The mirror could not be read as a JSON object."""


def canonical_profile() -> dict:
    return {field: LEAGUE[field] for field in PROFILE_FIELDS}


def load_mirror(path: pathlib.Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileFileError(f"cannot read {path}: {exc.__class__.__name__}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        hint = " (looks like HTML)" if text.lstrip().startswith("<") else ""
        raise ProfileFileError(f"{path} is not valid JSON{hint}") from exc
    if not isinstance(data, dict):
        raise ProfileFileError(f"{path} must contain a JSON object, got {type(data).__name__}")
    return data


def compare_profile(canonical: dict, mirror: dict) -> list[str]:
    """Return one line per difference; an empty list means the mirror matches."""
    problems = []
    for field, expected in canonical.items():
        if field not in mirror:
            problems.append(f"{field}: missing from the GM mirror (DD={expected!r})")
            continue
        actual = mirror[field]
        if type(actual) is not type(expected) or actual != expected:
            problems.append(
                f"{field}: DD={expected!r} ({type(expected).__name__}) "
                f"GM={actual!r} ({type(actual).__name__})"
            )
    for key in sorted(set(mirror) - set(canonical) - METADATA_KEYS):
        problems.append(f"{key}: unexpected field in the GM mirror (not part of the league profile)")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gm-config", required=True, type=pathlib.Path,
                        help="path to gm-command/dynasty_config.json")
    args = parser.parse_args(argv)
    try:
        mirror = load_mirror(args.gm_config)
    except ProfileFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    problems = compare_profile(canonical_profile(), mirror)
    if problems:
        print(f"GM mirror {args.gm_config} disagrees with config/dynasty_config.py:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"GM mirror {args.gm_config} matches the canonical league profile ({len(PROFILE_FIELDS)} fields).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
