"""
Phase A (conversation quality): the backend prompt contract.

What changed, approved wording only:
  A1  HOW TO READ HIS MESSAGE: infer evaluation / discussion / action.
  A2  rule 1: look up current facts that matter to the claim, not every name.
  A3  rule 14: changing facts vs. established facts vs. his claims; never
      manufacture freshness.
  A4  rule 10: name what he sends/gets on the first verdict, not every turn.
  A5  rule 11: test checkable football arguments with evidence that can test
      them; keep the verdict steady.
  A6  rule 17: production weighs by decision and horizon; box-score limits.
  A7  rule 18: verification is quiet.
  A8  follow-up guidance: most follow-ups aren't challenges.
What must not change: the grounding rules (2-9, 12, 13, 15, 16), the tool
definitions, and the assembly order.
"""
import json
import pathlib
import re
import unittest

import api
import chat_prompt
from evals import baseline_chat_prompt as baseline

TODAY = "2026-11-23"
NEW = chat_prompt.CHAT_SYSTEM_PROMPT.format(today=TODAY)
OLD = baseline.CHAT_SYSTEM_PROMPT.format(today=TODAY)
FOLLOW = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "chat_loop_golden.json").read_text())


def rule(text: str, n: int) -> str:
    """The text of numbered rule n, up to the next rule."""
    m = re.search(rf"(?m)^{n}\. (.*?)(?=^{n + 1}\. |\Z)", text, re.S)
    if not m:
        raise AssertionError(f"rule {n} not found")
    return m.group(1)


class AssemblyTest(unittest.TestCase):
    def test_order_and_follow_up_guidance(self):
        first = chat_prompt.build_chat_system("APP", [{"role": "user", "content": "x"}], TODAY)
        follow = chat_prompt.build_chat_system("APP", FOLLOW, TODAY)
        self.assertTrue(first.startswith(NEW))
        self.assertTrue(first.endswith("\n\nAPP"))
        self.assertEqual(follow.count("THIS IS A FOLLOW-UP."), 1)
        self.assertTrue(follow.endswith(chat_prompt._conversation_stance(FOLLOW)))
        self.assertLess(follow.index("APP"), follow.index("THIS IS A FOLLOW-UP."))

    def test_format_placeholders_intact(self):
        self.assertIn(f"Today's date is {TODAY}.", NEW)
        self.assertNotIn("{", NEW.replace("{today}", ""))


class RequiredWordingTest(unittest.TestCase):
    def test_a1_precedes_the_rules(self):
        self.assertLess(NEW.index("HOW TO READ HIS MESSAGE."), NEW.index("CRITICAL RULES"))
        for s in ("An EVALUATION", "DISCUSSION", "An ACTION", "Relevance, not length, decides how much work a turn gets",
                  "Never name these categories"):
            with self.subTest(s):
                self.assertIn(s, NEW)

    def test_a2_a3_grounding_without_manufactured_freshness(self):
        r1, r14 = rule(NEW, 1), rule(NEW, 14)
        self.assertIn("When one of them matters to what you are claiming", r1)
        self.assertIn("not every name in the conversation", r1)
        for s in ("Current, changing facts", "never present an earlier figure as freshly checked",
                  "Things already established earlier that do not go stale", "without implying you just verified them",
                  "What he tells you", "it is his claim", "do not restate it as confirmed unless a lookup confirmed it"):
            with self.subTest(s):
                self.assertIn(s, r14)

    def test_a4_a5_a6_a7(self):
        self.assertIn("Do not repeat that opening on follow-ups about the same trade", rule(NEW, 10))
        r11 = rule(NEW, 11)
        for s in ("teammates competing for the same touches", "using evidence that can actually test it",
                  "something no tool returns", "do not pretend a box score settles it", "Keep your verdict steady in both directions"):
            with self.subTest(s):
                self.assertIn(s, r11)
        r17 = rule(NEW, 17)
        for s in ("Its weight depends on the decision", "in dynasty, age, future opportunity and long-term value",
                  "they do not show snap share, routes run, or why a coach used a player"):
            with self.subTest(s):
                self.assertIn(s, r17)
        self.assertIn("Verification is quiet.", rule(NEW, 18))

    def test_a8_follow_up_guidance(self):
        st = chat_prompt._conversation_stance(FOLLOW)
        self.assertEqual(chat_prompt._conversation_stance([{"role": "user", "content": "x"}]), "")
        for s in ("Most follow-ups are not challenges to your earlier answer", "investigate the checkable part with tools before you reply",
                  "do not flip a verdict on pressure alone", "Earned agreement is fine", "Never narrate any of this"):
            with self.subTest(s):
                self.assertIn(s, st)


class RemovedWordingTest(unittest.TestCase):
    def test_replaced_instructions_are_gone(self):
        combined = NEW + chat_prompt._conversation_stance(FOLLOW)
        for s in ("Call get_player_value for any player you discuss",
                  "If you are restating a figure you gave three turns ago, look it up again",
                  "You already gave him a position earlier in this conversation",
                  "Open any trade verdict by naming what he sends",
                  "recent production is usually the most decision-relevant thing"):
            with self.subTest(s):
                self.assertNotIn(s, combined)
                self.assertIn(s, OLD + baseline._conversation_stance(FOLLOW))   # proves the check can see it


class GroundingPinnedTest(unittest.TestCase):
    def test_untouched_rules_are_byte_identical(self):
        for n in (2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 15, 16):
            with self.subTest(rule=n):
                self.assertEqual(rule(NEW, n), rule(OLD, n))

    def test_rule_10_and_11_cores_unchanged(self):
        self.assertEqual(rule(NEW, 10).split("The first time")[0], rule(OLD, 10).split("Open any trade")[0])
        for s in ("Hold your position until evidence moves it", "NEW FRAMING", "A verdict that tracks his mood is worse than a wrong one"):
            with self.subTest(s):
                self.assertIn(s, rule(NEW, 11))

    def test_tool_definitions_unchanged(self):
        import hashlib
        recorded = GOLDEN["scenarios"]["first_turn_text"]["calls"][1]["tools_sha256"]
        now = hashlib.sha256(json.dumps(api.CHAT_TOOLS, sort_keys=True).encode()).hexdigest()
        self.assertEqual(now, recorded)

    def test_size_growth_is_bounded(self):
        # Approved wording adds ~3.4k chars (~850 tokens) to the backend rules.
        self.assertLess(len(NEW), len(OLD) * 1.35)


if __name__ == "__main__":
    unittest.main()
