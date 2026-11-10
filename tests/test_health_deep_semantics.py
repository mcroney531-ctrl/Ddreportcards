"""
Stage 3F: /health/deep reports spend accounting truthfully.

/health stays the cheap liveness probe Render polls: always 200, never a
budget-store call, so an Upstash outage can't get a healthy process
restarted. /health/deep is the control-plane check. Spend accounting gates
all billable work (the report/chat guards fail closed without it), so a deep
check that can't reach or build the store is not "ok":

  store configured and reachable  -> 200, status "ok"
  store unreachable               -> 503, status "degraded"
  store misconfigured             -> 503, status "degraded"

The diagnostic body (chat_budget + warnings) is returned either way.
Before Stage 3F, an unreachable store returned 200 with status "ok"; that's
what hid the deleted Upstash database until a real report was refused.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import budget
import chat_guard

HEALTHY = {"backend": "redis", "configured": True, "day": "2026-11-10", "limit_micro": 5_000_000,
           "limit_usd": 5.0, "durable": True, "reachable": True, "spent_micro": 0,
           "remaining_micro": 5_000_000}
UNREACHABLE = {"backend": "redis", "configured": True, "day": "2026-11-10", "limit_micro": 5_000_000,
               "limit_usd": 5.0, "durable": True, "reachable": False, "error": "budget_store_unavailable"}
MISCONFIGURED = {"backend": "redis", "configured": False, "day": "2026-11-10", "limit_micro": 5_000_000,
                 "limit_usd": 5.0, "durable": True, "reachable": False, "error": "budget_store_unavailable"}
LOCAL_MEMORY = {"backend": "memory", "configured": True, "day": "2026-11-10", "limit_micro": 5_000_000,
                "limit_usd": 5.0, "durable": False, "reachable": True, "spent_micro": 0,
                "remaining_micro": 5_000_000}
UNREACHABLE_WARNING = ("Budget store is unreachable — /chat and /report are refusing requests (503) "
                       "rather than spending unmetered.")


class DeepHealthStatusTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def _deep(self, budget_status, warnings=()):
        with mock.patch.object(chat_guard, "budget_status", return_value=budget_status), \
             mock.patch.object(chat_guard, "config_warnings", return_value=list(warnings)):
            return self.client.get("/health/deep")

    def test_healthy_store_is_200_ok(self):
        resp = self._deep(HEALTHY)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "chat_budget": HEALTHY, "warnings": []})

    def test_unreachable_store_is_503_degraded_with_diagnostics(self):
        resp = self._deep(UNREACHABLE, [UNREACHABLE_WARNING])
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"status": "degraded", "chat_budget": UNREACHABLE,
                                       "warnings": [UNREACHABLE_WARNING]})

    def test_misconfigured_store_is_503_degraded(self):
        resp = self._deep(MISCONFIGURED, ["Budget store is misconfigured — ..."])
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "degraded")
        self.assertEqual(resp.json()["chat_budget"], MISCONFIGURED)

    def test_missing_reachability_field_is_not_assumed_ok(self):
        self.assertEqual(self._deep({}).status_code, 503)

    def test_local_memory_store_is_reachable_so_200(self):
        # Development backend: not durable (warned about), but it does meter.
        resp = self._deep(LOCAL_MEMORY, ["Budget store is not durable ..."])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_real_code_path_against_an_unreachable_store(self):
        class _Unreachable:
            durable = True

            def get(self, key):
                raise budget.BudgetUnavailable("simulated outage")

        with mock.patch.object(budget, "store", return_value=_Unreachable()):
            resp = self.client.get("/health/deep")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "degraded")
        self.assertIn(UNREACHABLE_WARNING, resp.json()["warnings"])

    def test_real_code_path_against_a_misconfigured_store(self):
        with mock.patch.object(budget, "store", side_effect=budget.BudgetUnavailable("no url")):
            resp = self.client.get("/health/deep")
        self.assertEqual(resp.status_code, 503)


class LivenessUnaffectedTest(unittest.TestCase):
    def test_health_is_200_whatever_the_store_does(self):
        client = TestClient(api.app)
        for status in (HEALTHY, UNREACHABLE, MISCONFIGURED):
            with self.subTest(reachable=status["reachable"]), \
                 mock.patch.object(chat_guard, "budget_status", side_effect=AssertionError("no store call")), \
                 mock.patch.object(chat_guard, "config_warnings", side_effect=AssertionError("no store call")), \
                 mock.patch.object(budget, "store", side_effect=AssertionError("no store call")):
                resp = client.get("/health")
            self.assertEqual((resp.status_code, resp.json()), (200, {"status": "ok"}))


if __name__ == "__main__":
    unittest.main()
