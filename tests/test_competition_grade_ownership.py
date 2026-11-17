"""
D2: the FantasyCalc competition-grade policy has one owner.

The Situation Agent used to carry its own GRADE_TIERS and _value_grade, an exact
copy of the canonical policy in dynasty_core.fantasycalc that DD's own facade,
data/fantasycalc_client.py, already re-exports. Competition grading now goes
through that facade; the agent keeps no copy and never imports dynasty_core
directly. The grades themselves are unchanged.
"""
import ast
import pathlib
import unittest
from unittest import mock

from agents import situation_agent
from data import fantasycalc_client

AGENT_PATH = pathlib.Path(situation_agent.__file__)

# (position, redraft position rank) -> grade under the canonical policy,
# at every tier edge and one past it.
BOUNDARIES = {
    "QB": [(1, "A"), (12, "A"), (13, "B"), (20, "B"), (21, "C"), (28, "C"), (29, "D"), (36, "D"), (37, "F")],
    "RB": [(12, "A"), (13, "B"), (24, "B"), (25, "C"), (36, "C"), (37, "D"), (48, "D"), (49, "F")],
    "WR": [(12, "A"), (13, "B"), (24, "B"), (25, "C"), (36, "C"), (37, "D"), (54, "D"), (55, "F")],
    "TE": [(6, "A"), (7, "B"), (12, "B"), (13, "C"), (18, "C"), (19, "D"), (24, "D"), (25, "F")],
}


def _assess(players, fc_index, position="WR"):
    with mock.patch.object(situation_agent.sleeper_client, "get_all_players", return_value=players), \
         mock.patch.object(situation_agent.fantasycalc_client, "get_dynasty_values", return_value=[]), \
         mock.patch.object(situation_agent.fantasycalc_client, "index_by_sleeper_id_with_redraft_rank",
                           return_value=fc_index):
        return situation_agent.assess_position_competition("TEN", position, exclude_player_id="me")


class OwnershipTest(unittest.TestCase):
    def test_agent_defines_no_local_grade_policy(self):
        tree = ast.parse(AGENT_PATH.read_text(encoding="utf-8"))
        assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertNotIn("GRADE_TIERS", assigned)
        self.assertNotIn("_value_grade", defined)
        self.assertFalse(hasattr(situation_agent, "GRADE_TIERS"))
        self.assertFalse(hasattr(situation_agent, "_value_grade"))

    def test_agent_does_not_reach_past_the_facade(self):
        src = AGENT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("dynasty_core", src)
        self.assertIn("fantasycalc_client.value_grade(", src)

    def test_competition_grading_is_delegated_to_the_facade(self):
        players = {"p1": {"full_name": "Rival", "team": "TEN", "position": "WR", "active": True}}
        fc_index = {"p1": {"value": 5000, "redraftPositionRank": 3}}
        with mock.patch.object(fantasycalc_client, "value_grade", return_value="Z") as patched:
            out = _assess(players, fc_index)
        patched.assert_called_once_with("WR", 3)
        self.assertEqual(out["competitors"][0]["grade"], "Z")


class GradesUnchangedTest(unittest.TestCase):
    def test_boundaries_through_competition_grading(self):
        for position, cases in BOUNDARIES.items():
            for rank, expected in cases:
                with self.subTest(position=position, rank=rank):
                    players = {"p1": {"full_name": "Rival", "team": "TEN", "position": position, "active": True}}
                    out = _assess(players, {"p1": {"value": 100, "redraftPositionRank": rank}}, position)
                    self.assertEqual(out["competitors"][0]["grade"], expected)

    def test_missing_rank_is_f(self):
        players = {"p1": {"full_name": "Rival", "team": "TEN", "position": "QB", "active": True}}
        out = _assess(players, {"p1": {"value": 100, "redraftPositionRank": None}}, "QB")
        self.assertEqual(out["competitors"][0]["grade"], "F")
        self.assertEqual(fantasycalc_client.value_grade("QB", None), "F")


if __name__ == "__main__":
    unittest.main()
