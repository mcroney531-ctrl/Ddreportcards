"""
Contract test for api._budget_http_error() and its wiring into
POST /report/player/{sleeper_id}: a budget refusal raised mid-pipeline (after
the endpoint's own preflight passed) must surface as the status code that
matches what actually happened, not a generic 500 -- a working spend control
should never look like a broken service.

  BudgetExhausted            -> 429 (the caller can retry tomorrow)
  UnpricedModel               -> 500 (server misconfiguration, not the
                                  caller's fault -- an agent is wired to a
                                  model the spend system cannot cost)
  BudgetUnavailable/
  BudgetMisconfigured         -> 503 (the store itself is unreachable/broken)
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import budget

REAL_SECRET = "test-real-secret"


class BudgetHttpErrorMappingUnitTest(unittest.TestCase):
    def test_budget_exhausted_maps_to_429(self):
        err = api._budget_http_error(budget.BudgetExhausted("daily cap reached"))
        self.assertEqual(err.status_code, 429)

    def test_unpriced_model_maps_to_500(self):
        err = api._budget_http_error(budget.UnpricedModel("no price for model x"))
        self.assertEqual(err.status_code, 500)

    def test_budget_unavailable_maps_to_503(self):
        err = api._budget_http_error(budget.BudgetUnavailable("store unreachable"))
        self.assertEqual(err.status_code, 503)

    def test_budget_misconfigured_maps_to_503(self):
        # BudgetMisconfigured is a BudgetUnavailable subclass and must fall
        # into the same bucket -- both mean "cannot authorise", not "your
        # request was rejected."
        err = api._budget_http_error(budget.BudgetMisconfigured("bad backend config"))
        self.assertEqual(err.status_code, 503)


class ReportPlayerSurfacesTheMappedStatusEndToEndTest(unittest.TestCase):
    def _post_with(self, exc):
        with mock.patch.object(api.chat_guard, "enforce_report", return_value=None), \
             mock.patch("agents.synthesis_agent.run_synthesis_agent", side_effect=exc):
            client = TestClient(api.app, raise_server_exceptions=False)
            return client.post("/report/player/12501", headers={"X-GM-Key": "x"})

    def test_exhausted_is_429(self):
        resp = self._post_with(budget.BudgetExhausted("daily cap reached"))
        self.assertEqual(resp.status_code, 429)

    def test_unpriced_model_is_500(self):
        resp = self._post_with(budget.UnpricedModel("no price for model x"))
        self.assertEqual(resp.status_code, 500)

    def test_unavailable_is_503(self):
        resp = self._post_with(budget.BudgetUnavailable("store unreachable"))
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
