"""
Stage 3F: keep the Upstash spend store from being deleted for inactivity.

Upstash deletes inactive databases (free: after 14 days; paid: after a much
longer window). /health deliberately never touches the store, and the
Netlify keepalive only polls /health. So a quiet stretch with no chat or
report traffic meant zero Redis requests, which is how the original database
was deleted while the service itself stayed up.

The fix uses the process that's already running: one read-only GET of a
dedicated keepalive key at startup and then every 30 days. It never reserves
or settles, so spend totals can't change. Failures are logged by exception
class only (an HTTP error message can carry the Upstash endpoint) and never
raised, because the keepalive must never take the service down.
"""
import asyncio
import logging
import unittest
from unittest import mock

import api
import budget


class _RecordingStore:
    durable = True

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def get(self, key):
        self.calls.append(("get", key))
        if self.fail:
            raise self.fail
        return 0

    def reserve(self, *a, **k):
        raise AssertionError("keepalive must not reserve")

    def settle(self, *a, **k):
        raise AssertionError("keepalive must not settle")


class TouchStoreTest(unittest.TestCase):
    def test_one_read_only_get_of_the_keepalive_key(self):
        store = _RecordingStore()
        with mock.patch.object(budget, "store", return_value=store):
            self.assertTrue(budget.touch_store())
        self.assertEqual(store.calls, [("get", budget.KEEPALIVE_KEY)])

    def test_keepalive_key_is_not_a_spend_key(self):
        self.assertTrue(budget.KEEPALIVE_KEY.startswith(budget.KEY_PREFIX))
        self.assertNotEqual(budget.KEEPALIVE_KEY, budget.day_key())

    def test_failure_is_returned_logged_by_class_and_not_raised(self):
        secret_bearing = budget.BudgetUnavailable("ConnectError: https://secret-host.upstash.io token=abc")
        store = _RecordingStore(fail=secret_bearing)
        with mock.patch.object(budget, "store", return_value=store), \
             self.assertLogs("gm.budget", level=logging.WARNING) as logs:
            self.assertFalse(budget.touch_store())
        joined = " ".join(logs.output)
        self.assertIn("BudgetUnavailable", joined)
        self.assertNotIn("upstash.io", joined)
        self.assertNotIn("token", joined)

    def test_unbuildable_store_is_handled(self):
        with mock.patch.object(budget, "store", side_effect=budget.BudgetUnavailable("no url https://x")), \
             self.assertLogs("gm.budget", level=logging.WARNING) as logs:
            self.assertFalse(budget.touch_store())
        self.assertNotIn("https://", " ".join(logs.output))

    def test_memory_store_is_a_no_op(self):
        store = _RecordingStore()
        store.durable = False
        with mock.patch.object(budget, "store", return_value=store):
            self.assertTrue(budget.touch_store())
        self.assertEqual(store.calls, [])


class KeepaliveLoopTest(unittest.TestCase):
    def test_interval_is_thirty_days(self):
        self.assertEqual(api.BUDGET_KEEPALIVE_INTERVAL_SECONDS, 30 * 24 * 3600)

    def test_loop_touches_immediately_then_sleeps_the_interval(self):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            raise asyncio.CancelledError

        with mock.patch.object(budget, "touch_store", return_value=True) as touch, \
             mock.patch.object(api.asyncio, "sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(api._budget_keepalive_loop())
        touch.assert_called_once_with()
        self.assertEqual(sleeps, [api.BUDGET_KEEPALIVE_INTERVAL_SECONDS])

    def test_lifespan_starts_the_keepalive_and_cancels_it_on_shutdown(self):
        from fastapi.testclient import TestClient

        async def no_prewarm():
            return None

        with mock.patch.object(api, "_prewarm_litellm", no_prewarm), \
             mock.patch.object(budget, "touch_store", return_value=True) as touch:
            with TestClient(api.app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
        touch.assert_called()


if __name__ == "__main__":
    unittest.main()
