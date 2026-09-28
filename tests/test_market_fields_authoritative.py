"""
Stage 3C.6: every Market field a tool produced is set from the tool result
in code, end to end. The model writes narrative only.

  get_market_consensus -> player (name), position, dynasty_value, trend_30day
  blend_with_market    -> hybrid_market_value, market_percentile
  code                 -> trade_value_score, trade_value_grade
  model                -> trend_note, key_factors, summary

The tool results are captured from the agent's own tool calls (no second
FantasyCalc call), and only calls for the run's player_id count. The
proprietary composite the model hands blend_with_market must equal the one
computed in code from run_market_agent's own inputs (pure math): otherwise
the "deterministic" hybrid value would rest on a model-routed number.

Missing, mismatched or wrong-player tool results fail the run with the
project's {"error": ...} shape, never with model-authored market facts. The
synthesis card copies the deterministic Market facts, and fails as a card
({"error", "player", "position"}) when the Market result is unusable. The
app already offers a retry for that, and api.py leaves it out of the roster grade.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import agents.market_agent as market_agent
import agents.synthesis_agent as synthesis_agent

# 101 distinct RB values with 1400 at rank 41 -> market_percentile exactly 59.0.
POOL = [{"player": {"position": "RB"}, "value": 1400 + (41 - i) * 10} for i in range(101)]
FC_ENTRY = {"player": {"position": "RB", "name": "Test RB"}, "value": 1400, "positionRank": 42,
            "overallRank": 120, "trend30Day": -25, "redraftValue": 900}

WRONG = {"player": "Wrong Player", "position": "QB", "dynasty_value": 9999, "hybrid_market_value": 1,
         "market_percentile": 12, "trade_value_score": 12, "trade_value_grade": "F", "trend_30day": 999,
         "trend_note": "model note", "key_factors": ["model factor"], "summary": "model summary"}

INPUTS = {"low": (75, 80, 5, "low"), "high": (75, 80, 5, "high")}


def _fc(entry=FC_ENTRY):
    return mock.patch.multiple(
        market_agent.fantasycalc_client,
        get_value_for_sleeper_id=mock.Mock(return_value=entry),
        get_dynasty_values=mock.Mock(return_value=POOL),
    )


def _event(text):
    event = mock.Mock()
    event.is_final_response.return_value = True
    event.content.parts = [mock.Mock(text=text)]
    return event


def _tool(agent, name):
    return next(t for t in agent.tools if getattr(t, "__name__", "") == name)


def fake_market_runner(inputs, model_json=WRONG, consensus=True, blend=True,
                       blend_player="p1", composite_override=None):
    """Plays the model: calls the agent's own tools, then answers with model_json."""
    opp, prod, health, aging = inputs

    class FakeRunner:
        def __init__(self, *, agent, **_):
            self.agent = agent

        async def run_async(self, **_):
            if consensus:
                _tool(self.agent, "get_market_consensus")(player_id="p1")
            comp = _tool(self.agent, "compute_proprietary_composite")(opp, prod, health, aging)["proprietary_composite"]
            if blend:
                _tool(self.agent, "blend_with_market")(
                    proprietary_composite=comp if composite_override is None else composite_override,
                    player_id=blend_player)
            yield _event(json.dumps(model_json))
    return FakeRunner


def run_market(inputs=INPUTS["low"], entry=FC_ENTRY, **runner_kw):
    with _fc(entry), mock.patch.object(market_agent, "Runner", fake_market_runner(inputs, **runner_kw)):
        return asyncio.run(market_agent.run_market_agent("p1", *inputs))


