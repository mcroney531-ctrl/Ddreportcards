"""
Experiment 6: distinguish process-global first-touch cost from same-agent
declaration caching and different-agent first-use cost.

Experiment 5 found a ~19.3s / +22.5MB gap between Runner.run_async() starting
and the first before_model callback firing on situation_agent's first
invocation. This endpoint isolates that gap with three pre-model-only trials
(first situation_agent, second situation_agent -- same tool functions, so ADK's
FunctionTool declaration cache is reused -- and first production_agent, with
different tool functions) plus an independent cache-amortization measurement.

It is deliberately pre-model-only: each trial's before_model_callback is
replaced with one that records the checkpoint and raises immediately, before
any budget reservation or provider call can happen. These tests verify that
contract directly -- zero Anthropic calls, zero budget reservations, zero tool
invocations -- plus the same flag+secret gating and restoration-on-failure
guarantees the other two profiling endpoints already have.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import budget
from google.adk.models.lite_llm import LiteLlm

FAKE_SLEEPER_PLAYERS = {"1": {"full_name": "Fake Player"}}
FAKE_FANTASYCALC_VALUES = [
    {"player": {"sleeperId": "1", "position": "RB"}, "value": 100, "redraftValue": 90}
]


class RunnerSetupGatingTest(unittest.TestCase):
    def test_returns_404_when_flag_is_unset(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", False):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile-runner-setup",
                headers={"X-GM-Key": "anything"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_returns_401_with_wrong_secret_when_enabled(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile-runner-setup",
                headers={"X-GM-Key": "wrong"},
            )
        self.assertEqual(resp.status_code, 401)


class RunnerSetupIsPreModelOnlyTest(unittest.TestCase):
    """The whole point of Experiment 6: it must never reserve budget, never
    call the provider, and never execute a tool -- the sentinel callback
    always fires and stops the trial before any of those can happen."""

    def _run(self, **extra_patches):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"), \
             mock.patch(
                 "dynasty_core.sleeper.get_all_players", return_value=FAKE_SLEEPER_PLAYERS
             ), \
             mock.patch(
                 "dynasty_core.fantasycalc.get_dynasty_values", return_value=FAKE_FANTASYCALC_VALUES
             ):
            for target, side_effect in extra_patches.items():
                self.enterContext(mock.patch(target, side_effect=side_effect))
            client = TestClient(api.app)
            return client.post(
                "/internal/memory-profile-runner-setup",
                headers={"X-GM-Key": "real-secret"},
            )

    def test_succeeds_with_the_expected_summary_shape(self):
        resp = self._run()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertIn("player_id", body)
        self.assertIn("python_version", body)
        self.assertTrue(body["checkpoints"])

        for key in ("first_fetch", "second_fetch"):
            self.assertIn("elapsed_ms", body["cache_summary"][key])
            self.assertIn("rss_delta_mb", body["cache_summary"][key])

        for key in ("first_situation", "second_situation", "first_production"):
            self.assertIn("elapsed_ms", body["runner_setup_summary"][key])
            self.assertIn("rss_delta_mb", body["runner_setup_summary"][key])

    def test_never_reserves_budget(self):
        resp = self._run(**{
            "budget.reserve": AssertionError("Experiment 6 must never reserve budget"),
        })
        self.assertEqual(resp.status_code, 200)

    def test_never_calls_the_provider(self):
        with mock.patch.object(
            LiteLlm, "generate_content_async",
            side_effect=AssertionError("Experiment 6 must never call the provider"),
        ):
            resp = self._run()
        self.assertEqual(resp.status_code, 200)

    def test_never_executes_a_situation_tool(self):
        resp = self._run(**{
            "agents.situation_agent.lookup_player_situation": AssertionError(
                "Experiment 6 must never execute a tool"
            ),
        })
        self.assertEqual(resp.status_code, 200)

    def test_never_executes_a_production_tool(self):
        resp = self._run(**{
            "agents.production_agent.lookup_player_info": AssertionError(
                "Experiment 6 must never execute a tool"
            ),
        })
        self.assertEqual(resp.status_code, 200)

    def test_response_never_leaks_secrets_or_env_vars(self):
        resp = self._run()
        body_text = resp.text.lower()
        for leaked in ("anthropic_api_key", "gm_upstash", "gm_chat_secret", "x-gm-key", "real-secret"):
            self.assertNotIn(leaked, body_text)


class RunnerSetupRestoresCallbacksTest(unittest.TestCase):
    def test_both_modules_callbacks_restored_after_a_normal_run(self):
        import agents.situation_agent as situation_mod
        import agents.production_agent as production_mod

        original_situation = situation_mod.check_budget_before_model
        original_production = production_mod.check_budget_before_model

        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"), \
             mock.patch(
                 "dynasty_core.sleeper.get_all_players", return_value=FAKE_SLEEPER_PLAYERS
             ), \
             mock.patch(
                 "dynasty_core.fantasycalc.get_dynasty_values", return_value=FAKE_FANTASYCALC_VALUES
             ):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile-runner-setup",
                headers={"X-GM-Key": "real-secret"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertIs(situation_mod.check_budget_before_model, original_situation)
        self.assertIs(production_mod.check_budget_before_model, original_production)

    def test_situation_callback_restored_even_when_a_trial_fails_for_a_real_reason(self):
        import agents.situation_agent as situation_mod

        original_situation = situation_mod.check_budget_before_model

        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"), \
             mock.patch(
                 "dynasty_core.sleeper.get_all_players", return_value=FAKE_SLEEPER_PLAYERS
             ), \
             mock.patch(
                 "dynasty_core.fantasycalc.get_dynasty_values", return_value=FAKE_FANTASYCALC_VALUES
             ), \
             mock.patch.object(
                 situation_mod, "build_situation_agent",
                 side_effect=RuntimeError("simulated construction failure"),
             ):
            client = TestClient(api.app, raise_server_exceptions=False)
            resp = client.post(
                "/internal/memory-profile-runner-setup",
                headers={"X-GM-Key": "real-secret"},
            )

        self.assertEqual(resp.status_code, 500)
        self.assertIs(situation_mod.check_budget_before_model, original_situation)


if __name__ == "__main__":
    unittest.main()
