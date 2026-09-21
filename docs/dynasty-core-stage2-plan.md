# Stage 2A — Shared `dynasty_core` Contract Plan

No runtime code is changed by this document. This is the function-level
capability matrix and proposed target API requested before any migration
work begins, per the reframing of the Phase 4C `dynasty_core` finding: this
is a half-finished consolidation with two different API surfaces, not two
implementations fighting each other, and it should be resolved as a
deliberate architecture decision, not a symmetry-driven file merge.

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
| `get_all_trades_all_seasons(league_id)` | `data/trade_history.py` | Composed | Full trade history across the season chain | None | Propagates | DDREPORTCARDS-SPECIFIC |
| `get_all_players()` | `agents/production_agent.py`, `agents/situation_agent.py`, `api.py`, `data/trade_history.py` | `GET /players/nfl` (~14MB) | Full player dict keyed by `player_id` | **6h in-process TTL, module-level global** | `raise_for_status()` | COMMON in intent (same endpoint as scoutcap's `get_nfl_players`) but **semantics disagree — see §4** |
| `get_trending_adds(lookback_hours, limit)` | `agents/situation_agent.py`, `api.py` | `GET /players/nfl/trending/add` | Add-trend list only | None | `raise_for_status()` | COMMON in intent, narrower than scoutcap's `get_trending` — see §4 |
| `get_roster_by_display_name(league_id, display_name)` | `api.py`, `app.py` | Composed (`get_league_users` + `get_league_rosters`) | Resolve one roster by owner's display name | None | Raises `ValueError` on no match (not an HTTP error) | DDREPORTCARDS-SPECIFIC (Ddreportcards operates against one fixed league + a small set of known display names; Scout has no roster-resolution need) |
| `resolve_roster_players(roster)` | `api.py`, `app.py` | Composed (`get_all_players`) | Attach player metadata to a roster's `player_id` list | Inherits `get_all_players`' cache | Propagates | DDREPORTCARDS-SPECIFIC |

### `scoutcap tools.sleeper`

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `get_user(username)` | `agents/synthesis_agent.py`, `app.py` | `GET /user/{username}` | Resolve a Sleeper username to a user object | None | `raise_for_status()` | SCOUTCAP-SPECIFIC (user/league discovery for onboarding a new league; Ddreportcards hardcodes its one league via `config/dynasty_config.py` and never needs this) |
| `get_leagues(user_id, season)` | **none found** | `GET /user/{id}/leagues/nfl/{season}` | List a user's leagues for a season | None | `raise_for_status()` | SCOUTCAP-SPECIFIC, and currently **unused** — zero callers anywhere in scoutcap outside its own definition |
| `get_rosters(league_id)` | `agents/synthesis_agent.py`, `app.py` | `GET /league/{id}/rosters` | Same endpoint as `dynasty_core.get_league_rosters` | None | `raise_for_status()` | COMMON (identical endpoint, different name) |
| `get_users_in_league(league_id)` | `agents/synthesis_agent.py` | `GET /league/{id}/users` | Same endpoint as `dynasty_core.get_league_users` | None | `raise_for_status()` | COMMON (identical endpoint, different name) |
| `get_nfl_players()` | `mcp_server.py`, `agents/situation_agent.py`, `agents/synthesis_agent.py`, `app.py` | `GET /players/nfl` | Full player dict | **None — despite a docstring claiming "cache locally after first fetch," there is no caching code.** Every call re-fetches ~5MB. | `raise_for_status()` | COMMON in intent, **semantics disagree — see §4** |
| `get_player(player_id)` | **none found** | `GET /players/nfl/{player_id}` | Single-player lookup | None | Returns `None` on 404 (only function in either Sleeper module that does this instead of raising) | SCOUTCAP-SPECIFIC, and currently **unused** |
| `search_players(name)` | `mcp_server.py`, `agents/production_agent.py`, `agents/situation_agent.py`, `agents/synthesis_agent.py` | Composed (`get_nfl_players` + client-side filter) | Name search, filtered to QB/RB/WR/TE only | Inherits `get_nfl_players`' (non-existent) cache | Propagates | SCOUTCAP-SPECIFIC (Ddreportcards never searches by name — it always has a known `sleeper_id` from a roster) |
| `get_trending(type, sport, limit)` | `mcp_server.py`, `agents/situation_agent.py`, `agents/synthesis_agent.py` | `GET /players/{sport}/trending/{type}` | Generalized: any type (`add`/`drop`), any sport | None | `raise_for_status()` | COMMON in intent, broader than `dynasty_core`'s add-only, NFL-only version |
| `get_traded_picks(league_id)` | **none found** (the one grep hit was `dynasty_core/sleeper.py`, an unrelated name match, not a real caller — that module isn't imported anywhere in scoutcap) | `GET /league/{id}/traded_picks` | Same endpoint and shape as `dynasty_core`'s version | None | `raise_for_status()` | COMMON, and currently **unused in scoutcap** |

---

## 2. ESPN capability matrix

### `Ddreportcards dynasty_core.espn` (active-NFL layer)

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `team_espn_id(sleeper_team_abbr)` | `agents/production_agent.py` | Static lookup table (`TEAM_ESPN_IDS` + `TEAM_ABBR_ALIASES`) | Sleeper team abbreviation → ESPN team id | N/A (static dict) | Returns `None` on unknown abbreviation | COMMON primitive — both apps need Sleeper-abbr↔ESPN-id translation, though Scout doesn't currently call this specific function (its `get_nfl_team` resolves the other direction, ref→name) |
| `get_season_statistics(espn_athlete_id, season)` | `agents/production_agent.py`, `api.py` | `GET .../seasons/{season}/types/2/athletes/{id}/statistics` | Single-season stats | None | Returns `{}` on 400/404, otherwise `raise_for_status()` | DDREPORTCARDS-SPECIFIC today (active NFL production grading) |
| `get_career_statistics(espn_athlete_id)` | `agents/production_agent.py` | `GET .../athletes/{id}/statistics` | Career totals | None | Returns `{}` on 400/404 | DDREPORTCARDS-SPECIFIC today |
| `get_event_log(espn_athlete_id)` | Internal only (used by `get_recent_game_logs`) | `GET .../athletes/{id}/eventlog` | Per-game refs | None | `raise_for_status()` | DDREPORTCARDS-SPECIFIC (internal helper, not called externally) |
| `flatten_statistics(stats_response)` | `agents/production_agent.py`, `api.py` | Pure transform, no network | Flattens ESPN's nested category/stat structure | N/A | N/A | COMMON primitive — any ESPN stats consumer needs this, including Scout's college-stats path, which currently reimplements its own flattening inline in `get_college_stats` instead of reusing this |
| `get_recent_game_logs(espn_athlete_id, limit, position)` | `api.py` | Composed (`get_event_log` + per-game dereference) | Recent per-game stat lines | None | Catches and degrades to `{"available": False}` rather than raising | DDREPORTCARDS-SPECIFIC |
| `fantasy_relevant_stats(flat, position)` | `api.py` | Pure transform | Drops always-zero fields by position | N/A | N/A | DDREPORTCARDS-SPECIFIC in current form (position-keyed core-stat sets are built for active NFL fantasy scoring; Scout's `get_college_stats` has its own, differently-shaped filtering) |
| `get_player_injury_notes(espn_athlete_id, espn_team_id)` | `agents/production_agent.py` | Composed (`GET .../teams/{id}/injuries`, filtered by athlete) | Per-team injury feed, filtered to one athlete | None | Propagates | DDREPORTCARDS-SPECIFIC, and **semantically different from Scout's injury function — see §4** |
| `get_injury_detail(ref_url)` | Internal only | `GET {ref_url}` (dereferences a `$ref`) | Generic $ref resolver | None | `raise_for_status()` | COMMON primitive in spirit (ESPN's `$ref` pagination pattern recurs everywhere, including Scout's draft-athlete resolution), but not currently shared |

### `scoutcap tools.espn` (draft/college layer)

| Function | Current callers | Provider/endpoint | Semantics | Caching | Error behavior | Classification |
|---|---|---|---|---|---|---|
| `search_draft_prospects(name, season)` | `mcp_server.py`, `agents/production_agent.py`, `agents/situation_agent.py` | Composed, over `CFB_BASE` draft-athletes index | Name search over the cached draft-class index | **Module-level cache, built once per process via a 20-worker parallel fetch** | Individual athlete resolution failures are swallowed (`except: return None`) during index build | SCOUTCAP-SPECIFIC — explicitly documented as such in `dynasty_core/espn.py`'s own module docstring ("draft/college ESPN functions... are scout-specific and live in scoutcap/tools/espn.py, not here") |
| `get_espn_athlete_id(draft_athlete_id, season)` | `agents/production_agent.py` | Cache lookup, falls back to `GET .../draft/athletes/{id}` + ref dereference | Resolve a draft-specific id to the base ESPN athlete id | Uses the same draft-roster cache | Returns `None` on failure at any step | SCOUTCAP-SPECIFIC |
| `get_draft_prospect(espn_athlete_id, season)` | **none found** | Cache lookup only | Draft capital + basic info | Uses the same draft-roster cache | Returns an `{"error": ...}` dict, never raises | SCOUTCAP-SPECIFIC, and currently **unused** |
| `get_college_stats(espn_athlete_id, season)` | `agents/production_agent.py` | `GET .../college-football/.../statistics/0` | College production stats, career or single-season | None | Returns `{"error": ...}` dict on 404, `raise_for_status()` otherwise | SCOUTCAP-SPECIFIC — legitimately so; this is the college/draft workflow the map's proposed-batches section explicitly said not to force into shared core |
| `get_nfl_injuries(espn_athlete_id)` | `agents/production_agent.py` | `GET .../nfl/athletes/{id}/injuries` (**direct per-athlete endpoint**) | Historical injury records for one athlete | None | Returns `{"injuries": [], "note": ...}` on 404 | **Same domain as `dynasty_core.get_player_injury_notes`, but a different ESPN endpoint entirely — see §4** |
| `get_nfl_team(team_ref)` | **none found** | `GET {team_ref}` | Resolve a team `$ref` to name/abbreviation | None | `raise_for_status()` (via shared `_get`) | SCOUTCAP-SPECIFIC in current form, currently **unused**; conceptually the inverse of `dynasty_core.team_espn_id` (ref→name vs. abbr→id) |

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
target shape for Sleeper and ESPN, not "delete the app-specific file and
import `dynasty_core` directly everywhere," which would remove the layer
that's allowed to hold app-specific policy (like Scout's QB/RB/WR/TE-only
filtering in `search_players`, which has no reason to live in shared code).

---

## 4. Where implementations actually disagree (not just names)

This is the substantive finding Stage 2 has to resolve — not the presence
of two files, but three real behavioral disagreements:

1. **`get_all_players()` vs. `get_nfl_players()` — caching.**
   `dynasty_core.sleeper.get_all_players()` caches the ~14MB player dict for
   6 hours, module-level. `tools.sleeper.get_nfl_players()`'s docstring says
   *"Cache locally after first fetch"* but there is no caching code at all —
   every call re-fetches the full payload. This is the clearest case of two
   implementations of the "same" function having different real behavior,
   not just different names. Any consolidation must decide whether Scout's
   callers actually want the 6h TTL (probably yes — nothing about a draft
   evaluation session needs sub-6-hour freshness on the full player list)
   or documented as an intentional difference (there's no evidence it's
   intentional; the stale docstring suggests it's an oversight).

2. **`get_trending_adds()` vs. `get_trending()` — scope.**
   `dynasty_core`'s version is hardcoded to `type=add`, NFL only.
   `tools.sleeper`'s version generalizes both the trend type (`add`/`drop`)
   and the sport. This isn't a bug in either — it's `dynasty_core` only ever
   having had one caller need (add-trending for the situation agent) — but a
   shared version should be the general one, with `dynasty_core` gaining the
   `type`/`sport` parameters (defaulted to preserve every current caller's
   behavior) rather than Scout's generality being discarded.

3. **`get_player_injury_notes()` vs. `get_nfl_injuries()` — different ESPN endpoints entirely.**
   `dynasty_core.espn.get_player_injury_notes(espn_athlete_id, espn_team_id)`
   goes through `.../teams/{team_id}/injuries`, fetches the whole team's
   injury feed (paginated), and filters to the one athlete by regex-matching
   `$ref` URLs. `tools.espn.get_nfl_injuries(espn_athlete_id)` goes through
   `.../athletes/{id}/injuries` directly — a completely different endpoint
   that doesn't require knowing the team at all. These may return different
   data shapes and different completeness (a per-team feed vs. a per-athlete
   history) for the same player. **This needs verification against real ESPN
   responses before any merge decision** — it is not safe to assume one
   subsumes the other without checking, and this plan does not do that
   verification (no runtime code is exercised in Stage 2A by design).

---

## 5. Proposed Stage 2 target API

```
dynasty_core/
  sleeper.py
    # Existing dynasty_core functions, largely unchanged:
    get_league_users, get_league_rosters, get_league_info, get_traded_picks,
    get_league_drafts, get_league_season_chain, get_transactions,
    get_all_trades, get_all_trades_all_seasons, get_roster_by_display_name,
    resolve_roster_players

    # get_all_players gains no new params -- Scout's get_nfl_players becomes
    # a thin wrapper around this (see migration batch 1), inheriting the 6h
    # cache Scout's own docstring already claimed to want.
    get_all_players

    # get_trending_adds generalizes to accept type/sport, defaulted to
    # preserve every existing call site's behavior unchanged:
    get_trending(type="add", sport="nfl", lookback_hours=24, limit=25)

    # New: promoted from tools/sleeper.py, since both apps could use
    # user/league discovery (Ddreportcards doesn't today, but nothing about
    # it is Scout-specific -- it's generic Sleeper API surface):
    get_user, get_leagues

    # New: single-player lookup and name search are generic Sleeper
    # capabilities. search_players' QB/RB/WR/TE filter is Scout POLICY,
    # not provider primitive -- see the facade split below.
    get_player

  espn.py  (unchanged -- active-NFL layer stays exactly as scoped)
    team_espn_id, get_season_statistics, get_career_statistics,
    get_event_log, flatten_statistics, get_recent_game_logs,
    fantasy_relevant_stats, get_player_injury_notes, get_injury_detail

    # New: promote the generic $ref-dereferencing pattern explicitly as a
    # primitive both NFL and draft/college code lean on:
    # (get_injury_detail already IS this -- just document it as reusable,
    # not NFL-injury-specific, since Scout's draft-athlete resolution does
    # the identical thing with its own private _get() helper today)

  fantasycalc.py  (unchanged -- already the target shape)

scoutcap/tools/
  sleeper.py
    # Thin facade, matching tools/fantasycalc.py's pattern:
    from dynasty_core.sleeper import get_user, get_leagues, get_player, \
        get_all_players as get_nfl_players, get_trending

    # Stays here, Scout-specific policy (position filter is a choice, not
    # a Sleeper API fact):
    def search_players(name): ...  # composed from get_nfl_players()

    # Stays here, thin renames if kept at all (see migration batches --
    # these may just get deleted in favor of calling dynasty_core directly,
    # since they're zero-value renames, not policy):
    get_rosters -> dynasty_core.get_league_rosters
    get_users_in_league -> dynasty_core.get_league_users

  espn.py
    # Stays entirely Scout-owned -- draft/college is not shared-core
    # material. No change to search_draft_prospects, get_espn_athlete_id,
    # get_draft_prospect, get_college_stats, get_nfl_team.

    # get_nfl_injuries: HOLD. Do not delegate to dynasty_core's
    # get_player_injury_notes until the endpoint-disagreement in §4 is
    # actually verified against live ESPN data -- they may not be
    # interchangeable.

  fantasycalc.py  (unchanged -- already correct)
```

---

## 6. Answers to the specific questions asked

**Which `tools/sleeper.py` functions should become wrappers around existing shared-core functions?**
`get_rosters` → `dynasty_core.get_league_rosters` (identical endpoint).
`get_users_in_league` → `dynasty_core.get_league_users` (identical
endpoint). `get_nfl_players` → `dynasty_core.get_all_players` (once the
caching disagreement in §4.1 is resolved — this is the one migration that
actually changes behavior for Scout, in the correct direction, since the
"cache locally" docstring already says this is what was intended).
`get_traded_picks` can be dropped entirely rather than wrapped — it has
zero callers in scoutcap today.

**Which shared Sleeper functions are missing and should be added before migration?**
`get_user` and `get_leagues` don't exist in `dynasty_core.sleeper` yet, but
should be added there (they're generic account/league-discovery calls, not
Scout policy) before `tools/sleeper.py` can become a pure facade for them.
A single-player lookup (`get_player`, matching `tools.sleeper`'s existing
one) is also missing from `dynasty_core` and worth adding as a shared
primitive, since "look up one player by id" is not an app-specific need.

**Which Scout ESPN functions can delegate to shared NFL primitives?**
None of the draft/college functions can or should — `search_draft_prospects`,
`get_espn_athlete_id`, `get_draft_prospect`, and `get_college_stats` all
operate against `CFB_BASE` (college football), a different ESPN sport
entirely from `dynasty_core.espn`'s `NFL_BASE`. The one candidate,
`get_nfl_injuries`, is on `NFL_BASE` and looks like it overlaps with
`get_player_injury_notes` — but per §4.3, that needs live-data verification
before treating it as a safe delegation, not an assumption from the two
functions' names looking similar. `flatten_statistics` is a plain transform
Scout's `get_college_stats` could reuse for its own category/stat
flattening today, with zero endpoint risk, since it takes already-fetched
JSON and returns a dict — this is the one clearly-safe cross-domain reuse
in this entire matrix.

**Which Scout ESPN functions must remain Scout-only because they are draft/college-specific?**
`search_draft_prospects`, `get_espn_athlete_id`, `get_draft_prospect`, and
`get_college_stats` — all four operate on `CFB_BASE` and the draft-athlete
object model, which has no active-NFL-roster equivalent. This matches
`dynasty_core/espn.py`'s own module docstring, which already states this
split was a deliberate decision, not an oversight.

**After migration, should scoutcap continue shipping a physical copy of `dynasty_core`, or should Stage 2 promote it to one installable shared package?**
Neither, yet. Promoting to an installable package now would package the
current inconsistency (two Sleeper/ESPN surfaces, one caching disagreement,
one unverified endpoint overlap) as a versioned artifact, making it harder
to fix later without a release cycle. The right sequence is: converge the
contract first (resolve §4's three disagreements, land the facade-only
`tools/sleeper.py`), confirm both apps' test/smoke coverage stays green
through that convergence, and only then consider whether a physical
identical-copy arrangement still causes enough real friction (drift risk,
manual two-repo edits) to justify the packaging investment. It may turn out
the physical-copy model is fine once the actual inconsistency is gone —
packaging solves a distribution problem, not a correctness problem, and the
correctness problem is the one actually found in Phase 4C.

---

## 7. Proposed migration batches (not implemented in this pass)

1. **Verify the `get_player_injury_notes` vs. `get_nfl_injuries` endpoint
   disagreement (§4.3) against live ESPN data.** This blocks any decision
   about that pair and should happen before other batches, since it's the
   one open question that isn't already answerable from reading the code.
2. **Add `get_user`, `get_leagues`, `get_player` to `dynasty_core.sleeper`**
   (new functions, zero risk to existing callers in either app).
3. **Fix `get_all_players`/`get_nfl_players` caching**: either make
   `tools.sleeper.get_nfl_players` a thin wrapper around
   `dynasty_core.get_all_players` (picking up the 6h cache), or add real
   caching to `tools.sleeper.get_nfl_players` directly if there's a reason
   Scout needs to stay independent here. Needs a decision, not just a
   default.
4. **Generalize `get_trending_adds` → `get_trending`** in `dynasty_core`,
   parameters defaulted so every current caller's behavior is unchanged.
5. **Convert `tools/sleeper.py` into a pure facade** (matching
   `tools/fantasycalc.py`'s pattern) once batches 2-4 land, dropping
   `get_rosters`/`get_users_in_league` (thin renames) and `get_traded_picks`
   (unused) in favor of calling `dynasty_core` directly, or re-exporting
   them under their existing names if call-site churn isn't worth it.
6. **Reuse `flatten_statistics` in Scout's `get_college_stats`** — safe,
   independent of every other batch, since it's a pure transform.
7. **Revisit the installable-package question** only after 1-6 are done and
   both apps' test/smoke suites are green against the converged contract.

No batch here touches `tools/espn.py`'s draft/college functions — those
stay Scout-owned permanently, not as a temporary state.