class MarketFieldsFromToolsTest(unittest.TestCase):
    def test_every_wrong_model_fact_is_overwritten(self):
        out = run_market()
        expected = {"player": "Test RB", "position": "RB", "dynasty_value": 1400, "hybrid_market_value": 1481,
                    "market_percentile": 59.0, "trade_value_score": 59, "trade_value_grade": "B-",
                    "trend_30day": -25}
        self.assertEqual({k: out[k] for k in expected}, expected)
        self.assertNotIn("error", out)

    def test_narrative_fields_stay_model_authored(self):
        out = run_market()
        self.assertEqual((out["trend_note"], out["key_factors"], out["summary"]),
                         ("model note", ["model factor"], "model summary"))

    def test_output_keeps_the_schema_keys(self):
        self.assertEqual(set(run_market()), set(WRONG))

    def test_aging_changes_the_returned_hybrid_value(self):
        low, high = run_market(INPUTS["low"]), run_market(INPUTS["high"])
        self.assertEqual((low["hybrid_market_value"], high["hybrid_market_value"]), (1481, 1439))
        self.assertEqual((low["trade_value_score"], low["trade_value_grade"]),
                         (high["trade_value_score"], high["trade_value_grade"]))

    def test_out_of_pool_uses_the_tool_floor_and_unknown_identity(self):
        out = run_market(entry=None)
        self.assertEqual({k: out[k] for k in ("dynasty_value", "hybrid_market_value", "market_percentile",
                                              "trade_value_score", "trade_value_grade")},
                         {"dynasty_value": 0, "hybrid_market_value": 0, "market_percentile": 0,
                          "trade_value_score": 0, "trade_value_grade": "F"})
        self.assertEqual((out["player"], out["position"], out["trend_30day"]), (None, None, None))


class MissingOrUntrustedToolResultsFailTest(unittest.TestCase):
    def _assert_failed(self, out):
        self.assertIn("error", out)
        for key in ("market_percentile", "trade_value_score", "trade_value_grade",
                    "hybrid_market_value", "dynasty_value"):
            self.assertNotIn(key, out)

    def test_no_blend_call_fails_instead_of_using_the_model_percentile(self):
        self._assert_failed(run_market(blend=False))

    def test_no_consensus_call_fails(self):
        self._assert_failed(run_market(consensus=False))

    def test_blend_for_another_player_fails(self):
        self._assert_failed(run_market(blend_player="someone-else"))

    def test_blend_with_a_composite_that_does_not_match_the_inputs_fails(self):
        self._assert_failed(run_market(composite_override=99.0))


def _run_synthesis(market_result, card_json=None):
    async def fake_market(*_a, **_k):
        return market_result

    card_json = card_json or {
        "player": "Test RB", "position": "RB", "team": "KC", "opportunity_score": 75, "production_score": 80,
        "dynasty_value": 8888, "hybrid_market_value": 2, "trend_30day": 777,
        "trade_value_score": 13, "trade_value_grade": "D", "narrative": "synthesis narrative",
    }

    class FakeRunner:
        def __init__(self, *, agent, **_):
            self.agent = agent

        async def run_async(self, **_):
            if market_result is not None:
                await _tool(self.agent, "evaluate_market")("p1", 75, 80, 5, "low")
            yield _event(json.dumps(card_json))

    with mock.patch.object(synthesis_agent, "run_market_agent", fake_market), \
         mock.patch.object(synthesis_agent, "Runner", FakeRunner):
        return asyncio.run(synthesis_agent.run_synthesis_agent("p1"))


class SynthesisCopiesMarketFactsTest(unittest.TestCase):
    def test_card_market_facts_come_from_the_market_result(self):
        market = run_market()
        card = _run_synthesis(market)
        for key in ("dynasty_value", "hybrid_market_value", "trend_30day", "trade_value_score", "trade_value_grade"):
            with self.subTest(key):
                self.assertEqual(card[key], market[key])
        self.assertEqual(card["narrative"], "synthesis narrative")

    def test_card_fails_when_market_failed(self):
        card = _run_synthesis(run_market(blend=False))
        self.assertIn("error", card)
        self.assertEqual((card["player"], card["position"]), ("Test RB", "RB"))
        self.assertNotIn("hybrid_market_value", card)

    def test_card_fails_when_market_never_ran(self):
        card = _run_synthesis(None)
        self.assertIn("error", card)
        self.assertNotIn("trade_value_score", card)


if __name__ == "__main__":
    unittest.main()
