# Phase A evaluation harness

Compares the GM Assistant's **baseline** prompt (pre-Phase-A) with the
**candidate** (Phase A) by running production's own chat loop, tools and
prompt builder for both. The prompt text is the only thing that differs.
Nothing here changes `/chat` or adds a production code path.

| File | What it does |
|---|---|
| `run_eval.py` | `smoke` calls every chat tool once, with no model calls. `paid` runs `cases.json` for both variants under a hard spend cap. |
| `frontend_capture.js` | Runs the real GM Command `index.html` in headless Chromium and captures its half of the prompt. |
| `build_snapshots.py` | Writes the full combined prompt per variant and situation, plus the instruction inventory, to `tests/fixtures/prompt_snapshots/`. |
| `instruction_inventory.py` | Every directive by source and topic; ownership rules; the conflict list. |
| `baseline_chat_prompt.py` | A verbatim pre-Phase-A copy of `chat_prompt.py`, pinned by a test. |
| `cases.json` | Six cases: C1 replays the Jones/Montgomery conversation against today's rosters; C2–C5 branch from its first turn; C6 covers intel. |
| `eval_intel.json` | Synthetic League Intel ("Marcus"). No real notes. |
| `runs/` | Approval files. Adding one in a push is what triggers a paid run. |

CI runs in `.github/workflows/phase-a-eval.yml`, only on pushes to
`claude/phase-a-conversation-quality`.

## One-time credential setup (owner)

1. In the Anthropic Console, create a **separate workspace**, for example
   `gm-eval`.
2. In that workspace's limits, set a **monthly spend limit of $10**. This is
   a backstop: the harness stops itself at $3 per variant before this is
   reached, and the Console limit may not apply instantly.
3. Create an **API key in that workspace**. Never paste it into chat, a file
   or a commit.
4. In GitHub, go to **mcroney531-ctrl/Ddreportcards → Settings → Secrets and
   variables → Actions → New repository secret**:
   - Name: `ANTHROPIC_EVAL_API_KEY`
   - Value: the key from step 3
5. Leave the production key (on Render) alone. The harness never uses it.

The secret reaches only the `paid` job, from pushes to this branch. GitHub
masks it in logs, and the harness refuses to save any output containing a
key-shaped string.

## Approving a paid run

A paid run happens only when a push adds exactly one file under
`evals/runs/`, shaped like this:

```json
{"run_id": "phase-a-1", "approved_by": "owner (in chat)", "approved_on": "YYYY-MM-DD",
 "cap_usd_per_variant": 3, "variants": ["baseline", "candidate"]}
```

The cap is clamped to `HARD_CAP_USD` ($3) in code, whatever the file says.

## Spend control

- Every model round reserves its maximum possible cost before the call: the
  counted input plus the full `max_tokens` of output, at the production
  prices in `budget.MODEL_PRICING` (`claude-sonnet-5`: $2 / $10 per million
  tokens).
- It settles to the actual cost afterwards. A failed call keeps its
  reservation.
- The first refused reservation or model API error stops the whole run:
  no further requests go to either variant. Partial results are still
  reported, and the ledger records `run_stopped` (reason, case, turn,
  variant, and an error kind only, never the raw message) plus
  `completed_turns` per variant.
- SDK retries are off (`max_retries=0`), and the harness never retries.
- Spend is tracked under a fixed key per run, so the cap can't reset at
  midnight UTC.
- `ledger.json` reports actual tokens and dollars per variant.

## Comparable evidence

Both variants share one cache of tool responses. The first identical call
goes live; every later one replays the recorded response. Turns alternate
between variants, so a run cut short leaves both with equal coverage. The cache is saved with the results.
