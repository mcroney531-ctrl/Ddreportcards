"""
Stage 3C: what each Market output means, and who consumes it.

The contract (present in the math since the first pipeline commit; the
wording had drifted from it):
  trade_value_score / trade_value_grade = FantasyCalc market-consensus
      standing (market_percentile within position), no proprietary blend
  dynasty_value        = raw FantasyCalc value
  hybrid_market_value  = our internal valuation: dynasty_value nudged (max
      +/-25%) by proprietary_composite, the only place the composite lands
  roster quality_score = opportunity + production + trade_value_score, so
      the market-consensus standing; not hybrid_market_value
  Trade Agent sell_high = our internal risk flag (raw current_health_score /
      aging_risk) while trade_value_score is still elevated

These tests pin the deterministic math and the instruction wording. The
model authors trade_value_score, so only the instruction contract can be
asserted for it.
"""
import json
import os
import re
import unittest
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.market_agent as market_agent
import agents.roster_agent as roster_agent
import agents.synthesis_agent as synthesis_agent
import agents.trade_agent as trade_agent

# 21 RBs valued 2000..0; our player at 1400 sits at position percentile 70.0.
POOL = [{"player": {"position": "RB"}, "value": v} for v in range(2000, -1, -100)]
ME = {"player": {"position": "RB", "name": "Test RB"}, "value": 1400}

RISK_CASES = {
    "healthy/low-aging": (5, "low"),
    "healthy/high-aging": (5, "high"),
    "poor-health/low-aging": (2, "low"),
    "poor-health/high-aging": (2, "high"),
}


