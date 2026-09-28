# Stage 2C — Post-Convergence Audit Before Packaging

Status: **docs only.** No runtime code, requirements, or tests were changed by
this audit. It answers one question: what must be resolved before
`dynasty_core` becomes one installable package?

Evidence baseline (all facts below were re-derived from the trees at these
commits, not carried over from earlier documents):

| Repo | Branch | HEAD |
|---|---|---|
| Ddreportcards | `claude/dynasty-umbrella-consolidation-4waag0` | `ed9fd00` |
| scoutcap | `claude/dynasty-umbrella-consolidation-4waag0` | `8b5f5eb` |
| gm-command | (read-only reference) | — |

Method: `sha256sum`, repo-wide `grep`, an AST-based export/consumer table,
and an isolated-directory import experiment for the FantasyCalc config
coupling (§2.2). Offline suites were run after writing this document (see
Verification, at the end).

---

## 1. Converged baseline

| File | Ddreportcards SHA-256 | scoutcap SHA-256 | Identical |
|---|---|---|---|
| `dynasty_core/__init__.py` | `b69efcf8f0ab90ea38dbafcbd1e8a2b1e43e84150b01cb832f346a29fc23bb58` | `b69efcf8…c23bb58` | yes |
| `dynasty_core/sleeper.py` | `194cc03611eed72c18216cd310fc1d912eaf67f49fc415965dce1618293adb4c` | `194cc036…93adb4c` | yes |
| `dynasty_core/espn.py` | `40c6bb07d8f472504be258472b4ede5713ffd9c3a1f921a700dc41dc64d9d777` | `40c6bb07…d9d777` | yes |
| `dynasty_core/fantasycalc.py` | `b3f9208412135d80c72616684d808d5bfdeac7e6635d6aca9e834ae4354bb932` | `b3f92084…4bb932` | yes |
| `config/dynasty_config.py` | `0c4c9f4dae247027875a839990c27eca1b138e7c6640796f61122f6a9ddd3a5c` | `0c4c9f4d…ddd3a5c` | yes |

`dynasty_core/` contains exactly these four modules in both repos (655 lines
total). `leaguelogs.py` is gone from both (Phase 4C).

Byte-identity is real but it is **not** the same as a clean package
boundary: `fantasycalc.py`'s behavior depends on a *host* file
(`config/dynasty_config.py`) that happens to also be identical today. See §2.

A third, manually-maintained mirror of the same league settings exists
outside Python: `gm-command/dynasty_config.json` (values currently match
the Python config field-for-field).

---

## 2. Outbound-dependency map

### 2.1 Per module

| Module | Dependency | Class |
|---|---|---|
| `__init__.py` | none (docstring only; no `__version__`, no re-exports) | — |
| `sleeper.py` | `__future__.annotations`, `time`, `typing.Any` | standard library |
| | `httpx` | third-party runtime |
| `espn.py` | `__future__.annotations`, `re` | standard library |
| | `httpx` | third-party runtime |
| `fantasycalc.py` | `__future__.annotations`, `re`, `time`, `collections.defaultdict` | standard library |
| | `httpx` | third-party runtime |
| | `config.dynasty_config.LEAGUE` inside `try/except ImportError` | **host-application dependency, with optional/fallback behavior** |

No module imports another `dynasty_core` module (no shared-package-internal
dependencies). The **only** third-party runtime dependency of the whole
package is `httpx`.

### 2.2 The FantasyCalc config coupling — tested, not inferred

```python
try:
    from config.dynasty_config import LEAGUE as _LEAGUE
    _DEFAULT_PPR = _LEAGUE["ppr"]; _DEFAULT_NUM_QBS = _LEAGUE["num_qbs"]; _DEFAULT_NUM_TEAMS = _LEAGUE["num_teams"]
except ImportError:
    _DEFAULT_PPR = 0.5; _DEFAULT_NUM_QBS = 2; _DEFAULT_NUM_TEAMS = 12
```

These values become the **default arguments** of `get_dynasty_values()` at
import time, and every no-argument caller (`get_value_for_sleeper_id`,
`get_player_value`, the condensed index, both apps' agents, `picks.py`,
`api.py`) inherits them.

A copy of `dynasty_core` was imported from isolated directories with
different ambient `config` packages on `sys.path`:

| Scenario | Result |
|---|---|
| No `config` package at all (standalone package) | `ppr=0.5 num_qbs=2 num_teams=12` (fallback) |
| Foreign `config` package without `dynasty_config` | `ppr=0.5 num_qbs=2 num_teams=12` (fallback) |
| Namespace `config` package (no `__init__.py`) without `dynasty_config` | `ppr=0.5 num_qbs=2 num_teams=12` (fallback) |
| `config.dynasty_config` for a *different* league (1QB, 10 teams, full PPR) | `ppr=1.0 num_qbs=1 num_teams=10` — **defaults silently changed** |
| `config.dynasty_config` present but `LEAGUE` missing `"ppr"` | **`KeyError: 'ppr'` at import** — the importing app fails to start (only `ImportError` is caught) |
| Ddreportcards repo root (actual) | `0.5 / 2 / 12` from `Ddreportcards/config/dynasty_config.py` |
| scoutcap repo root (actual) | `0.5 / 2 / 12` from `scoutcap/config/dynasty_config.py` |

Answers:

- **Is `dynasty_core` importable and semantically complete without either
  host's `config/`?** Importable: yes. Semantically complete: only by
  coincidence — the hardcoded fallback happens to equal the current league.
  Nothing guarantees the two stay equal, and nothing reports which source won.
