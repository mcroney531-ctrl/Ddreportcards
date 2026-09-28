"""
Stage 3C.5: the Trade Value Grade is deterministic Market-specific math.

Before this, MARKET_CALIBRATION held two incompatible scales for the same
market_percentile: the Market percentile bands (55-74 -> B/B-) and the
generic 0-100 conversion (<60 -> F). The prompt's own example
(59 -> B-) followed the bands, and the model picked the letter. Now:

  market_percentile  from the blend_with_market tool result (captured in code)
  trade_value_grade  = market_grade_from_percentile(raw percentile)
  trade_value_score  = percentile rounded half-up, for display/roster math

Grading uses the RAW percentile, so 89.6 is A- (not a 90th-percentile A),
even though its displayed score rounds to 90.

The synthesis card re-copies both fields from the Market result, because the
card (not the Market result) is what the app shows and the roster grades.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.market_agent as market_agent
import agents.synthesis_agent as synthesis_agent

BOUNDARIES = {
    100: "A+", 96: "A+", 95: "A", 90: "A", 89: "A-", 83: "A-", 82: "B+", 75: "B+",
    74: "B", 65: "B", 64: "B-", 59: "B-", 55: "B-", 54: "C+", 45: "C+", 44: "C",
    35: "C", 34: "D+", 25: "D+", 24: "D", 15: "D", 14: "D-", 8: "D-", 7: "F", 0: "F",
}

# 21 RBs valued 2000..0; value 1200 sits at position percentile 60.0.
POOL = [{"player": {"position": "RB"}, "value": v} for v in range(2000, -1, -100)]


def _entry(value):
    return {"player": {"position": "RB", "name": "T"}, "value": value,
            "positionRank": 1, "overallRank": 1, "trend30Day": 0, "redraftValue": 0}


def _fc(value):
    return mock.patch.multiple(
        market_agent.fantasycalc_client,
        get_value_for_sleeper_id=mock.Mock(return_value=_entry(value)),
        get_dynasty_values=mock.Mock(return_value=POOL),
    )


def _final_event(text):
    event = mock.Mock()
    event.is_final_response.return_value = True
    event.content.parts = [mock.Mock(text=text)]
    return event


def _fake_runner(model_json, inputs=(75, 80, 5, "low"), call_blend=True):
    """Stands in for the ADK Runner: calls the agent's own tools the way the
    model would (consensus, composite from the run's inputs, blend), then
    returns the model's (possibly wrong) JSON. Stage 3C.6 requires both
    FantasyCalc-backed tools to have run for this player."""
    class FakeRunner:
        def __init__(self, *, agent, **_):
            self.agent = agent

        async def run_async(self, **_):
            tools = {getattr(t, "__name__", ""): t for t in self.agent.tools}
            tools["get_market_consensus"](player_id="p1")
            comp = tools["compute_proprietary_composite"](*inputs)["proprietary_composite"]
            if call_blend:
                tools["blend_with_market"](proprietary_composite=comp, player_id="p1")
            yield _final_event(json.dumps(model_json))
    return FakeRunner


WRONG = {"player": "T", "position": "RB", "dynasty_value": 1, "hybrid_market_value": 1,
         "market_percentile": 12.0, "trade_value_score": 12, "trade_value_grade": "F",
         "trend_30day": 0, "trend_note": "", "key_factors": [], "summary": ""}


def _run_market(value, inputs=(75, 80, 5, "low"), model_json=WRONG, call_blend=True):
    with _fc(value), mock.patch.object(market_agent, "Runner", _fake_runner(model_json, inputs, call_blend)):
        return asyncio.run(market_agent.run_market_agent("p1", *inputs))


class MarketGradeTableTest(unittest.TestCase):
    def test_every_band_transition(self):
        for pct, grade in BOUNDARIES.items():
            with self.subTest(pct=pct):
                self.assertEqual(market_agent.market_grade_from_percentile(pct), grade)

    def test_grades_from_raw_not_rounded_percentile(self):
        cases = {89.6: "A-", 89.9: "A-", 90.0: "A", 95.9: "A", 54.9: "C+", 55.0: "B-",
                 7.9: "F", 8.0: "D-", 14.5: "D-"}
        for pct, grade in cases.items():
            with self.subTest(pct=pct):
                self.assertEqual(market_agent.market_grade_from_percentile(pct), grade)

    def test_score_rounds_half_up(self):
        self.assertEqual(market_agent.market_score_from_percentile(89.6), 90)
        self.assertEqual(market_agent.market_score_from_percentile(59.5), 60)
        self.assertEqual(market_agent.market_score_from_percentile(60.5), 61)
        self.assertEqual(market_agent.market_score_from_percentile(59.0), 59)


class CalibrationHasOneScaleTest(unittest.TestCase):
    def test_generic_conversion_table_is_gone(self):
        cal = market_agent.MARKET_CALIBRATION
        self.assertNotIn("<60→F", cal)
        self.assertNotIn("same scale used by the other agents", cal)

    def test_calibration_lists_the_deterministic_table(self):
        cal = market_agent.MARKET_CALIBRATION
        for lo, grade in market_agent.MARKET_GRADE_TABLE:
            self.assertIn(f"→{grade}", cal)
        self.assertIn("not the Production/Opportunity 0-100 scale", cal)

    def test_prompt_says_the_grade_is_supplied_not_chosen(self):
        self.assertIn("trade_value_score and trade_value_grade are deterministic", market_agent.SYSTEM_PROMPT)


class RunnerEnforcesDeterministicGradeTest(unittest.TestCase):
    def test_wrong_model_score_and_grade_are_overwritten_from_the_tool(self):
        # Value 1200 -> tool percentile 60.0 -> 60 / B-. The model claimed 12.0 / 12 / F.
        out = _run_market(1200)
        self.assertEqual(out["market_percentile"], 60.0)
        self.assertEqual(out["trade_value_score"], 60)
        self.assertEqual(out["trade_value_grade"], "B-")

    def test_percentile_59_gives_59_b_minus(self):
        # Pool of 101 values 100..0 puts value 59 at exactly percentile 59.0.
        pool = [{"player": {"position": "RB"}, "value": v} for v in range(100, -1, -1)]
        with mock.patch.multiple(
            market_agent.fantasycalc_client,
            get_value_for_sleeper_id=mock.Mock(return_value=_entry(59)),
            get_dynasty_values=mock.Mock(return_value=pool),
        ), mock.patch.object(market_agent, "Runner", _fake_runner(WRONG)):
            out = asyncio.run(market_agent.run_market_agent("p1", 75, 80, 5, "low"))
        self.assertEqual((out["market_percentile"], out["trade_value_score"], out["trade_value_grade"]), (59.0, 59, "B-"))

    def test_composite_cannot_move_score_or_grade(self):
        # Composites 82.2 / 70.2 / 55.2 / 32.2 from our own inputs: the grade holds,
        # while (since Stage 3C.6) the returned hybrid value follows the tool.
        inputs = [(75, 80, 5, "low"), (75, 80, 5, "high"), (75, 80, 2, "high"), (40, 40, 1, "high")]
        results = [_run_market(1200, inputs=i) for i in inputs]
        self.assertEqual({(r["trade_value_score"], r["trade_value_grade"]) for r in results}, {(60, "B-")})
        self.assertEqual(len({r["hybrid_market_value"] for r in results}), len(inputs))

    def test_without_a_blend_result_the_run_fails(self):
        # Stage 3C.6 removed the 3C.5 fallback to the model's own percentile.
        out = _run_market(1200, call_blend=False)
        self.assertIn("error", out)
        self.assertNotIn("trade_value_grade", out)


class SellBeforeDropWordingTest(unittest.TestCase):
    def test_detail_is_observational_and_math_unchanged(self):
        import agents.trade_agent as trade_agent
        card = {"player": "T", "position": "RB", "opportunity_score": 50, "production_score": 80,
                "trade_value_score": 65, "risk_modifier": {"current_health_score": 5, "aging_risk": "low"}}
        signals = trade_agent.detect_sell_signals(json.dumps([card]))["flagged_players"][0]["signals"]
        self.assertEqual([s["type"] for s in signals], ["sell_before_drop"])
        self.assertNotIn("hasn't priced in", signals[0]["detail"])
        self.assertIn("market standing is 15 pts above our internal opportunity score", signals[0]["detail"])
        self.assertNotIn("hasn't priced in", trade_agent.__doc__)
        card["trade_value_score"] = 64  # gap 14 < SELL_BEFORE_DROP_GAP
        self.assertEqual(trade_agent.detect_sell_signals(json.dumps([card]))["flagged_players"], [])


class SynthesisCardUsesMarketResultTest(unittest.TestCase):
    def test_card_fields_are_copied_from_the_market_result(self):
        market = {"market_percentile": 59.0, "trade_value_score": 59, "trade_value_grade": "B-"}

        async def fake_market(*_a, **_k):
            return market

        class FakeRunner:
            def __init__(self, *, agent, **_):
                self.agent = agent

            async def run_async(self, **_):
                tools = {t.__name__: t for t in self.agent.tools}
                await tools["evaluate_situation"]()
                await tools["evaluate_production"]()
                await tools["evaluate_market"]()  # Stage 3C.7: no arguments
                yield _final_event(json.dumps({"player": "T", "trade_value_score": 12, "trade_value_grade": "F"}))

        async def fake_situation(*_a):
            return {"player": "T", "position": "RB", "team": "KC", "opportunity_score": 75, "opportunity_grade": "B+"}

        async def fake_production(*_a):
            return {"production_score": 80, "production_grade": "B",
                    "risk_modifier": {"current_health_score": 5, "aging_risk": "low"}}

        with mock.patch.object(synthesis_agent, "run_market_agent", fake_market), \
             mock.patch.object(synthesis_agent, "run_situation_agent", fake_situation), \
             mock.patch.object(synthesis_agent, "run_production_agent", fake_production), \
             mock.patch.object(synthesis_agent, "Runner", FakeRunner):
            card = asyncio.run(synthesis_agent.run_synthesis_agent("p1"))
        self.assertEqual((card["trade_value_score"], card["trade_value_grade"]), (59, "B-"))


if __name__ == "__main__":
    unittest.main()
