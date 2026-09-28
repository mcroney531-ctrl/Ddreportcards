"""
Stage 3C.7: Production's aging signal is set in code, not copied by the model.

compute_age_curve_signal is deterministic, but the Production model used to
copy its result into risk_modifier.aging_risk / career_window_note, and that
copy was what Market, the card and sell_high saw. Now run_production_agent
captures the actual lookup_player_info result for the run's player_id
(wrapped with functools.wraps: same ADK name/schema; no extra Sleeper call)
and recomputes the signal from that lookup's position/age/years_exp.
current_health_score stays model-authored (a judgment over current status).
A missing or errored lookup fails the run with {"error": ...}.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.production_agent as production_agent

MODEL_JSON = {
    "player": "Model Name", "position": "WR", "espn_athlete_id": "1", "current_season": 2025,
    "key_stats": {}, "trend_note": "t",
    "risk_modifier": {"current_health_score": 4, "injury_notes": "model notes",
                      "aging_risk": "low", "career_window_note": "young player"},
    "production_score": 80, "production_grade": "B-", "key_factors": [], "concerns": [], "summary": "s",
}


def _event(text):
    event = mock.Mock()
    event.is_final_response.return_value = True
    event.content.parts = [mock.Mock(text=text)]
    return event


def fake_production_runner(model_json=MODEL_JSON, lookup_player="p1", call_lookup=True):
    class FakeRunner:
        def __init__(self, *, agent, **_):
            self.agent = agent

        async def run_async(self, **_):
            if call_lookup:
                lookup = next(t for t in self.agent.tools if getattr(t, "__name__", "") == "lookup_player_info")
                lookup(player_id=lookup_player)
            yield _event(json.dumps(model_json))
    return FakeRunner


def run_production(age, position="RB", years_exp=7, players=None, **runner_kw):
    players = players if players is not None else {
        "p1": {"full_name": "Test RB", "position": position, "team": "KC", "age": age,
               "years_exp": years_exp, "espn_id": "123"},
    }
    with mock.patch.object(production_agent.sleeper_client, "get_all_players", return_value=players), \
         mock.patch.object(production_agent, "Runner", fake_production_runner(**runner_kw)):
        return asyncio.run(production_agent.run_production_agent("p1"))


class ProductionAgingSignalIsDeterministicTest(unittest.TestCase):
    def test_model_age_rating_is_overwritten_from_the_lookup(self):
        out = run_production(age=30)
        expected = production_agent.compute_age_curve_signal("RB", 30, 7)
        self.assertEqual(out["risk_modifier"]["aging_risk"], "high")
        self.assertEqual(out["risk_modifier"]["career_window_note"], expected["career_window_note"])
        self.assertIn("this model's configured RB high aging-risk threshold (29)",
                      out["risk_modifier"]["career_window_note"])

    def test_no_age_on_file_is_unknown(self):
        out = run_production(age=None)
        self.assertEqual(out["risk_modifier"]["aging_risk"], "unknown")
        self.assertEqual(out["risk_modifier"]["career_window_note"], "no age on file")

    def test_current_health_and_injury_notes_stay_model_authored(self):
        for age in (30, None):
            with self.subTest(age=age):
                rm = run_production(age=age)["risk_modifier"]
                self.assertEqual((rm["current_health_score"], rm["injury_notes"]), (4, "model notes"))

    def test_production_score_and_narrative_untouched(self):
        out = run_production(age=30)
        self.assertEqual((out["production_score"], out["production_grade"], out["summary"]), (80, "B-", "s"))

    def test_raw_helper_shape_unchanged(self):
        self.assertEqual(production_agent.compute_age_curve_signal("RB", None, 3),
                         {"aging_risk": "unknown", "note": "no age on file"})


class ProductionFailsWithoutAnAuthoritativeLookupTest(unittest.TestCase):
    def test_no_lookup_call_fails(self):
        out = run_production(age=30, call_lookup=False)
        self.assertIn("error", out)
        self.assertNotIn("risk_modifier", out)

    def test_lookup_for_another_player_fails(self):
        out = run_production(age=30, lookup_player="someone-else")
        self.assertIn("error", out)

    def test_lookup_error_fails(self):
        out = run_production(age=30, players={})  # lookup_player_info returns {"error": ...}
        self.assertIn("error", out)
        self.assertNotIn("risk_modifier", out)

    def test_lookup_tool_keeps_its_adk_identity(self):
        tool = next(t for t in production_agent.build_production_agent({}).tools
                    if getattr(t, "__name__", "") == "lookup_player_info")
        from google.adk.tools import FunctionTool
        decl = FunctionTool(tool)._get_declaration()
        self.assertEqual(decl.name, "lookup_player_info")
        self.assertEqual(list((decl.parameters_json_schema or {}).get("properties", {})), ["player_id"])


if __name__ == "__main__":
    unittest.main()
