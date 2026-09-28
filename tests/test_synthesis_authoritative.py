"""
Stage 3C.7: Synthesis orchestrates and narrates; it does not re-author
sub-agent results.

- The three tools take no arguments. They are closed over the run's
  player_id, so the Synthesis model can't choose or change the player.
- evaluate_market reads its inputs from the Situation/Production results
  (opportunity_score, production_score, risk_modifier.current_health_score,
  risk_modifier.aging_risk). It refuses with {"error": ...}, instead of
  substituting anything, when those results aren't there yet or aren't usable.
- The card copies SITUATION_CARD_FIELDS, PRODUCTION_CARD_FIELDS and
  MARKET_CARD_FIELDS from the sub-agent results. The Synthesis model keeps
  key_strengths, key_concerns and narrative. An unusable Situation,
  Production or Market result fails the card.

The last class is the Stage 3D gate: a deterministic age classification
flows, with no LLM copy in between, into Market's hybrid value, the card's
risk_modifier, sell_high and the roster quality inputs.
"""
import asyncio
import inspect
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.market_agent as market_agent
import agents.production_agent as production_agent
import agents.roster_agent as roster_agent
import agents.synthesis_agent as synthesis_agent
import agents.trade_agent as trade_agent

SITUATION = {"player": "Test RB", "position": "RB", "team": "KC", "depth_chart_order": 1,
             "opportunity_score": 75, "opportunity_grade": "B+", "summary": "situation"}
PRODUCTION = {"player": "Test RB", "position": "RB", "production_score": 80, "production_grade": "B",
              "risk_modifier": {"current_health_score": 5, "injury_notes": "", "aging_risk": "high",
                                "career_window_note": "configured note"}}
MARKET = {"dynasty_value": 1400, "hybrid_market_value": 1439, "trend_30day": -25,
          "trade_value_score": 59, "trade_value_grade": "B-", "market_percentile": 59.0}

LYING_CARD = {
    "player": "Wrong Player", "position": "QB", "team": "XXX",
    "opportunity_score": 1, "opportunity_grade": "F", "production_score": 2, "production_grade": "F",
    "risk_modifier": {"current_health_score": 1, "aging_risk": "low", "career_window_note": "lie"},
    "dynasty_value": 8888, "hybrid_market_value": 2, "trend_30day": 777,
    "trade_value_score": 13, "trade_value_grade": "D",
    "key_strengths": ["synthesis strength"], "key_concerns": ["synthesis concern"], "narrative": "synthesis narrative",
}


def _event(text):
    event = mock.Mock()
    event.is_final_response.return_value = True
    event.content.parts = [mock.Mock(text=text)]
    return event


def _async_returning(value, calls=None):
    async def fake(*args):
        if calls is not None:
            calls.append(args)
        return value
    return fake


def fake_synthesis_runner(order=("evaluate_situation", "evaluate_production", "evaluate_market"),
                          card=LYING_CARD, tool_results=None):
    class FakeRunner:
        def __init__(self, *, agent, **_):
            self.agent = agent

        async def run_async(self, **_):
            tools = {t.__name__: t for t in self.agent.tools}
            for name in order:
                result = await tools[name]()
                if tool_results is not None:
                    tool_results.append((name, result))
            yield _event(json.dumps(card))
    return FakeRunner


def run_synthesis(situation=SITUATION, production=PRODUCTION, market=MARKET, market_calls=None, **runner_kw):
    with mock.patch.object(synthesis_agent, "run_situation_agent", _async_returning(situation)), \
         mock.patch.object(synthesis_agent, "run_production_agent", _async_returning(production)), \
         mock.patch.object(synthesis_agent, "run_market_agent", _async_returning(market, market_calls)), \
         mock.patch.object(synthesis_agent, "Runner", fake_synthesis_runner(**runner_kw)):
        return asyncio.run(synthesis_agent.run_synthesis_agent("p1"))


class SynthesisToolsAreClosedOverThePlayerTest(unittest.TestCase):
    def test_tools_take_no_arguments(self):
        for tool in synthesis_agent._make_tools({}, "p1"):
            with self.subTest(tool.__name__):
                self.assertEqual(list(inspect.signature(tool).parameters), [])

    def test_sub_agents_are_called_for_the_run_player(self):
        seen = []

        async def rec(pid):
            seen.append(pid)
            return SITUATION

        with mock.patch.object(synthesis_agent, "run_situation_agent", rec):
            evaluate_situation = next(t for t in synthesis_agent._make_tools({}, "p1") if t.__name__ == "evaluate_situation")
            asyncio.run(evaluate_situation())
        self.assertEqual(seen, ["p1"])


class MarketInputsComeFromSubAgentsTest(unittest.TestCase):
    def test_market_receives_the_sub_agent_values(self):
        calls = []
        run_synthesis(market_calls=calls)
        self.assertEqual(calls, [("p1", 75, 80, 5, "high")])

    def test_market_before_upstream_results_refuses(self):
        calls, results = [], []
        run_synthesis(order=("evaluate_market",), market_calls=calls, tool_results=results)
        self.assertEqual(calls, [])
        self.assertIn("error", results[0][1])

    def test_unusable_upstream_values_are_refused_not_substituted(self):
        bad_productions = {
            "missing score": {**PRODUCTION, "production_score": None},
            "risk_modifier not a dict": {**PRODUCTION, "risk_modifier": "high"},
            "missing health": {**PRODUCTION, "risk_modifier": {"aging_risk": "high"}},
            "missing aging": {**PRODUCTION, "risk_modifier": {"current_health_score": 5}},
            "errored": {"error": "production evaluation incomplete"},
        }
        for label, production in bad_productions.items():
            with self.subTest(label):
                calls = []
                card = run_synthesis(production=production, market_calls=calls)
                self.assertEqual(calls, [])
                self.assertIn("error", card)
        calls = []
        card = run_synthesis(situation={**SITUATION, "opportunity_score": "high"}, market_calls=calls)
        self.assertEqual(calls, [])
        self.assertIn("error", card)


