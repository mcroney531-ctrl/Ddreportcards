"""
Stage 3F: the deployed dependency graph is reproducible and the build
config lives in the repo.

  requirements.txt      intentional direct dependencies (human-owned;
                        test_requirements_are_pinned.py keeps its contract)
  requirements.lock.txt the full resolved graph that Render installs: every
                        package exact-pinned, generated under pip 26.3 on
                        Python 3.12 (see scripts/render_build.sh)

Before this, Render re-resolved transitive dependencies on every build. A
newer transitive release sent pip 25.0.1 into backtracking that ended in a
failed pandas 2.1.0 source build, and the fix (pinning pip) existed only in
the Render dashboard.
"""
import pathlib
import re
import unittest

import scripts.check_dependency_versions as check_versions

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock.txt"
BUILD_SCRIPT = ROOT / "scripts" / "render_build.sh"
RENDER_YAML = ROOT / "render.yaml"


def _norm(name):
    return check_versions.normalize_name(name)


class LockFileTest(unittest.TestCase):
    def test_every_line_is_an_exact_pin_or_the_immutable_core_pin(self):
        # Same strict parser as requirements.txt: no ranges, no local paths,
        # no editable installs, no other VCS forms.
        specs = check_versions.parse_requirements(LOCK)
        self.assertGreater(len(specs), 100)

    def test_direct_dependencies_appear_with_the_same_pins(self):
        direct = check_versions.parse_requirements()
        locked = {_norm(n): s for n, s in check_versions.parse_requirements(LOCK).items()}
        for name, spec in direct.items():
            with self.subTest(name):
                self.assertIn(_norm(name), locked)
                self.assertEqual(locked[_norm(name)]["value"], spec["value"])
                self.assertEqual(locked[_norm(name)]["kind"], spec["kind"])

    def test_pip_itself_is_not_in_the_lock(self):
        self.assertNotIn("pip", {_norm(n) for n in check_versions.parse_requirements(LOCK)})


class BuildConfigTest(unittest.TestCase):
    def test_build_script_pins_pip_installs_the_lock_and_checks(self):
        script = BUILD_SCRIPT.read_text()
        self.assertIn("set -euo pipefail", script)
        self.assertIn('pip install "pip==26.3"', script)
        self.assertIn("pip install -r requirements.lock.txt", script)
        self.assertIn("pip check", script)
        self.assertNotRegex(script, r"pip install -r requirements\.txt")

    def test_render_yaml_uses_the_build_script(self):
        build = re.search(r"^\s*buildCommand:\s*(.+)$", RENDER_YAML.read_text(), re.M).group(1).strip()
        self.assertEqual(build, "bash scripts/render_build.sh")


if __name__ == "__main__":
    unittest.main()
