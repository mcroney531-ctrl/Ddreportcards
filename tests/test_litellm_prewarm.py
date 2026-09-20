"""
Prewarm experiment for the 502 stall: pay ADK's lazy litellm import cost at
FastAPI startup instead of inside the first real report/chat request.

Context: google.adk.models.lite_llm._ensure_litellm_imported() is a plain
synchronous function -- a bare `import litellm` plus logger setup -- that
LiteLlm.generate_content_async() calls as its literal first line, before any
await (confirmed by reading the installed ADK source). Measured cold-import
cost in a Python 3.11 sandbox: ~4.2-4.3s, ~177.5MB RSS -- and this happens
directly on the event loop on the first real model call, when nothing else
can run, including /health. Production is Python 3.14.3 on constrained
Render hardware, so the sandbox number isn't trustworthy as-is; this
experiment moves the cost to startup and measures it for real there.

This is a single-variable change: budget callbacks, Redis semantics, health
endpoints, and report/agent behavior are all untouched.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api


class PrewarmInvokesAdkInitializerTest(unittest.TestCase):
    def test_prewarm_calls_ensure_litellm_imported_exactly_once_at_startup(self):
        with mock.patch(
            "google.adk.models.lite_llm._ensure_litellm_imported"
        ) as mock_init:
            with TestClient(api.app):
                pass
        mock_init.assert_called_once_with()

    def test_prewarm_failure_fails_startup(self):
        with mock.patch(
            "google.adk.models.lite_llm._ensure_litellm_imported",
            side_effect=RuntimeError("cannot import litellm"),
        ):
            with self.assertRaises(RuntimeError):
                with TestClient(api.app):
                    pass


class PrewarmDoesNotTouchAnthropicOrBudgetTest(unittest.TestCase):
    def test_prewarm_never_calls_anthropic(self):
        with mock.patch.object(
            api._anthropic_client.messages, "create",
            side_effect=AssertionError("prewarm must not call Anthropic"),
        ), mock.patch.object(
            api._anthropic_client.messages, "count_tokens",
            side_effect=AssertionError("prewarm must not call Anthropic"),
        ):
            with TestClient(api.app):
                pass  # no exception means Anthropic was never touched

    def test_prewarm_never_touches_the_budget_store(self):
        import budget

        with mock.patch.object(
            budget, "store",
            side_effect=AssertionError("prewarm must not touch the budget store"),
        ):
            with TestClient(api.app):
                pass  # no exception means budget.store() was never called


class HealthEndpointsUnchangedByPrewarmTest(unittest.TestCase):
    def test_health_and_health_deep_still_respond_normally_after_startup(self):
        with mock.patch("google.adk.models.lite_llm._ensure_litellm_imported"):
            with TestClient(api.app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json(), {"status": "ok"})

                deep = client.get("/health/deep")
                self.assertEqual(deep.status_code, 200)
                body = deep.json()
                self.assertEqual(body["status"], "ok")
                self.assertIn("chat_budget", body)
                self.assertIn("warnings", body)


class SecondInitializationIsAlreadyCachedTest(unittest.TestCase):
    def test_second_call_to_ensure_litellm_imported_is_a_cheap_noop(self):
        # Exercises the real ADK function (no mocking) to prove its own
        # module-level guard makes a repeat call effectively free -- this is
        # what makes prewarming safe: the real first-model-call path still
        # calls _ensure_litellm_imported() itself, and that second call must
        # not re-pay the import cost.
        from google.adk.models import lite_llm as m

        m._ensure_litellm_imported()  # real first call, pays the cost once
        self.assertTrue(m._LITELLM_IMPORTED)

        import time
        t0 = time.perf_counter()
        m._ensure_litellm_imported()  # second call, should be a fast no-op
        elapsed = time.perf_counter() - t0

        self.assertLess(
            elapsed, 0.05,
            f"second _ensure_litellm_imported() call took {elapsed:.3f}s -- "
            "expected a cheap no-op via the module's own _LITELLM_IMPORTED guard",
        )


if __name__ == "__main__":
    unittest.main()