- **Do its defaults silently change depending on an ambient
  `config.dynasty_config`?** Yes, demonstrated above. `config` is a generic
  top-level name; once installed, the package resolves it against whatever
  the *host process* has on `sys.path` (the app root, in both apps today).
  A malformed host config also turns into an import-time crash.
- **Acceptable for the intended package?** No. An installed package whose
  provider query parameters are decided by an undeclared, ambient,
  host-owned module is a boundary defect: the package's behavior would not
  be a function of the package version. League configuration must become
  an explicit package contract before extraction (§10, blocker B1).

Supporting fact: in scoutcap, `config/dynasty_config.py` has **no importer
other than `dynasty_core.fantasycalc`** — it exists only to feed these
defaults. In Ddreportcards, `api.py` also imports it (for `league_id` and
`my_display_name`).

---

## 3. Inbound-consumer map

### 3.1 Import surfaces

| Repo | Module | Imports from |
|---|---|---|
| Ddreportcards | `data/sleeper_client.py` (facade) | `dynasty_core.sleeper` |
| | `data/espn_client.py` (facade) | `dynasty_core.espn` |
| | `data/fantasycalc_client.py` (facade) | `dynasty_core.fantasycalc` |
| | `api.py` (**direct**) | `dynasty_core.sleeper`, `.fantasycalc`, `.espn` |
| | `picks.py` (**direct**) | `dynasty_core.sleeper`, `.fantasycalc` |
| | agents, `data/trade_history.py`, `app.py` | via the three `data/*_client` facades |
| scoutcap | `tools/sleeper.py` (facade, Batch 6) | `dynasty_core.sleeper` |
| | `tools/fantasycalc.py` (facade) | `dynasty_core.fantasycalc` |
| | everything else | via `tools.*` only — **no direct `dynasty_core` imports** |
| scoutcap | — | **does not use `dynasty_core.espn` at all** (its ESPN needs are draft/college, served by Scout-owned `tools/espn.py`) |

### 3.2 De-facto package API (what is actually called)

Real call sites, excluding tests, the package itself, and facade definitions.
Word-matched, then false positives removed by hand (e.g. `get_player_value`
in Ddreportcards `api.py` is a chat-tool *name*, not a FantasyCalc call).

**`dynasty_core.sleeper`**

| Function | Ddreportcards | scoutcap |
|---|---|---|
| `get_user` | — | synthesis_agent, app (via `tools.sleeper`) |
| `get_leagues` | — | — (facade re-export only) |
| `get_league_users` | api, picks, data/trade_history | synthesis_agent (as `get_users_in_league`) |
| `get_league_rosters` | api, picks, data/trade_history | synthesis_agent, app (as `get_rosters`) |
| `get_league_info` | api, picks | — |
| `get_traded_picks` | picks | — |
| `get_league_drafts` | picks | — |
| `get_league_season_chain` | data/trade_history (+ internal use) | — |
| `get_transactions` | — (internal to `get_all_trades`) | — |
| `get_all_trades` | — (internal to `get_all_trades_all_seasons`) | — |
| `get_all_trades_all_seasons` | data/trade_history | — |
| `get_all_players` | production/situation agents, api, data/trade_history | situation/synthesis agents, app, mcp_server (as `get_nfl_players`) |
| `get_player` | — | — (facade re-export only) |
| `get_trending` | — | situation/synthesis agents, mcp_server |
| `get_trending_adds` | situation_agent, api | — |
| `get_roster_by_display_name` | api, app | — |
| `resolve_roster_players` | api, app | — |

**`dynasty_core.espn`** — Ddreportcards only: `team_espn_id`,
`get_season_statistics`, `get_career_statistics`, `flatten_statistics`,
`get_player_injury_notes` (production_agent, via facade);
`get_season_statistics`, `flatten_statistics`, `get_recent_game_logs`,
`fantasy_relevant_stats` (api, direct). Exported but uncalled:
`get_event_log`, `get_injury_detail` (the latter is used internally by
`get_player_injury_notes`), `TEAM_ESPN_IDS`, `TEAM_ABBR_ALIASES`.

**`dynasty_core.fantasycalc`**

| Function | Ddreportcards | scoutcap |
|---|---|---|
| `get_dynasty_values` | market/situation agents, api, data/trade_history, picks | — |
| `index_by_sleeper_id` | api, data/trade_history | — |
| `index_by_sleeper_id_with_redraft_rank` | situation_agent | — |
| `get_value_for_sleeper_id` | market/production agents, api | — |
| `index_picks_by_label` | api, picks (direct) | — |
| `get_player_value` | — | situation/synthesis agents, app, mcp_server |
| `value_grade` | — (see note) | situation/synthesis agents, mcp_server |
| `GRADE_TIERS` | — (see note) | — |

Note: Ddreportcards' `agents/situation_agent.py` carries its **own copy** of
`GRADE_TIERS` and a private `_value_grade` identical to the shared ones
(predates consolidation, commit `373cd46`). Duplication debt, not a provider
bypass.

Every name above that has a caller is part of the API that the first package
version must preserve (or deliberately relocate, §5).

---

## 4. Provider bypasses

Scan for `api.sleeper.app`, `sports.core.api.espn.com`, `api.fantasycalc.com`,
`httpx.get/post/Client/AsyncClient`, `requests.*`, `urllib.request` across
both repos (tests excluded), plus every module that imports `httpx`,
`requests` or `aiohttp`.

