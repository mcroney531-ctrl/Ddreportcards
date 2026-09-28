"""
Stage 3B baseline for the Stage 3C age-curve calibration: pins today's aging
thresholds, classifications, Market penalties and composite outputs, so 3B's
wording changes provably leave the numbers alone and 3C's changes are
explicit diffs against this file.

The thresholds are this model's configured values with no cited source.
compute_age_curve_signal's notes must say so and must not describe them as
"typical" decline or cliff ages.
"""
import os
import unittest

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.market_agent as market_agent
import agents.production_agent as production_agent


class AgeCurveBaselineTest(unittest.TestCase):
    def test_age_curves_unchanged(self):
        self.assertEqual(production_agent.AGE_CURVES, {
            "RB": {"decline_age": 27, "cliff_age": 29},
            "WR": {"decline_age": 30, "cliff_age": 32},
            "TE": {"decline_age": 30, "cliff_age": 32},
            "QB": {"decline_age": 36, "cliff_age": 39},
        })

    def test_boundary_classifications_per_position(self):
        curves = dict(production_agent.AGE_CURVES)
        curves["K"] = {"decline_age": 29, "cliff_age": 31}  # generic fallback curve
        for pos, c in curves.items():
            cases = {
                c["decline_age"] - 1: "low",
                c["decline_age"]: "moderate",
                c["cliff_age"] - 1: "moderate",
                c["cliff_age"]: "high",
            }
            for age, expected in cases.items():
                with self.subTest(position=pos, age=age):
                    out = production_agent.compute_age_curve_signal(pos, age, 3)
                    self.assertEqual(out["aging_risk"], expected)
            with self.subTest(position=pos, age=None):
                self.assertEqual(production_agent.compute_age_curve_signal(pos, None, 3)["aging_risk"], "unknown")

    def test_output_keys_unchanged(self):
        out = production_agent.compute_age_curve_signal("RB", 25, 3)
        self.assertEqual(list(out), ["position", "age", "years_exp", "aging_risk", "career_window_note"])
        self.assertEqual(production_agent.compute_age_curve_signal("RB", None, 3),
                         {"aging_risk": "unknown", "note": "no age on file"})

    def test_notes_describe_configured_thresholds_not_typical_ages(self):
        for pos, age in (("RB", 24), ("RB", 27), ("RB", 30), ("QB", 40), ("K", 35)):
            with self.subTest(position=pos, age=age):
                note = production_agent.compute_age_curve_signal(pos, age, 3)["career_window_note"]
                self.assertNotIn("typical", note)
                self.assertNotIn("short window remaining", note)
                self.assertIn("this model's configured", note)


class MarketCompositeBaselineTest(unittest.TestCase):
    def test_weights_and_aging_penalty_unchanged(self):
        self.assertEqual(market_agent.WEIGHTS, {"production": 0.45, "opportunity": 0.35, "risk": 0.20})
        self.assertEqual(market_agent.AGING_RISK_PENALTY, {"low": 0, "moderate": -5, "high": -12, "unknown": 0})

    def test_composite_outputs_for_fixed_inputs(self):
        expected = {
            (5, "low"): 82.2, (5, "moderate"): 77.2, (5, "high"): 70.2,
            (3, "low"): 72.2, (3, "high"): 60.2, (1, "high"): 50.2,
            (5, "unknown"): 82.2,
        }
        for (health, aging), value in expected.items():
            with self.subTest(health=health, aging=aging):
                out = market_agent.compute_proprietary_composite(75, 80, health, aging)
                self.assertEqual(out["proprietary_composite"], value)

    def test_age_enters_the_composite_only_through_the_penalty(self):
        # Same production/opportunity/health: the whole age effect is the
        # AGING_RISK_PENALTY delta, and nothing else in the composite reads age.
        for prod in (40, 68, 80):
            low = market_agent.compute_proprietary_composite(75, prod, 5, "low")["proprietary_composite"]
            high = market_agent.compute_proprietary_composite(75, prod, 5, "high")["proprietary_composite"]
            self.assertAlmostEqual(high - low, market_agent.AGING_RISK_PENALTY["high"], places=6)

    def test_health_enters_the_composite_only_through_risk_score(self):
        # Each health step is worth 25 risk_score points x 0.20 weight = 5 composite points.
        h5 = market_agent.compute_proprietary_composite(75, 80, 5, "low")["proprietary_composite"]
        h1 = market_agent.compute_proprietary_composite(75, 80, 1, "low")["proprietary_composite"]
        self.assertAlmostEqual(h5 - h1, 20.0, places=6)


if __name__ == "__main__":
    unittest.main()
