"""
Ddreportcards correctness interlude: agents/production_agent.py asked the
model for a fabricated injury_chance_pct (a numeric future-injury
probability with no predictive model behind it) alongside a durability_score
that current injury/status evidence never actually established as a
historical record. Both propagated further here than in Scout: Production ->
Synthesis -> Market's proprietary composite -> Trade's sell-high signal ->
the Streamlit UI.

The fix splits the Risk Modifier into two supported signals:
  current_health_score -- present health/availability only, scored from
    Sleeper's live injury/practice status + ESPN's current team-feed notes.
    Not a historical durability assessment, not a forecast of future injury
    probability.
  aging_risk -- age/career-window signal, computed separately by
    compute_age_curve_signal and untouched by this pass.

injury_chance_pct is removed entirely, with no percentage replacement.
The risk_modifier container name, WEIGHTS["risk"] naming, Market's weights/
aging penalties, and Trade's sell-high threshold VALUE are all unchanged --
this is a semantic correction, not a scoring-model redesign.
"""
import inspect
import json
import os
import unittest

import agents.production_agent as production_agent
import agents.synthesis_agent as synthesis_agent
import agents.market_agent as market_agent
import agents.trade_agent as trade_agent


class InjuryChancePctIsGoneTest(unittest.TestCase):
    """injury_chance_pct must not appear anywhere in active Production/
    Synthesis/Market/Trade prompts, output schemas, or UI source."""

    def test_production_agent_prompt_has_no_injury_chance_pct(self):
        self.assertNotIn("injury_chance_pct", production_agent.SYSTEM_PROMPT)

    def test_production_agent_calibration_has_no_injury_chance_pct(self):
        self.assertNotIn("injury_chance_pct", production_agent.PRODUCTION_CALIBRATION)

    def test_production_agent_calibration_has_no_probability_percentages(self):
        # The old calibration tiers read like "4 / 10-20%" and "1 / >50%".
        # Those percentage ranges must be gone.
        for stale_pct in ("<10%", "10-20%", "20-35%", "35-50%", ">50%"):
            self.assertNotIn(stale_pct, production_agent.PRODUCTION_CALIBRATION)

    def test_production_agent_prompt_bans_numeric_injury_probability(self):
        collapsed = " ".join(production_agent.PRODUCTION_CALIBRATION.lower().split())
        self.assertIn("do not output a numeric injury probability", collapsed)

    def test_synthesis_agent_prompt_has_no_injury_chance_pct(self):
        self.assertNotIn("injury_chance_pct", synthesis_agent.SYSTEM_PROMPT)

    def test_market_agent_prompt_has_no_injury_chance_pct(self):
        self.assertNotIn("injury_chance_pct", market_agent.SYSTEM_PROMPT)

    def test_trade_agent_prompt_has_no_injury_chance_pct(self):
        self.assertNotIn("injury_chance_pct", trade_agent.SYSTEM_PROMPT)

    def test_app_source_has_no_injury_chance_pct(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")
        with open(app_path, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("injury_chance_pct", source)
        self.assertNotIn("Injury chance", source)


class DurabilityScoreIsGoneFromActiveScoringTest(unittest.TestCase):
    """durability_score must not appear in active Production/Synthesis/
    Market/Trade scoring paths or the UI."""

    def test_production_agent_prompt_has_no_durability_score(self):
        self.assertNotIn("durability_score", production_agent.SYSTEM_PROMPT)

    def test_synthesis_agent_prompt_has_no_durability_score(self):
        self.assertNotIn("durability_score", synthesis_agent.SYSTEM_PROMPT)

    def test_market_agent_prompt_has_no_durability_score(self):
        self.assertNotIn("durability_score", market_agent.SYSTEM_PROMPT)

    def test_market_agent_compute_signature_has_no_durability_score(self):
        params = inspect.signature(market_agent.compute_proprietary_composite).parameters
        self.assertNotIn("durability_score", params)

    def test_market_agent_run_signature_has_no_durability_score(self):
        params = inspect.signature(market_agent.run_market_agent).parameters
        self.assertNotIn("durability_score", params)

    def test_trade_agent_has_no_durability_constant_or_field(self):
        self.assertFalse(hasattr(trade_agent, "SELL_HIGH_DURABILITY_MAX"))
        self.assertNotIn("durability_score", trade_agent.SYSTEM_PROMPT)

    def test_app_source_has_no_durability_score(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")
        with open(app_path, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("durability_score", source)
        self.assertNotIn("Durability", source)


class CurrentHealthScoreFlowsProductionToSynthesisToMarketTest(unittest.TestCase):
    def test_production_agent_prompt_uses_current_health_score(self):
        self.assertIn("current_health_score", production_agent.SYSTEM_PROMPT)

    def test_production_agent_calibration_describes_current_health_score(self):
        collapsed = " ".join(production_agent.PRODUCTION_CALIBRATION.lower().split())
        self.assertIn("current health score", collapsed)
        self.assertIn("not a forecast of future injury probability", collapsed)
        self.assertIn("not a historical durability assessment", collapsed)

    def test_synthesis_agent_evaluate_market_tool_takes_current_health_score(self):
        sub_results: dict = {}
        tools = synthesis_agent._make_tools(sub_results)
        evaluate_market = next(t for t in tools if t.__name__ == "evaluate_market")
        params = inspect.signature(evaluate_market).parameters
        self.assertIn("current_health_score", params)
        self.assertNotIn("durability_score", params)

    def test_synthesis_agent_prompt_uses_current_health_score(self):
        self.assertIn("current_health_score", synthesis_agent.SYSTEM_PROMPT)

    def test_market_agent_compute_signature_has_current_health_score(self):
        params = inspect.signature(market_agent.compute_proprietary_composite).parameters
        self.assertIn("current_health_score", params)

    def test_market_agent_run_signature_has_current_health_score(self):
        params = inspect.signature(market_agent.run_market_agent).parameters
        self.assertIn("current_health_score", params)

    def test_market_agent_prompt_uses_current_health_score(self):
        self.assertIn("current_health_score", market_agent.SYSTEM_PROMPT)

    def test_trade_agent_uses_current_health_score(self):
        self.assertTrue(hasattr(trade_agent, "SELL_HIGH_CURRENT_HEALTH_MAX"))
        self.assertIn("current_health_score", inspect.getsource(trade_agent.detect_sell_signals))

    def test_app_source_uses_current_health_score(self):
        app_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py")
        with open(app_path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("current_health_score", source)
        self.assertIn("Current health", source)


class MarketConversionAndWeightsUnchangedTest(unittest.TestCase):
    """The deterministic 1-5 -> 0-100 conversion, the 45/35/20 weights, and
    the aging-risk penalties must be byte-for-byte unchanged -- only the
    semantics of the input, not the math."""

    def test_weights_unchanged(self):
        self.assertEqual(market_agent.WEIGHTS, {"production": 0.45, "opportunity": 0.35, "risk": 0.20})

    def test_aging_risk_penalty_unchanged(self):
        self.assertEqual(
            market_agent.AGING_RISK_PENALTY,
            {"low": 0, "moderate": -5, "high": -12, "unknown": 0},
        )

    def test_max_blend_adjustment_unchanged(self):
        self.assertEqual(market_agent.MAX_BLEND_ADJUSTMENT, 0.25)

    def test_health_score_of_1_converts_to_risk_score_of_0(self):
        result = market_agent.compute_proprietary_composite(
            opportunity_score=0, production_score=0, current_health_score=1, aging_risk="unknown"
        )
        self.assertEqual(result["risk_score"], 0)

    def test_health_score_of_5_converts_to_risk_score_of_100(self):
        result = market_agent.compute_proprietary_composite(
            opportunity_score=0, production_score=0, current_health_score=5, aging_risk="unknown"
        )
        self.assertEqual(result["risk_score"], 100)


class TradeSellHighThresholdAndBranchesUnchangedTest(unittest.TestCase):
    """The sell-high threshold VALUE is unchanged (still 2), just renamed/
    reinterpreted, and both independent trigger paths (low health, high
    aging risk) still fire the same deterministic branch."""

    def test_threshold_value_is_still_2(self):
        self.assertEqual(trade_agent.SELL_HIGH_CURRENT_HEALTH_MAX, 2)

    def _cards(self, risk_modifier_a, risk_modifier_b=None):
        cards = [
            {
                "player": "Player A", "position": "RB", "team": "TEN",
                "opportunity_score": 60, "production_score": 60, "trade_value_score": 60,
                "risk_modifier": risk_modifier_a,
                "key_concerns": [], "narrative": "",
            },
        ]
        if risk_modifier_b is not None:
            cards.append({
                "player": "Player B", "position": "WR", "team": "KC",
                "opportunity_score": 60, "production_score": 60, "trade_value_score": 60,
                "risk_modifier": risk_modifier_b,
                "key_concerns": [], "narrative": "",
            })
        return cards

    def test_low_current_health_score_triggers_sell_high(self):
        cards = self._cards({"current_health_score": 2, "aging_risk": "low"})
        result = trade_agent.detect_sell_signals(json.dumps(cards))
        flagged = result["flagged_players"]
        self.assertEqual(len(flagged), 1)
        signal_types = [s["type"] for s in flagged[0]["signals"]]
        self.assertIn("sell_high", signal_types)

    def test_high_aging_risk_alone_triggers_sell_high(self):
        # current_health_score is healthy (5) -- only aging_risk="high" should fire it.
        cards = self._cards({"current_health_score": 5, "aging_risk": "high"})
        result = trade_agent.detect_sell_signals(json.dumps(cards))
        flagged = result["flagged_players"]
        self.assertEqual(len(flagged), 1)
        signal_types = [s["type"] for s in flagged[0]["signals"]]
        self.assertIn("sell_high", signal_types)

    def test_healthy_and_young_does_not_trigger_sell_high(self):
        cards = self._cards({"current_health_score": 5, "aging_risk": "low"})
        result = trade_agent.detect_sell_signals(json.dumps(cards))
        self.assertEqual(result["flagged_players"], [])

    def test_flag_detail_references_current_health_score_not_durability(self):
        cards = self._cards({"current_health_score": 1, "aging_risk": "low"})
        result = trade_agent.detect_sell_signals(json.dumps(cards))
        detail = result["flagged_players"][0]["signals"][0]["detail"]
        self.assertIn("current_health_score", detail)
        self.assertNotIn("durability_score", detail)


class VerifiedCurrentEspnInjuryNotesRemainAvailableTest(unittest.TestCase):
    """The working ESPN team-feed injury tool must not have been removed by
    this pass -- current_health_score still needs it as an evidence source."""

    def test_get_injury_notes_still_defined_and_registered(self):
        self.assertTrue(hasattr(production_agent, "get_injury_notes"))
        agent = production_agent.build_production_agent()
        tool_names = [t.__name__ for t in agent.tools]
        self.assertIn("get_injury_notes", tool_names)
        self.assertIn("lookup_player_info", tool_names)
        self.assertIn("compute_age_curve_signal", tool_names)


class NoHistoricalInjuryClaimIntroducedTest(unittest.TestCase):
    def test_production_agent_prompt_disclaims_historical_durability(self):
        collapsed = " ".join(production_agent.PRODUCTION_CALIBRATION.lower().split())
        self.assertIn("no verified source of historical injury data", collapsed)

    def test_production_agent_module_docstring_disclaims_historical_claims(self):
        collapsed = " ".join((production_agent.__doc__ or "").lower().split())
        self.assertIn("no predictive model behind a numeric injury probability", collapsed)

    def test_market_agent_docstring_disclaims_historical_durability(self):
        doc = inspect.getdoc(market_agent.compute_proprietary_composite) or ""
        self.assertIn("not a historical durability assessment", doc)


if __name__ == "__main__":
    unittest.main()