| Repo | Location | What | Classification |
|---|---|---|---|
| both | `dynasty_core/{sleeper,espn,fantasycalc}.py` | provider HTTP | legitimate shared-core provider implementation |
| Ddreportcards | `budget.py` | persistent `httpx.Client` to Upstash REST | unrelated HTTP |
| Ddreportcards | `scripts/smoke_production.py` | `urllib.request` to the app's own deploy | unrelated HTTP |
| scoutcap | `tools/espn.py` | ESPN `NFL_BASE` draft-athlete + `CFB_BASE` college endpoints | app-specific provider integration (draft/college object model, not in shared core by design — Stage 2 plan §4.4/§6) |
| scoutcap | `app.py::_load_pick_arsenal` (lines 854–866) | Sleeper `/league/{id}/users`, `/league/{id}/traded_picks` via own `httpx` | **accidental bypass / consolidation debt** |
| gm-command | `index.html` + `netlify/functions/fetch-proxy.js` | browser-side Sleeper calls | out of scope (JavaScript; not a consumer of a Python package) |

Ddreportcards has **zero** direct provider HTTP outside `dynasty_core`.
scoutcap's only Sleeper bypass is `_load_pick_arsenal`.

### 4.1 `scoutcap/app.py::_load_pick_arsenal` in detail

- **Skips `raise_for_status()`?** Yes, both direct calls:
  `httpx.get(...).json()`. A non-2xx body would be parsed as data and fail
  later with an unrelated error (e.g. `TypeError` iterating it).
- **Would shared primitives preserve semantics?** Yes on success:
  `get_league_users(league_id)` and `get_traded_picks(league_id)` hit the
  identical endpoints with the identical timeout (15 s) and return the
  decoded JSON unchanged. On failure they raise `httpx.HTTPStatusError`
  instead. That is not a new failure class for this function: it already
  calls the shared `get_user` and `get_rosters` first, which raise the same
  way. The single caller does not catch exceptions either way.
- **Callers:** exactly one — `app.py:1063`, the Mock Draft Simulator's
  "Load from Sleeper →" button, which passes `_load_pick_arsenal("2026")`
  explicitly. The `season="2026"` default is never exercised.