def _collapsed(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def _fc():
    return mock.patch.multiple(
        market_agent.fantasycalc_client,
        get_value_for_sleeper_id=mock.Mock(return_value=ME),
        get_dynasty_values=mock.Mock(return_value=POOL),
    )


def _card(health, aging, trade_value_score=70):
    return {"player": f"{health}/{aging}", "position": "RB", "opportunity_score": 75, "production_score": 80,
            "trade_value_score": trade_value_score,
            "risk_modifier": {"current_health_score": health, "aging_risk": aging}}


def _sell_signals(card):
    flagged = trade_agent.detect_sell_signals(json.dumps([card]))["flagged_players"]
    return [s["type"] for f in flagged for s in f["signals"]]


class DeterministicMarketContractTest(unittest.TestCase):
    def _blend(self, health, aging):
        comp = market_agent.compute_proprietary_composite(75, 80, health, aging)["proprietary_composite"]
        with _fc():
            return market_agent.blend_with_market(comp, "p1")

    def test_risk_moves_hybrid_value_but_not_market_percentile(self):
        results = {label: self._blend(*hr) for label, hr in RISK_CASES.items()}
        self.assertEqual({r["market_percentile"] for r in results.values()}, {70.0})
        self.assertEqual({r["dynasty_value"] for r in results.values()}, {1400})
        self.assertEqual(
            {label: r["hybrid_market_value"] for label, r in results.items()},
            {"healthy/low-aging": 1443, "healthy/high-aging": 1401,
             "poor-health/low-aging": 1390, "poor-health/high-aging": 1348},
        )

    def test_hybrid_equals_dynasty_value_when_composite_matches_market(self):
        with _fc():
            out = market_agent.blend_with_market(70.0, "p1")
        self.assertEqual(out["hybrid_market_value"], out["dynasty_value"])
        self.assertEqual(out["blend_multiplier"], 1.0)

    def test_roster_quality_ignores_risk_and_hybrid_value(self):
        base = roster_agent._quality_score(_card(5, "low"))
        for health, aging in RISK_CASES.values():
            card = _card(health, aging)
            card["hybrid_market_value"] = 1  # must not be read
            self.assertEqual(roster_agent._quality_score(card), base)
        self.assertAlmostEqual(base, 75 * 0.30 + 80 * 0.40 + 70 * 0.30)

    def test_sell_high_turns_on_from_internal_risk_alone(self):
        self.assertEqual(_sell_signals(_card(5, "low")), [])
        for label in ("healthy/high-aging", "poor-health/low-aging", "poor-health/high-aging"):
            with self.subTest(label):
                self.assertEqual(_sell_signals(_card(*RISK_CASES[label])), ["sell_high"])

    def test_sell_high_needs_elevated_market_standing(self):
        self.assertEqual(_sell_signals(_card(2, "high", trade_value_score=54)), [])

    def test_unknown_age_is_treated_like_low_age_everywhere(self):
        # Pinned as-is for Stage 3D: no composite penalty and no sell_high trigger.
        low = market_agent.compute_proprietary_composite(75, 80, 5, "low")
        unknown = market_agent.compute_proprietary_composite(75, 80, 5, "unknown")
        self.assertEqual(unknown, low)
        self.assertEqual(_sell_signals(_card(5, "unknown")), [])

    def test_thresholds_and_weights_unchanged(self):
        self.assertEqual(market_agent.MAX_BLEND_ADJUSTMENT, 0.25)
        self.assertEqual(trade_agent.SELL_HIGH_MARKET_FLOOR, 55)
        self.assertEqual(trade_agent.SELL_HIGH_CURRENT_HEALTH_MAX, 2)
        self.assertEqual(trade_agent.SELL_BEFORE_DROP_GAP, 15)
        self.assertEqual(roster_agent.QUALITY_WEIGHTS, {"opportunity": 0.30, "production": 0.40, "trade_value": 0.30})


class MarketWordingContractTest(unittest.TestCase):
    def test_prompt_defines_trade_value_score_as_market_percentile(self):
        prompt = _collapsed(market_agent.SYSTEM_PROMPT)
        self.assertIn("trade_value_score is market_percentile", prompt)
        self.assertIn("our internal composite does not change trade_value_score", prompt)

    def test_prompt_does_not_call_the_trade_value_grade_a_blend(self):
        prompt = _collapsed(market_agent.SYSTEM_PROMPT)
        self.assertNotIn("determine a player's dynasty trade value by blending", prompt)

    def test_prompt_places_the_blend_in_hybrid_market_value(self):
        self.assertIn("hybrid_market_value is the only output our internal composite changes",
                      _collapsed(market_agent.SYSTEM_PROMPT))

    def test_module_docstring_does_not_call_trade_value_hybrid(self):
        doc = _collapsed(market_agent.__doc__)
        self.assertNotIn("hybrid trade value", doc)
        self.assertIn("market-consensus", doc)

    def test_synthesis_tool_description_matches(self):
        evaluate_market = next(t for t in synthesis_agent._make_tools({}, "p1") if t.__name__ == "evaluate_market")
        doc = _collapsed(evaluate_market.__doc__)
        self.assertNotIn("trade value — a hybrid", doc)
        self.assertIn("fantasycalc market-consensus standing", doc)

    def test_sell_high_detail_does_not_claim_market_causality(self):
        detail = trade_agent.detect_sell_signals(json.dumps([_card(2, "high")]))["flagged_players"][0]["signals"][0]["detail"]
        self.assertNotIn("hasn't caught down", detail)
        self.assertIn("despite our internal risk flag", detail)
        self.assertNotIn("the market hasn't caught down", _collapsed(trade_agent.__doc__))

    def test_output_schema_unchanged(self):
        prompt = market_agent.SYSTEM_PROMPT
        start = prompt.index("{", prompt.index("Output format"))
        example = json.loads(prompt[start:prompt.rindex("}") + 1])
        self.assertEqual(list(example), [
            "player", "position", "dynasty_value", "hybrid_market_value", "market_percentile",
            "trade_value_score", "trade_value_grade", "trend_30day", "trend_note", "key_factors", "summary",
        ])


if __name__ == "__main__":
    unittest.main()
