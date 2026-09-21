"""
The gm.* logger family (gm.api, gm.budget, gm.agents.usage, gm.chat_guard)
has always called plain logging.getLogger("gm.x"), but nothing ever attached
a handler to it or to the root logger. Python's root logger defaults to
WARNING with no handler, so every existing log.info(...) call in this
codebase -- including the LiteLLM prewarm timing line -- was being silently
dropped in production; only ERROR-level calls reached stderr via Python's
lastResort handler.

api._configure_gm_logging() fixes this by attaching one handler to the "gm"
parent logger at import time. These tests verify it actually reaches child
loggers through the standard hierarchy, is idempotent (importing api.py
more than once in a process must not stack up duplicate handlers, which
would print every line multiple times), and doesn't touch anything outside
the "gm" namespace.
"""
import io
import logging
import unittest

import api
import budget
import agents.usage as usage_mod


class GmLoggingConfigurationTest(unittest.TestCase):
    def test_gm_logger_has_exactly_one_handler(self):
        gm_logger = logging.getLogger("gm")
        self.assertEqual(len(gm_logger.handlers), 1)

    def test_gm_logger_level_is_info(self):
        gm_logger = logging.getLogger("gm")
        self.assertEqual(gm_logger.level, logging.INFO)

    def test_gm_logger_does_not_propagate_to_root(self):
        gm_logger = logging.getLogger("gm")
        self.assertFalse(gm_logger.propagate)

    def test_reconfiguring_does_not_add_a_second_handler(self):
        gm_logger = logging.getLogger("gm")
        before = len(gm_logger.handlers)
        api._configure_gm_logging()
        api._configure_gm_logging()
        self.assertEqual(len(gm_logger.handlers), before)

    def test_uvicorn_loggers_are_untouched(self):
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            uvicorn_logger = logging.getLogger(name)
            self.assertNotIn(logging.getLogger("gm").handlers[0], uvicorn_logger.handlers)
            self.assertTrue(uvicorn_logger.propagate)


class ChildLoggersReachTheSharedHandlerTest(unittest.TestCase):
    """The whole point: gm.api/gm.budget/gm.agents.usage never attach their
    own handlers, so this only works if they inherit "gm"'s through the
    logging hierarchy at call time."""

    def _capture(self, emit) -> str:
        gm_logger = logging.getLogger("gm")
        handler = gm_logger.handlers[0]
        stream = io.StringIO()
        original_stream = handler.stream
        handler.stream = stream
        try:
            emit()
        finally:
            handler.stream = original_stream
        return stream.getvalue()

    def test_gm_api_info_reaches_the_handler(self):
        output = self._capture(lambda: api.log.info("gm_api_logging_smoke_marker"))
        self.assertIn("gm_api_logging_smoke_marker", output)
        self.assertIn("INFO", output)
        self.assertIn("gm.api", output)

    def test_gm_budget_info_reaches_the_same_handler(self):
        output = self._capture(lambda: budget.log.info("gm_budget_logging_smoke_marker"))
        self.assertIn("gm_budget_logging_smoke_marker", output)
        self.assertIn("gm.budget", output)

    def test_gm_agents_usage_info_reaches_the_same_handler(self):
        output = self._capture(lambda: usage_mod.log.info("gm_usage_logging_smoke_marker"))
        self.assertIn("gm_usage_logging_smoke_marker", output)
        self.assertIn("gm.agents.usage", output)


if __name__ == "__main__":
    unittest.main()
