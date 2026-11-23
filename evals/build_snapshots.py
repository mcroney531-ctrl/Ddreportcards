"""Build reviewable snapshots of the full combined chat prompt, both variants.

For each variant (baseline = pre-Phase-A, candidate = Phase A) this writes
the exact system prompt /chat would send in four situations, using the real
backend builder (chat_prompt.build_chat_system) and the real app text
captured by frontend_capture.js against synthetic Sleeper data:

  first_turn            first message, no players named
  first_turn_players    first message naming players (context block present)
  follow_up_players     third message naming players (follow-up guidance)
  fallback_first_turn   Sleeper not loaded yet (the app's fallback prompt)

plus inventory.json, every directive by source and topic
(instruction_inventory.py). The snapshots and inventory are committed, so any
change to any instruction source shows up as a readable diff.

Usage:
  python -m evals.build_snapshots --capture-baseline <json> --capture-candidate <json>
  python -m evals.build_snapshots --capture-gm-baseline <gm_dir> --capture-gm-candidate <gm_dir>
"""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

import api
import chat_prompt
from evals import baseline_chat_prompt as baseline
from evals.instruction_inventory import conflicts_present, directives, ownership_violations

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "tests" / "fixtures" / "prompt_snapshots"
TODAY = "<TODAY>"
NO_PLAYERS = "What should my priorities be this week?"
WITH_PLAYERS = "Jones is better than you think. Mason is a dud and Cousins changed their offense."
SPEC = {"sleeper": str(HERE / "fixtures" / "synthetic_sleeper.json"),
        "intel": {"1001": {"name": "Marcus", "breakdown": "Synthetic test breakdown: prefers future picks.",
                           "observations": ["Synthetic note: asked about 2028 picks."]}},
        "messages": [NO_PLAYERS, WITH_PLAYERS]}
VARIANTS = {"baseline": baseline, "candidate": chat_prompt}


def capture(gm_dir: str) -> dict:
    with tempfile.TemporaryDirectory() as td:
        spec, out = pathlib.Path(td, "spec.json"), pathlib.Path(td, "out.json")
        spec.write_text(json.dumps(SPEC))
        subprocess.run(["node", str(HERE / "frontend_capture.js"), gm_dir, str(spec), str(out)], check=True)
        return json.loads(out.read_text())


def tool_descriptions() -> str:
    return "\n".join(t["description"] for t in api.CHAT_TOOLS)


def build(module, cap: dict) -> dict[str, dict]:
    """Return {situation: {"system": str, "segments": {source: text}}}."""
    ctx = {c["text"]: c for c in cap["contexts"]}
    first = [{"role": "user", "content": NO_PLAYERS}]
    first_p = [{"role": "user", "content": WITH_PLAYERS}]
    follow = [{"role": "user", "content": "Trade offer: Kyle Williams for Aaron Jones."},
              {"role": "assistant", "content": "(earlier reply)"},
              {"role": "user", "content": WITH_PLAYERS}]
    rules = module.CHAT_SYSTEM_PROMPT.format(today=TODAY)
    news_block = ctx[WITH_PLAYERS]["news"]

    def assemble(frontend, msgs, context=""):
        fs = frontend + ("\n\n" + context if context else "")
        old = (chat_prompt.CHAT_SYSTEM_PROMPT, chat_prompt._conversation_stance)
        chat_prompt.CHAT_SYSTEM_PROMPT, chat_prompt._conversation_stance = module.CHAT_SYSTEM_PROMPT, module._conversation_stance
        try:
            system = chat_prompt.build_chat_system(fs, msgs, TODAY)
        finally:
            chat_prompt.CHAT_SYSTEM_PROMPT, chat_prompt._conversation_stance = old
        segs = {"backend_rules": rules, "app_system": frontend, "tool_descriptions": tool_descriptions()}
        if context:
            segs["context_block"] = context
        stance = module._conversation_stance(msgs)
        if stance:
            segs["stance"] = stance
        if news_block and msgs is not first:
            segs["news_block"] = news_block
        return {"system": system, "segments": segs}

    return {
        "first_turn": assemble(cap["liveSystemPrompt"], first),
        "first_turn_players": assemble(cap["liveSystemPrompt"], first_p, ctx[WITH_PLAYERS]["context"]),
        "follow_up_players": assemble(cap["liveSystemPrompt"], follow, ctx[WITH_PLAYERS]["context"]),
        "fallback_first_turn": assemble(cap["fallbackSystemPrompt"], first),
    }


def write(captures: dict[str, dict]) -> dict:
    report = {}
    for variant, module in VARIANTS.items():
        cap = captures[variant]
        d = OUT / variant
        d.mkdir(parents=True, exist_ok=True)
        (d / "frontend_capture.json").write_text(json.dumps(cap, indent=1, sort_keys=True) + "\n")
        built = build(module, cap)
        inv = {}
        for situation, b in built.items():
            (d / f"{situation}.txt").write_text(b["system"])
            items = directives(b["segments"])
            inv[situation] = items
            report[(variant, situation)] = {
                "chars": len(b["system"]),
                "ownership_violations": len(ownership_violations(items)),
                "conflicts": conflicts_present(b["system"]),
            }
        (d / "inventory.json").write_text(json.dumps(inv, indent=1) + "\n")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture-gm-baseline")
    ap.add_argument("--capture-gm-candidate")
    a = ap.parse_args()
    captures = {"baseline": capture(a.capture_gm_baseline), "candidate": capture(a.capture_gm_candidate)}
    for (variant, situation), r in write(captures).items():
        print(f"{variant:9} {situation:20} chars={r['chars']:6} ownership_violations={r['ownership_violations']} "
              f"conflicts={r['conflicts']}")


if __name__ == "__main__":
    sys.exit(main())
