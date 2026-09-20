"""
Experiment 1 for the production OOM crash-loop: budget.py's Upstash HTTP
client lifecycle.

Context: UpstashBudgetStore._cmd() used to call the module-level
httpx.post(...) shortcut on every Redis command. httpx's own docs say the
top-level request functions open a new connection (and rebuild the SSL
context) on every call rather than reusing a pooled Client -- and Render
polls /health every few seconds, with each probe issuing two of these
commands (budget.status()'s store().get() and config_warnings()'s
budget.has_room(), which is another store().get()) -- so that churn ran
continuously for the entire life of the process, which stayed near its
536MB ceiling on a ~30-40 minute OOM/restart cycle even with zero
/report or /chat traffic.

This is a narrow A/B change: hold health-check traffic and Redis command
volume exactly constant, and change only the HTTP client lifecycle to one
persistent httpx.Client owned by the store. These tests do not prove the
OOM's root cause by themselves -- that needs a production memory
observation over a full idle cycle -- they prove the code-level change is
exactly what it claims to be: one Client for the store's whole lifetime,
identical wire behavior (headers, timeout, command JSON shape, response
parsing) to the pre-change code.
"""
import concurrent.futures
import unittest
from unittest import mock

import budget


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.Client: records construction args and every
    .post() call, without touching the network."""

    instances = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.post_calls = []
        self.closed = False
        _FakeClient.instances.append(self)

    def post(self, url, json=None):
        self.post_calls.append({"url": url, "json": json})
        return _FakeResponse({"result": "OK"})

    def close(self):
        self.closed = True


class UpstashClientReuseTest(unittest.TestCase):
    def setUp(self):
        _FakeClient.instances = []
        self.patcher = mock.patch.object(budget.httpx, "Client", _FakeClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_one_client_created_for_store_lifetime(self):
        store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")
        for _ in range(10):
            store._cmd("PING")
        self.assertEqual(
            len(_FakeClient.instances), 1,
            "UpstashBudgetStore must create exactly one httpx.Client for its "
            "whole lifetime, not one per command",
        )

    def test_cmd_reuses_the_same_client_instance(self):
        store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")
        first_client = store._client
        store._cmd("GET", "some:key")
        store._cmd("GET", "some:other:key")
        self.assertIs(store._client, first_client)
        self.assertEqual(len(first_client.post_calls), 2)

    def test_top_level_httpx_post_is_never_used(self):
        with mock.patch.object(
            budget.httpx, "post",
            side_effect=AssertionError("must not call top-level httpx.post"),
        ):
            store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")
            store._cmd("PING")
            store._cmd("GET", "some:key")

    def test_wire_shape_unchanged(self):
        store = budget.UpstashBudgetStore("https://example.upstash.io", "secret-token")
        client = store._client
        # Headers and timeout are now bound at Client construction instead of
        # per-call kwargs, applying to every request made through it -- same
        # effective behavior on the wire as before.
        self.assertEqual(client.init_kwargs["headers"], {"Authorization": "Bearer secret-token"})
        self.assertEqual(client.init_kwargs["timeout"], 4.0)

        result = store._cmd("EVAL", "return 1", 1, "some:key", 100, 500)
        call = client.post_calls[0]
        self.assertEqual(call["url"], "https://example.upstash.io")
        self.assertEqual(call["json"], ["EVAL", "return 1", "1", "some:key", "100", "500"])
        self.assertEqual(result, "OK")

    def test_error_body_still_raises_budget_unavailable(self):
        class _ErrorClient(_FakeClient):
            def post(self, url, json=None):
                self.post_calls.append({"url": url, "json": json})
                return _FakeResponse({"error": "WRONGTYPE"})

        with mock.patch.object(budget.httpx, "Client", _ErrorClient):
            store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")
            with self.assertRaises(budget.BudgetUnavailable):
                store._cmd("GET", "some:key")

    def test_network_failure_still_raises_budget_unavailable(self):
        class _BrokenClient(_FakeClient):
            def post(self, url, json=None):
                raise ConnectionError("boom")

        with mock.patch.object(budget.httpx, "Client", _BrokenClient):
            store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")
            with self.assertRaises(budget.BudgetUnavailable):
                store._cmd("GET", "some:key")

    def test_concurrent_calls_through_shared_store_succeed(self):
        store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")

        def do_get(i):
            return store._cmd("GET", f"key:{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(do_get, range(50)))

        self.assertEqual(results, ["OK"] * 50)
        self.assertEqual(len(_FakeClient.instances), 1)
        self.assertEqual(len(store._client.post_calls), 50)

    def test_close_delegates_to_client(self):
        store = budget.UpstashBudgetStore("https://example.upstash.io", "tok")
        store.close()
        self.assertTrue(store._client.closed)


if __name__ == "__main__":
    unittest.main()
