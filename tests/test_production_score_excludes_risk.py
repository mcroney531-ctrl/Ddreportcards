"""
Stage 3B: production_score / production_grade measure demonstrated on-field
production only. Current health and aging are reported separately in
risk_modifier and applied numerically once, downstream, by the Market
agent's deterministic proprietary composite
(compute_proprietary_composite: current_health_score -> risk_score at 20%
weight, aging_risk -> AGING_RISK_PENALTY).

Before 3B the Production prompt said "PRODUCTION only" but also listed
current health and aging risk under "Focus on", and its last step was
"Synthesize all into a Production Grade". That let the model lower
production_score for age or current injury status, after which Market
applied both signals again.

An LLM's scoring can't be asserted deterministically, so these tests pin
the instructions: the contract must be explicit and non-contradictory,
the risk signals must still be gathered and reported, and the output
schema must be unchanged.
"""
import json
import os
import re
import unittest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.production_agent as production_agent


def _collapsed(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _output_example() -> dict:
    prompt = production_agent.SYSTEM_PROMPT
    start = prompt.index("{", prompt.index("Output format"))
    return json.loads(prompt[start:prompt.rindex("}") + 1])


class ProductionScoreOwnershipTest(unittest.TestCase):
    def test_prompt_states_production_score_is_production_only(self):
        self.assertIn(
            "production_score and production_grade measure demonstrated on-field production only",
            _collapsed(production_agent.SYSTEM_PROMPT),
        )

    def test_prompt_forbids_moving_production_score_for_age_or_health(self):
        self.assertIn(
            "do not lower or raise production_score because of age or current health",
            _collapsed(production_agent.SYSTEM_PROMPT),
        )

    def test_prompt_says_risk_is_reported_separately(self):
        self.assertIn(
            "current_health_score and aging_risk are reported separately in risk_modifier",
            _collapsed(production_agent.SYSTEM_PROMPT),
        )

    def test_no_instruction_folds_everything_into_the_production_grade(self):
        prompt = _collapsed(production_agent.SYSTEM_PROMPT)
        self.assertNotIn("synthesize all into a production grade", prompt)

    def test_focus_list_does_not_make_risk_a_production_input(self):
        # The "Focus on:" sentence is what the grade is built from; health and
        # aging must not be listed there as grade inputs.
        prompt = _collapsed(production_agent.SYSTEM_PROMPT)
        focus = prompt[prompt.index("focus on:"):prompt.index(".", prompt.index("focus on:"))]
        self.assertNotIn("aging risk", focus)
        self.assertNotIn("current health", focus)

    def test_calibration_scopes_the_grade_to_production_evidence(self):
        cal = _collapsed(production_agent.PRODUCTION_CALIBRATION)
        self.assertIn("age and current health do not move this grade", cal)


class RiskSignalsStillGatheredAndReportedTest(unittest.TestCase):
    def test_health_injury_and_age_tools_still_wired(self):
        tools = {t.__name__ for t in production_agent.build_production_agent().tools}
        self.assertTrue({"lookup_player_info", "get_injury_notes", "compute_age_curve_signal"} <= tools)

    def test_steps_still_call_the_risk_tools(self):
        prompt = production_agent.SYSTEM_PROMPT
        self.assertIn("Call get_injury_notes", prompt)
        self.assertIn("Call compute_age_curve_signal", prompt)

    def test_output_schema_unchanged(self):
        example = _output_example()
        self.assertEqual(
            list(example),
            ["player", "position", "espn_athlete_id", "current_season", "key_stats", "trend_note",
             "risk_modifier", "production_score", "production_grade", "key_factors", "concerns", "summary"],
        )
        self.assertEqual(
            list(example["risk_modifier"]),
            ["current_health_score", "injury_notes", "aging_risk", "career_window_note"],
        )


if __name__ == "__main__":
    unittest.main()
