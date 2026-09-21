"""
Contract test: each external data API has exactly one owning client module,
and each agent only reaches into the client(s) that own the data it actually
needs. This is what keeps a future change to one integration (like the
LeagueLogs retirement) from having to be hunted down across every agent
file, instead of being contained to its owning module.

  sleeper_client      — roster/player profile, trending adds, live status
  fantasycalc_client  — dynasty valuations, ESPN-id sync fallback
  espn_client         — NFL production stats, career totals, injury notes
"""
import unittest

import agents.production_agent as production_agent
import agents.situation_agent as situation_agent


class SituationAgentOwnsOnlySleeperAndFantasyCalcTest(unittest.TestCase):
    def test_situation_agent_has_no_espn_client_reference(self):
        # situation_agent evaluates opportunity/depth-chart/competition --
        # none of that is ESPN stats data. If this module ever starts
        # referencing espn_client, the ownership boundary has moved and the
        # agent split needs re-examining, not a silent cross-boundary call.
        self.assertFalse(hasattr(situation_agent, "espn_client"))

    def test_situation_agent_imports_sleeper_and_fantasycalc(self):
        self.assertTrue(hasattr(situation_agent, "sleeper_client"))
        self.assertTrue(hasattr(situation_agent, "fantasycalc_client"))


class ProductionAgentOwnsEspnForStatsTest(unittest.TestCase):
    def test_production_agent_imports_espn_client_for_stats(self):
        self.assertTrue(hasattr(production_agent, "espn_client"))

    def test_production_agent_also_reaches_sleeper_for_profile_lookup(self):
        # lookup_player_info needs the player's basic profile + live status
        # from Sleeper before it can even ask ESPN for stats by athlete id.
        self.assertTrue(hasattr(production_agent, "sleeper_client"))


class ClientModulesExposeTheFunctionsTheirOwningAgentsCallTest(unittest.TestCase):
    """A boundary is only real if the function actually exists where the
    agent expects it -- this catches the client module's own API drifting
    out from under the agent that owns calling it."""

    def test_sleeper_client_exposes_get_all_players(self):
        from data import sleeper_client
        self.assertTrue(callable(getattr(sleeper_client, "get_all_players", None)))

    def test_fantasycalc_client_exposes_get_dynasty_values(self):
        from data import fantasycalc_client
        self.assertTrue(callable(getattr(fantasycalc_client, "get_dynasty_values", None)))

    def test_espn_client_exposes_season_and_career_and_injury_lookups(self):
        from data import espn_client
        for name in ("get_season_statistics", "get_career_statistics", "get_player_injury_notes"):
            self.assertTrue(callable(getattr(espn_client, name, None)), f"espn_client.{name} missing")


if __name__ == "__main__":
    unittest.main()
