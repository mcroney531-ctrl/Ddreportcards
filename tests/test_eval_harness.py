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
        env["GITHUB_RUN_ATTEMPT"] = getattr(self, "run_attempt", "1")
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(run_eval.anthropic, "Anthropic", FakeAnthropic), \
             mock.patch.object(api, "_execute_chat_tool", stub):
            if not env_key:
                os.environ.pop("ANTHROPIC_EVAL_API_KEY", None)
            run_eval.paid(args)
        return json.loads((self.dir / "out" / "ledger.json").read_text())


class RefusalTest(HarnessTestCase):
    def test_consumed_approval_no_run(self):
        for run_id in ("phase-a-1", "phase-a-2"):
            with self.subTest(run_id):
                self.approval.write_text(json.dumps({"run_id": run_id, "approved_by": "t", "approved_on": "t",
                                                     "cap_usd_per_variant": 3, "variants": ["baseline", "candidate"]}))
                with self.assertRaises(SystemExit):
                    self.run_paid()
        self.assertEqual(FakeAnthropic.instances, [])

    def test_every_committed_approval_is_consumed(self):
        root = pathlib.Path(run_eval.__file__).parent
        consumed = json.loads((root / "consumed_runs.json").read_text())["consumed"]
        for f in (root / "runs").glob("*.json"):
            with self.subTest(f.name):
                self.assertIn(json.loads(f.read_text())["run_id"], consumed)

    def test_workflow_rerun_no_run(self):
        self.run_attempt = "2"
        with self.assertRaises(SystemExit):
            self.run_paid()
        self.assertEqual(FakeAnthropic.instances, [])

    def test_workflow_spends_only_on_first_attempt_of_a_newly_added_approval(self):
        wf = (pathlib.Path(run_eval.__file__).parent.parent / ".github" / "workflows" / "phase-a-eval.yml").read_text()
        self.assertIn("github.run_attempt == 1", wf)
        self.assertIn("--diff-filter=A ", wf)
        self.assertNotIn("--diff-filter=AM", wf)

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
        # The post-run drift audit deliberately re-fetches everything; leave it out here.
        with mock.patch.object(run_eval, "audit_drift", lambda cache: {}):
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
        # The first refusal stops the whole run, with equal coverage.
        self.assertEqual(ledger["run_stopped"]["reason"], "budget")
        self.assertEqual(ledger["completed_turns"], {"baseline": 1, "candidate": 1})

    def test_approval_above_hard_cap_is_clamped(self):
        self.write_approval(50)
        ledger = self.run_paid()
        self.assertEqual(ledger["baseline"]["cap_usd"], run_eval.HARD_CAP_USD)
        self.assertEqual(run_eval.HARD_CAP_USD, 3.00)

    def test_pricing_comes_from_the_production_table(self):
        self.assertEqual(budget.price_of("claude-sonnet-5"), {"input": 2 * budget.USD, "output": 10 * budget.USD})


class FailFastTest(HarnessTestCase):
    def test_first_api_error_stops_the_whole_run(self):
        calls = {"n": 0}
        orig_init = FakeAnthropic.__init__

        def failing_init(self, *a, **k):
            orig_init(self, *a, **k)
            real = self.messages.create

            def create(**kw):
                calls["n"] += 1
                if calls["n"] > 2:   # baseline turn 1 ok (2 calls), then the account runs dry
                    raise RuntimeError("Error code: 400 - {'type': 'error', 'error': {'type': "
                                       "'invalid_request_error', 'message': 'Your credit balance is too low ROSTER'}}")
                return real(**kw)
            self.messages.create = create
        with mock.patch.object(FakeAnthropic, "__init__", failing_init):
            ledger = self.run_paid()
        self.assertEqual(calls["n"], 3)                                  # nothing sent after the first error
        self.assertEqual(ledger["run_stopped"]["reason"], "api_error")
        self.assertEqual(ledger["run_stopped"]["detail"],
                         {"status": 400, "type": "invalid_request_error", "kind": "credit_balance"})
        self.assertNotIn("ROSTER", json.dumps(ledger))
        self.assertEqual(ledger["completed_turns"], {"baseline": 1, "candidate": 0})

    def test_turns_alternate_between_variants(self):
        self.run_paid()
        base, cand = FakeAnthropic.instances
        # every turn: tool round + text round, baseline then candidate
        self.assertEqual(len(base.creates), len(cand.creates))
        ledger = json.loads((self.dir / "out" / "ledger.json").read_text())
        self.assertIsNone(ledger["run_stopped"])
        self.assertEqual(ledger["completed_turns"]["baseline"], ledger["completed_turns"]["candidate"])


class CredentialScanTest(unittest.TestCase):
    def test_key_shaped_output_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "x.json").write_text('{"k": "sk-ant-api03-abcdefghijklmnop"}')
            with self.assertRaises(SystemExit):
                run_eval.scan_for_credentials(pathlib.Path(d))
            pathlib.Path(d, "x.json").write_text('{"ok": true}')
            run_eval.scan_for_credentials(pathlib.Path(d))



