"""
Regression test for the production 502: agents/synthesis_agent.py's ADK tool
functions (evaluate_situation/evaluate_production/evaluate_market) must not
block the asyncio event loop while their sub-agent runs.

Root cause: each tool used to be a plain `def` calling `_run_in_thread(coro)`
synchronously. ADK's FunctionTool._invoke_callable() only awaits a tool when
inspect.iscoroutinefunction(target) is true; a plain `def` tool is instead
called directly on the caller's event loop with no automatic thread-pooling.
`_run_in_thread` blocks on `concurrent.futures.Future.result()` for the full
duration of the nested sub-agent run (its Anthropic call plus the synchronous
Sleeper/FantasyCalc/ESPN httpx calls dynasty_core makes) -- so that wait froze
the whole event loop, including FastAPI's /health endpoint, for tens of
seconds. On Render that exceeded the health-check window and the process was
restarted mid-request (the 502 this test guards against).

Fix: the tool functions are now `async def`, and offload the blocking wait
itself with `await asyncio.to_thread(_run_in_thread, coro)`, so ADK awaits
them cooperatively instead of calling them directly. dynasty_core.sleeper and
_run_in_thread's own thread+fresh-event-loop isolation are unchanged.
"""
import asyncio
import time
import unittest
from unittest import mock

from agents import synthesis_agent

SUB_AGENT_DELAY_SECONDS = 0.5
HEARTBEAT_INTERVAL_SECONDS = 0.05


async def _slow_stub_situation_agent(player_id: str) -> dict:
    # Simulates the real sub-agent's blocking work. _run_in_thread runs this
    # coroutine via asyncio.run() on its own dedicated thread, so a plain
    # blocking time.sleep here never touches the caller's event loop --
    # exactly like the real Anthropic/httpx calls it stands in for.
    time.sleep(SUB_AGENT_DELAY_SECONDS)
    return {"opportunity_score": 80, "opportunity_grade": "B"}


class ToolFunctionsAreAsyncTest(unittest.TestCase):
    def test_evaluate_functions_are_coroutine_functions(self):
        # ADK only awaits a tool directly (the is_async branch of
        # FunctionTool._invoke_callable) when this is true. A plain `def`
        # tool is instead called synchronously on the event loop -- the
        # exact defect this file guards against being reintroduced.
        evaluate_situation, evaluate_production, evaluate_market = synthesis_agent._make_tools({})
        for fn in (evaluate_situation, evaluate_production, evaluate_market):
            self.assertTrue(
                asyncio.iscoroutinefunction(fn),
                f"{fn.__name__} must be `async def` so ADK awaits it instead of "
                "calling it directly on the event loop",
            )


class EvaluateSituationDoesNotBlockEventLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_keeps_ticking_during_sub_agent_call(self):
        sub_results: dict = {}
        evaluate_situation, _evaluate_production, _evaluate_market = synthesis_agent._make_tools(sub_results)

        heartbeat_ticks = 0
        stop = asyncio.Event()

        async def heartbeat():
            nonlocal heartbeat_ticks
            while not stop.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                heartbeat_ticks += 1

        with mock.patch.object(synthesis_agent, "run_situation_agent", _slow_stub_situation_agent):
            heartbeat_task = asyncio.create_task(heartbeat())
            result = await evaluate_situation("12501")
            stop.set()
            await heartbeat_task

        self.assertEqual(result, {"opportunity_score": 80, "opportunity_grade": "B"})
        self.assertIs(sub_results["situation"], result)

        # If the event loop were blocked for the ~0.5s sub-agent call (the
        # pre-fix behavior, calling _run_in_thread synchronously from a
        # plain `def` tool), the heartbeat would get zero or one tick.
        # Non-blocking execution ticks roughly delay/interval times; require
        # at least half that as a generous margin against test-runner jitter.
        expected_min_ticks = int(SUB_AGENT_DELAY_SECONDS / HEARTBEAT_INTERVAL_SECONDS * 0.5)
        self.assertGreaterEqual(
            heartbeat_ticks,
            expected_min_ticks,
            f"event loop starved during evaluate_situation: only {heartbeat_ticks} "
            f"heartbeat ticks in ~{SUB_AGENT_DELAY_SECONDS}s (expected >= {expected_min_ticks}); "
            "the sub-agent call is blocking the event loop again",
        )


if __name__ == "__main__":
    unittest.main()
