"""
Regression test for agents/usage.py's ADK before/after_model callbacks
blocking the event loop: check_budget_before_model and record_model_usage
made a synchronous budget.reserve()/budget.settle() HTTP call directly from
an ordinary `def` callback, so awaiting Runner.run_async() blocked the whole
event loop -- including /health -- for the length of that Upstash round-trip,
every single model round. Same defect class as the original 502 (a `def`
tool blocking the loop via a synchronous wait), just on the budget-
accounting path instead of the tool path.

Fix: both callbacks are now `async def`, and the actual reserve()/settle()
call is offloaded with `await asyncio.to_thread(...)`, so ADK awaits them
cooperatively -- confirmed both by google.adk.utils._callback_pipeline
._run_callbacks (awaits a callback's result when inspect.isawaitable(result)
is true) and by the test below, which runs a real ADK Runner with
intentionally slow, blocking stand-ins for reserve/settle and proves a
concurrent heartbeat keeps ticking throughout.
"""
import asyncio
import time
import unittest
from unittest import mock

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

import agents.usage as usage_mod
import budget

SLOW_IO_SECONDS = 0.3
HEARTBEAT_INTERVAL_SECONDS = 0.05


def _slow_fake_reserve(model, input_tokens, max_tokens, now=None):
    time.sleep(SLOW_IO_SECONDS)  # stands in for the real synchronous Upstash HTTP call
    return budget.Reservation(key="test-day-key", model=model, micros=1000)


def _slow_fake_settle(reservation, input_tokens, output_tokens):
    time.sleep(SLOW_IO_SECONDS)  # stands in for the real synchronous Upstash HTTP call
    return 500


async def _fake_generate_content_async(self, llm_request, stream: bool = False):
    yield LlmResponse(
        content=genai_types.Content(role="model", parts=[genai_types.Part(text="ok")]),
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=5
        ),
    )


class BudgetCallbacksAreAsyncTest(unittest.TestCase):
    def test_callbacks_are_coroutine_functions(self):
        # google.adk.utils._callback_pipeline._run_callbacks only awaits a
        # callback's result when it's awaitable -- an ordinary `def`
        # returning a plain value is used directly, on the caller's own
        # event loop, with no automatic thread-pooling.
        self.assertTrue(asyncio.iscoroutinefunction(usage_mod.check_budget_before_model))
        self.assertTrue(asyncio.iscoroutinefunction(usage_mod.record_model_usage))

    def test_on_model_error_callback_is_still_sync(self):
        # Unchanged by design: it does no network I/O, so there is nothing
        # to offload.
        self.assertFalse(asyncio.iscoroutinefunction(usage_mod.clear_invocation_reservation))


class BudgetCallbacksDoNotBlockTheEventLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_keeps_ticking_through_a_real_runner_round(self):
        agent = LlmAgent(
            model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key="test-key"),
            before_model_callback=usage_mod.check_budget_before_model,
            after_model_callback=usage_mod.record_model_usage,
            name="usage_callback_test_agent",
            instruction="Say hi.",
        )
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="usage_callback_test", session_service=session_service)
        session_id = "usage_callback_test_session"
        await session_service.create_session(
            app_name="usage_callback_test", user_id="user", session_id=session_id
        )
        message = genai_types.Content(role="user", parts=[genai_types.Part(text="hi")])

        heartbeat_ticks = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal heartbeat_ticks
            while not stop.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                heartbeat_ticks += 1

        with mock.patch.object(budget, "reserve", side_effect=_slow_fake_reserve), \
             mock.patch.object(budget, "settle", side_effect=_slow_fake_settle), \
             mock.patch.object(LiteLlm, "generate_content_async", _fake_generate_content_async):
            heartbeat_task = asyncio.create_task(heartbeat())
            events = [
                event
                async for event in runner.run_async(
                    user_id="user", session_id=session_id, new_message=message
                )
            ]
            stop.set()
            await heartbeat_task

        self.assertTrue(events)

        # If either callback blocked the event loop for its full 0.3s
        # synchronous sleep (the pre-fix behavior), the heartbeat would get
        # far fewer ticks than the ~2 * SLOW_IO_SECONDS / HEARTBEAT_INTERVAL
        # expected from two such calls (one reserve, one settle) running
        # cooperatively. Require at least half that as a generous margin
        # against test-runner jitter.
        expected_min_ticks = int(2 * SLOW_IO_SECONDS / HEARTBEAT_INTERVAL_SECONDS * 0.5)
        self.assertGreaterEqual(
            heartbeat_ticks,
            expected_min_ticks,
            f"event loop starved during the budget callbacks: only {heartbeat_ticks} "
            f"heartbeat ticks across ~{2 * SLOW_IO_SECONDS}s of reserve+settle "
            f"(expected >= {expected_min_ticks}); a callback is blocking the loop again",
        )


if __name__ == "__main__":
    unittest.main()