- **Is `season="2026"` a bug?** Classified as **intentional application
  policy, not accidental provider staleness.** Evidence: Scout is explicitly
  a 2026 rookie-draft product — "2026 Mock Draft Simulator", "2026 Rookie
  Draft Board", "Full 2026 rookie class", `search_draft_prospects(...,
  season=2026)` in `mcp_server.py` and both agents, and `tools/espn.py`'s
  draft functions all default to 2026 — about 15 occurrences. Unlike the old
  `get_leagues(..., "2025")` default (a provider primitive silently pinned
  to a past year, with no caller), this is a product scoped to one draft
  class, and its only caller states the year. The real risk is **scatter**,
  not staleness: rolling Scout to the 2027 class means editing ~15 literals.
  That is a Scout product decision (a single Scout-owned draft-season
  setting), not a package concern.
- **Adjacent observations (app-owned, not package concerns):** the arsenal
  code hardcodes 4 rounds and 12 teams (`range(1, 5)`, `teams=12`),
  duplicating `config/league.py`'s `rounds`/`picks_per_round`. And an
  `{"error": ...}` return is silently treated as "not loaded"
  (`using_real_picks` requires a `"picks"` key), so the user never sees the
  error message.

---

## 5. Package-boundary ownership

Test: *does the function express a Sleeper/FantasyCalc fact or data shape
(provider/domain primitive), or one app's choice of identity, annotation, or
workflow?* All five candidates are called only by Ddreportcards; none has
a test in either repo.

| Function | Callers | Both apps? | Nature | Move out before packaging simplifies boundary? | Migration risk |
|---|---|---|---|---|---|
| `get_league_season_chain` | DD `data/trade_history` (+ `get_all_trades_all_seasons`) | no | **Provider/domain primitive** — walks Sleeper's own `previous_league_id` links, which is how Sleeper models dynasty continuity. No app policy. | No — keep. | n/a |
| `get_all_trades_all_seasons` | DD `data/trade_history` | no | **Ddreportcards workflow** — mutates provider records with app-invented `_season`/`_league_id` keys and imposes a sort order. | Yes — the annotation convention would otherwise be frozen into the package's v1 contract. | Low: one caller, reached through DD's own facade (`data/sleeper_client`), which can re-point without touching the caller. |
| `get_roster_by_display_name` | DD `api.py` (×3), `app.py` | no | **Ddreportcards workflow** — resolves identity by display name and raises `ValueError`; Scout resolves the same thing by `user_id` instead, in its own code. | Yes — identity policy is app-owned. | Low: DD-only; `api.py` imports it directly (one import line to change) plus the facade. |
| `resolve_roster_players` | DD `api.py` (×2), `app.py` | no | **Ddreportcards composition** — thin merge of roster ids with the catalog. | Yes, small. | Low (same pattern as above). |
| `index_picks_by_label` (FantasyCalc) | DD `api.py`, `picks.py` | no | **Provider/domain primitive** — interprets FantasyCalc's own payload (position `"PICK"`, `"YYYY Nth"` labels); the natural twin of `index_by_sleeper_id`. | No — keep. | n/a |

Also noted (keep; no change proposed): `get_all_trades` and
`get_transactions` are generic Sleeper primitives, currently reached only
internally. `GRADE_TIERS`/`value_grade` in `fantasycalc.py` is
league-calibrated grading policy ("12-team superflex") inside a provider
module — defensible for a one-league package, but tied to the league-config
decision in §10, B1.

---

## 6. Dead / obsolete shared-adjacent code (scoutcap `tools/espn.py`)

Kept separate from extraction — none of these are in `dynasty_core`.

| Function | Active callers | Classification | Why |
|---|---|---|---|
| `get_nfl_injuries` | none (only tests asserting it's *not* used, plus a comment) | **needs provider forensics** | Endpoint 404s for every real athlete tested (Stage 2 plan §8.1), and it converts that 404 into "No injury history found". It is dangerous to reuse as written, but whether a working per-athlete injury route exists is unknown. Resolve by forensics, then delete or fix. Do not revive casually. |
| `get_nfl_team` | none | **dead and safe to remove** | Unused `$ref` → team-name resolver; `dynasty_core.espn` already owns team mapping for active NFL. |
| `get_draft_prospect` | none | **dormant but intentional** | Coherent part of Scout's draft-object surface (same cached draft roster that `search_draft_prospects` and `get_espn_athlete_id` use). No network cost of its own. A deliberate Scout API decision, not an extraction concern. |

Related, from the Phase 4C dead-code map: item S2 (Scout's unused
`dynasty_core/sleeper.py`) is **resolved** by Batch 6. S3 (Scout ships
`dynasty_core/espn.py` but never imports it) remains true but becomes moot
once the package is installed: shipping an unused module in a dependency
costs nothing.

---

## 7. Packaging / deployment constraints

| Fact | Ddreportcards | scoutcap |
|---|---|---|
| Python | `runtime.txt` `python-3.12`, `.python-version` `3.12` | `runtime.txt` `python-3.12`, `.python-version` `3.12` |
| Install | `pip install -r requirements.txt` (Render `buildCommand`) | `pip install -r requirements.txt` |
| Requirements | 8 direct deps, all exact `==` pins (Phase 4B); `httpx==0.28.1` | 7 direct deps, **none pinned** (`google-adk[extensions]`, `anthropic`, `httpx`, `python-dotenv`, `streamlit`, `pandas`, `mcp[cli]`) |
| Deploy | Render web service (`render.yaml`, `uvicorn api:app`), auto-deploys from this branch | no deploy config in repo; README says "Deployed live at: *[URL TBD]*"; MCP config points at a local Windows path — i.e. local/Streamlit, not a managed deploy |
| Pin enforcement | `tests/test_requirements_are_pinned.py` + `scripts/check_dependency_versions.py` | none |

Observations:

- **Python compatibility.** All three modules use PEP 604 unions under
  `from __future__ import annotations`; nothing newer is used. Both apps
  target 3.12; both offline suites pass on 3.11.15 in this sandbox. A
  package `requires-python = ">=3.11"`, with 3.12 as the tested
  deployment target, is supported by evidence.
- **Package runtime dependencies:** exactly `httpx`. It belongs in package
  metadata as a compatible **range** (e.g. `httpx>=0.28,<1`), while each
  app keeps its own **exact** pin. That is standard library-vs-application
  practice and leaves Ddreportcards' pinned environment unchanged as long
  as the range admits `0.28.1`.
- **Scout reproducibility concern: real.** With nothing pinned, a green
  Scout suite against a package candidate today is not reproducible
  tomorrow, and it cannot serve as a trustworthy release gate.
- **Interaction with Ddreportcards' pin contract: known conflict.** A Git
  dependency line (`dynasty-core @ git+https://…@<sha>`) would fail two of
  the three tests in `test_requirements_are_pinned.py`: every line must
  match `name[extras]==version`, and the direct-dependency set must equal
  exactly the current eight. `scripts/check_dependency_versions.py`'s
  `_PIN_PATTERN` has the same limit. The packaging batch must deliberately
  extend the contract to accept exactly one form of immutable reference (a
  full 40-hex commit SHA) and to keep rejecting branches or tags. Doing it
  by accident would erode the Phase 4B guarantee.
- **Credentials:** `Ddreportcards`, `scoutcap` and `gm-command` are all
  **public** GitHub repos. A public `dynasty-core` repo installs over
  `git+https` with no tokens on Render or locally. A private one would
  need a token in Render's build environment.

---

## 8. Distribution options

| | Single source of truth | Reproducibility | Deploy complexity | Rollback / versioning | Credentials (repos are public) | Local Claude workflow | Drift risk |
|---|---|---|---|---|---|---|---|
| **A. Dedicated repo + Git tag** | strong | **weak alone** — tags are mutable (can be force-moved) | low | tag per release | none if public | cross-repo edits; add repo to session | low |
| **B. Dedicated repo + exact commit pin** | strong | **strong** — a SHA is immutable | low; needs `git` at build time (to be verified on Render in the first batch) | roll back = revert the pin line; tags can still label releases for humans | none if public | cross-repo edits; `pip install -e ../dynasty-core` for local iteration | low |
| **C. Package registry (PyPI)** | strong | strong (immutable versions) | medium — build/upload ceremony per release | clean semver | public index namespace, or private-index credentials | slowest inner loop | low |
| **D. One app repo owns the package; other installs a subdirectory** | medium — the package's history is tangled with one app's deploy history | strong if SHA-pinned | low for owner, awkward for consumer | every owner-app commit is a potential package "version" | none if public | asymmetric; the owner app's Render auto-deploy fires on package-only edits | medium |
| **E. Synchronized physical copies (status quo)** | **weak** — two copies plus a manual process | per-repo only | none | none | none | easy to edit, easy to forget one side | **high** — this already happened (Phase 4C S2, and the Stage 2 plan exists because of it) |
| **F. Monorepo / submodule** | strong | strong (submodule pins a SHA) | submodules add clone/deploy friction; a monorepo would merge two independent deploy units | per-commit | none | submodules are error-prone for the manual, cross-repo workflow used here | low |

