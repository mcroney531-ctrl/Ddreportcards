"""
Contract test: /chat and /report/* are the endpoints that spend real money
(Anthropic calls, agent pipelines) or run the tool-calling loop against
arbitrary caller input. Both must reject a missing or wrong X-GM-Key before
doing any real work -- chat_guard.enforce()/enforce_report() both call
check_secret() first, "in the order that fails cheapest first" (its own
docstring), so this never reaches origin/rate-limit/budget checks, real
agents, or the network for a bad key.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api

REAL_SECRET = "test-real-secret"


class ChatRejectsMissingOrBadSecretTest(unittest.TestCase):
    def test_missing_key_is_401(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET):
            client = TestClient(api.app)
            resp = client.post(
                "/chat", json={"model": "x", "max_tokens": 10, "messages": []}
            )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_is_401(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET):
            client = TestClient(api.app)
            resp = client.post(
                "/chat",
                json={"model": "x", "max_tokens": 10, "messages": []},
                headers={"X-GM-Key": "wrong"},
            )
        self.assertEqual(resp.status_code, 401)


class ReportPlayerRejectsMissingOrBadSecretTest(unittest.TestCase):
    def test_missing_key_is_401(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET):
            client = TestClient(api.app)
            resp = client.post("/report/player/12501")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_is_401(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET):
            client = TestClient(api.app)
            resp = client.post("/report/player/12501", headers={"X-GM-Key": "wrong"})
        self.assertEqual(resp.status_code, 401)


class ReportRosterRejectsMissingOrBadSecretTest(unittest.TestCase):
    def test_missing_key_is_401(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET):
            client = TestClient(api.app)
            resp = client.post("/report/roster/someowner")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_is_401(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET):
            client = TestClient(api.app)
            resp = client.post("/report/roster/someowner", headers={"X-GM-Key": "wrong"})
        self.assertEqual(resp.status_code, 401)


class NoRealWorkHappensBeforeTheSecretCheckTest(unittest.TestCase):
    """check_secret() must run before any agent, budget, or network call --
    otherwise a bad key could still trigger billable work."""

    def test_report_player_never_imports_synthesis_agent_with_a_bad_key(self):
        with mock.patch.object(api.chat_guard, "SHARED_SECRET", REAL_SECRET), \
             mock.patch(
                 "agents.synthesis_agent.run_synthesis_agent",
                 side_effect=AssertionError("must not be called with a bad key"),
             ):
            client = TestClient(api.app)
            resp = client.post("/report/player/12501", headers={"X-GM-Key": "wrong"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
