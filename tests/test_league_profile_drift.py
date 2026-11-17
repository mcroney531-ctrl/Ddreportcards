"""
D4: GM Command's dynasty_config.json is a checked mirror of DD's canonical
league profile (config/dynasty_config.py LEAGUE).

scripts/check_league_profile.py compares the two from a local path only:

  python scripts/check_league_profile.py --gm-config ../gm-command/dynasty_config.json

Exact field set, exact values, strict JSON types. Only documented metadata
keys (_note, _schema) may be added; anything else fails so the mirror can't
grow fields nobody compares. Malformed JSON and HTML (GM's _redirects serves
index.html with a 200 for a missing path) are errors, not mismatches.

These tests use fixture files in a temp dir: no sibling clone, no network.
"""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

import scripts.check_league_profile as clp
from config.dynasty_config import LEAGUE


class _TmpMirror:
    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._dir.name) / "dynasty_config.json"

    def write(self, payload):
        self.path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return self.path

    def close(self):
        self._dir.cleanup()


class CanonicalProfileTest(unittest.TestCase):
    def test_profile_is_exactly_the_league_fields(self):
        self.assertEqual(set(clp.PROFILE_FIELDS), set(LEAGUE))
        self.assertEqual(clp.canonical_profile(), dict(LEAGUE))


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.good = {"_note": "mirror", **LEAGUE}

    def test_identical_profile_passes_and_metadata_is_ignored(self):
        self.assertEqual(clp.compare_profile(clp.canonical_profile(), self.good), [])
        self.assertEqual(clp.compare_profile(clp.canonical_profile(), {**self.good, "_schema": 1}), [])

    def test_changed_values_fail(self):
        for field, value in (("league_id", "999"), ("my_display_name", "BCNH"),
                             ("my_team_name", "Pain"), ("ppr", 1.0), ("scoring_label", "PPR"),
                             ("num_teams", 10), ("taxi_slots", 3)):
            with self.subTest(field):
                problems = clp.compare_profile(clp.canonical_profile(), {**self.good, field: value})
                self.assertEqual(len(problems), 1)
                self.assertIn(field, problems[0])

    def test_wrong_types_fail_even_when_equal_in_python(self):
        for field, value in (("is_dynasty", 1), ("num_teams", 12.0), ("league_id", 1312201057664786432),
                             ("pass_td_pts", 4), ("ppr", "0.5")):
            with self.subTest(field):
                self.assertNotEqual(clp.compare_profile(clp.canonical_profile(), {**self.good, field: value}), [])

    def test_missing_field_fails(self):
        mirror = dict(self.good)
        del mirror["reserve_slots"]
        problems = clp.compare_profile(clp.canonical_profile(), mirror)
        self.assertTrue(any("reserve_slots" in p and "missing" in p for p in problems))

    def test_unknown_non_metadata_field_fails(self):
        problems = clp.compare_profile(clp.canonical_profile(), {**self.good, "draft_year": 2026})
        self.assertTrue(any("draft_year" in p and "unexpected" in p for p in problems))


class LoadAndMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _TmpMirror()

    def tearDown(self):
        self.tmp.close()

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = clp.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_matching_file_exits_zero(self):
        code, _ = self._main("--gm-config", str(self.tmp.write({"_note": "x", **LEAGUE})))
        self.assertEqual(code, 0)

    def test_mismatch_exits_one_and_names_both_sides(self):
        code, output = self._main("--gm-config", str(self.tmp.write({**LEAGUE, "league_id": "999"})))
        self.assertEqual(code, 1)
        self.assertIn("league_id", output)
        self.assertIn("'999'", output)
        self.assertIn(repr(LEAGUE["league_id"]), output)

    def test_unreadable_inputs_exit_two(self):
        cases = {
            "malformed json": "{not json",
            "html served for a missing path": "<!DOCTYPE html><html><body>GM</body></html>",
            "json array": "[]",
            "empty": "",
        }
        for label, payload in cases.items():
            with self.subTest(label):
                code, _ = self._main("--gm-config", str(self.tmp.write(payload)))
                self.assertEqual(code, 2)

    def test_missing_file_exits_two(self):
        code, _ = self._main("--gm-config", str(self.tmp.path.parent / "absent.json"))
        self.assertEqual(code, 2)

    def test_gm_config_argument_is_required_and_no_remote_mode_exists(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            clp.main([])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            clp.main(["--gm-url", "https://example.com/dynasty_config.json"])


if __name__ == "__main__":
    unittest.main()