**Recommendation: B, with A as the human-readable label.** A new public
`dynasty-core` repository. Each app pins
`dynasty-core @ git+https://github.com/<owner>/dynasty-core@<40-hex SHA>`,
and releases are also tagged (`v0.1.0`, …) for readability. The SHA, not the
tag, is what gets pinned. This is the only option that is simultaneously
single-source, immutable, credential-free here, and cheap to operate, and
it composes with Ddreportcards' Phase 4B pin discipline once that contract
is extended (§7). C buys nothing extra for two private consumers with a
public repo. D couples package history to one app's deploys. E is the
problem being solved. There is no evidence that F's costs are worth it.

---

## 9. Tests and release contract

### 9.1 Inventory of shared-behavior tests

| Test file | Tests | Kind | Location |
|---|---|---|---|
| `test_dynasty_core_get_player.py` | 5 | provider contract + cache | **byte-identical in both repos** |
| `test_dynasty_core_player_cache_ttl.py` | 6 | cache lifecycle | byte-identical in both |
| `test_dynasty_core_sleeper_user_league_primitives.py` | 10 | provider contract | byte-identical in both |
| `test_dynasty_core_trending.py` | 10 | provider contract + wrapper | byte-identical in both |
| `test_tools_sleeper_facade.py` (scoutcap) | 15 | facade identity + Scout policy + mocked cross-module consumer runs | app-owned |
| `test_dynasty_core_source_ownership.py` (DD) | 7 | module-ownership boundary for DD agents | app-owned |
| `test_production_agent_missing_espn_id.py` (DD) | 11 | agent guard (mocks `espn_client`) | app-owned |

Coverage gaps inside the package itself: **no provider-contract tests at
all for `dynasty_core.fantasycalc`**, including its per-parameter cache key
and the condensed-index invalidation, which the code's own comment
documents as a past silent-wrong-answer bug. **No tests for
`dynasty_core.espn`** (`flatten_statistics`, `team_espn_id` alias mapping,
injury-note paging). None for the remaining Sleeper primitives
(`get_league_users/rosters/info/drafts`, `get_traded_picks`,
`get_league_season_chain`, `get_transactions`, `get_all_trades`). Today the
31 shared tests are run twice, once per host repo, against two copies of the
code. That proves the copies are identical, not that the package is
correct.

### 9.2 What moves with the package vs stays

- **Move into the package repo:** the four `test_dynasty_core_*` files
  (deleted from both apps once the pin lands), plus new provider-contract
  tests for FantasyCalc (URL/params, cache key per parameter set, index
  invalidation, `index_picks_by_label` filtering, `value_grade` tiers) and
  ESPN (`flatten_statistics`, `team_espn_id` aliases, injury-note paging),
  plus URL/params tests for the untested Sleeper primitives.
- **New package test: import isolation** — import the package from a clean
  directory with (a) no `config`, (b) a foreign `config`, and (c) a
  hostile `config.dynasty_config`, and assert the defaults are identical and
  no import fails. This test turns §2.2's finding into a permanent
  regression guard.
- **Stay app-owned:** `test_tools_sleeper_facade.py`, DD's
  source-ownership and agent-guard tests, and every agent/UI test. They
  test each app's *use* of the package.
- **Cross-app integration:** each app's full offline suite run against a
  candidate SHA, plus Ddreportcards' free `/health` + `/health/deep`
  smoke after deploy. The billable `/report/player` smoke is reserved for
  changes that alter data a report card depends on.

### 9.3 Minimum release gate for a package version

1. Package suite green on Python 3.12 (and 3.11, which is free to add),
   including the import-isolation test.
2. Ddreportcards full offline suite green with the candidate SHA installed.
3. scoutcap full offline suite green with the candidate SHA installed —
   meaningful as a gate only once Scout's requirements are pinned (§7).
4. Tag the SHA; then move each app's pin in its own commit. After the DD
   deploy, run `/health` + `/health/deep`.

---

## 10. Decision table

### BLOCKER BEFORE PACKAGING

| # | Item | Evidence | Affected | Recommended action | Why a blocker |
|---|---|---|---|---|---|
| B1 | FantasyCalc defaults come from an ambient host module | §2.2: a different ambient `config.dynasty_config` silently changed `ppr/num_qbs/num_teams`; a malformed one crashes at import | `dynasty_core/fantasycalc.py` (both); `config/dynasty_config.py` (both) | Remove the `config.dynasty_config` import. Make league parameters an explicit package contract: a package-owned league-settings module whose values are the documented defaults, imported **inside** the package, with callers still able to pass explicit arguments. Scout's `config/dynasty_config.py`, whose only reader is this import, becomes deletable. DD's becomes a re-export of the package's settings (or stays app-owned for `league_id`/display name — see Q5). Add the import-isolation test. | Once installed, the package's behavior must be a function of the package version. Today it is a function of whatever the host has on `sys.path`. |

### FIX BEFORE PACKAGING

