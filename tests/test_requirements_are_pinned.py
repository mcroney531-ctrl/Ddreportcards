"""
Phase 4B: requirements.txt is now pinned to the exact versions verified
against the Render deployment that produced them (see commit history). This
only tests that every line is a clean, unambiguous ==-pin -- checking
installed-vs-pinned VERSIONS belongs to
scripts/check_dependency_versions.py instead, which is deliberately kept
out of the offline suite since the installed set varies by environment (a
bare dev sandbox may not have every package installed at all) in a way
requirements.txt's own contents do not.

Stage 2C-7: the actual file now holds the eight ==-pins plus exactly one
immutable dynasty-core Git pin (the canonical shared package, replacing the
local dynasty_core/ copy). test_dependency_contract.py proves every other
VCS/URL form is rejected. parse_requirements() covers the whole contract;
parse_pins() intentionally returns only the ordinary version pins.
"""
import pathlib
import unittest

import scripts.check_dependency_versions as check_versions

REQUIREMENTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

# Canonical dynasty-core commit pinned by this app (Stage 2C-7).
CORE_SHA = "cef3c3d2b7120825110235eb2c30b4f8dd9a0247"

EXPECTED_DIRECT_DEPENDENCIES = {
    "google-adk",
    "litellm",
    "anthropic",
    "streamlit",
    "httpx",
    "python-dotenv",
    "fastapi",
    "uvicorn",
}


class RequirementsAreFullyPinnedTest(unittest.TestCase):
    def test_every_non_comment_line_parses_as_an_allowed_form(self):
        lines = [
            line for line in REQUIREMENTS_PATH.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        specs = check_versions.parse_requirements()
        self.assertEqual(
            len(specs), len(lines),
            "every non-comment line must be an exact ==-pin or the immutable dynasty-core Git pin",
        )

    def test_all_eight_direct_dependencies_are_present(self):
        pins = check_versions.parse_pins()
        self.assertEqual(set(pins.keys()), EXPECTED_DIRECT_DEPENDENCIES)

    def test_extras_are_preserved_on_the_two_packages_that_need_them(self):
        text = REQUIREMENTS_PATH.read_text()
        self.assertIn("google-adk[extensions]==", text)
        self.assertIn("uvicorn[standard]==", text)

    def test_eight_version_pins_plus_one_immutable_dynasty_core_pin(self):
        specs = check_versions.parse_requirements()
        self.assertEqual(len(specs), 9)
        self.assertEqual(sum(spec["kind"] == "version" for spec in specs.values()), 8)
        self.assertEqual(len(check_versions.parse_pins()), 8)
        git = {name: spec for name, spec in specs.items() if spec["kind"] == "git"}
        self.assertEqual(list(git), ["dynasty-core"])
        self.assertEqual(git["dynasty-core"]["repo"], "https://github.com/mcroney531-ctrl/dynasty-core")
        self.assertEqual(git["dynasty-core"]["value"], CORE_SHA)


if __name__ == "__main__":
    unittest.main()