class PublicArtifactSafetyTest(unittest.TestCase):
    """The repo is public: paid outputs leave the runner only sealed."""
    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_roundtrip_seal_deletes_plaintext_and_only_the_private_key_opens_it(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "evals").mkdir()
            for f in ("seal_results.sh", "unseal_results.sh"):
                shutil.copy(self.ROOT / "evals" / f, d / "evals" / f)
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                            "-out", str(d / "priv.pem")], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(d / "priv.pem"), "-pubout",
                            "-out", str(d / "evals" / "results_pubkey.pem")], check=True, capture_output=True)
            plain = d / "paid"
            plain.mkdir()
            (plain / "transcripts.candidate.json").write_text('{"reply": "ROSTER-SECRET-MARKER"}')
            (plain / "ledger.json").write_text('{"candidate": {"usd": 1.23}}')
            subprocess.run(["bash", str(d / "evals" / "seal_results.sh"), str(plain), str(d / "up")],
                           check=True, capture_output=True)
            self.assertFalse(plain.exists())
            self.assertEqual(sorted(p.name for p in (d / "up").iterdir()),
                             ["ledger.json", "results.key.enc", "results.tgz.enc"])
            for p in (d / "up").iterdir():
                self.assertNotIn(b"ROSTER-SECRET-MARKER", p.read_bytes())
            subprocess.run(["bash", str(d / "evals" / "unseal_results.sh"), str(d / "priv.pem"), str(d / "up"),
                            str(d / "out")], check=True, capture_output=True)
            self.assertIn("ROSTER-SECRET-MARKER", (d / "out" / "transcripts.candidate.json").read_text())

    def test_only_a_public_key_is_committed(self):
        pub = (self.ROOT / "evals" / "results_pubkey.pem").read_text()
        self.assertIn("BEGIN PUBLIC KEY", pub)
        for p in self.ROOT.rglob("*"):
            if p.is_file() and ".git" not in p.parts and p.suffix in (".pem", ".key", ".txt", ".json", ".py", ".sh", ".yml"):
                with self.subTest(str(p.relative_to(self.ROOT))):
                    self.assertNotIn("PRIVATE " + "KEY-----", p.read_text(errors="ignore"))

    def test_workflow_uploads_only_sealed_results_and_smoke_status(self):
        wf = (self.ROOT / ".github" / "workflows" / "phase-a-eval.yml").read_text()
        uploads = [l for l in wf.splitlines() if "upload-artifact" in l or "path:" in l and "name:" in l]
        paths = [l.split("path:")[1].split(",")[0].strip() for l in wf.splitlines() if "path: _eval" in l]
        self.assertEqual(sorted(paths), ["_eval_out/smoke", "_eval_upload"])
        self.assertIn("bash evals/seal_results.sh _eval_out/paid _eval_upload", wf)
        self.assertLess(wf.index("seal_results.sh"), wf.index("name: eval-results-sealed"))
        self.assertTrue(uploads)


class EvidenceChecksTest(unittest.TestCase):
    def test_drift_audit_reports_changed_responses_by_tool_name_only(self):
        with tempfile.TemporaryDirectory() as d:
            cache = run_eval.ToolCache(pathlib.Path(d, "c.json"))
            cache.entries = {
                "a": {"name": "get_player_stats", "input": {"sleeper_id": "1"}, "latest_user_text": "x",
                      "result": {"ypc": 4.0}},
                "b": {"name": "get_roster", "input": {"owner": "o"}, "latest_user_text": "x",
                      "result": {"players": ["P1"]}},
            }
            live = {"get_player_stats": {"ypc": 4.4}, "get_roster": {"players": ["P1"]}}
            with mock.patch.object(api, "_execute_chat_tool", lambda n, i, t="": live[n]):
                out = run_eval.audit_drift(cache)
        self.assertEqual(out, {"rechecked": 2, "changed": 1, "changed_tools": ["get_player_stats"], "errors": 0})

    def test_frontend_parity_allows_only_the_phase_a_sentences(self):
        base = json.loads((SNAP / "baseline" / "frontend_capture.json").read_text())
        cand = json.loads((SNAP / "candidate" / "frontend_capture.json").read_text())
        self.assertTrue(run_eval.frontend_parity({"baseline": base, "candidate": cand})["identical_except_phase_a"])
        drifted = json.loads(json.dumps(cand))
        drifted["liveSystemPrompt"] = drifted["liveSystemPrompt"].replace("Kyle Williams", "Someone Else")
        self.assertFalse(run_eval.frontend_parity({"baseline": base, "candidate": drifted})["identical_except_phase_a"])


class LedgerIsPublicSafeTest(HarnessTestCase):
    def test_ledger_holds_numbers_and_tool_names_only(self):
        self.run_paid()
        text = (self.dir / "out" / "ledger.json").read_text()
        for leak in ("Offer: I give", "I like 3rds", "reply", "Aaron Jones", "stub"):
            with self.subTest(leak):
                self.assertNotIn(leak, text)
        ledger = json.loads(text)
        self.assertIn("drift_audit", ledger["evidence"])
        self.assertIn("frontend_parity", ledger["evidence"])

    def test_unexpected_failure_reports_type_only(self):
        with mock.patch.object(run_eval, "paid", side_effect=ValueError("ROSTER-SECRET-MARKER")), \
             mock.patch("sys.argv", ["run_eval", "paid", "--out", str(self.dir / "o")]), \
             mock.patch("sys.stderr") as err:
            (self.dir / "o").mkdir()
            self.assertEqual(run_eval.main(), 1)
        written = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("ValueError", written)
        self.assertNotIn("ROSTER-SECRET-MARKER", written)

if __name__ == "__main__":
    unittest.main()