class CardFieldsComeFromTheirOwnersTest(unittest.TestCase):
    def test_every_lying_synthesis_copy_is_overwritten(self):
        card = run_synthesis()
        self.assertEqual({k: card[k] for k in ("player", "position", "team", "opportunity_score", "opportunity_grade")},
                         {"player": "Test RB", "position": "RB", "team": "KC",
                          "opportunity_score": 75, "opportunity_grade": "B+"})
        self.assertEqual((card["production_score"], card["production_grade"]), (80, "B"))
        self.assertEqual(card["risk_modifier"], PRODUCTION["risk_modifier"])
        for key in synthesis_agent.MARKET_CARD_FIELDS:
            self.assertEqual(card[key], MARKET[key])

    def test_synthesis_narrative_is_kept(self):
        card = run_synthesis()
        self.assertEqual((card["key_strengths"], card["key_concerns"], card["narrative"]),
                         (["synthesis strength"], ["synthesis concern"], "synthesis narrative"))

    def test_roster_quality_uses_the_authoritative_fields(self):
        card = run_synthesis()
        self.assertAlmostEqual(roster_agent._quality_score(card), 75 * 0.30 + 80 * 0.40 + 59 * 0.30)

    def test_unusable_situation_fails_the_card_without_guessed_identity(self):
        card = run_synthesis(situation={"raw_output": "?"})
        self.assertIn("error", card)
        self.assertIsNone(card.get("player"))


# ── Stage 3D gate ────────────────────────────────────────────────────────────

POOL = [{"player": {"position": "RB"}, "value": 1400 + (41 - i) * 10} for i in range(101)]
FC_ENTRY = {"player": {"position": "RB", "name": "Test RB"}, "value": 1400, "positionRank": 42,
            "overallRank": 120, "trend30Day": -25, "redraftValue": 900}


class _ProductionModel:
    """Plays the Production model: looks the player up, then claims 'low' aging."""
    def __init__(self, *, agent, **_):
        self.agent = agent

    async def run_async(self, **_):
        next(t for t in self.agent.tools if t.__name__ == "lookup_player_info")(player_id="p1")
        yield _event(json.dumps({**PRODUCTION, "risk_modifier": {
            "current_health_score": 5, "injury_notes": "", "aging_risk": "low", "career_window_note": "lie"}}))


class _MarketModel:
    """Plays the Market model: real tools on the run's own inputs, then wrong JSON."""
    def __init__(self, *, agent, **_):
        self.agent = agent

    async def run_async(self, *, new_message, **_):
        import re
        args = dict(re.findall(r"(\w+)=('?[\w.]+'?)", new_message.parts[0].text))
        tools = {t.__name__: t for t in self.agent.tools}
        tools["get_market_consensus"](player_id="p1")
        comp = tools["compute_proprietary_composite"](
            int(args["opportunity_score"]), int(args["production_score"]),
            int(args["current_health_score"]), args["aging_risk"].strip("'"))["proprietary_composite"]
        tools["blend_with_market"](proprietary_composite=comp, player_id="p1")
        yield _event(json.dumps({"player": "Wrong", "hybrid_market_value": 1, "trade_value_score": 12,
                                 "trade_value_grade": "F", "trend_note": "n", "key_factors": [], "summary": "s"}))


def run_pipeline(age):
    players = {"p1": {"full_name": "Test RB", "position": "RB", "team": "KC", "age": age,
                      "years_exp": 5, "espn_id": "123"}}
    with mock.patch.object(production_agent.sleeper_client, "get_all_players", return_value=players), \
         mock.patch.object(production_agent, "Runner", _ProductionModel), \
         mock.patch.multiple(market_agent.fantasycalc_client,
                             get_value_for_sleeper_id=mock.Mock(return_value=FC_ENTRY),
                             get_dynasty_values=mock.Mock(return_value=POOL)), \
         mock.patch.object(market_agent, "Runner", _MarketModel), \
         mock.patch.object(synthesis_agent, "run_situation_agent", _async_returning(SITUATION)), \
         mock.patch.object(synthesis_agent, "Runner", fake_synthesis_runner()):
        return asyncio.run(synthesis_agent.run_synthesis_agent("p1"))


class Stage3DGateTest(unittest.TestCase):
    def test_age_classification_flows_through_value_card_and_sell_high(self):
        young, old = run_pipeline(24), run_pipeline(30)

        self.assertEqual((young["risk_modifier"]["aging_risk"], old["risk_modifier"]["aging_risk"]), ("low", "high"))
        self.assertEqual((young["hybrid_market_value"], old["hybrid_market_value"]), (1481, 1439))
        self.assertEqual((young["trade_value_score"], old["trade_value_score"]), (59, 59))

        def sell(card):
            flagged = trade_agent.detect_sell_signals(json.dumps([card]))["flagged_players"]
            return [s["type"] for f in flagged for s in f["signals"]]

        self.assertEqual(sell(young), [])
        self.assertEqual(sell(old), ["sell_high"])
        self.assertEqual(roster_agent._quality_score(young), roster_agent._quality_score(old))


if __name__ == "__main__":
    unittest.main()