| # | Item | Evidence | Affected | Recommended action | Why here (not deferred) |
|---|---|---|---|---|---|
| F1 | Ddreportcards-only workflows inside the shared namespace | §5: `get_all_trades_all_seasons`, `get_roster_by_display_name`, `resolve_roster_players` are called only by DD and encode DD identity/annotation policy | `dynasty_core/sleeper.py`; DD `data/sleeper_client.py`, `api.py`, `app.py`, `data/trade_history.py` | Move them into a Ddreportcards-owned module. Re-point DD's facade and `api.py`'s direct import; callers otherwise unchanged. Keep `get_league_season_chain`. | Whatever ships in v1 becomes the versioned contract. Removing them later is a breaking package release; removing them now is a local DD refactor. |
| F2 | Package-level tests missing for FantasyCalc and ESPN | §9.1 | new package test suite | Write the FantasyCalc and ESPN provider-contract and cache tests (and the Sleeper primitive URL tests) **before** extraction, run them against the current copies, then move them with the package. | Extraction must not be the first time the package's behavior is specified. The FantasyCalc cache has a documented history of silent wrong answers. |
| F3 | Ddreportcards pin contract can't express a Git dependency | §7: `test_requirements_are_pinned.py` and `check_dependency_versions.py` accept only `==` pins and exactly 8 names | DD `tests/test_requirements_are_pinned.py`, `scripts/check_dependency_versions.py` | Extend both, deliberately, to accept one additional form: `dynasty-core @ git+https://…@<40-hex SHA>`. Reject branch names and tags. | Otherwise the first packaging commit either fails CI or quietly weakens the Phase 4B guarantee. |
| F4 | Scout requirements unpinned | §7 | scoutcap `requirements.txt` | Pin the direct dependencies to known-good versions (the same method as DD's Phase 4B). | The release gate (§9.3 step 3) is not reproducible without it. |
| F5 | Scout `_load_pick_arsenal` Sleeper bypass | §4.1 | scoutcap `app.py` 854–866 | Replace the two direct calls with `get_users_in_league` / a newly facade-exported `get_traded_picks`. Keep `season` as an explicit argument (the caller already passes `"2026"`). Do **not** change the year policy in this batch. | Small. It leaves exactly one Sleeper HTTP implementation per app before the pin is introduced, so the version pin governs *all* Sleeper behavior in both apps. It also stops a non-2xx body being parsed as data. |

### SAFE TO DEFER

| # | Item | Evidence | Affected | Recommended action | Why deferrable |
|---|---|---|---|---|---|
| D1 | Scout 2026 season literals scattered (~15) | §4.1 | scoutcap `app.py`, agents, `mcp_server.py`, `tools/espn.py` | When Scout rolls to the 2027 class, centralize into one Scout-owned draft-season setting. | Intentional product scope; app-owned; not a package concern. |
| D2 | Duplicate `GRADE_TIERS` / `_value_grade` in DD situation agent | §3.2 note | DD `agents/situation_agent.py` | Use shared `value_grade` via the facade. | Identical values today; no package-boundary impact. |
| D3 | Arsenal hardcodes 4 rounds × 12 teams; error dict silently swallowed | §4.1 | scoutcap `app.py` | Read from `config/league.py`; surface `error` in the UI. | App UX/config hygiene. |
| D4 | League identity has four sources | `api.py` → `config/dynasty_config`; DD Streamlit → env `SLEEPER_LEAGUE_ID`/`ROSTER_OWNER_ID` (`.env.example` says `BCNH`, code defaults to `TitansTrev55`); Scout → env/Streamlit secrets; GM Command → `dynasty_config.json` | all three repos | Decide after B1 whether identity (league id, owner) joins the package settings or stays deployment configuration. Add a drift check for the GM Command JSON mirror either way. | Identity is deployment configuration, not provider behavior; B1 only has to fix the FantasyCalc parameters. |
| D5 | `get_event_log`, `get_leagues`, `get_player`, `TEAM_*` exported but uncalled | §3.2 | package | Keep. They're coherent provider primitives; revisit at 1.0. | Unused-but-correct primitives cost nothing. |

### REMOVE / RETIRE BEFORE PACKAGING

| # | Item | Evidence | Affected | Recommended action | Why here |
|---|---|---|---|---|---|
| R1 | Scout `config/dynasty_config.py` | §2.2: sole importer is the fantasycalc import that B1 removes | scoutcap | Delete in the same batch as B1. | Otherwise it's a fourth copy of league settings with no reader. |
| R2 | Per-app copies of `dynasty_core/` and of the four `test_dynasty_core_*` files | §1, §9 | both | Delete when the pin lands (the extraction batch itself). | Leaving them turns the pin into a fifth source of truth. |

Deliberately **not** in this category: `tools/espn.py::get_nfl_team` is dead
and safe to remove, and `get_nfl_injuries` needs forensics (§6). Both are
Scout app code outside the package, so they're independent of extraction.

---

## 11. Proposed implementation batches (not executed)

1. **2C-1 — Package test baseline (F2).** Add FantasyCalc, ESPN and
   remaining-Sleeper provider-contract/cache tests plus the
   import-isolation test to both repos' copies. They must pass against the
   current code, except the isolation test, which is expected to fail and
   documents B1. No runtime change.
2. **2C-2 — League-settings contract (B1 + R1).** Package-owned settings
   module, remove the ambient import, isolation test goes green, delete
   Scout's orphan config, DD config re-exports or stays app-owned per the
   Q5 decision. Byte-identical in both copies.
3. **2C-3 — Boundary trim (F1).** Move the three DD workflows into
   Ddreportcards; re-point the facade and `api.py`.
4. **2C-4 — Scout arsenal bypass (F5).** Swap in shared primitives; the
   season stays an explicit argument.
5. **2C-5 — Scout pins (F4).**
6. **2C-6 — Pin-contract extension (F3)**, landed with a test fixture line
   before any real Git dependency exists.
7. **2C-7 — Extraction (R2).** Create `dynasty-core` from the then-identical
   copy with `pyproject.toml` (`httpx` range, `requires-python >=3.11`);
   move the package tests there; tag `v0.1.0`; pin the SHA in Scout first
   (no deploy), then Ddreportcards (Render verifies `git` at build);
   delete both local copies; DD health smoke.

Deferred items D1–D5 follow at leisure.

---

## 12. Direct answers

1. **Is the Sleeper consolidation itself complete?** Yes. Both apps route
   all Sleeper *tool/provider-layer* access through the byte-identical
   `dynasty_core.sleeper`: Ddreportcards has no direct Sleeper HTTP at all,
   and Scout's `tools/sleeper.py` is a pure facade (Batch 6). The only
   remaining direct Sleeper HTTP is one Scout UI helper, `_load_pick_arsenal`
   (F5). That is app-layer debt, not an incomplete provider consolidation.
2. **Is `dynasty_core` clean enough to extract unchanged?** No. The code is
   identical and depends only on `httpx`, but FantasyCalc's defaults are
   decided by an ambient host module (B1). Its package-level test coverage
   is also Sleeper-only (F2).
3. **What must change before extraction?** B1 is required. F1–F5 should
   also land before extraction; §10 gives the reasons.
4. **What should the canonical package contain?** `sleeper`, `espn` and
   `fantasycalc` provider primitives and provider-shape transforms
   (including `index_picks_by_label` and `get_league_season_chain`), a
   package-owned league-settings module (B1), and the package test suite.
   It should **not** contain the three Ddreportcards workflows (F1).
   Scout's draft/college ESPN code stays in Scout.
5. **Where should canonical league configuration live?** The FantasyCalc
   query parameters (`ppr`, `num_qbs`, `num_teams`, and whether the league
   is dynasty) belong **inside the package**, as an explicit settings
   module. The package is already single-league by design (its grade tiers
   are calibrated to this league), and this removes two of the three
   hand-mirrored copies. Deployment identity (league id, owner display name)
   is better treated as app configuration until D4 is decided. This split
   is a recommendation for review, not a settled fact. The alternative is to
   make every FantasyCalc call take explicit parameters with no defaults.
   That is purer, but it touches every call site in both apps.
6. **Which distribution mechanism fits best?** Option B: a dedicated public
   `dynasty-core` repo, installed by exact commit SHA, with tags only as
   human labels (§8).
7. **Which residual direct-provider paths should be fixed before extraction,
   and which deferred?** Before: Scout `_load_pick_arsenal`'s two Sleeper
   calls (F5). Deferred, because they are app-owned provider integrations or
   out of scope: Scout `tools/espn.py` (draft/college, by design; with
   `get_nfl_injuries` forensics and `get_nfl_team` removal tracked
   separately), DD `budget.py` (Upstash, unrelated), and GM Command's
   browser-side Sleeper calls (JavaScript).

---

## Verification

After writing this document, both repos' offline suites were re-run
unchanged: Ddreportcards 160 tests OK, scoutcap 79 tests OK. No runtime file,
requirement or test was modified in either repo.

---

## Review amendments (accepted with the audit)

1. **Settings scope narrowed.** For B1 the package owns a *FantasyCalc
   query profile* (`is_dynasty`, `num_qbs`, `num_teams`, `ppr`), e.g. in
   `dynasty_core/settings.py`. It does **not** own league identity: league
   id, owner/display name, draft year and roster identity stay app-owned.
   This supersedes the broader wording in §12 Q4/Q5.
2. **`requires-python = ">=3.12"`**, not `>=3.11`. Both consumers are
   deliberately on 3.12, and there's no reason to widen the support
   contract during extraction. This supersedes the §7 observation and
   the 2C-7 note.
3. **No committed red tests.** 2C-1 adds only passing tests and runs the B1
   isolation probe out of band. The permanent import-isolation regression
   test lands in 2C-2 alongside the fix, green immediately.

---

## 2C-1 completion — package behavioral test baseline

**Tests only.** No `dynasty_core/*.py` implementation file, config,
requirement, or other runtime file changed in either repo. All four
implementation SHAs are identical to §1.

### Tests added (byte-identical in both repos)

| File | Tests | Covers | SHA-256 |
|---|---|---|---|
| `tests/test_dynasty_core_fantasycalc.py` | 18 | request contract (URL, `isDynasty`/`numQbs`/`numTeams`/`ppr`, timeout 20, `raise_for_status`, passthrough); parameter-keyed cache (SF vs 1QB never share an entry; same key inside TTL = no refetch; expired = refetch; failed refresh keeps the previous good entry); condensed-index invalidation (reused while default values unchanged; dropped when the default parameter set refreshes; untouched when a non-default set refreshes); `index_by_sleeper_id`, `index_by_sleeper_id_with_redraft_rank`, `index_picks_by_label`, `get_value_for_sleeper_id`, `get_player_value`, `value_grade`, `GRADE_TIERS` | `d08ed06f36e534b20403a54bf7109066db75230ee7fc65336c86b02f566ef787` |
| `tests/test_dynasty_core_espn.py` | 16 | `team_espn_id` (normal, `WAS`→`WSH`, unknown→`None`, 32-team map); `flatten_statistics` exact `name→value` contract and its difference from Scout's college transform; season/career stat URLs, timeout, 400/404→`{}`, other errors raise; `get_player_injury_notes` athlete filtering (incl. id-prefix collision), dereference-only-matches, feed pagination, empty result, feed error; `fantasy_relevant_stats` | `ca64d9ebe4c6361f2880bb19e5a879478ff7ae47b003448947141b4a910fb1c3` |
| `tests/test_dynasty_core_sleeper_league_primitives.py` | 7 | URL/timeout/passthrough/non-2xx for `get_league_users`, `get_league_rosters`, `get_league_info`, `get_traded_picks`, `get_league_drafts`, `get_transactions`; `get_league_season_chain` (newest-first walk, empty-string stop, cycle guard); `get_all_trades` (weeks 1–18, completed trades only, dedupe by `transaction_id`) | `d21df9c9380501c9cba66db22e4867ab415d05c91db19a4c2cad40b5170079de` |

Deliberately **not** tested: `get_all_trades_all_seasons`,
`get_roster_by_display_name`, `resolve_roster_players` (they leave the
package in 2C-3, F1).

Totals: Ddreportcards 160 → **201**, scoutcap 79 → **120**, all green.
Shared `dynasty_core` behavioral tests now number 72 (31 existing + 41 new),
in 7 byte-identical files per repo.

### B1 fail-first probe (run out of band, not committed)

A copy of the unchanged package (`fantasycalc.py` SHA `b3f92084…`) was
imported from isolated directories:

| Scenario | Result |
|---|---|
| A. no `config` package | imported; `ppr=0.5 num_qbs=2 num_teams=12` (hardcoded fallback) |
| B. unrelated `config` package | imported; `ppr=0.5 num_qbs=2 num_teams=12` (hardcoded fallback) |
| C. hostile `config.dynasty_config` (1QB, 10 teams, 1.0 PPR) | imported; `ppr=1.0 num_qbs=1 num_teams=10` — **defaults silently changed** |
| D. malformed `config.dynasty_config` (no `ppr`) | **`KeyError: 'ppr'` — import fails** |

This matches §2.2 exactly. The B1 decision is unchanged. The permanent
isolation regression test is intentionally deferred to 2C-2, so that every
committed checkpoint stays green.

### New observations while specifying FantasyCalc (not acted on, not encoded as contract)

- **O1 — FIXED in 2C-1.5** (see the 2C-1.5 section below). Original
  finding, kept for the record: the condensed index never checked the TTL.
  - What happens: `_build_condensed_index()` returns `_index_cache` as soon
    as it is set, without calling `get_dynasty_values()`. So
    `get_player_value()` keeps serving the first-built index until something
    else refetches the *default* parameter set.
  - Who is affected: Scout reads FantasyCalc only through
    `get_player_value`/`value_grade` and never calls `get_dynasty_values()`
    itself. So in a long-lived Scout process (Streamlit, MCP server),
    FantasyCalc values never refresh after first load. Ddreportcards doesn't
    call `get_player_value`, so it's unaffected.
  - Handled as its own correctness interlude (2C-1.5), separately from B1.
- **O2 — OPEN. Classification: pre-extraction cleanup candidate; no
  demonstrated current correctness bug.**
  `index_by_sleeper_id_with_redraft_rank()` writes `redraftPositionRank`
  into the entry dicts it's given. When the caller passes the cached
  `get_dynasty_values()` list, as Ddreportcards' situation agent (its only
  active caller) does, that mutates the shared cache. It's harmless today
  because every caller derives the same field the same way. Hidden
  mutation is still undesirable package behavior, so clean it up before
  extraction.

