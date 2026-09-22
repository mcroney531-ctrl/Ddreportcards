# Stage 2A — Shared `dynasty_core` Contract Plan

**Revision 2** — amended after review. No runtime code has been changed by
this document at any revision. This is the function-level capability matrix
and proposed target API requested before any migration work begins, per the
reframing of the Phase 4C `dynasty_core` finding: this is a half-finished
consolidation with two different API surfaces, not two implementations
fighting each other, and it should be resolved as a deliberate architecture
decision, not a symmetry-driven file merge.

**What changed in this revision** (see the review that produced each):
1. Corrected the ESPN classification — `search_draft_prospects`,
   `get_espn_athlete_id`, and `get_draft_prospect` hit `NFL_BASE`'s
   draft-athlete endpoints, not `CFB_BASE`. Only `get_college_stats` is
   actually college-football. They're still Scout-specific, but because
   they belong to the NFL Draft object model, not because they're all CFB.
2. Retracted the claim that `flatten_statistics` is a safe drop-in for
   Scout's college-stats transform — the two have observably different
   output schemas (see §4.4). Removed from the migration batches.
3. Withdrew the recommendation to promote Scout's `get_player()` network
   call as written — its endpoint isn't confirmed to be a documented,
   supported Sleeper route, and the function has zero callers today. If
   shared core gains a `get_player`, it should be a lookup over the cached
   full-player map, not a new network dependency.
4. Reframed the 6h player-map cache as a decision to make explicitly, not
   something to auto-inherit — Sleeper's own guidance is to fetch the full
   map sparingly (documented as no more than about once a day).
5. Added an explicit compatibility-wrapper requirement for
   `get_trending_adds` during the `get_trending` generalization.
6. Flagged the Ddreportcards-only composed workflows
   (`get_all_trades_all_seasons`, `get_roster_by_display_name`,
   `resolve_roster_players`) as application/domain composition living
   inside a provider module, for a later boundary decision — not moved now.
7. Changed the packaging conclusion: one installable shared package is the
   intended Stage 2 end state, not a permanently-acceptable alternative to
   converged physical copies. Converge first, package second, but package.
8. Expanded the first migration batch into two required live verifications
   (the ESPN injury-endpoint disagreement, and whether Sleeper's
   single-player route actually exists/is supported) — reported before any
   migration proceeds, not assumed from reading the code.

**Repos/files covered:**
`Ddreportcards/dynasty_core/{sleeper,espn}.py`,
`scoutcap/dynasty_core/{sleeper,espn}.py` (byte-identical to Ddreportcards'
copies, confirmed by `sha256sum` in Phase 4C), `scoutcap/tools/{sleeper,espn}.py`,
and `dynasty_core/fantasycalc.py` + `scoutcap/tools/fantasycalc.py` as the
reference pattern.

---

## 1. Sleeper capability matrix

