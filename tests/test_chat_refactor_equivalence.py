"""
The /chat refactor changes structure only, not behaviour.

The backend prompt and its assembly moved to chat_prompt.py
(build_chat_system) and the tool loop to api.run_chat_loop, so the offline
evaluation harness can run exactly what production runs. Before moving
anything, tests/_chat_golden.py recorded the ORIGINAL inline implementation
(d154a90) into tests/fixtures/chat_loop_golden.json: every count_tokens and
create call (model, max_tokens, full system text, full messages, a hash of the
tool definitions), every tool execution, and the HTTP response, across five
scenarios. The refactored code, run with the baseline prompt text
(evals/baseline_chat_prompt.py, a verbatim copy), must reproduce that
recording exactly; Phase A then changes only the wording.
"""
import datetime
import inspect
import json
import pathlib
import unittest

import api
import chat_prompt
from tests import _chat_golden as g

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures" / "chat_loop_golden.json").read_text())


class RefactorEquivalenceTest(unittest.TestCase):
    def test_every_scenario_matches_the_original_recording(self):
        today = datetime.date.today().isoformat()
        for name, expected in GOLDEN["scenarios"].items():
            with self.subTest(name):
                self.assertEqual(g.record(name, today), expected)

    def test_fixture_covers_the_loop_shapes(self):
        sc = GOLDEN["scenarios"]
        self.assertEqual([c["call"] for c in sc["follow_up_with_tools"]["calls"]].count("create"), 3)
        self.assertEqual(len(sc["follow_up_with_tools"]["tools_run"]), 3)
        self.assertIn("maximum rounds", sc["five_tool_rounds_exhausted"]["response"]["error"])
        self.assertIn("length cap", sc["empty_text_max_tokens"]["response"]["error"])
        self.assertIn("THIS IS A FOLLOW-UP TURN", sc["follow_up_with_tools"]["calls"][0]["system"])
        self.assertNotIn("FOLLOW-UP", sc["first_turn_text"]["calls"][0]["system"])


class SingleAssemblyPathTest(unittest.TestCase):
    def test_chat_uses_the_shared_builder_and_loop(self):
        src = inspect.getsource(api.chat)
        self.assertIn("build_chat_system(", src)
        self.assertIn("run_chat_loop(", src)
        self.assertNotIn("CHAT_SYSTEM_PROMPT.format", src)
        self.assertNotIn("_conversation_stance(", src)

    def test_api_reexports_the_same_objects(self):
        self.assertIs(api.CHAT_SYSTEM_PROMPT, chat_prompt.CHAT_SYSTEM_PROMPT)
        self.assertIs(api._conversation_stance, chat_prompt._conversation_stance)

    def test_baseline_copy_is_the_pre_phase_a_prompt(self):
        import subprocess
        from evals import baseline_chat_prompt as baseline
        original = subprocess.run(["git", "show", "223b16f:chat_prompt.py"], capture_output=True, text=True,
                                  cwd=pathlib.Path(__file__).parent.parent)
        if original.returncode != 0:
            self.skipTest("git history not available")
        ns = {}
        exec(compile(original.stdout, "orig", "exec"), ns)
        self.assertEqual(baseline.CHAT_SYSTEM_PROMPT, ns["CHAT_SYSTEM_PROMPT"])
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
        self.assertEqual(baseline._conversation_stance(msgs), ns["_conversation_stance"](msgs))

    def test_loop_takes_client_and_tool_executor_explicitly(self):
        params = inspect.signature(api.run_chat_loop).parameters
        for name in ("model", "max_tokens", "system", "messages", "client", "execute_tool", "tools"):
            with self.subTest(name):
                self.assertIn(name, params)


if __name__ == "__main__":
    unittest.main()