O2 is not asserted by any test, so fixing it later won't fight the baseline.

---

## 2C-1.5 completion — FantasyCalc condensed-index freshness (O1)

**Fix.** `_build_condensed_index()` now calls `get_dynasty_values()` first,
on every call, and only then reuses `_index_cache`. `_values_cache` +
`_VALUES_TTL_SECONDS` stays the single freshness authority:
- While the default values are fresh, the call is a cache hit (no request)
  and the existing index is reused.
- When the default entry expires and refreshes successfully,
  `get_dynasty_values()` already clears `_index_cache`, so the index rebuilds
  from the new payload.
- If the refresh fails, the provider error propagates, and both the old
  `_values_cache` entry and the old `_index_cache` stay intact. No stale
  data is served silently.

No new timer, TTL, network layer, or refresh mode was added. B1 (ambient
config) and O2 are untouched.

**Tests** (added to `tests/test_dynasty_core_fantasycalc.py`, byte-identical
in both repos): five cases through `get_player_value()` —
1. the first call fetches and builds the index;
2. a repeat inside the TTL runs the freshness path with no extra request;
3. after the TTL there is exactly one new request, the index rebuilds, and
   the new value is returned;
4. a failed refresh after the TTL raises and preserves both caches;
5. a non-default parameter-set refresh doesn't rebuild the default index.

Against the pre-fix implementation, cases 2, 3 and 4 fail: no freshness
check, no refetch, and stale data served silently instead of raising.
Cases 1 and 5 guard unchanged behavior.

**Scout consumer check** (mocked Sleeper and FantasyCalc, moving clock, not
committed):
- Before the TTL, MCP `dynasty_value` and situation
  `assess_veteran_competition` resolved through the real `get_player_value`
  with one FantasyCalc request.
- After the TTL, the next call made exactly one new request and returned
  the refreshed payload.
- Streamlit `AppTest` of `app.py` raised no exceptions.

Totals: Ddreportcards 201 → **206**, scoutcap 120 → **125**, all green.