### `Ddreportcards dynasty_core.sleeper`

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `get_league_users(league_id)` | `picks.py`, `api.py`, `data/trade_history.py` (via `data/sleeper_client.py` re-export) | `GET /league/{id}/users` | Full user list for one league | None | `raise_for_status()` — propagates httpx errors | COMMON (same endpoint as scoutcap's `get_users_in_league`) |
| `get_league_rosters(league_id)` | `picks.py`, `api.py`, `data/trade_history.py` | `GET /league/{id}/rosters` | Full roster list | None | `raise_for_status()` | COMMON (same endpoint as scoutcap's `get_rosters`) |
| `get_league_info(league_id)` | `picks.py`, `api.py` | `GET /league/{id}` | Single league object | None | `raise_for_status()` | DDREPORTCARDS-SPECIFIC today (scoutcap has no direct equivalent; used internally for season-chain walking) |
| `get_traded_picks(league_id)` | `picks.py` | `GET /league/{id}/traded_picks` | Moved-picks list | None | `raise_for_status()` | COMMON (same endpoint, same shape, as scoutcap's `get_traded_picks` — the two implementations don't disagree here) |
| `get_league_drafts(league_id)` | `picks.py` | `GET /league/{id}/drafts` | Draft objects, newest first | None | `raise_for_status()` | DDREPORTCARDS-SPECIFIC (needed for draft-pick inventory reconstruction; no Scout equivalent) |
| `get_league_season_chain(league_id)` | `data/trade_history.py` | Composed from `get_league_info` (walks `previous_league_id`) | Every season back to league founding, newest first | None (each hop re-fetches) | Propagates from `get_league_info` | DDREPORTCARDS-SPECIFIC (multi-season dynasty history; Scout evaluates one draft class at a time, no season-chain need) |
| `get_transactions(league_id, week)` | `data/sleeper_client.py` (internal, used by `get_all_trades`) | `GET /league/{id}/transactions/{week}` | One week's transactions | None | `raise_for_status()` | DDREPORTCARDS-SPECIFIC |
| `get_all_trades(league_id)` | `data/sleeper_client.py` (internal, used by `get_all_trades_all_seasons`) | Composed (scans weeks 1-18) | Deduped completed trades, one season | None | Propagates | DDREPORTCARDS-SPECIFIC |
| `get_all_trades_all_seasons(league_id)` | `data/trade_history.py` | Composed | Full trade history across the season chain | None | Propagates | **APPLICATION/DOMAIN COMPOSITION** — see note below |
| `get_all_players()` | `agents/production_agent.py`, `agents/situation_agent.py`, `api.py`, `data/trade_history.py` | `GET /players/nfl` (~14MB) | Full player dict keyed by `player_id` | **6h in-process TTL, module-level global — a policy choice to revisit, see §4.1** | `raise_for_status()` | COMMON in intent (same endpoint as scoutcap's `get_nfl_players`) but **semantics disagree — see §4.1** |
| `get_trending_adds(lookback_hours, limit)` | `agents/situation_agent.py`, `api.py` | `GET /players/nfl/trending/add` | Add-trend list only | None | `raise_for_status()` | COMMON in intent, narrower than scoutcap's `get_trending` — see §4.2. **Must survive as a compatibility wrapper if generalized, not be replaced at every call site in the same commit.** |
| `get_roster_by_display_name(league_id, display_name)` | `api.py`, `app.py` | Composed (`get_league_users` + `get_league_rosters`) | Resolve one roster by owner's display name | None | Raises `ValueError` on no match (not an HTTP error) | **APPLICATION/DOMAIN COMPOSITION** — see note below |
| `resolve_roster_players(roster)` | `api.py`, `app.py` | Composed (`get_all_players`) | Attach player metadata to a roster's `player_id` list | Inherits `get_all_players`' cache | Propagates | **APPLICATION/DOMAIN COMPOSITION** — see note below |

> **Note on the three functions marked APPLICATION/DOMAIN COMPOSITION:**
> `get_all_trades_all_seasons`, `get_roster_by_display_name`, and
> `resolve_roster_players` are not provider primitives — they're
> Ddreportcards-only workflows (trade-history assembly, owner-name
> resolution, roster enrichment) that happen to live inside the shared
> `dynasty_core.sleeper` module today. That's acceptable as a temporary
> state; it is not the target state. A later package-boundary pass should
> decide whether these belong in Ddreportcards' own application code
> instead of the shared provider module, since "provider primitive" and
> "one app's composed workflow" are different kinds of code with different
> reasons to change. **Not moved in this plan.**

### `scoutcap tools.sleeper`

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `get_user(username)` | `agents/synthesis_agent.py`, `app.py` | `GET /user/{username}` | Resolve a Sleeper username to a user object | None | `raise_for_status()` | SCOUTCAP-SPECIFIC (user/league discovery for onboarding a new league; Ddreportcards hardcodes its one league via `config/dynasty_config.py` and never needs this) |
| `get_leagues(user_id, season)` | **none found** | `GET /user/{id}/leagues/nfl/{season}` | List a user's leagues for a season | None | `raise_for_status()` | SCOUTCAP-SPECIFIC, and currently **unused** — zero callers anywhere in scoutcap outside its own definition |
| `get_rosters(league_id)` | `agents/synthesis_agent.py`, `app.py` | `GET /league/{id}/rosters` | Same endpoint as `dynasty_core.get_league_rosters` | None | `raise_for_status()` | COMMON (identical endpoint, different name) |
| `get_users_in_league(league_id)` | `agents/synthesis_agent.py` | `GET /league/{id}/users` | Same endpoint as `dynasty_core.get_league_users` | None | `raise_for_status()` | COMMON (identical endpoint, different name) |
| `get_nfl_players()` | `mcp_server.py`, `agents/situation_agent.py`, `agents/synthesis_agent.py`, `app.py` | `GET /players/nfl` | Full player dict | **None — despite a docstring claiming "cache locally after first fetch," there is no caching code.** Every call re-fetches ~5MB. | `raise_for_status()` | COMMON in intent, **semantics disagree — see §4.1** |
| `get_player(player_id)` | **none found** | `GET /players/nfl/{player_id}` | Single-player lookup, returns `None` on 404 | None | Returns `None` on 404 (only function in either Sleeper module that does this instead of raising) | SCOUTCAP-SPECIFIC, currently **unused**, and **the endpoint's documented/supported status is unverified — see §4.3. Do not promote this as written; see §5.** |
| `search_players(name)` | `mcp_server.py`, `agents/production_agent.py`, `agents/situation_agent.py`, `agents/synthesis_agent.py` | Composed (`get_nfl_players` + client-side filter) | Name search, filtered to QB/RB/WR/TE only | Inherits `get_nfl_players`' (non-existent) cache | Propagates | SCOUTCAP-SPECIFIC (Ddreportcards never searches by name — it always has a known `sleeper_id` from a roster) |
| `get_trending(type, sport, limit)` | `mcp_server.py`, `agents/situation_agent.py`, `agents/synthesis_agent.py` | `GET /players/{sport}/trending/{type}` | Generalized: any type (`add`/`drop`), any sport | None | `raise_for_status()` | COMMON in intent, broader than `dynasty_core`'s add-only, NFL-only version |
| `get_traded_picks(league_id)` | **none found** (the one grep hit was `dynasty_core/sleeper.py`, an unrelated name match, not a real caller — that module isn't imported anywhere in scoutcap) | `GET /league/{id}/traded_picks` | Same endpoint and shape as `dynasty_core`'s version | None | `raise_for_status()` | COMMON, and currently **unused in scoutcap** |

---

## 2. ESPN capability matrix

### `Ddreportcards dynasty_core.espn` (active-NFL layer)

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `team_espn_id(sleeper_team_abbr)` | `agents/production_agent.py` | Static lookup table (`TEAM_ESPN_IDS` + `TEAM_ABBR_ALIASES`) | Sleeper team abbreviation → ESPN team id | N/A (static dict) | Returns `None` on unknown abbreviation | COMMON primitive — both apps need Sleeper-abbr↔ESPN-id translation, though Scout doesn't currently call this specific function (its `get_nfl_team` resolves the other direction, ref→name) |
| `get_season_statistics(espn_athlete_id, season)` | `agents/production_agent.py`, `api.py` | `GET .../seasons/{season}/types/2/athletes/{id}/statistics` (`NFL_BASE`) | Single-season stats | None | Returns `{}` on 400/404, otherwise `raise_for_status()` | DDREPORTCARDS-SPECIFIC today (active NFL production grading) |
| `get_career_statistics(espn_athlete_id)` | `agents/production_agent.py` | `GET .../athletes/{id}/statistics` (`NFL_BASE`) | Career totals | None | Returns `{}` on 400/404 | DDREPORTCARDS-SPECIFIC today |
| `get_event_log(espn_athlete_id)` | Internal only (used by `get_recent_game_logs`) | `GET .../athletes/{id}/eventlog` (`NFL_BASE`) | Per-game refs | None | `raise_for_status()` | DDREPORTCARDS-SPECIFIC (internal helper, not called externally) |
| `flatten_statistics(stats_response)` | `agents/production_agent.py`, `api.py` | Pure transform, no network | Flattens ESPN's nested category/stat structure to `stat["name"] -> stat["value"]`, unfiltered | N/A | N/A | DDREPORTCARDS-SPECIFIC output shape. **Not a safe reuse for Scout's college transform — see §4.4. Do not merge these.** |
| `get_recent_game_logs(espn_athlete_id, limit, position)` | `api.py` | Composed (`get_event_log` + per-game dereference) | Recent per-game stat lines | None | Catches and degrades to `{"available": False}` rather than raising | DDREPORTCARDS-SPECIFIC |
| `fantasy_relevant_stats(flat, position)` | `api.py` | Pure transform | Drops always-zero fields by position | N/A | N/A | DDREPORTCARDS-SPECIFIC (position-keyed core-stat sets are built for active NFL fantasy scoring; Scout's `get_college_stats` has its own, differently-shaped filtering — see §4.4) |
| `get_player_injury_notes(espn_athlete_id, espn_team_id)` | `agents/production_agent.py` | Composed (`GET .../teams/{id}/injuries`, `NFL_BASE`, filtered by athlete) | Per-team injury feed, filtered to one athlete | None | Propagates | DDREPORTCARDS-SPECIFIC, and **semantically different from Scout's injury function — see §4.3** |
| `get_injury_detail(ref_url)` | Internal only | `GET {ref_url}` (dereferences a `$ref`) | Generic $ref resolver | None | `raise_for_status()` | COMMON primitive in spirit (ESPN's `$ref` pagination pattern recurs everywhere, including Scout's draft-athlete resolution, which reimplements it privately as `_get`), but not currently shared |

### `scoutcap tools.espn`

**Correction from the prior revision:** this module is not uniformly a
"draft/college" or "CFB_BASE" layer. Checked directly against the file's
own `NFL_BASE`/`CFB_BASE` constants and every function's actual URL:

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `search_draft_prospects(name, season)` | `mcp_server.py`, `agents/production_agent.py`, `agents/situation_agent.py` | Composed, over **`NFL_BASE`'s `/seasons/{season}/draft/athletes`** index — this is ESPN's NFL Draft object model, not college football | Name search over the cached draft-class index | **Module-level cache, built once per process via a 20-worker parallel fetch** | Individual athlete resolution failures are swallowed (`except: return None`) during index build | SCOUTCAP-SPECIFIC — legitimately so, but because it's the **NFL Draft object model**, a different resource type from active-roster NFL players, not because it's college football |
| `get_espn_athlete_id(draft_athlete_id, season)` | `agents/production_agent.py` | Cache lookup, falls back to **`GET NFL_BASE/seasons/{season}/draft/athletes/{id}`** + `$ref` dereference | Resolve a draft-specific id to the base ESPN athlete id | Uses the same draft-roster cache | Returns `None` on failure at any step | SCOUTCAP-SPECIFIC — NFL Draft object model, same correction as above |
| `get_draft_prospect(espn_athlete_id, season)` | **none found** | Cache lookup only (backed by `NFL_BASE`) | Draft capital + basic info | Uses the same draft-roster cache | Returns an `{"error": ...}` dict, never raises | SCOUTCAP-SPECIFIC — NFL Draft object model, currently **unused** |
| `get_college_stats(espn_athlete_id, season)` | `agents/production_agent.py` | `GET CFB_BASE/.../statistics/0` — **the only function in this file that is actually college-football** | College production stats, career or single-season; output keyed `"<category_abbr>_<stat_abbr>" -> stat["displayValue"]`, filtered to `rush`/`rec`/`gen`/`s` categories | None | Returns `{"error": ...}` dict on 404, `raise_for_status()` otherwise | SCOUTCAP-SPECIFIC — genuinely college-football, and the only function here that is |
| `get_nfl_injuries(espn_athlete_id)` | `agents/production_agent.py` | `GET NFL_BASE/athletes/{id}/injuries` (**direct per-athlete endpoint**) | Historical injury records for one athlete | None | Returns `{"injuries": [], "note": ...}` on 404 | **Same domain as `dynasty_core.get_player_injury_notes`, but a different ESPN endpoint entirely — see §4.3** |
| `get_nfl_team(team_ref)` | **none found** | `GET {team_ref}` (`NFL_BASE`) | Resolve a team `$ref` to name/abbreviation | None | `raise_for_status()` (via shared `_get`) | SCOUTCAP-SPECIFIC in current form, currently **unused**; conceptually the inverse of `dynasty_core.team_espn_id` (ref→name vs. abbr→id) |

**Revised architectural boundary for ESPN:**

```
shared ESPN core (dynasty_core.espn):
  generic active-NFL-roster primitives (stats, injuries, team map)
  generic provider mechanics (the $ref-dereference pattern) -- candidate
  for explicit promotion, since both this module's get_injury_detail and
  Scout's private _get()/_resolve_draft_athlete() do the same kind of
  ref-walking independently today

Scout ESPN facade (tools/espn.py):
  NFL Draft object model (search_draft_prospects, get_espn_athlete_id,
  get_draft_prospect) -- Scout-specific because it's a different resource
  type (draft-class athletes vs. active-roster athletes), not because of
  which ESPN sport code it uses
  college-football statistics (get_college_stats) -- Scout-specific because
  it is, genuinely, a different sport in ESPN's API
  Scout-specific composition/policy generally
```

This distinction matters for later provider-mechanics extraction: a shared
`$ref` resolver or generic NFL-athlete-endpoint helper could plausibly serve
both the active-roster and NFL-draft-athlete code, since both are `NFL_BASE`
resources. College football genuinely cannot share that helper without
crossing into a different ESPN sport entirely.

---

## 3. FantasyCalc — the reference pattern (already consolidated)

`dynasty_core.fantasycalc` already merges both call styles into one
implementation, per its own module docstring: *"Merges scoutcap/tools/fantasycalc.py
(grade logic + condensed index accessor) and ddreportcards/data/fantasycalc_client.py
(raw list interface + full caching)."*

| Function | Repo(s) that use it | Classification |
|---|---|---|
| `get_dynasty_values`, `index_by_sleeper_id`, `get_value_for_sleeper_id`, `index_by_sleeper_id_with_redraft_rank` | Ddreportcards (raw accessor style) | COMPATIBILITY FACADE consumer — Ddreportcards' `data/fantasycalc_client.py` re-exports exactly these |
| `get_player_value`, `value_grade`, `GRADE_TIERS` | scoutcap (condensed accessor style) | COMPATIBILITY FACADE consumer — scoutcap's `tools/fantasycalc.py` re-exports exactly these |
| `index_picks_by_label` | Ddreportcards only (`api.py`) | DDREPORTCARDS-SPECIFIC, lives in shared core anyway since it operates on the same `get_dynasty_values()` payload |

Both facades are a single `from dynasty_core.fantasycalc import (...)` block,
nothing else — zero divergent logic, zero re-implementation. This is the
target shape for Sleeper and ESPN's genuinely-common surface, not "delete
the app-specific file and import `dynasty_core` directly everywhere," which
would remove the layer that's allowed to hold app-specific policy (like
Scout's QB/RB/WR/TE-only filtering in `search_players`, which has no reason
to live in shared code).

---

## 4. Where implementations actually disagree, and what's unverified

1. **`get_all_players()` vs. `get_nfl_players()` — caching, and the TTL
   itself is a decision, not a given.**
   `dynasty_core.sleeper.get_all_players()` caches the ~14MB player dict for
   6 hours, module-level. `tools.sleeper.get_nfl_players()`'s docstring says
   *"Cache locally after first fetch"* but there is no caching code at all —
   every call re-fetches the full payload. That gap is a real bug to fix
   either way. But the 6h number itself should not be inherited by default:
   Sleeper's own guidance is to fetch the full player map sparingly — no
   more than about once a day is the documented recommendation. Stage 2
   needs to record an explicit decision here, not assume the existing
   Ddreportcards number is correct just because it already exists:
     - keep 6h for Ddreportcards' product-freshness reasons (worth writing
       down what those reasons actually are), or
     - move the shared cache toward a 24h TTL (or a manual/scheduled
       refresh) closer to Sleeper's stated guidance, with Ddreportcards
       opting into a shorter TTL only if there's a concrete reason it needs
       fresher data than Scout does.
   No behavior change happens in this documentation pass either way.

2. **`get_trending_adds()` vs. `get_trending()` — scope.**
   `dynasty_core`'s version is hardcoded to `type=add`, NFL only.
   `tools.sleeper`'s version generalizes both the trend type (`add`/`drop`)
   and the sport. A shared version should be the general one, with
   `dynasty_core` gaining the `type`/`sport` parameters — but
   `get_trending_adds` itself should be kept as a thin compatibility
   wrapper over the generalized function during migration, so Ddreportcards'
   existing call sites don't need to change in the same commit that adds
   the general form.

3. **`get_player_injury_notes()` vs. `get_nfl_injuries()` — different ESPN
   endpoints entirely. VERIFIED (Stage 2B Batch 1, §8): not complementary
   — `get_nfl_injuries`'s endpoint 404s for every athlete tested,
   functional or not.** See §8.1 for the full evidence and the resulting
   decision.

4. **`flatten_statistics()` vs. Scout's `get_college_stats()` transform —
   confirmed NOT interchangeable.**
   `dynasty_core.espn.flatten_statistics` produces `stat["name"] ->
   stat["value"]`, unfiltered. Scout's `get_college_stats` deliberately
   builds `"<category_abbreviation>_<stat_abbreviation>" ->
   stat["displayValue"]`, filtered to the `rush`/`rec`/`gen`/`s` categories
   only. These are observably different payload schemas — different key
   format, different value field, filtered vs. unfiltered. **This is not a
   safe dedupe.** Scout's college transform stays exactly as it is unless a
   separately-designed shared transform can be built that preserves its
   exact output contract, which is a real design task, not a batch item.

5. **Sleeper's single-player endpoint — existence/support checked. VERIFIED
   (Stage 2B Batch 1, §8): the route exists and responds, but its shape
   disagrees with the trusted full-map entry.** See §8.2. This reinforces,
   rather than changes, the recommendation not to promote it as-is.

---

## 5. Proposed Stage 2 target API

```
dynasty_core/
  sleeper.py
    # Existing dynasty_core functions, unchanged:
    get_league_users, get_league_rosters, get_league_info, get_traded_picks,
    get_league_drafts, get_transactions, get_all_trades

    # Kept here for now, but flagged (see §1's note) as
    # application/domain composition that a later boundary pass should
    # reconsider moving into Ddreportcards' own code:
    get_league_season_chain, get_all_trades_all_seasons,
    get_roster_by_display_name, resolve_roster_players

    # get_all_players: TTL is an explicit decision (§4.1), not carried over
    # by default. Whatever is decided, Scout's get_nfl_players becomes a
    # thin wrapper around this rather than staying its own uncached fetch.
    get_all_players

    # get_trending_adds stays as a compatibility wrapper over the
    # generalized form -- existing Ddreportcards call sites are untouched:
    get_trending(type="add", sport="nfl", lookback_hours=24, limit=25)
    def get_trending_adds(lookback_hours=24, limit=25):
        return get_trending(type="add", sport="nfl",
                             lookback_hours=lookback_hours, limit=limit)

    # New: promoted from tools/sleeper.py -- generic account/league
    # discovery, not Scout policy:
    get_user, get_leagues

    # New, but NOT a promotion of tools.sleeper's existing network call
    # (§4.5 -- that endpoint is unverified and has zero callers). If added,
    # implemented as a lookup over the cached map instead, introducing no
    # new provider dependency:
    def get_player(player_id):
        return get_all_players().get(str(player_id))

  espn.py  (active-NFL layer -- unchanged in scope)
    team_espn_id, get_season_statistics, get_career_statistics,
    get_event_log, flatten_statistics, get_recent_game_logs,
    fantasy_relevant_stats, get_player_injury_notes, get_injury_detail
    # get_player_injury_notes vs. get_nfl_injuries: HOLD, pending §4.3's
    # live verification. No merge, no delegation, until that's answered.

  fantasycalc.py  (unchanged -- already the target shape)

scoutcap/tools/
  sleeper.py
    # Thin facade, matching tools/fantasycalc.py's pattern:
    from dynasty_core.sleeper import get_user, get_leagues, get_trending, \
        get_all_players as get_nfl_players
    # get_player: only added here if §4.5's verification and the
    # cached-map implementation above are both settled first.

    # Stays here, Scout-specific policy (position filter is a choice, not
    # a Sleeper API fact):
    def search_players(name): ...  # composed from get_nfl_players()

    # Thin renames, low value -- may just get deleted in favor of calling
    # dynasty_core directly, or re-exported under existing names if
    # call-site churn isn't worth it:
    get_rosters -> dynasty_core.get_league_rosters
    get_users_in_league -> dynasty_core.get_league_users

  espn.py
    # Stays entirely Scout-owned. Reclassified, not moved: the NFL Draft
    # object model (search_draft_prospects, get_espn_athlete_id,
    # get_draft_prospect) is Scout-specific because it's a different
    # resource type from active-roster players, not because of sport code.
    # get_college_stats is Scout-specific because it's genuinely a
    # different sport. No change to any of these five functions.

    # get_nfl_injuries: HOLD, same as dynasty_core side -- no delegation
    # until §4.3 is verified.

    # get_nfl_team: no change, currently unused, no shared-core interaction
    # proposed.

  fantasycalc.py  (unchanged -- already correct)
```

---

## 6. Answers to the specific questions asked

**Which `tools/sleeper.py` functions should become wrappers around existing shared-core functions?**
`get_rosters` → `dynasty_core.get_league_rosters` (identical endpoint).
`get_users_in_league` → `dynasty_core.get_league_users` (identical
endpoint). `get_nfl_players` → `dynasty_core.get_all_players`, once the
cache-TTL decision in §4.1 is made (not automatically inheriting 6h).
`get_trending` is already the general form both sides should converge on.
`get_traded_picks` can be dropped entirely rather than wrapped — it has
zero callers in scoutcap today. `get_player` is **not** a candidate for a
thin wrapper around the existing network call — see the next answer.

**Which shared Sleeper functions are missing and should be added before migration?**
`get_user` and `get_leagues` don't exist in `dynasty_core.sleeper` yet, but
should be added there (they're generic account/league-discovery calls, not
Scout policy) before `tools/sleeper.py` can become a pure facade for them.
**Added in Stage 2B Batch 2.** Scout's existing `tools.sleeper.get_leagues`
hardcodes `season: str = "2025"`, which is already stale — that default is
not promoted into shared core; `dynasty_core.sleeper.get_leagues` requires
`season` explicitly, and no "current season" helper was introduced to paper
over that.
A single-player `get_player` is also worth adding, but **not** as a
promotion of Scout's existing `GET /players/nfl/{player_id}` call — that
endpoint's documented/supported status is unverified (§4.5) and the
function is currently unused, so there's no basis for blessing it as shared
contract. If added, it should be implemented deterministically as
`get_all_players().get(str(player_id))`, inheriting the common cache and
introducing no new provider dependency.

**Which Scout ESPN functions can delegate to shared NFL primitives?**
None with certainty yet. `get_nfl_injuries` is on `NFL_BASE` and looks like
it overlaps with `get_player_injury_notes`, but per §4.3 that needs
live-data verification before treating it as a safe delegation — the
two endpoints are different enough (per-team feed vs. per-athlete history)
that assuming interchangeability from the names would be a mistake. The
NFL Draft object model functions (`search_draft_prospects`,
`get_espn_athlete_id`, `get_draft_prospect`) don't delegate to anything in
`dynasty_core.espn` today because there's no active-roster equivalent of a
draft-athlete object — but the underlying `$ref`-dereferencing mechanic
they share with `dynasty_core.get_injury_detail` is a real candidate for a
future shared *primitive* (not a shared *function*), once that mechanic is
extracted deliberately rather than assumed. **`flatten_statistics` is
explicitly NOT a candidate** — see §4.4; the prior revision of this plan
incorrectly claimed it was a safe reuse, and that claim is retracted.

**Which Scout ESPN functions must remain Scout-only because they are draft/college-specific?**
All five: `search_draft_prospects`, `get_espn_athlete_id`,
`get_draft_prospect`, `get_college_stats`, and `get_nfl_team` (currently
unused, but conceptually part of the same Scout-owned surface). Corrected
from the prior revision: only `get_college_stats` is actually
college-football (`CFB_BASE`). The three draft-athlete functions are on
`NFL_BASE`'s NFL Draft object model — still legitimately Scout-only, but
because a draft-class athlete is a different resource type from an
active-roster player, not because of the ESPN sport code. This matches
`dynasty_core/espn.py`'s own module docstring, which already states the
draft/college split was a deliberate decision.

**After migration, should scoutcap continue shipping a physical copy of `dynasty_core`, or should Stage 2 promote it to one installable shared package?**
The intended end state is one installable shared package — the
duplicate-physical-copy arrangement has already demonstrated drift risk
(this entire Stage 2 effort exists because of exactly that), so it isn't
being held out as an acceptable permanent alternative. The sequencing is
still converge-then-package, not package-then-converge: promoting to an
installable package before resolving §4's disagreements would version and
distribute the current inconsistency, making it harder to fix without a
release cycle. Packaging is deferred, not abandoned.

---

## 7. Proposed migration batches (not implemented in this pass)

Revised order — verification first, then additive changes, then the one
behavior-changing fix, then facade conversion, then packaging:

1. ~~Live-verify both open questions before anything else proceeds~~ —
   **DONE, Stage 2B Batch 1. See §8 for full findings.** Summary: the ESPN
   pair is not complementary — `get_nfl_injuries` 404s for every tested
   athlete and should not be merged with or delegated to from
   `get_player_injury_notes`; it looks like a live bug in scoutcap's
   `agents/production_agent.py` path, independent of Stage 2. The Sleeper
   single-player route exists and responds, but its shape disagrees with
   the full-map entry, reinforcing the cached-map-lookup recommendation
   already in this plan.
2. **Add `get_user`, `get_leagues` to `dynasty_core.sleeper`** — new
   functions, zero risk to existing callers in either app.
3. **Decide the player-map cache policy (§4.1)** — record the TTL decision
   explicitly (keep 6h with stated rationale, or move toward Sleeper's
   ~24h guidance) before anything depends on it.
4. **Add `get_player` to `dynasty_core.sleeper`** as a cached-map lookup
   (`get_all_players().get(str(player_id))`), not a new network call —
   contingent on batch 3 landing first, since it depends on the shared
   cache existing.
5. **Generalize `get_trending_adds` → `get_trending`**, keeping
   `get_trending_adds` as a compatibility wrapper so no Ddreportcards call
   site needs to change in this batch.
6. **Convert `tools/sleeper.py` into a pure facade**, once batches 2-5 are
   settled: `get_rosters`/`get_users_in_league` become thin
   renames-or-removals, `get_nfl_players` wraps `get_all_players`, and
   `get_traded_picks` is dropped (unused).
7. **Leave `tools/espn.py`'s `get_nfl_injuries` as-is for Stage 2 purposes**
   (no merge, no delegation — decided, §8.1) but **file it separately as a
   likely production bug**: it silently reports "no injury history found"
   for real, currently-injured players because its endpoint 404s. This is
   an application-correctness fix for whoever owns `agents/production_agent.py`,
   not a Stage 2 architecture task — flagging it here so it doesn't get
   lost, not scheduling it as a batch. No other function in `tools/espn.py`
   is touched. The NFL Draft object model and college-stats functions are
   not migration candidates at all, per §4.4 and §6.
8. **Revisit the installable-package question** only after 2-7 are done and
   both apps' test/smoke suites are green against the converged contract —
   with packaging as the intended destination, not an open question.

No batch touches Scout's draft/college ESPN functions beyond the
already-decided `get_nfl_injuries` finding in batch 7, and no batch reuses
`flatten_statistics` for Scout's college transform.

---

## 8. Stage 2B Batch 1 — Live Provider Verification Findings

Verification only, run against real ESPN and Sleeper responses. No runtime
code was changed to produce these findings — both checks were made as raw
HTTP calls replicating the exact URL patterns the two codebases already
use, from outside either app.

### 8.1 ESPN: `get_player_injury_notes` vs. `get_nfl_injuries`

Three real athletes tested, chosen live from Sleeper's own current
`injury_status` field rather than picked in advance — two Sleeper currently
marks `"Out"`, one with no current status:

| Athlete | Team | Sleeper `injury_status` | Team-feed path (`get_player_injury_notes`) | Athlete-history path (`get_nfl_injuries`) |
|---|---|---|---|---|
| Jauan Jennings | MIN | Out | **200** — 1 matching entry: `fantasyStatus="QUESTIONABLE"`, `type="Personal"`, `location="Other"`, `detail="Not Specified"`, `returnDate="2026-09-27"` | **404** |
| Sam Darnold | SEA | Out | **200** — 1 matching entry: `fantasyStatus="QUESTIONABLE"`, `type="Lower Body"`, `detail="Soreness"`, `side="Right"`, `returnDate="2026-09-27"` | **404** |
| Salvon Ahmed | CHI | (none) | **200** — 0 matching entries (consistent with no current injury) | **404** |

**The athlete-history endpoint (`NFL_BASE/athletes/{id}/injuries`) returned
404 for all three athletes**, including the two with real, populated
current entries on the team-feed path. This was tested with the exact same
`espn_athlete_id` values that worked correctly on the team-feed path for
the same request, so it isn't an id-resolution problem on this end.

**Answering the four original questions directly:**
1. Can the athlete endpoint replace the team-feed implementation? **No.**
   It returned no data for any tested athlete, including confirmed-injured
   ones.
2. Does the team feed contain current/status information absent from
   athlete history? **Yes** — team feed has real, structured entries;
   athlete-history has nothing to compare, since it never returns 200.
3. Does athlete history contain old injuries absent from the current team
   feed? **Unknown, and can't be determined from this data** — an endpoint
   that 404s for every real athlete never gets the chance to surface
   historical entries either.
4. Should both remain separate because they answer different questions?
   **Reframe: this isn't two valid endpoints answering different
   questions. One of them (`get_nfl_injuries`'s endpoint) does not appear
   to return usable data at all**, at least not via this URL pattern for
   real player ids.

**Architecture decision:** Do not merge, delegate, or treat these as
interchangeable. `get_player_injury_notes` remains the sole verified
functional ESPN injury path in this comparison — Sleeper itself already
supplies current injury/status information via a separate provider.
`tools.espn.get_nfl_injuries` is not a Stage 2 consolidation candidate —
it's a separate correctness problem.

**Separately, flagging a likely production bug, outside Stage 2's scope:**
`get_nfl_injuries` catches its 404 and returns
`{"espn_id": ..., "injuries": [], "note": "No injury history found"}` —
a well-formed, plausible-looking response that is indistinguishable from
"this player has a clean injury history." Since `agents/production_agent.py`
calls this function, Scout's production-grading agent is currently told
"no injury history" for players ESPN's own team-feed shows as actively
`QUESTIONABLE` with a specific injury type and expected return date. This
is worth its own fix independent of any Stage 2 architecture work — noted
here so it doesn't get lost, not scheduled as a Stage 2 batch.

**Caveat:** ESPN's Core API is undocumented and hidden; this result could
in principle reflect a wrong URL shape, a required parameter this plan
didn't try, or a route that's been deprecated/changed since
`tools/espn.py` was written, rather than "this was never valid." The
finding here is "returns 404 for real ids today, consistently, 3 for 3" —
enough to justify the architecture decision above, but a deeper
investigation (if the bug fix is picked up) should still check for
alternate request shapes before concluding the route is gone entirely.

### 8.2 Sleeper: single-player endpoint

Tested `GET /players/nfl/{player_id}` for one known-valid id (Jauan
Jennings, sleeper id `7049`, the same athlete from §8.1) and one
deliberately invalid id (`9999999999`), compared against that same
player's entry in the full `/players/nfl` map fetched in the same session.

| | Valid id (`7049`) | Invalid id (`9999999999`) |
|---|---|---|
| HTTP status | 200 | 404 |
| Body | A player object | `null` |
| Same field set as full-map entry? | **No** (`same_fields: false`) | N/A |
| Notable difference | `full_name` is null/absent on the single-player response; it's populated (`"Jauan Jennings"`) on the full-map entry for the same id | Clean `404` + `null` body, no anomaly |

**Conclusion:**
- **Route exists today** and behaves sensibly at the HTTP-status level (200
  for a real id, 404 for a fabricated one, no error-body weirdness on the
  miss).
- **Behavior is stable-looking, not anomalous**, for the two cases tested —
  but the response **shape genuinely disagrees** with the full-map entry
  for the same player, at minimum on `full_name`.
- **Still effectively undocumented/unsupported as a primary access
  pattern**: whether or not it's formally documented, its own data doesn't
  match the shape the rest of both codebases already trust (the full-map
  entry), so treating it as equivalent would be a mistake regardless of
  its documentation status.

**Architecture decision:** Unchanged from the prior revision, now backed by
live evidence rather than a documentation-based inference: do not promote
this network call into `dynasty_core`. If `get_player(player_id)` is added
to shared core, it must be `get_all_players().get(str(player_id))` — a
lookup over the cache both apps already trust — not a second, differently-shaped
provider call.

### 8.3 What this changes going forward

- Migration batch 1 (verification) is complete; batches 2-5 (additive
  Sleeper functions, cache-policy decision, cached-map `get_player`,
  `get_trending` generalization with a compatibility wrapper) are unblocked
  and can proceed in a future pass.
- Batch 7's `get_nfl_injuries` question is resolved as "leave alone,
  file separately as a bug" rather than left open.
- No change to the packaging conclusion (§6, §7 batch 8): still
  converge-then-package, unaffected by these findings.

Still no runtime code changed by this section. Ran the offline suite after
this documentation update: all 91 tests pass (unaffected, as expected for
a docs-only change).
