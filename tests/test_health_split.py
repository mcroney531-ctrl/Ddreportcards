"""
Experiment 2 for the idle OOM residual leak: remove Upstash I/O from the
liveness probe Render polls every few seconds.

Context: /health used to call chat_guard.budget_status() (an Upstash GET
via budget.status()) and chat_guard.config_warnings() (another Upstash GET,
via budget.has_room()) on every hit. Render polls /health continuously, so
that was two real Redis round-trips per probe, indefinitely -- on top of
the httpx-client-lifecycle fix (Experiment 1), this is the next suspect for
the residual ~0.23 MB/min growth Experiment 1 didn't eliminate.

Both actual pollers of /health -- GM Command's warmReportCards() in
index.html and the Netlify keepalive.js scheduled function -- only check
for a response/status code and never read the body (confirmed by reading
both), so the response shape was free to change.

/health is now a bare process-liveness check with zero budget-store
commands. The previous rich payload (spend status + config warnings) moved
to /health/deep, fetched manually or by a deploy check, never polled.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import budget
import chat_guard


class HealthIsShallowTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_health_returns_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_health_makes_zero_budget_store_calls_even_when_hit_repeatedly(self):
        with mock.patch.object(
            chat_guard, "budget_status",
            side_effect=AssertionError("/health must not call budget_status()"),
        ), mock.patch.object(
            chat_guard, "config_warnings",
            side_effect=AssertionError("/health must not call config_warnings()"),
        ):
            for _ in range(20):
                resp = self.client.get("/health")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"status": "ok"})


class HealthDeepPreservesRichPayloadTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_health_deep_returns_previous_rich_payload(self):
        fake_budget = {
            "backend": "redis", "configured": True, "day": "2026-09-20",
            "limit_micro": 5_000_000, "limit_usd": 5.0, "durable": True,
            "reachable": True, "spent_micro": 12345, "remaining_micro": 4987655,
        }
        with mock.patch.object(chat_guard, "budget_status", return_value=fake_budget), \
             mock.patch.object(chat_guard, "config_warnings", return_value=[]):
            resp = self.client.get("/health/deep")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "status": "ok",
            "chat_budget": fake_budget,
            "warnings": [],
        })

    def test_health_deep_surfaces_a_config_warning(self):
        with mock.patch.object(chat_guard, "budget_status", return_value={}), \
             mock.patch.object(
                 chat_guard, "config_warnings",
                 return_value=["GM_CHAT_SECRET is not set — /chat accepts unauthenticated requests."],
             ):
            resp = self.client.get("/health/deep")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["warnings"],
            ["GM_CHAT_SECRET is not set — /chat accepts unauthenticated requests."],
        )

    def test_health_deep_still_detects_store_unreachable_via_real_code_path(self):
        # Exercises the real chat_guard.budget_status/config_warnings code
        # (not mocked) against a store forced unreachable, proving
        # /health/deep still does real reachability detection.
        class _UnreachableStore:
            durable = True

            def get(self, key):
                raise budget.BudgetUnavailable("simulated outage")

        with mock.patch.object(budget, "store", return_value=_UnreachableStore()):
            resp = self.client.get("/health/deep")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["chat_budget"]["reachable"])
        self.assertIn(
            "Budget store is unreachable — /chat and /report are "
            "refusing requests (503) rather than spending unmetered.",
            body["warnings"],
        )


if __name__ == "__main__":
    unittest.main()
