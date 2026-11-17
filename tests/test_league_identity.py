"""
D4: one league identity per app, and two meanings kept apart.

config/dynasty_config.py LEAGUE is Ddreportcards' single source of deployment
identity (league_id, league/team/display names) and verified league facts.
Before D4 the FastAPI app read it while the Streamlit app read a separate
SLEEPER_LEAGUE_ID / ROSTER_OWNER_ID pair from the environment, and the deployed
Streamlit secret still named "BCNH", the hard-coded test account from Phase 1
that Stage 0 removed from the code default but not from .env.example.

DD's num_teams / num_qbs / ppr / is_dynasty are verified *league facts*.
dynasty_core.settings holds numerically identical FantasyCalc *query
defaults*, chosen to price this league. Neither is derived from the other;
the invariant below says they must agree.
"""
import ast
import json
import os
import pathlib
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import dynasty_core.settings as core_settings
from config.dynasty_config import LEAGUE

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_PATH = ROOT / "app.py"
IDENTITY_ENV = ("SLEEPER_LEAGUE_ID", "ROSTER_OWNER_ID")

# /league as served before D4 (8b485ba). D4 must not change its JSON.
LEAGUE_BEFORE_D4 = {
    "active_roster_size": 24, "is_dynasty": True, "league_id": "1312201057664786432",
    "league_name": "Dynasty Daddies", "my_display_name": "TitansTrev55",
    "my_team_name": "The Psych Ward", "num_qbs": 2, "num_teams": 12, "pass_td_pts": 4.0,
    "ppr": 0.5, "reserve_slots": 4, "scoring_label": "half-PPR", "taxi_slots": 4,
}


def _app_identity(env: dict) -> dict:
    """Run app.py's module-level identity assignments under a given environment."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in ("LEAGUE_ID", "OWNER") for t in n.targets)]
    ns = {"os": os, "LEAGUE": LEAGUE}
    with mock.patch.dict(os.environ, env, clear=False):
        exec(compile(ast.Module(body=wanted, type_ignores=[]), str(APP_PATH), "exec"), ns)
    return {"LEAGUE_ID": ns.get("LEAGUE_ID"), "OWNER": ns.get("OWNER")}


def _runtime_sources():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("tests", "research", "docs") or any(p.startswith(".") for p in rel.parts):
            continue
        if "site-packages" in rel.parts or "__pycache__" in rel.parts:
            continue
        yield rel, path.read_text(encoding="utf-8")


class OneIdentitySourceTest(unittest.TestCase):
    def test_streamlit_and_api_resolve_the_same_identity_from_config(self):
        # An environment that still carries the old fork must not win.
        app = _app_identity({"SLEEPER_LEAGUE_ID": "999", "ROSTER_OWNER_ID": "BCNH"})
        self.assertEqual(app["LEAGUE_ID"], LEAGUE["league_id"])
        self.assertEqual(app["OWNER"], LEAGUE["my_display_name"])
        self.assertEqual(api.LEAGUE_ID, LEAGUE["league_id"])

    def test_app_identity_does_not_need_the_environment(self):
        clean = {k: v for k, v in os.environ.items() if k not in IDENTITY_ENV}
        with mock.patch.dict(os.environ, clean, clear=True):
            app = _app_identity({})
        self.assertEqual(app, {"LEAGUE_ID": LEAGUE["league_id"], "OWNER": LEAGUE["my_display_name"]})

    def test_no_runtime_code_reads_identity_from_the_environment(self):
        for rel, src in _runtime_sources():
            for key in IDENTITY_ENV:
                with self.subTest(file=str(rel), key=key):
                    self.assertNotIn(key, src)

    def test_secret_bridge_syncs_only_real_secrets(self):
        tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_init_secrets")
        keys = {c.value for c in ast.walk(fn) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]+", c.value)}
        self.assertEqual(keys, {"ANTHROPIC_API_KEY", "GOOGLE_API_KEY"})


class StaleIdentityGoneTest(unittest.TestCase):
    def test_env_example_has_no_identity_mirror_but_keeps_secrets(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for stale in ("BCNH", *IDENTITY_ENV):
            with self.subTest(stale):
                self.assertNotIn(stale, text)
        for secret in ("ANTHROPIC_API_KEY=", "GOOGLE_API_KEY="):
            with self.subTest(secret):
                self.assertIn(secret, text)

    def test_readme_has_no_test_account_or_identity_env(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for stale in ("BCNH", *IDENTITY_ENV):
            with self.subTest(stale):
                self.assertNotIn(stale, text)


class LeagueEndpointCompatibilityTest(unittest.TestCase):
    def test_league_json_is_unchanged(self):
        body = TestClient(api.app).get("/league").json()
        self.assertEqual(json.dumps(body, sort_keys=True), json.dumps(LEAGUE_BEFORE_D4, sort_keys=True))


class LeagueFactsMatchFantasyCalcProfileTest(unittest.TestCase):
    """Facts stay DD-owned; FantasyCalc defaults stay package-owned; they must agree."""

    PAIRS = (
        ("num_teams", "FANTASYCALC_NUM_TEAMS"),
        ("num_qbs", "FANTASYCALC_NUM_QBS"),
        ("ppr", "FANTASYCALC_PPR"),
        ("is_dynasty", "FANTASYCALC_IS_DYNASTY"),
    )

    def test_fantasycalc_defaults_price_this_league(self):
        for fact, setting in self.PAIRS:
            with self.subTest(fact):
                self.assertEqual(
                    LEAGUE[fact], getattr(core_settings, setting),
                    f"League fact {fact}={LEAGUE[fact]!r} disagrees with dynasty_core.settings."
                    f"{setting}={getattr(core_settings, setting)!r}: FantasyCalc would be queried for the "
                    "wrong league format. Pass explicit query parameters for this league rather than "
                    "editing either value to match.",
                )

    def test_facts_are_not_derived_from_the_provider_profile(self):
        src = (ROOT / "config" / "dynasty_config.py").read_text(encoding="utf-8")
        self.assertNotIn("dynasty_core", re.sub(r'"""[\s\S]*?"""', "", src))


if __name__ == "__main__":
    unittest.main()
