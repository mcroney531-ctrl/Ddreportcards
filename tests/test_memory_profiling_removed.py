"""
Memory investigation closed (Experiments 4-7): the attribution question is
answered (see commit history) and the temporary profiling surface -- which
monkeypatched agents' before/after_model callbacks and LiteLlm.generate_content_async
for the duration of a request -- has been removed rather than left "just in
case." This proves the removal actually happened and stays that way.
"""
import unittest

from fastapi.testclient import TestClient

import api

REMOVED_PATHS = {
    "/internal/memory-profile",
    "/internal/memory-profile-agent",
    "/internal/memory-profile-agent/late",
    "/internal/memory-profile-runner-setup",
}

REMOVED_ATTRIBUTES = (
    "MEMORY_PROFILE_ENABLED",
    "memory_profile",
    "memory_profile_agent",
    "memory_profile_agent_late",
    "memory_profile_runner_setup",
    "_late_memory_checkpoints",
    "_record_late_checkpoints",
    "_response_has_function_call",
    "wrap_tool_with_checkpoints",
    "_Experiment6Stop",
)


class ProfilingRoutesAreGoneTest(unittest.TestCase):
    def test_no_route_registered_at_any_removed_path(self):
        registered_paths = {getattr(route, "path", None) for route in api.app.routes}
        overlap = REMOVED_PATHS & registered_paths
        self.assertEqual(overlap, set(), f"stale profiling route(s) still registered: {overlap}")

    def test_removed_paths_are_actually_unreachable(self):
        client = TestClient(api.app)
        for path in REMOVED_PATHS:
            resp = client.post(path, headers={"X-GM-Key": "anything"})
            self.assertEqual(resp.status_code, 404, f"{path} should be gone, got {resp.status_code}")


class ProfilingInternalsAreGoneTest(unittest.TestCase):
    def test_no_leftover_module_attributes(self):
        for name in REMOVED_ATTRIBUTES:
            self.assertFalse(hasattr(api, name), f"api.{name} should have been removed")


class RssHelperSurvivesForPrewarmInstrumentationTest(unittest.TestCase):
    """_current_rss_mb() predates the profiling endpoints and is still used by
    the LiteLLM startup-prewarm timing log -- it must not be removed with them."""

    def test_current_rss_mb_still_exists_and_callable(self):
        self.assertTrue(hasattr(api, "_current_rss_mb"))
        self.assertTrue(callable(api._current_rss_mb))


if __name__ == "__main__":
    unittest.main()
