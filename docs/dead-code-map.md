# Dead-Code and Duplication Map — Dynasty Umbrella

Phase 4C of the Ddreportcards stabilization effort. Inventory only — nothing
in this document has been deleted or modified. Every finding below was
verified by reading the actual file and tracing its inbound references with
`grep`, not inferred from naming or intuition. Where a shared `dynasty_core`
file was involved, both `Ddreportcards` and `scoutcap` were checked before
any classification, per instruction.

**Repos covered:** `mcroney531-ctrl/gm-command`, `mcroney531-ctrl/Ddreportcards`,
`mcroney531-ctrl/scoutcap`
**Branch inspected in each:** `claude/dynasty-umbrella-consolidation-4waag0`
(gm-command and scoutcap also have `origin/main`; Ddreportcards has no
`main`/`master` on origin at all — only two `claude/*` branches)
**Date:** 2026-09-21

**Scope note:** this pass focused on the categories explicitly called out,
plus whatever else surfaced while tracing those. It is not a line-by-line
audit of every file in all three repos. Categories checked with no findings
are stated as such below rather than omitted, so absence reads as "checked,
clean" and not "not looked at."

---

## Ddreportcards

| # | Path/Symbol | Inbound references | Known consumer | Classification | Reason | Deletion risk | Proposed action |
|---|---|---|---|---|---|---|---|
| D1 | `data/leaguelogs_client.py` | Only its own test (`tests/test_leaguelogs_retired.py`) asserting it's unused | None | **DEFINITELY DEAD** | Thin re-export of `dynasty_core.leaguelogs`. `grep` across every `.py` file in the repo found zero imports of `data.leaguelogs_client` anywhere outside its own test. LeagueLogs' entire Developer API returns HTTP 410 Gone on every endpoint (confirmed by direct probe earlier in this engagement), and the tool that used to call it (`get_player_blurb`) was already removed from `api.py` and the chat tool schema. | Low | Delete in the same batch as D2. |
| D2 | `dynasty_core/leaguelogs.py` | `data/leaguelogs_client.py` only (itself dead, D1) | None | **DEFINITELY DEAD** | Same reasoning as D1, one layer down. Checked scoutcap too (see S1) — not imported there either, so this isn't a case of "Ddreportcards doesn't need it but scoutcap does." Byte-identical between both repos. | Low | Delete alongside D1 and S1 in one cross-repo batch, once confirmed no other branch/consumer exists. |
| D3 | `app.py` (536 lines, Streamlit frontend) | `render.yaml` does not reference it; only entry point deployed is `uvicorn api:app` | Possibly the repo owner, for local manual testing | **DEPLOYMENT-SPECIFIC / AMBIGUOUS — VERIFY BEFORE DELETE** | `render.yaml` defines exactly one service (`type: web`, `startCommand: uvicorn api:app ...`). There is no Streamlit deployment anywhere in the Render config. The actual product frontend is `gm-command` (a static site) talking to `api.py` directly. `app.py` may still be run locally (`streamlit run app.py`, per the README's own Setup section) for ad hoc testing, so this is not confirmed dead — only confirmed **not deployed**. | Medium (only because it's unclear if it's still used locally — ask before deleting) | Ask the repo owner whether `streamlit run app.py` is still part of their workflow. If not, delete; if so, keep but note in the README that it's dev-only, not the deployed product. |
| D4 | `runtime.txt` (contains `python-3.12`) | Render's Python-version selection now reads `.python-version` / `PYTHON_VERSION` env var, not `runtime.txt` alone (confirmed earlier in this engagement — this file existing alongside `.python-version` was part of what masked the Python-3.14.3 runtime mismatch that contributed to the original 502 investigation) | None at deploy time | **AMBIGUOUS — VERIFY BEFORE DELETE** | Not itself broken (it agrees with `.python-version`), but it's redundant with `.python-version` and was previously a source of confusion about which file Render actually reads. Low risk to remove, but confirm no other tooling (a CI step, a different host) reads `runtime.txt` specifically before removing it. | Low | Remove once confirmed nothing else reads it; `.python-version` alone is sufficient for Render. |
| D5 | Comments describing "the old Netlify function" for `/chat` (e.g. `api.py`'s module docstring: "same as the old Netlify function, but without Netlify's 10s ceiling") | N/A — comments, not code | N/A | **ACTIVE (not dead)** | These are accurate historical context explaining *why* the current code is shaped the way it is (the chat loop moved server-side from a Netlify function). Not stale — the thing they describe genuinely happened and the comment is still correct. Not a deletion candidate; noted here only because it was checked against the "stale Netlify-era documentation" category and found to be accurate, not stale. | N/A | No action. |

**Categories checked with no findings in Ddreportcards:** old/unused model
identifiers (both `claude-sonnet-4-6`, used by the report-pipeline agents,
and `claude-sonnet-5`, the `/chat` endpoint's default/allowed model in
`chat_guard.py` and its price entry in `budget.py`, are current and
intentionally different per-purpose, not stale); tracked generated
artifacts (`git ls-files` found no `__pycache__`, `.pyc`, or similar
committed anywhere); unreachable ESPN-id fallback branches (the
Sleeper→FantasyCalc→None chain is exactly two real branches, already
covered by `tests/test_leaguelogs_retired.py::EspnAthleteIdFallbackIsSleeperFantasyCalcOnlyTest`).

---

## scoutcap

| # | Path/Symbol | Inbound references | Known consumer | Classification | Reason | Deletion risk | Proposed action |
|---|---|---|---|---|---|---|---|
| S1 | `dynasty_core/leaguelogs.py` | None found anywhere in scoutcap | None | **DEFINITELY DEAD** | Byte-identical to Ddreportcards' copy (D2), same dead upstream API. `grep -rn "dynasty_core"` across every `.py` file in scoutcap shows only `dynasty_core.fantasycalc` is ever actually imported (via `tools/fantasycalc.py`'s re-export) — `leaguelogs` doesn't appear as an import anywhere. | Low | Delete alongside D1/D2. |
| S2 | `dynasty_core/sleeper.py` | None — only `tools/sleeper.py` is imported by any active scoutcap code (`agents/situation_agent.py`, `agents/production_agent.py`, `agents/synthesis_agent.py`, `mcp_server.py`, `app.py`) | Ddreportcards actively uses its own (identical) copy | **DUPLICATED — unused in this repo specifically** | Byte-identical (`sha256` match) to Ddreportcards' `dynasty_core/sleeper.py`, which IS actively used there. In scoutcap, `tools/sleeper.py` is a completely independent, direct-httpx implementation that does **not** delegate to `dynasty_core.sleeper` at all — unlike `tools/fantasycalc.py`, which does. A thorough repo-wide grep for `dynasty_core` found no reference to `dynasty_core.sleeper` anywhere in scoutcap outside the file's own docstring. | Low functionally (nothing in scoutcap would break today), Medium for the shared-package plan (the `dynasty_core/__init__.py` docstring's "both engines ship an identical copy" claim would no longer be true for this file if deleted only here) | Do not delete unilaterally. Flag for the person planning the Stage 2 installable-package promotion: either finish wiring `tools/sleeper.py` to delegate to `dynasty_core.sleeper` (matching the `fantasycalc` pattern) and retire the independent implementation, or explicitly drop `sleeper`/`espn` from scoutcap's copy of `dynasty_core` and update the docstring to say so. |
| S3 | `dynasty_core/espn.py` | None — `tools/espn.py` is the active implementation | Ddreportcards actively uses its own (identical) copy | **DUPLICATED — unused in this repo specifically** | Same situation as S2. `tools/espn.py` is independent (own `NFL_BASE`/`CFB_BASE` httpx calls) and additionally covers college football endpoints that `dynasty_core/espn.py` doesn't have (scoutcap evaluates draft prospects, not just rostered NFL players) — so this isn't a case of "just duplicate the same thing," `tools/espn.py` does strictly more. | Same as S2 | Same recommendation as S2 — resolve as part of the same decision, since both `sleeper` and `espn` are in the same half-migrated state. |
| S4 | `tools/sleeper.py`, `tools/espn.py` | `agents/situation_agent.py`, `agents/production_agent.py`, `agents/synthesis_agent.py`, `mcp_server.py`, `app.py` | Active | **ACTIVE** | The real, currently-used implementations. Noted here only for contrast with S2/S3 — not a deletion candidate. | N/A | No action. |
| S5 | `activate.ps1` (repo root) | N/A — a personal venv-activation convenience script | The repo owner's own machine | **AMBIGUOUS — VERIFY BEFORE DELETE** | Looks like a personal dev convenience script (PowerShell venv activation) rather than project code. Harmless to keep, but worth asking whether it belongs in source control at all versus a personal dotfile/alias. | Low | Ask; not urgent either way. |
| S6 | `Capstone_Rubric_Checklist.html`, `.claude/launch.json`, `.devcontainer/devcontainer.json` | N/A | Coursework/dev-environment context | **DEPLOYMENT-SPECIFIC (non-runtime)** | These indicate scoutcap started as an academic capstone project, separate in origin from the Dynasty Umbrella idea, later brought under this consolidation. None of these affect runtime behavior. | Low | No action needed for stability; consider whether the rubric checklist is still relevant to keep in the repo root versus a `docs/` subfolder, purely for tidiness — not a functional concern. |

**Categories checked with no findings in scoutcap:** tracked generated
artifacts (none); stale model identifiers (only `claude-sonnet-4-6`
appears, consistent and current); Netlify-era code (scoutcap has no
Netlify presence at all — it's a Streamlit app, confirmed by
`.streamlit/config.toml` and `.streamlit/secrets.toml.example`).

---

## gm-command

| # | Path/Symbol | Inbound references | Known consumer | Classification | Reason | Deletion risk | Proposed action |
|---|---|---|---|---|---|---|---|
| G1 | `dynasty_config.json` (repo root) | None found in `index.html` — no `fetch`/reference to the filename anywhere | None at runtime | **DEFINITELY DEAD (static asset, never loaded)** | The file's own `_note` field says it "Mirrors config/dynasty_config.py in the Python engines," implying it's meant to be the frontend's copy of league config. But `index.html` never fetches it — the same values (`"The Psych Ward"`, `"TitansTrev55"`, `"12-team superflex, half-PPR"`, etc.) are hardcoded directly as string literals inside the JS (e.g. in the system-prompt builders). A repo-wide grep for `dynasty_config` in `index.html` returns nothing. | Low | Either delete it, or (better, since it exists specifically to avoid config drift with the Python engines) wire `index.html` to actually fetch and use it instead of the hardcoded literals — that would also fix a real drift risk: nothing currently keeps the hardcoded JS values in sync with `config/dynasty_config.py` in the two Python engines. Flagging as a design gap worth a deliberate decision, not just a deletion. |
| G2 | `netlify/functions/keepalive.js` (scheduled every 5 min per `netlify.toml`) | Configured, actively scheduled | Netlify's scheduler | **DUPLICATED / AMBIGUOUS — VERIFY BEFORE DELETE** | Its own comment states its purpose: ping Render's `/health` every 5 minutes so the free-tier dyno doesn't cold-start before a chat message. Render's own health-check polling (configured directly on the `dynasty-report-cards-api` service, confirmed hitting `/health` roughly every 5 seconds throughout this whole engagement's production observations) already keeps the same dyno warm, continuously, at a much tighter interval — this may now be fully redundant. Not confirmed dead (it's still actively deployed and scheduled, and removing it is a live infrastructure change, not a code cleanup), so it stays out of "definitely dead." | Low functionally if truly redundant, but this is an infrastructure/cost decision (Netlify scheduled function invocations), not a pure code-deletion call | Verify Render's own health-check cadence is enough on its own (it appears to be, based on this engagement's production logs), then retire the Netlify scheduled function and its `netlify.toml` entry together. |
| G3 | Live chat system prompt's parenthetical `"(FantasyCalc values, Sleeper depth charts, ESPN stats, a LeagueLogs blurb)"` inside `window.liveSystemPrompt` | N/A — a string literal inside active, used code | The `/chat` system prompt sent on every request | **AMBIGUOUS — stale content, not dead code** | The surrounding code (`buildSystemPrompt`) is very much alive and used every chat turn. But this specific citation-source list still names "a LeagueLogs blurb" as something the assistant might cite — LeagueLogs' blurb tool no longer exists anywhere in the backend (confirmed retired, `tests/test_leaguelogs_retired.py` in Ddreportcards). The sibling fallback `systemPrompt` a few hundred lines later does **not** mention LeagueLogs, so the two prompts have already drifted from each other on this point. | Low (a one-line string edit) | Drop the LeagueLogs mention from the live prompt's citation list to match reality and the fallback prompt. |
| G4 | `netlify/functions/fetch-proxy.js` | `index.html` line 1626 (news feed CORS proxy) | Active | **ACTIVE** | Confirmed actively called for feeds the browser can't reach directly. Its own extensive comments describe a real SSRF-hardening history (denylist → allowlist). Not dead — checked specifically because a "proxy" function is an easy thing to assume is Netlify-era cruft; it isn't. | N/A | No action. |
| G5 | `sw.js` (service worker, hand-written PWA cache) | `manifest.json` (PWA), browser-registered | Active | **ACTIVE, not generated** | Checked specifically against the "generated/service-worker artifacts that should not be source-controlled" category. This is a small, hand-written, legitimate PWA service worker (cache install/activate lifecycle for offline support) — not a build tool's generated output. Belongs in source control. | N/A | No action. |

**Categories checked with no findings in gm-command:** duplicate/superseded
chat call paths beyond the intentional live-vs-fallback split (G3 covers
the one real drift found); old analyst/source-attribution UI (the chat
system prompt explicitly instructs the assistant never to attribute claims
to a named analyst or publication — this is a deliberate anti-hallucination
guard already in place, not leftover UI); tracked generated artifacts
(none beyond the two PWA files already addressed as active, G5).

---

## Cross-repo summary

| Classification | Count |
|---|---|
| DEFINITELY DEAD | 4 (D1, D2, S1, G1) |
| DUPLICATED (byte-identical, unused in one repo) | 2 (S2, S3 — same underlying decision) |
| DUPLICATED / AMBIGUOUS (infrastructure) | 1 (G2) |
| AMBIGUOUS — VERIFY BEFORE DELETE | 4 (D3, D4, S5, S6) |
| Stale content, not dead code | 1 (G3) |
| ACTIVE (checked, confirmed not dead) | 5 (D5, S4, G4, G5, plus the picks.py/fetch-proxy-style checks noted inline) |

## Proposed deletion batches (lowest to highest risk)

1. **Batch 1 — LeagueLogs remnants (lowest risk):** D1 (`Ddreportcards/data/leaguelogs_client.py`), D2 (`Ddreportcards/dynasty_core/leaguelogs.py`), S1 (`scoutcap/dynasty_core/leaguelogs.py`). Zero live consumers anywhere in either repo; the upstream API is confirmed globally gone. This is the cleanest, most confidently-dead batch.

2. **Batch 2 — One-line stale content fix:** G3 (drop the LeagueLogs mention from gm-command's live system prompt). Not a deletion in the code sense, but bundled here since it's the same LeagueLogs-retirement cleanup, essentially zero risk, and it's a stale-content fix rather than dead code.

3. **Batch 3 — Dead static config:** G1 (`gm-command/dynasty_config.json`) — but only after a deliberate decision on whether to wire it up instead of deleting it (it exists for a real reason: keeping the frontend's league config in sync with the Python engines' `config/dynasty_config.py`, which nothing currently enforces).

4. **Batch 4 — Redundant infrastructure:** G2 (`gm-command`'s Netlify `keepalive` scheduled function), after confirming Render's own health-check cadence is sufficient on its own (it appears to be, based on this engagement's own production observations).

5. **Batch 5 — Requires the repo owner's input (do not delete without asking):** D3 (`Ddreportcards/app.py`, Streamlit frontend — only if confirmed no longer used locally), D4 (`Ddreportcards/runtime.txt`), S5 (`scoutcap/activate.ps1`).

6. **Highest risk / not a deletion batch, a design decision:** S2 + S3 (scoutcap's unused `dynasty_core/sleeper.py` and `dynasty_core/espn.py` copies) — resolving these means either finishing the migration (`tools/sleeper.py`/`tools/espn.py` delegating to `dynasty_core`, matching how `tools/fantasycalc.py` already does) or explicitly deciding scoutcap's copy of `dynasty_core` only carries `fantasycalc` going forward. Either is a real design choice for whoever owns the Stage 2 shared-package plan, not a cleanup task.

No code was deleted or modified in producing this map.
