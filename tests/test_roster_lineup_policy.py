"""
D2: the Roster Agent grades the league's verified starting lineup.

Sleeper's roster_positions for Dynasty Daddies (fetched live, 2026-11-17):
  QB, RB, RB, WR, WR, WR, TE, FLEX, SUPER_FLEX, K + 14 BN
config/dynasty_config.py stores that array verbatim as ROSTER_POSITIONS: a
verified snapshot, not a live setting, kept outside LEAGUE so /league and the
GM mirror don't change.

The Roster Agent used to own its own lineup (QB1 RB2 WR2 TE1 + FLEX2 + SF1) and
a hand-copied MIN_CONTRIBUTORS. It now derives both from ROSTER_POSITIONS when
it grades:
  graded starters: QB1 RB2 WR3 TE1 + FLEX1 + SF1 = 9
  K is a real starter but has no DD player card, so it is not graded:
  10 lineup starters, 9 graded.
An unknown slot type fails roster grading loudly, not the module import.

Weights, premiums, thresholds and the letter table are unchanged.
"""
import json
import unittest
from unittest import mock

import agents.roster_agent as roster_agent
import agents.trade_agent as trade_agent
from config.dynasty_config import LEAGUE, ROSTER_POSITIONS

VERIFIED_SLEEPER_ROSTER_POSITIONS = (
    "QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K",
    "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN", "BN",
)


def _card(name, position, q):
    """A card whose quality_score comes out exactly q (all three scores = q)."""
    return {"player": name, "position": position,
            "opportunity_score": q, "production_score": q, "trade_value_score": q}


def _grade(cards):
    return roster_agent.compute_roster_grade(json.dumps(cards))


# Shaped so the old WR2/FLEX2 lineup and the verified WR3/FLEX1 lineup disagree:
# the third WR (60) is worse than two spare RBs (78, 77).
SPLIT_ROSTER = [
    _card("QB-A", "QB", 90), _card("QB-B", "QB", 70),
    _card("RB-1", "RB", 85), _card("RB-2", "RB", 80), _card("RB-3", "RB", 78), _card("RB-4", "RB", 77),
    _card("WR-1", "WR", 88), _card("WR-2", "WR", 75), _card("WR-3", "WR", 60),
    _card("TE-1", "TE", 70), _card("TE-2", "TE", 50),
]


class SnapshotTest(unittest.TestCase):
    def test_snapshot_is_the_verified_sleeper_array(self):
        self.assertEqual(tuple(ROSTER_POSITIONS), VERIFIED_SLEEPER_ROSTER_POSITIONS)

    def test_snapshot_agrees_with_the_league_profile_without_joining_it(self):
        self.assertEqual(len(ROSTER_POSITIONS), LEAGUE["active_roster_size"])
        self.assertNotIn("roster_positions", LEAGUE)
        self.assertEqual(ROSTER_POSITIONS.count("BN"), 14)


class PolicyTest(unittest.TestCase):
    def test_derived_allocation(self):
        policy = roster_agent.starter_policy()
        self.assertEqual(policy.base, {"QB": 1, "RB": 2, "WR": 3, "TE": 1})
        self.assertEqual((policy.flex, policy.sf), (1, 1))
        self.assertEqual(policy.ungraded, {"K": 1})
        self.assertEqual(policy.graded_starters, 9)
        self.assertEqual(policy.lineup_starters, 10)
        self.assertEqual(policy.min_contributors, {"QB": 1, "RB": 2, "WR": 3, "TE": 1})

    def test_no_independent_lineup_facts_remain_in_the_agent(self):
        for name in ("BASE_STARTER_SLOTS", "FLEX_SLOTS", "SF_SLOTS", "MIN_CONTRIBUTORS"):
            with self.subTest(name):
                self.assertFalse(hasattr(roster_agent, name))

    def test_sleeper_spelling_is_normalized_and_bench_slots_ignored(self):
        policy = roster_agent.starter_policy(("QB", "SUPER_FLEX", "FLEX", "WR", "BN", "TAXI", "IR"))
        self.assertEqual((policy.sf, policy.flex), (1, 1))
        self.assertEqual(policy.base, {"QB": 1, "RB": 0, "WR": 1, "TE": 0})
        self.assertEqual(policy.graded_starters, 4)

    def test_unknown_slot_fails_loudly_only_when_grading(self):
        for slot in ("REC_FLEX", "WRRB_FLEX", "DEF", "IDP_FLEX"):
            with self.subTest(slot):
                with self.assertRaises(roster_agent.UnsupportedRosterSlot) as ctx:
                    roster_agent.starter_policy(VERIFIED_SLEEPER_ROSTER_POSITIONS + (slot,))
                self.assertIn(slot, str(ctx.exception))
        with mock.patch.object(roster_agent, "ROSTER_POSITIONS", VERIFIED_SLEEPER_ROSTER_POSITIONS + ("DEF",)):
            with self.assertRaises(roster_agent.UnsupportedRosterSlot):
                _grade(SPLIT_ROSTER)


class StarterAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.out = _grade(SPLIT_ROSTER)
        self.slots = [(s["name"], s["starter_slot"]) for s in self.out["starters"]]

    def test_verified_lineup_assignment(self):
        self.assertEqual(self.slots, [
            ("QB-A", "QB"), ("RB-1", "RB"), ("RB-2", "RB"),
            ("WR-1", "WR"), ("WR-2", "WR"), ("WR-3", "WR"), ("TE-1", "TE"),
            ("RB-3", "FLEX"),
            ("QB-B", "SF"),
        ])

    def test_counts(self):
        self.assertEqual(len(self.out["starters"]), 9)
        self.assertEqual(self.out["bench_count"], len(SPLIT_ROSTER) - 9)
        self.assertEqual([s for _, s in self.slots].count("FLEX"), 1)
        self.assertEqual([s for _, s in self.slots].count("SF"), 1)

    def test_third_wr_is_required_even_when_spare_rbs_are_better(self):
        names = {n for n, _ in self.slots}
        self.assertIn("WR-3", names)
        self.assertNotIn("RB-4", names)   # the old FLEX2 lineup started RB-4 and benched WR-3

    def test_base_slots_fill_before_flex(self):
        order = [s for _, s in self.slots]
        self.assertLess(max(i for i, s in enumerate(order) if s in ("QB", "RB", "WR", "TE")), order.index("FLEX"))

    def test_score_is_the_mean_of_the_nine_graded_starters(self):
        self.assertEqual(self.out["starter_quality_score"], round((90 + 85 + 80 + 88 + 75 + 60 + 70 + 78 + 70) / 9, 1))

    def test_sf_qb_premium_is_unchanged(self):
        # 70 * 1.18 = 82.6 beats a spare RB at 80 for the SF slot.
        cards = [_card("QB-A", "QB", 90), _card("QB-B", "QB", 70),
                 _card("RB-1", "RB", 95), _card("RB-2", "RB", 94), _card("RB-3", "RB", 93), _card("RB-4", "RB", 80),
                 _card("WR-1", "WR", 90), _card("WR-2", "WR", 89), _card("WR-3", "WR", 88), _card("TE-1", "TE", 70)]
        sf = [s["name"] for s in _grade(cards)["starters"] if s["starter_slot"] == "SF"]
        self.assertEqual(sf, ["QB-B"])


class ThinPositionTest(unittest.TestCase):
    def test_thin_flags_follow_the_verified_base_slots(self):
        cards = [_card("QB-A", "QB", 90), _card("RB-1", "RB", 85), _card("RB-2", "RB", 80),
                 _card("WR-1", "WR", 88), _card("WR-2", "WR", 75), _card("WR-3", "WR", 40),
                 _card("TE-1", "TE", 70)]
        flags = _grade(cards)["flags"]
        self.assertTrue(any(f.startswith("thin at WR — only 2 real contributor(s)") for f in flags))
        self.assertFalse(any(f.startswith("thin at RB") or f.startswith("thin at QB") or f.startswith("thin at TE")
                             for f in flags))


class PreservedPolicyTest(unittest.TestCase):
    def test_scoring_constants_are_unchanged(self):
        self.assertEqual(roster_agent.POSITION_VALUE, {"QB": 1.18, "RB": 1.0, "WR": 1.0, "TE": 0.95})
        self.assertEqual(roster_agent.QUALITY_WEIGHTS, {"opportunity": 0.30, "production": 0.40, "trade_value": 0.30})
        self.assertEqual(roster_agent.CONTRIBUTOR_THRESHOLD, 50)
        self.assertEqual(roster_agent.LETTER_GRADE_TABLE, [
            (97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
            (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"), (63, "D"), (60, "D-"), (0, "F"),
        ])
        self.assertEqual(trade_agent.SURPLUS_MIN_CONTRIBUTORS, {"QB": 3, "RB": 4, "WR": 4, "TE": 3})

    def test_output_schema_is_unchanged(self):
        out = _grade(SPLIT_ROSTER)
        self.assertEqual(set(out), {"overall_grade", "starter_quality_score", "starters",
                                    "positional_breakdown", "flags", "bench_count"})
        self.assertEqual(set(out["starters"][0]), {"name", "position", "starter_slot", "quality_score"})
        self.assertEqual(set(out["positional_breakdown"]["WR"]),
                         {"roster_count", "real_contributors", "starters_assigned", "avg_starter_quality", "top_players"})


if __name__ == "__main__":
    unittest.main()
