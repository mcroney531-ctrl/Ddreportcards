"""
Contract test: a player with no ESPN athlete id on file (too deep on the
roster for Sleeper or FantasyCalc to have synced one) is a legitimate
no-data case, not a network crash. agents/production_agent.py's four
ESPN-dependent tools all guard with _valid_espn_id() before ever calling
espn_client -- without it, an id that arrives as the literal string "null"/
"none" (seen from the LLM echoing back a None field) would reach ESPN's API
as a URL segment and come back a real HTTP 400, not a clean {"error": ...}.
"""
import unittest
from unittest import mock

from agents import production_agent


class ValidEspnIdTest(unittest.TestCase):
    def test_none_is_invalid(self):
        self.assertFalse(production_agent._valid_espn_id(None))

    def test_empty_string_is_invalid(self):
        self.assertFalse(production_agent._valid_espn_id(""))

    def test_literal_null_string_is_invalid(self):
        self.assertFalse(production_agent._valid_espn_id("null"))
        self.assertFalse(production_agent._valid_espn_id("Null"))

    def test_literal_none_string_is_invalid(self):
        self.assertFalse(production_agent._valid_espn_id("none"))
        self.assertFalse(production_agent._valid_espn_id("None"))

    def test_real_id_is_valid(self):
        self.assertTrue(production_agent._valid_espn_id("3916148"))


class MissingEspnIdNeverReachesTheNetworkTest(unittest.TestCase):
    """Each tool must return a clean error dict without calling espn_client
    at all when the id is missing -- this is the actual no-crash guarantee,
    not just the _valid_espn_id() helper's own return value."""

    def _assert_espn_client_untouched(self, fn, *args):
        with mock.patch.object(
            production_agent.espn_client, "get_season_statistics",
            side_effect=AssertionError("must not call ESPN with no id on file"),
        ), mock.patch.object(
            production_agent.espn_client, "get_career_statistics",
            side_effect=AssertionError("must not call ESPN with no id on file"),
        ), mock.patch.object(
            production_agent.espn_client, "get_player_injury_notes",
            side_effect=AssertionError("must not call ESPN with no id on file"),
        ):
            result = fn(*args)
        self.assertIn("error", result)
        self.assertNotIn("stats", result)
        return result

    def test_current_season_production_with_none_id(self):
        self._assert_espn_client_untouched(production_agent.get_current_season_production, None)

    def test_current_season_production_with_literal_null_string(self):
        self._assert_espn_client_untouched(production_agent.get_current_season_production, "null")

    def test_prior_season_production_with_none_id(self):
        self._assert_espn_client_untouched(production_agent.get_prior_season_production, None)

    def test_career_production_with_none_id(self):
        self._assert_espn_client_untouched(production_agent.get_career_production, None)

    def test_injury_notes_with_none_id(self):
        self._assert_espn_client_untouched(production_agent.get_injury_notes, None, "TEN")


class ValidEspnIdStillReachesTheNetworkTest(unittest.TestCase):
    """The guard must not swallow real ids -- confirms this isn't a
    stub that always short-circuits."""

    def test_current_season_production_with_real_id_calls_espn_client(self):
        with mock.patch.object(
            production_agent.espn_client, "get_season_statistics", return_value={"stats": {}}
        ) as mock_stats, mock.patch.object(
            production_agent.espn_client, "flatten_statistics", return_value={}
        ):
            result = production_agent.get_current_season_production("3916148", season=2025)

        mock_stats.assert_called_once_with("3916148", 2025)
        self.assertNotIn("error", result)


if __name__ == "__main__":
    unittest.main()
