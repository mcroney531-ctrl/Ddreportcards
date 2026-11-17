"""
D4 (model-facing): the league facts the agents are told must be the league's.

This is deliberately a separate change from the identity cleanup because it
alters model context. Dynasty Daddies is half-PPR (rec = 0.5, verified
against Sleeper), but the Roster and Synthesis agent prompts called it a
"PPR dynasty", and the Trade Agent prompt still described "BCNH's full
roster" -- BCNH being the Phase 1 test account, not the user. Only the
wording of those facts changes: grading, weights, formulas, models, token
budgets and agent structure do not.
"""
import pathlib
import re
import unittest

from agents import roster_agent, synthesis_agent, trade_agent
from config.dynasty_config import LEAGUE

ROOT = pathlib.Path(__file__).resolve().parent.parent
_BARE_PPR = re.compile(r"(?<!half-)\bPPR\b(?!\s*setting)")


class LeagueFormatInPromptsTest(unittest.TestCase):
    def test_prompts_state_the_verified_scoring(self):
        self.assertEqual(LEAGUE["scoring_label"], "half-PPR")
        for name, prompt in (("roster", roster_agent.SYSTEM_PROMPT), ("synthesis", synthesis_agent.SYSTEM_PROMPT)):
            with self.subTest(name):
                self.assertIn("half-PPR dynasty", prompt)
                self.assertIsNone(_BARE_PPR.search(prompt), "bare 'PPR' still describes the league")

    def test_no_prompt_describes_the_test_account(self):
        for name, prompt in (("roster", roster_agent.SYSTEM_PROMPT), ("synthesis", synthesis_agent.SYSTEM_PROMPT),
                             ("trade", trade_agent.SYSTEM_PROMPT)):
            with self.subTest(name):
                self.assertNotIn("BCNH", prompt)
        self.assertIn("the user's full roster", trade_agent.SYSTEM_PROMPT)


class LeagueFormatInCommentsAndDocsTest(unittest.TestCase):
    def test_no_stale_ppr_or_test_account_in_agents_or_readme(self):
        for rel in ("agents/roster_agent.py", "agents/synthesis_agent.py", "agents/trade_agent.py", "README.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(rel):
                self.assertNotIn("BCNH", text)
                self.assertIsNone(re.search(r"(?<!half-)\bPPR dynasty", text))


if __name__ == "__main__":
    unittest.main()
