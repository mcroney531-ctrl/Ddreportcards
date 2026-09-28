"""
Phase 4B: requirements.txt is now pinned to the exact versions verified
against the Render deployment that produced them (see commit history). This
only tests that every line is a clean, unambiguous ==-pin -- checking
installed-vs-pinned VERSIONS belongs to
scripts/check_dependency_versions.py instead, which is deliberately kept
out of the offline suite since the installed set varies by environment (a
bare dev sandbox may not have every package installed at all) in a way
requirements.txt's own contents do not.
"""
import pathlib
import unittest

import scripts.check_dependency_versions as check_versions

REQUIREMENTS_PATH = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

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
    def test_every_non_comment_line_parses_as_an_exact_pin(self):
        lines = [
            line for line in REQUIREMENTS_PATH.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        pins = check_versions.parse_pins()
        self.assertEqual(
            len(pins), len(lines),
            "every non-comment line in requirements.txt must be an exact ==-pinned dependency",
        )

    def test_all_eight_direct_dependencies_are_present(self):
        pins = check_versions.parse_pins()
        self.assertEqual(set(pins.keys()), EXPECTED_DIRECT_DEPENDENCIES)

    def test_extras_are_preserved_on_the_two_packages_that_need_them(self):
        text = REQUIREMENTS_PATH.read_text()
        self.assertIn("google-adk[extensions]==", text)
        self.assertIn("uvicorn[standard]==", text)

    def test_no_git_dependency_yet(self):
        # Stage 2C-6 taught the checker the immutable dynasty-core Git form,
        # but the real pin arrives in 2C-7. Until then the actual file holds
        # only the eight ==-pins; 2C-7 updates this test deliberately.
        specs = check_versions.parse_requirements()
        self.assertEqual(len(specs), 8)
        self.assertTrue(all(spec["kind"] == "version" for spec in specs.values()))


if __name__ == "__main__":
    unittest.main()
