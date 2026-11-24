"""Phase A evaluation harness: baseline vs. candidate prompt, same everything else.

What runs is production code: api.run_chat_loop (the /chat tool loop),
api.CHAT_TOOLS, api._execute_chat_tool (the real tools), and
chat_prompt.build_chat_system. The ONLY difference between variants is the
prompt text: evals/baseline_chat_prompt.py (pre-Phase-A) vs chat_prompt.py,
plus the app's half of the prompt captured by frontend_capture.js from
gm-command main (baseline) and the Phase A branch (candidate).

Modes
  smoke  No model calls. Calls each tool once from wherever this runs and
         reports which upstream services answer. Costs nothing.
  paid   Runs evals/cases.json for both variants. Refuses to start without a
         committed approval file (evals/runs/<id>.json) and the
         ANTHROPIC_EVAL_API_KEY environment variable.

Spend control (paid)
  * Each variant gets its own budget store with a cap of
    min(approval cap, HARD_CAP_USD) and a fixed key, so the run's total
    can't reset at midnight UTC.
  * The production loop reserves every model round's upper bound before the
    call (counted input plus the full max_tokens of output, at
    budget.MODEL_PRICING) and settles it to actual usage afterwards. A
    failed call keeps its reservation. A refused reservation ends the
    variant; whatever finished is reported.
  * The Anthropic client has max_retries=0, so the SDK can't retry and bill
    more than one attempt per reservation. The harness never retries.
  * The Anthropic workspace limit on the evaluation key is a backstop only.

Comparable evidence
  Tool calls go through a cache shared by both variants (key: tool name,
  canonical input, latest user text). The first call to a key goes live and
  is recorded; every later identical call, including by the other variant,
  replays the same response. Cases run variant-interleaved, so live misses
  land minutes apart. The cache is saved, so a rerun can replay it exactly.

Outputs (--out): per-variant transcripts with tool calls and results, a
usage and cost ledger, the tool cache, and a blind copy of the transcripts
labelled A/B with the mapping in a separate file. Nothing written contains a
credential: the output directory is scanned before the run ends.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
import pathlib
import random
import re
import sys
import time
from unittest import mock

import anthropic
from fastapi import HTTPException

import api
import budget
import chat_prompt
from evals import baseline_chat_prompt

HERE = pathlib.Path(__file__).resolve().parent
HARD_CAP_USD = 3.00
VARIANT_PROMPTS = {"baseline": baseline_chat_prompt, "candidate": chat_prompt}
KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")


# ── shared tool cache ────────────────────────────────────────────────────────
class ToolCache:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.entries = json.loads(path.read_text()) if path.exists() else {}
        self.misses = []

    @staticmethod
    def key(name, tool_input, latest_user_text):
        raw = json.dumps([name, tool_input, latest_user_text], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def executor(self, variant, log):
        def run(name, tool_input, latest_user_text=""):
            k = self.key(name, tool_input, latest_user_text)
            if k in self.entries:
                result, source = self.entries[k]["result"], "replay"
            else:
                result = api._execute_chat_tool(name, tool_input, latest_user_text)
                self.entries[k] = {"name": name, "input": tool_input, "latest_user_text": latest_user_text,
                                   "result": result, "first_by": variant, "at": time.time()}
                self.misses.append({"variant": variant, "name": name})
                source = "live"
            log.append({"name": name, "input": tool_input, "source": source, "result": result})
            return result
        return run

    def save(self):
        self.path.write_text(json.dumps(self.entries, indent=1, default=str))


# ── metered client ──────────────────────────────────────────────────────────
class Meter:
    def __init__(self):
        self.calls = []

    def wrap(self, client):
        meter = self
        real_create, real_count = client.messages.create, client.messages.count_tokens

        def create(**kw):
            r = real_create(**kw)
            u = r.usage
            meter.calls.append({"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                                "usd": budget.usage_cost_micro(kw["model"], u.input_tokens, u.output_tokens) / budget.USD})
            return r

        client.messages.create = create
        client.messages.count_tokens = real_count
        return client

    @property
    def usd(self):
        return sum(c["usd"] for c in self.calls)


# ── paid run ────────────────────────────────────────────────────────────────
def load_approval(path: str) -> dict:
    a = json.loads(pathlib.Path(path).read_text())
    for field in ("run_id", "approved_by", "approved_on", "cap_usd_per_variant", "variants"):
        if field not in a:
            raise SystemExit(f"approval file missing {field!r}")
    if set(a["variants"]) - set(VARIANT_PROMPTS):
        raise SystemExit("approval names an unknown variant")
    return a


def reply_text(payload: dict) -> str:
    if payload.get("error"):
        return f"[error] {payload['error']}"
    return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


_ERR_STATUS = re.compile(r"Error code: (\d{3})")
_ERR_TYPE = re.compile(r"'type': '([a-z_]+_error)'")


def classify_api_error(message: str) -> dict:
    """Public-safe summary of a model API error: status, type, and a coarse
    kind. Never the raw message (it can echo request content)."""
    status = _ERR_STATUS.search(message or "")
    etype = _ERR_TYPE.search(message or "")
    text = (message or "").lower()
    kind = ("credit_balance" if "credit balance" in text else
            "rate_limit" if "rate" in text and "limit" in text else
            "overloaded" if "overloaded" in text else "other")
    return {"status": int(status.group(1)) if status else None,
            "type": etype.group(1) if etype else None, "kind": kind}


def new_case_state(case, transcripts):
    history = []
    if "branch_from" in case:
        src = transcripts[case["branch_from"]["case"]]
        history = copy.deepcopy(src["history"][: 2 * case["branch_from"]["after_turns"]])
    out = {"case": case["id"], "turns": [], "history": history}
    transcripts[case["id"]] = out
    return out


def run_turn(variant, text, cases_doc, frontend, cache, client, out):
    """One user turn for one variant. Returns ("ok", None), ("budget", detail)
    or ("api_error", classification)."""
    module = VARIANT_PROMPTS[variant]
    contexts = {c["text"]: c for c in frontend["contexts"]}
    history = out["history"]
    history.append({"role": "user", "content": text})
    ctx = contexts.get(text, {})
    app_system = frontend["liveSystemPrompt"] + ("\n\n" + ctx["context"] if ctx.get("context") else "")
    outbound = copy.deepcopy(history)
    if ctx.get("news"):
        outbound[-1] = {"role": "user", "content": ctx["news"] + "\n\n" + text}
    with mock.patch.object(chat_prompt, "CHAT_SYSTEM_PROMPT", module.CHAT_SYSTEM_PROMPT), \
         mock.patch.object(chat_prompt, "_conversation_stance", module._conversation_stance):
        system = chat_prompt.build_chat_system(app_system, outbound, time.strftime("%Y-%m-%d", time.gmtime()))
    tool_log = []
    try:
        payload = asyncio.run(api.run_chat_loop(
            model=cases_doc["model"], max_tokens=cases_doc["max_tokens"], system=system,
            messages=copy.deepcopy(outbound), client=client, execute_tool=cache.executor(variant, tool_log)))
    except HTTPException as exc:
        history.pop()
        out["turns"].append({"user": text, "stopped": f"budget: {exc.status_code}"})
        return "budget", str(exc.status_code)
    if payload.get("error") and str(payload["error"]).startswith("Error code:"):
        history.pop()
        info = classify_api_error(str(payload["error"]))
        out["turns"].append({"user": text, "stopped": f"api_error: {info}"})
        return "api_error", info
    reply = reply_text(payload)
    history.append({"role": "assistant", "content": reply})
    out["turns"].append({"user": text, "reply": reply, "tools": tool_log})
    return "ok", None


def paid(args):
    approval = load_approval(args.approval)
    key = os.environ.get("ANTHROPIC_EVAL_API_KEY", "")
    if not key:
        raise SystemExit("ANTHROPIC_EVAL_API_KEY is not set; refusing to run.")
    cap_usd = min(float(approval["cap_usd_per_variant"]), HARD_CAP_USD)
    cases_doc = json.loads(pathlib.Path(args.cases).read_text())
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frontends = {v: json.loads(pathlib.Path(getattr(args, f"frontend_{v}")).read_text()) for v in approval["variants"]}
    cache = ToolCache(out / "tool_cache.json")
    state = {}
    for v in approval["variants"]:
        store = budget.MemoryBudgetStore()
        meter = Meter()
        client = meter.wrap(anthropic.Anthropic(api_key=key, max_retries=0))
        state[v] = {"store": store, "meter": meter, "client": client, "transcripts": {}, "stopped": None}
    # Alternate variants turn by turn, so a run that stops early (cap, API
    # error) leaves both variants with the same coverage, and both see live
    # data at nearly the same moment. Any budget refusal or API error stops
    # the WHOLE run: no further requests are sent.
    run_stop = None
    for case in cases_doc["cases"]:
        if run_stop:
            break
        outs = {v: new_case_state(case, state[v]["transcripts"]) for v in approval["variants"]}
        for i, text in enumerate(case["turns"], 1):
            if run_stop:
                break
            for v in approval["variants"]:
                s = state[v]
                with mock.patch.object(budget, "_store", s["store"]), \
                     mock.patch.object(budget, "DAILY_LIMIT_MICRO", int(cap_usd * budget.USD)), \
                     mock.patch.object(budget, "day_key", lambda now=None, _v=v: f"eval:{approval['run_id']}:{_v}"):
                    status, info = run_turn(v, text, cases_doc, frontends[v], cache, s["client"], outs[v])
                if status != "ok":
                    s["stopped"] = case["id"]
                    run_stop = {"reason": status, "detail": info, "case": case["id"], "turn": i, "variant": v}
                    break
    cache.save()
    drift = audit_drift(cache)
    parity = frontend_parity(frontends)
    ledger = {v: {"usd": round(s["meter"].usd, 4), "cap_usd": cap_usd, "model_calls": len(s["meter"].calls),
                  "input_tokens": sum(c["input_tokens"] for c in s["meter"].calls),
                  "output_tokens": sum(c["output_tokens"] for c in s["meter"].calls),
                  "stopped_at_case": s["stopped"]} for v, s in state.items()}
    ledger["tool_cache"] = {"entries": len(cache.entries), "live_misses": cache.misses}
    ledger["evidence"] = {"drift_audit": drift, "frontend_parity": parity}
    ledger["run_stopped"] = run_stop
    ledger["completed_turns"] = {v: sum(1 for c in s["transcripts"].values() for t in c["turns"] if "reply" in t)
                                 for v, s in state.items()}
    (out / "ledger.json").write_text(json.dumps(ledger, indent=1))
    for v, s in state.items():
        (out / f"transcripts.{v}.json").write_text(json.dumps(s["transcripts"], indent=1, default=str))
    labels = list(state)
    random.SystemRandom().shuffle(labels)
    mapping = dict(zip("AB", labels))
    for letter, v in mapping.items():
        (out / f"blind.{letter}.json").write_text(json.dumps(state[v]["transcripts"], indent=1, default=str))
    (out / "blind_key.json").write_text(json.dumps(mapping))
    print(json.dumps(ledger, indent=1))



# ── evidence checks (paid) ──────────────────────────────────────────────────
def audit_drift(cache: "ToolCache") -> dict:
    """Re-fetch every recorded tool response after both variants finish.

    Identical calls already replay identical responses. A call only one variant
    made went live once, minutes from the other variant's calls. If nothing in
    the cache changed by the end of the run, every response either variant saw
    was current for the whole window, including the calls it alone made.
    Reports tool names and counts only, never response content.
    """
    changed, errors = [], 0
    for entry in cache.entries.values():
        try:
            now = api._execute_chat_tool(entry["name"], entry["input"], entry.get("latest_user_text", ""))
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if json.dumps(now, sort_keys=True, default=str) != json.dumps(entry["result"], sort_keys=True, default=str):
            changed.append(entry["name"])
    return {"rechecked": len(cache.entries), "changed": len(changed), "changed_tools": sorted(set(changed)),
            "errors": errors}


A9 = ("When evaluating trades give a clear verdict.", "When he asks for a verdict, give a clear one.")
A10 = ("so a live lookup OVERRIDES it: call ",
       "so when one of those facts matters to your answer, a live lookup overrides it: call ")


def frontend_parity(frontends: dict) -> dict:
    """The two app captures must differ only by the A9/A10 sentences.

    Both are captured from live Sleeper seconds apart; if league data changed in
    between, the variants would see different rosters. Normalise the two
    approved edits and require everything else to be identical.
    """
    if set(frontends) != {"baseline", "candidate"}:
        return {"checked": False}

    def norm(cap):
        text = json.dumps({k: cap[k] for k in ("liveSystemPrompt", "contexts")}, sort_keys=True)
        for old, new in (A9, A10):
            text = text.replace(old, new)
        return text

    return {"checked": True, "identical_except_phase_a": norm(frontends["baseline"]) == norm(frontends["candidate"])}

# ── smoke ───────────────────────────────────────────────────────────────────
def smoke(args):
    results = []

    def call(name, tool_input):
        t = time.time()
        try:
            r = api._execute_chat_tool(name, tool_input, "")
            ok = isinstance(r, dict) and "error" not in r
            detail = "" if ok else str(r.get("error"))[:160]
        except Exception as exc:  # noqa: BLE001
            r, ok, detail = None, False, type(exc).__name__
        results.append({"tool": name, "ok": ok, "ms": int((time.time() - t) * 1000), "detail": detail})
        return r

    def first_id(result):
        hits = (result or {}).get("results") or []
        return hits[0].get("player_id") if hits else None

    pid = first_id(call("get_player_value", {"player_name": "Aaron Jones"}))
    pid2 = first_id(api._execute_chat_tool("get_player_value", {"player_name": "David Montgomery"}, ""))
    call("get_roster", {"owner": "TitansTrev55"})
    call("get_pick_inventory", {"owner": "TitansTrev55"})
    call("get_league_rosters_summary", {})
    call("get_team_roster", {"team": "MIN"})
    call("get_trending_players", {"limit": 3})
    call("get_league_info", {})
    if pid:
        call("get_player_news", {"sleeper_id": pid})
        call("get_player_stats", {"sleeper_id": pid, "games": 3})
        if pid2:
            call("evaluate_trade", {"team_a_sends": [pid], "team_b_sends": [pid2]})
    else:
        results.append({"tool": "get_player_news/get_player_stats/evaluate_trade", "ok": False, "ms": 0,
                        "detail": "skipped: no player_id from get_player_value"})
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "smoke.json").write_text(json.dumps(results, indent=1))
    for r in results:
        print(f"{'OK ' if r['ok'] else 'ERR'} {r['tool']:28} {r['ms']:6}ms {r['detail']}")
    return 0 if all(r["ok"] for r in results) else 1


def scan_for_credentials(out_dir: pathlib.Path) -> None:
    for p in out_dir.rglob("*"):
        if p.is_file() and KEY_PATTERN.search(p.read_text(errors="ignore")):
            raise SystemExit(f"credential-shaped string found in {p.name}; refusing to keep outputs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["smoke", "paid"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--approval")
    ap.add_argument("--cases", default=str(HERE / "cases.json"))
    ap.add_argument("--frontend-baseline")
    ap.add_argument("--frontend-candidate")
    args = ap.parse_args()
    try:
        rc = smoke(args) if args.mode == "smoke" else (paid(args) or 0)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # CI logs are public: report the failure by type only, never a
        # traceback that could carry roster or transcript content.
        print(f"evaluation failed: {type(exc).__name__}", file=sys.stderr)
        rc = 1
    scan_for_credentials(pathlib.Path(args.out))
    return rc


if __name__ == "__main__":
    sys.exit(main())
