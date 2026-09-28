"""
Stage 2C-2 (post-convergence audit B1): dynasty_core's FantasyCalc defaults
are owned by the package (dynasty_core.settings) and must not depend on
whatever top-level `config` package the host process happens to have.

Before 2C-2, fantasycalc.py read `config.dynasty_config` from the host at
import time: a different host config silently changed the defaults, and a
malformed one crashed the import. The isolation tests below copy the package
into temporary directories next to different `config` packages and import it
in a fresh interpreter, so the already-imported module in this process can't
mask anything.

`-E` keeps PYTHONPATH from leaking a host repo onto sys.path; user
site-packages stay enabled so httpx resolves however it was installed.
"""
import ast
import inspect
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import dynasty_core
import dynasty_core.fantasycalc as fc
import dynasty_core.settings as settings

PACKAGE_DIR = pathlib.Path(dynasty_core.__file__).resolve().parent
EXPECTED = {"is_dynasty": True, "num_qbs": 2, "num_teams": 12, "ppr": 0.5}

PROBE = """
import inspect, json, sys
import dynasty_core.fantasycalc as fc
d = inspect.signature(fc.get_dynasty_values).parameters
print(json.dumps({
    "is_dynasty": d["is_dynasty"].default,
    "num_qbs": d["num_qbs"].default,
    "num_teams": d["num_teams"].default,
    "ppr": d["ppr"].default,
    "default_key": list(fc._default_params_key()),
    "host_config_loaded": "config.dynasty_config" in sys.modules,
}))
"""

SCENARIOS = {
    "no_config": {},
    "unrelated_config": {
        "config/__init__.py": "",
        "config/settings.py": "SETTINGS = {'unrelated': True}\n",
    },
    "hostile_different_profile": {
        "config/__init__.py": "",
        "config/dynasty_config.py": (
            "LEAGUE = {'is_dynasty': False, 'num_qbs': 1, 'num_teams': 10, 'ppr': 1.0}\n"
        ),
    },
    "malformed_missing_keys": {
        "config/__init__.py": "",
        "config/dynasty_config.py": "LEAGUE = {'num_teams': 12}\n",
    },
}


def _run_scenario(files: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        shutil.copytree(PACKAGE_DIR, root / "dynasty_core",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        proc = subprocess.run(
            [sys.executable, "-E", "-c", PROBE],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
    if proc.returncode != 0:
        raise AssertionError(f"import failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class AmbientConfigIsolationTest(unittest.TestCase):
    def test_every_host_config_scenario_yields_package_defaults(self):
        for name, files in SCENARIOS.items():
            with self.subTest(name):
                result = _run_scenario(files)
                self.assertEqual(
                    {k: result[k] for k in EXPECTED}, EXPECTED,
                    f"{name}: defaults must come from the package, not the host",
                )
                self.assertEqual(result["default_key"], [True, 2, 12, 0.5])
                self.assertFalse(result["host_config_loaded"],
                                 f"{name}: package must not import config.dynasty_config")


class NoHostImportsTest(unittest.TestCase):
    def _imports(self, module):
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                found.append(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
        return found

    def test_package_modules_do_not_import_a_host_config(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            with self.subTest(path.name):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.level == 0:
                        self.assertFalse((node.module or "").split(".")[0] == "config", path.name)
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotEqual(alias.name.split(".")[0], "config", path.name)

    def test_settings_module_has_no_imports_at_all(self):
        self.assertEqual(self._imports(settings), [])

    def test_fantasycalc_takes_defaults_from_package_settings(self):
        self.assertIn(".settings", self._imports(fc))


class SettingsContractTest(unittest.TestCase):
    def test_profile_values(self):
        self.assertIs(settings.FANTASYCALC_IS_DYNASTY, True)
        self.assertEqual(settings.FANTASYCALC_NUM_QBS, 2)
        self.assertEqual(settings.FANTASYCALC_NUM_TEAMS, 12)
        self.assertEqual(settings.FANTASYCALC_PPR, 0.5)

    def test_settings_holds_only_the_fantasycalc_profile(self):
        public = sorted(n for n in vars(settings) if not n.startswith("_"))
        self.assertEqual(public, ["FANTASYCALC_IS_DYNASTY", "FANTASYCALC_NUM_QBS",
                                  "FANTASYCALC_NUM_TEAMS", "FANTASYCALC_PPR"])

    def test_get_dynasty_values_signature_exposes_the_profile(self):
        d = inspect.signature(fc.get_dynasty_values).parameters
        self.assertEqual(
            {k: d[k].default for k in EXPECTED},
            {"is_dynasty": settings.FANTASYCALC_IS_DYNASTY, "num_qbs": settings.FANTASYCALC_NUM_QBS,
             "num_teams": settings.FANTASYCALC_NUM_TEAMS, "ppr": settings.FANTASYCALC_PPR},
        )
        self.assertEqual(fc._default_params_key(), (True, 2, 12, 0.5))

    def test_no_argument_call_sends_the_same_provider_request_as_before(self):
        from unittest import mock
        saved = dict(fc._values_cache), fc._index_cache
        fc._values_cache.clear()
        try:
            resp = mock.Mock()
            resp.json.return_value = []
            resp.raise_for_status.return_value = None
            with mock.patch.object(fc.httpx, "get", return_value=resp) as m:
                fc.get_dynasty_values()
            m.assert_called_once_with(
                "https://api.fantasycalc.com/values/current",
                params={"isDynasty": "true", "numQbs": 2, "numTeams": 12, "ppr": 0.5},
                timeout=20,
            )
        finally:
            fc._values_cache.clear()
            fc._values_cache.update(saved[0])
            fc._index_cache = saved[1]


if __name__ == "__main__":
    unittest.main()
