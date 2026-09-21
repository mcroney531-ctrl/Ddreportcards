"""
LeagueLogs Developer API is globally retired -- confirmed via direct probe:
every endpoint (/v1/players and /v1/players/{id}/blurb, across 5 different
Sleeper player IDs) returns 410 Gone. This isn't a per-player or per-endpoint
quirk, so the fix is retiring the active integration everywhere it's wired
in, not catching 410 at the one call site that happened to crash first.

dynasty_core/leaguelogs.py and data/leaguelogs_client.py are deliberately
left in place -- they're the shared Dynasty Umbrella copy, and scoutcap may
still reference them; dead-file deletion is a separate cleanup pass that
needs cross-repo verification first. These tests only prove Ddreportcards'
*active* paths (production_agent, api.py's endpoints/chat tools/system
prompt, app.py's Streamlit UI) no longer call into it.
"""
import inspect
import os
import unittest

from fastapi.testclient import TestClient

import api
from agents import production_agent

_APP_PY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")


class ProductionAgentHasNoLeagueLogsTest(unittest.TestCase):
    def test_tool_list_excludes_get_status_blurb(self):
        tools = production_agent.build_production_agent().tools
        names = [t.__name__ for t in tools]
        self.assertNotIn("get_status_blurb", names)
        self.assertNotIn("leaguelogs", " ".join(names).lower())

    def test_module_no_longer_imports_leaguelogs_client(self):
        source = inspect.getsource(production_agent)
        self.assertNotIn("leaguelogs", source.lower())

    def test_system_prompt_has_no_leaguelogs_mention(self):
        self.assertNotIn("leaguelogs", production_agent.SYSTEM_PROMPT.lower())

    def test_lookup_player_info_falls_back_cleanly_with_no_leaguelogs_call(self):
        # A player missing both Sleeper's own espn_id and FantasyCalc's must
        # come back with a clean null id -- no LeagueLogs call exists to make.
        from unittest import mock

        fake_player = {
            "full_name": "Deep Roster Guy", "position": "WR", "team": "KC",
            "age": 24, "years_exp": 1, "status": "Active", "injury_status": None,
            "injury_start_date": None, "practice_participation": None,
            "espn_id": None,
        }
        with mock.patch.object(
            production_agent.sleeper_client, "get_all_players",
            return_value={"999": fake_player},
        ), mock.patch.object(
            production_agent.fantasycalc_client, "get_value_for_sleeper_id",
            return_value=None,
        ):
            result = production_agent.lookup_player_info("999")

        self.assertIsNone(result["espn_id"])


class ChatToolsHaveNoLeagueLogsTest(unittest.TestCase):
    def test_no_get_player_blurb_tool_definition(self):
        names = [t["name"] for t in api.CHAT_TOOLS]
        self.assertNotIn("get_player_blurb", names)

    def test_no_leaguelogs_mentioned_in_any_tool_schema(self):
        self.assertNotIn("leaguelogs", api._CHAT_TOOLS_JSON.lower())

    def test_no_leaguelogs_attribution_in_chat_system_prompt(self):
        rendered = api.CHAT_SYSTEM_PROMPT.format(today="2026-09-21")
        self.assertNotIn("leaguelogs", rendered.lower())

    def test_dispatcher_has_no_get_player_blurb_branch(self):
        source = inspect.getsource(api._execute_chat_tool)
        self.assertNotIn("get_player_blurb", source)


class BlurbEndpointIsGoneTest(unittest.TestCase):
    def test_players_blurb_route_no_longer_exists(self):
        client = TestClient(api.app)
        resp = client.get("/players/12501/blurb")
        self.assertEqual(resp.status_code, 404)


class EspnAthleteIdFallbackIsSleeperFantasyCalcOnlyTest(unittest.TestCase):
    def test_no_third_leaguelogs_fallback(self):
        source = inspect.getsource(api._espn_athlete_id)
        self.assertNotIn("leaguelogs", source.lower())
        self.assertNotIn("get_espn_id", source)


class StreamlitUiHasNoLeagueLogsAttributionTest(unittest.TestCase):
    def test_app_source_has_no_leaguelogs_reference(self):
        # Read as plain text rather than importing app.py: streamlit isn't
        # installed in every environment that runs this test suite (it's a
        # separate deployment surface from the FastAPI service these other
        # tests exercise), and this doesn't need a live Streamlit context.
        with open(_APP_PY_PATH) as f:
            source = f.read()
        self.assertNotIn("leaguelogs", source.lower())


if __name__ == "__main__":
    unittest.main()
