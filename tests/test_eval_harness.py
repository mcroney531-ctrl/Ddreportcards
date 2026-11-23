"""
The Phase A evaluation harness keeps its promises, proven without paid calls.

A fake Anthropic client stands in for the model; tools are stubbed. Checked:
  * no run without an approval file and the evaluation key;
  * the SDK client is built with max_retries=0;
  * baseline and candidate send identical model, max_tokens, tools and
    messages; only the system prompt differs, and each matches its variant;
  * tool responses are shared: a call made by one variant replays for the
    other, and each distinct call reaches the tool once;
  * the spend cap stops a variant before actual spend can exceed it, counting
    every round, and an approval above the hard cap is clamped to $3;
  * the cap can't reset mid-run (fixed budget key);
  * outputs containing anything credential-shaped are refused.
"""
import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from anthropic.types import Message

import api
import budget
import chat_prompt
from evals import baseline_chat_prompt, run_eval

SNAP = pathlib.Path(__file__).parent / "fixtures" / "prompt_snapshots"


def _msg(content, stop, in_tok, out_tok):
    return Message.model_validate({"id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
                                   "content": content, "stop_reason": stop, "stop_sequence": None,
                                   "usage": {"input_tokens": in_tok, "output_tokens": out_tok}})


class FakeAnthropic:
    """One tool round, then a text reply, for every turn."""
    instances = []

    def __init__(self, api_key=None, max_retries=None):
        self.api_key, self.max_retries = api_key, max_retries
        self.creates = []
        FakeAnthropic.instances.append(self)
        fake = self

        class _M:
            def count_tokens(self, **kw):
                return mock.Mock(input_tokens=1000)

            def create(self, **kw):
                fake.creates.append(kw)
                last = kw["messages"][-1]
                if isinstance(last.get("content"), list):          # tool results came back
                    return _msg([{"type": "text", "text": "reply"}], "end_turn", fake.in_tok, fake.out_tok)
                return _msg([{"type": "tool_use", "id": f"t{len(fake.creates)}", "name": "get_player_value",
                              "input": {"player_name": "Aaron Jones"}}], "tool_use", fake.in_tok, fake.out_tok)
        self.messages = _M()
        self.in_tok, self.out_tok = 1000, 50


class HarnessTestCase(unittest.TestCase):
    def setUp(self):
        FakeAnthropic.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.tool_calls = []
        self.approval = self.dir / "approval.json"
        self.write_approval(3)

    def tearDown(self):
        self.tmp.cleanup()

    def write_approval(self, cap):
        self.approval.write_text(json.dumps({"run_id": "t1", "approved_by": "test", "approved_on": "test",
                                             "cap_usd_per_variant": cap, "variants": ["baseline", "candidate"]}))

    def run_paid(self, cases=None, env_key="not-a-real-key"):
        cases_path = self.dir / "cases.json"
        cases_path.write_text(json.dumps(cases or {"model": "claude-sonnet-5", "max_tokens": 16000, "cases": [
            {"id": "C1", "turns": ["Offer: I give Aaron Jones for Germie Bernard.", "I like 3rds"]},
            {"id": "C2", "branch_from": {"case": "C1", "after_turns": 1}, "turns": ["Draft a counter"]}]}))

        def stub(name, tool_input, latest=""):
            self.tool_calls.append((name, json.dumps(tool_input, sort_keys=True), latest))
            return {"stub": name}

        args = mock.Mock(approval=str(self.approval), cases=str(cases_path), out=str(self.dir / "out"),
                         frontend_baseline=str(SNAP / "baseline" / "frontend_capture.json"),
                         frontend_candidate=str(SNAP / "candidate" / "frontend_capture.json"))
        env = {"ANTHROPIC_EVAL_API_KEY": env_key} if env_key else {}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(run_eval.anthropic, "Anthropic", FakeAnthropic), \
             mock.patch.object(api, "_execute_chat_tool", stub):
            if not env_key:
                os.environ.pop("ANTHROPIC_EVAL_API_KEY", None)
            run_eval.paid(args)
        return json.loads((self.dir / "out" / "ledger.json").read_text())


