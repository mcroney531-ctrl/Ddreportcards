"""
Experiment 5: one real situation_agent execution, instrumented to separate
Python-object retention (tracemalloc) from native/allocator/library
retention (RSS) around the first real model call after Experiment 4 ruled
out static caches/construction as the dominant cost.

This endpoint makes a real Anthropic call when actually exercised (that's
the point -- Experiment 4 covered everything with zero model calls, this
one is deliberately the opposite), so these tests cover what's safe and
meaningful to verify without spending money or needing network access:
the same flag+secret gating Experiment 4 already has, the pure
tool-call-detection helper, and that the callback/tool instrumentation
is restored even when the run fails partway through -- never left
patched in over a real request path.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import agents.usage as usage_mod
from google.adk.models.lite_llm import LiteLlm


class MemoryProfileAgentGatingTest(unittest.TestCase):
    def test_post_returns_404_when_flag_is_unset(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", False):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile-agent",
                headers={"X-GM-Key": "anything"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_late_returns_404_when_flag_is_unset(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", False):
            client = TestClient(api.app)
            resp = client.get(
                "/internal/memory-profile-agent/late",
                headers={"X-GM-Key": "anything"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_post_returns_401_with_wrong_secret_when_enabled(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"):
            client = TestClient(api.app)
            resp = client.post(
                "/internal/memory-profile-agent",
                headers={"X-GM-Key": "wrong"},
            )
        self.assertEqual(resp.status_code, 401)

    def test_late_returns_401_with_wrong_secret_when_enabled(self):
        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"):
            client = TestClient(api.app)
            resp = client.get(
                "/internal/memory-profile-agent/late",
                headers={"X-GM-Key": "wrong"},
            )
        self.assertEqual(resp.status_code, 401)


class WrapToolWithCheckpointsPreservesSignatureTest(unittest.TestCase):
    """Regression test for the actual production failure on the first
    Experiment 5 run: a plain (*args, **kwargs) wrapper around a tool
    function erases the signature ADK's FunctionTool introspects to build
    the schema sent to the model, so the model calls the tool with the
    wrong (or no) arguments and the real function raises a TypeError.
    Confirmed via a real Render 500 with "Dynamic node situation_agent
    failed" before this fix."""

    def test_wrapped_tool_keeps_the_original_introspectable_signature(self):
        import inspect

        def lookup_player_situation(player_id: str) -> dict:
            """Docstring the model needs to see too."""
            return {"player_id": player_id}

        checkpoints_seen = []
        wrapped = api.wrap_tool_with_checkpoints(
            "lookup_player_situation", lookup_player_situation, checkpoints_seen.append
        )

        self.assertEqual(
            inspect.signature(wrapped), inspect.signature(lookup_player_situation)
        )
        self.assertEqual(wrapped.__doc__, lookup_player_situation.__doc__)
        self.assertEqual(wrapped.__name__, "lookup_player_situation")

    def test_wrapped_tool_still_calls_through_and_records_checkpoints(self):
        def get_position_depth(team: str, position: str) -> dict:
            return {"team": team, "position": position}

        checkpoints_seen = []
        wrapped = api.wrap_tool_with_checkpoints(
            "get_position_depth", get_position_depth, checkpoints_seen.append
        )

        result = wrapped(team="TEN", position="RB")

        self.assertEqual(result, {"team": "TEN", "position": "RB"})
        self.assertEqual(
            checkpoints_seen,
            ["tool_get_position_depth_before_call", "tool_get_position_depth_after_call"],
        )


class ResponseHasFunctionCallTest(unittest.TestCase):
    def test_true_when_a_part_has_a_function_call(self):
        part = mock.Mock(function_call=object())
        response = mock.Mock(content=mock.Mock(parts=[part]))
        self.assertTrue(api._response_has_function_call(response))

    def test_false_when_no_part_has_a_function_call(self):
        part = mock.Mock(function_call=None)
        response = mock.Mock(content=mock.Mock(parts=[part]))
        self.assertFalse(api._response_has_function_call(response))

    def test_false_when_content_is_missing(self):
        response = mock.Mock(content=None)
        self.assertFalse(api._response_has_function_call(response))


class InstrumentationRestoredOnFailureTest(unittest.TestCase):
    def test_callbacks_and_tools_restored_after_a_failed_run(self):
        original_check = usage_mod.check_budget_before_model
        original_record = usage_mod.record_model_usage
        original_generate = LiteLlm.generate_content_async

        import agents.situation_agent as situation_mod
        original_tools = {
            name: getattr(situation_mod, name)
            for name in (
                "lookup_player_situation",
                "get_position_depth",
                "assess_position_competition",
                "get_trending_sentiment",
            )
        }

        with mock.patch.object(api, "MEMORY_PROFILE_ENABLED", True), \
             mock.patch.object(api.chat_guard, "SHARED_SECRET", "real-secret"), \
             mock.patch(
                 "dynasty_core.sleeper.get_all_players", return_value={"1": {}}
             ), \
             mock.patch(
                 "dynasty_core.fantasycalc.get_dynasty_values", return_value=[]
             ), \
             mock.patch.object(
                 situation_mod, "build_situation_agent",
                 side_effect=RuntimeError("simulated failure before any model call"),
             ):
            client = TestClient(api.app, raise_server_exceptions=False)
            resp = client.post(
                "/internal/memory-profile-agent",
                headers={"X-GM-Key": "real-secret"},
            )

        self.assertEqual(resp.status_code, 500)
        self.assertIs(usage_mod.check_budget_before_model, original_check)
        self.assertIs(usage_mod.record_model_usage, original_record)
        self.assertIs(LiteLlm.generate_content_async, original_generate)
        for name, fn in original_tools.items():
            self.assertIs(getattr(situation_mod, name), fn)


if __name__ == "__main__":
    unittest.main()
