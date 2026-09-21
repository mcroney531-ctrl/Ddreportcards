"""
/report/player/{sleeper_id} now logs one INFO line on completion
(player_report_complete) and one on the existing budget-exception failure
path (player_report_failed), each carrying only duration_ms/rss_before_mb/
rss_after_mb/rss_delta_mb. This replaces the removed profiling endpoints as
the long-term, always-on way to notice whether the small warm-worker RSS
residual Experiment 7 found (+1.8MB and +6.2MB on two real back-to-back
reports) ever grows into something that matters, without keeping an
invasive profiler around. Never logs secrets, prompts, model output,
headers, or player payloads.
"""
import io
import logging
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api
import budget


class ReportPlayerCompletionLoggingTest(unittest.TestCase):
    def _capture(self, make_request):
        gm_logger = logging.getLogger("gm")
        handler = gm_logger.handlers[0]
        stream = io.StringIO()
        original_stream = handler.stream
        handler.stream = stream
        try:
            resp = make_request()
        finally:
            handler.stream = original_stream
        return resp, stream.getvalue()

    def test_successful_report_logs_player_report_complete_with_rss_and_duration(self):
        fake_card = {"_detail": {}, "dynasty_grade": "B+"}
        with mock.patch.object(api.chat_guard, "enforce_report", return_value=None), \
             mock.patch(
                 "agents.synthesis_agent.run_synthesis_agent", return_value=fake_card
             ) as mock_run:
            client = TestClient(api.app)
            resp, output = self._capture(
                lambda: client.post("/report/player/12501", headers={"X-GM-Key": "x"})
            )

        self.assertEqual(resp.status_code, 200)
        mock_run.assert_awaited_once_with("12501")
        self.assertIn("player_report_complete", output)
        self.assertIn("duration_ms=", output)
        self.assertIn("rss_before_mb=", output)
        self.assertIn("rss_after_mb=", output)
        self.assertIn("rss_delta_mb=", output)
        self.assertNotIn("player_report_failed", output)

    def test_budget_exhausted_logs_player_report_failed_not_complete(self):
        with mock.patch.object(api.chat_guard, "enforce_report", return_value=None), \
             mock.patch(
                 "agents.synthesis_agent.run_synthesis_agent",
                 side_effect=budget.BudgetExhausted("simulated"),
             ):
            client = TestClient(api.app, raise_server_exceptions=False)
            resp, output = self._capture(
                lambda: client.post("/report/player/12501", headers={"X-GM-Key": "x"})
            )

        self.assertEqual(resp.status_code, 429)
        self.assertIn("player_report_failed", output)
        self.assertIn("duration_ms=", output)
        self.assertNotIn("player_report_complete", output)

    def test_logged_lines_never_contain_card_content_or_the_secret_header(self):
        fake_card = {
            "_detail": {"situation": {"notes": "a very specific scouting note"}},
            "dynasty_grade": "B+",
            "summary": "a very specific player summary",
        }
        with mock.patch.object(api.chat_guard, "enforce_report", return_value=None), \
             mock.patch("agents.synthesis_agent.run_synthesis_agent", return_value=fake_card):
            client = TestClient(api.app)
            resp, output = self._capture(
                lambda: client.post(
                    "/report/player/12501", headers={"X-GM-Key": "super-secret-value"}
                )
            )

        for leaked in (
            "a very specific player summary",
            "a very specific scouting note",
            "super-secret-value",
            "dynasty_grade",
        ):
            self.assertNotIn(leaked, output)


if __name__ == "__main__":
    unittest.main()
