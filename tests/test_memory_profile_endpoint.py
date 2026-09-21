"""
Experiment 4: staged memory attribution on the actual Render runtime.

/internal/memory-profile is temporary, purpose-built diagnostic scaffolding
to isolate where the ~130MB persistent RSS jump from a report request comes
from (Sleeper/FantasyCalc caches vs. agent-module imports vs. ADK Agent/
Runner construction) with zero LLM spend, before spending a token on the
next experiment. It is gated behind an env flag (off by default) *and* the
same X-GM-Key secret /report uses, and is meant to be removed once the
experiment concludes -- these tests exist so a forgotten flag or a broken
gate doesn't ship an open diagnostic endpoint.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api

FAKE_SLEEPER_PLAYERS = {"1": {"full_name": "Fake Player"}}
FAKE_FANTASYCALC_VALUES = [
    {"player": {"sleeperId": "1", "position": "RB"}, "value": 100, "redraftValue": 90}
]


class MemoryProfileDisabledByDefaultTest(unittest.TestCase):
    def test_returns_404_when_flag_is_unset(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", False):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile",
                headers={"X-GM-Key": "anything"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_returns_404_even_with_correct_secret_when_flag_is_unset(self):
        # The env-flag gate must win even if the caller somehow already has
        # the real secret -- this endpoint should not exist in production
        # traffic's eyes at all while the flag is off.
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", False), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile",
                headers={"X-GM-Key": "real-secret"},
            )
        self.assertEqual(resp.status_code, 404)


class MemoryProfileRequiresSecretWhenEnabledTest(unittest.TestCase):
    def test_401_with_missing_key_when_flag_is_enabled(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"):
            client = TestClient(api.app)
            resp = client.post("/internal/memory-profile")
        self.assertEqual(resp.status_code, 401)

    def test_401_with_wrong_key_when_flag_is_enabled(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile",
                headers={"X-GM-Key": "wrong-secret"},
            )
        self.assertEqual(resp.status_code, 401)


class MemoryProfileStagedOutputTest(unittest.TestCase):
    def _run_with_mocks(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"), \
             mock.patch(
                 "dynasty_core.sleeper.get_all_players",
                 return_value=FAKE_SLEEPER_PLAYERS,
             ), \
             mock.patch(
                 "dynasty_core.fantasycalc.get_dynasty_values",
                 return_value=FAKE_FANTASYCALC_VALUES,
             ):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile",
                headers={"X-GM-Key": "real-secret"},
            )
        return resp

    def test_succeeds_and_returns_the_full_stage_table(self):
        resp = self._run_with_mocks()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["sleeper_record_count"], 1)
        self.assertEqual(body["fantasycalc_record_count"], 1)
        self.assertIn("sleeper_cache_prepopulated", body)
        self.assertIn("fantasycalc_cache_prepopulated", body)
        self.assertIn("thread_count", body)
        self.assertIn("python_version", body)

        stage_names = [s["stage"] for s in body["stages"]]
        expected_stages = [
            "0_baseline",
            "1_import_dynasty_core_sleeper",
            "2_sleeper_get_all_players",
            "3_gc_collect",
            "4_import_dynasty_core_fantasycalc",
            "5_fantasycalc_get_dynasty_values",
            "6_index_by_sleeper_id_with_redraft_rank",
            "7_gc_collect",
            "8_import_agents_situation_agent",
            "9_construct_build_situation_agent",
            "10_situation_session_service_and_runner",
            "11_gc_collect_after_situation",
            "12_import_agents_production_agent",
            "13_construct_build_production_agent",
            "14_production_session_service_and_runner",
            "15_gc_collect_after_production",
            "16_import_agents_market_agent",
            "17_market_agent_construct_session_runner",
            "18_gc_collect_after_market",
            "19_import_agents_synthesis_agent",
            "20_construct_build_synthesis_agent",
            "21_synthesis_session_service_and_runner",
            "22_gc_collect_after_synthesis",
        ]
        self.assertEqual(stage_names, expected_stages)
        for stage in body["stages"]:
            self.assertIn("rss_mb", stage)
            self.assertIn("delta_from_previous_mb", stage)
            self.assertIn("delta_from_baseline_mb", stage)

    def test_response_never_leaks_secrets_or_env_vars(self):
        resp = self._run_with_mocks()
        body_text = resp.text.lower()
        for leaked in ("anthropic_api_key", "gm_upstash", "gm_chat_secret", "x-gm-key", "real-secret"):
            self.assertNotIn(leaked, body_text)

    def test_makes_no_anthropic_or_model_call(self):
        with mock.patch.object(
            api._anthropic_client.messages, "create",
            side_effect=AssertionError("memory-profile must not call Anthropic"),
        ), mock.patch.object(
            api._anthropic_client.messages, "count_tokens",
            side_effect=AssertionError("memory-profile must not call Anthropic"),
        ):
            resp = self._run_with_mocks()
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