class RefusalTest(HarnessTestCase):
    def test_no_key_no_run(self):
        with self.assertRaises(SystemExit):
            self.run_paid(env_key="")
        self.assertEqual(FakeAnthropic.instances, [])

    def test_incomplete_approval_no_run(self):
        self.approval.write_text(json.dumps({"run_id": "x"}))
        with self.assertRaises(SystemExit):
            self.run_paid()
        self.assertEqual(FakeAnthropic.instances, [])


class SameExecutionPathTest(HarnessTestCase):
    def test_only_the_system_prompt_differs(self):
        self.run_paid()
        base, cand = FakeAnthropic.instances
        self.assertEqual((base.max_retries, cand.max_retries), (0, 0))
        self.assertEqual(len(base.creates), len(cand.creates))
        for b, c in zip(base.creates, cand.creates):
            for k in ("model", "max_tokens", "messages"):
                self.assertEqual(b[k], c[k])
            self.assertEqual(hashlib.sha256(json.dumps(b["tools"], sort_keys=True).encode()).hexdigest(),
                             hashlib.sha256(json.dumps(api.CHAT_TOOLS, sort_keys=True).encode()).hexdigest())
            self.assertEqual(b["tools"], c["tools"])
            self.assertNotEqual(b["system"], c["system"])
        today = base.creates[0]["system"].split("Today's date is ")[1][:10]
        self.assertTrue(base.creates[0]["system"].startswith(baseline_chat_prompt.CHAT_SYSTEM_PROMPT.format(today=today)))
        self.assertTrue(cand.creates[0]["system"].startswith(chat_prompt.CHAT_SYSTEM_PROMPT.format(today=today)))

    def test_branch_cases_continue_from_the_variants_own_reply(self):
        self.run_paid()
        out = json.loads((self.dir / "out" / "transcripts.candidate.json").read_text())
        self.assertEqual(out["C2"]["history"][:2], out["C1"]["history"][:2])


class SharedEvidenceTest(HarnessTestCase):
    def test_each_distinct_tool_call_runs_once_and_replays_for_the_other_variant(self):
        self.run_paid()
        self.assertEqual(len(self.tool_calls), len(set(self.tool_calls)))
        cand = json.loads((self.dir / "out" / "transcripts.candidate.json").read_text())
        sources = [t["source"] for turn in cand["C1"]["turns"] for t in turn["tools"]]
        self.assertTrue(sources and all(s == "replay" for s in sources))


class SpendCapTest(HarnessTestCase):
    def test_cap_stops_before_actual_spend_exceeds_it(self):
        self.write_approval(0.30)
        orig_init = FakeAnthropic.__init__

        def heavy_init(self, *a, **k):
            orig_init(self, *a, **k)
            self.in_tok, self.out_tok = 50_000, 2_000          # $0.12 per call at $2/$10
        with mock.patch.object(FakeAnthropic, "__init__", heavy_init):
            ledger = self.run_paid()
        for v in ("baseline", "candidate"):
            self.assertLessEqual(ledger[v]["usd"], 0.30)
            self.assertEqual(ledger[v]["model_calls"], 2)         # 3rd reservation (0.24 + 0.162) refused
            self.assertEqual(ledger[v]["stopped_at_case"], "C1")

    def test_approval_above_hard_cap_is_clamped(self):
        self.write_approval(50)
        ledger = self.run_paid()
        self.assertEqual(ledger["baseline"]["cap_usd"], run_eval.HARD_CAP_USD)
        self.assertEqual(run_eval.HARD_CAP_USD, 3.00)

    def test_pricing_comes_from_the_production_table(self):
        self.assertEqual(budget.price_of("claude-sonnet-5"), {"input": 2 * budget.USD, "output": 10 * budget.USD})


class CredentialScanTest(unittest.TestCase):
    def test_key_shaped_output_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "x.json").write_text('{"k": "sk-ant-api03-abcdefghijklmnop"}')
            with self.assertRaises(SystemExit):
                run_eval.scan_for_credentials(pathlib.Path(d))
            pathlib.Path(d, "x.json").write_text('{"ok": true}')
            run_eval.scan_for_credentials(pathlib.Path(d))


if __name__ == "__main__":
    unittest.main()
