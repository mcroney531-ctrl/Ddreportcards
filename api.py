"""Dynasty Report Cards — FastAPI server.

Always-on HTTP interface decoupled from the Streamlit UI so that GM Command
and Claude tool calls are not blocked by Streamlit Cloud's sleep-on-inactivity.

Run: uvicorn api:app --host 0.0.0.0 --port 8000

Fast endpoints (no LLM, sub-second):
  GET  /health
  GET  /league
  GET  /players/trending
  GET  /players/search?q=<name>
  GET  /players/{sleeper_id}
  GET  /roster/{owner}
  GET  /league/rosters/summary
  POST /trade/evaluate

Chat endpoint (Claude tool-calling loop, runs server-side):
  POST /chat   GM Command's assistant. No Netlify 10s ceiling here, and tool
               execution is direct in-process function calls — no HTTP hop
               back out to this same server, unlike the old Netlify-side loop.

Pipeline endpoints (LLM, seconds–minutes):
  POST /report/player/{sleeper_id}   single player full card (synthesis_agent)
  POST /report/roster/{owner}        full roster pipeline + overall grade
"""
import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("gm.api")


def _configure_gm_logging() -> None:
    """Give the gm.* logger family (gm.api, gm.budget, gm.agents.usage,
    gm.chat_guard, ...) a handler, once.

    Plain logging.getLogger("gm.x") calls scattered across modules produce
    no output on their own: the root logger defaults to WARNING with no
    handler attached, so every log.info(...) call in this codebase was
    being silently dropped in production, INFO and DEBUG never reaching
    Render's log stream at all. Configuring the "gm" parent logger once,
    here, gives every gm.* child a handler through the standard logging
    hierarchy (child loggers with no handler of their own propagate up to
    the nearest ancestor's) without scattering logging.basicConfig() calls
    through budget.py/chat_guard.py/agents/usage.py individually.
    propagate=False keeps gm.* records from also reaching the root
    logger's own handling, so nothing here can print a line twice or
    interfere with Uvicorn's separate uvicorn/uvicorn.access/uvicorn.error
    loggers, which live outside the "gm" hierarchy entirely and are
    untouched by this.
    """
    gm_logger = logging.getLogger("gm")
    if gm_logger.handlers:
        return  # already configured -- re-importing this module must not double up
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    gm_logger.addHandler(handler)
    gm_logger.setLevel(logging.INFO)
    gm_logger.propagate = False


_configure_gm_logging()

import budget
import chat_guard
import intel_store
import picks as picks_module
from config.dynasty_config import LEAGUE
from dynasty_core.fantasycalc import index_picks_by_label  # noqa: E402
from dynasty_core.sleeper import (
    get_all_players,
    get_league_users,
    get_league_rosters,
    get_trending_adds,
)
from data.sleeper_workflows import (
    get_roster_by_display_name,
    resolve_roster_players,
)
from dynasty_core.fantasycalc import (
    get_dynasty_values,
    index_by_sleeper_id,
    get_value_for_sleeper_id,
)
from dynasty_core.espn import (
    get_recent_game_logs,
    get_season_statistics,
    flatten_statistics,
    fantasy_relevant_stats,
)
LEAGUE_ID: str = LEAGUE["league_id"]


def _current_rss_mb() -> float | None:
    """Current resident set size in MB, read from /proc/self/status. Linux
    only, which is what Render runs -- no profiling dependency needed for
    one startup measurement."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        return None
    return None


async def _prewarm_litellm() -> None:
    """Pay ADK's lazy litellm import cost at startup instead of inside the
    first real report/chat request.

    google.adk.models.lite_llm._ensure_litellm_imported() is a plain
    synchronous function -- a bare `import litellm` plus logger setup --
    that LiteLlm.generate_content_async() calls as its literal first line,
    before any await. On a cold process, the first real model call pays that
    cost directly on the event loop, blocking everything else (including
    /health) for however long the import takes. Calling it here, off the
    loop via asyncio.to_thread, pays it once at boot instead.

    Raises whatever _ensure_litellm_imported() raises: a process that can't
    import its own model stack should fail startup, not pretend it warmed.
    """
    from google.adk.models.lite_llm import _ensure_litellm_imported

    log.info("litellm_prewarm_start")
    rss_before = _current_rss_mb()
    t0 = time.perf_counter()
    await asyncio.to_thread(_ensure_litellm_imported)
    duration_ms = (time.perf_counter() - t0) * 1000
    rss_after = _current_rss_mb()
    rss_delta = (
        rss_after - rss_before if rss_before is not None and rss_after is not None else None
    )
    log.info(
        "litellm_prewarm_complete duration_ms=%.1f rss_before_mb=%s rss_after_mb=%s rss_delta_mb=%s",
        duration_ms,
        f"{rss_before:.1f}" if rss_before is not None else "unknown",
        f"{rss_after:.1f}" if rss_after is not None else "unknown",
        f"{rss_delta:.1f}" if rss_delta is not None else "unknown",
    )


# Upstash deletes a database after a period with no requests (paid plans:
# about three months). 30 days leaves a 3x margin, and the loop also touches
# once at every startup, so each deploy or restart counts too. It depends on
# the process running: the Netlify keepalive polls /health every 5 minutes,
# which keeps the instance up, but /health itself deliberately never touches
# the store.
BUDGET_KEEPALIVE_INTERVAL_SECONDS = 30 * 24 * 3600


async def _budget_keepalive_loop() -> None:
    """Touch the spend store now, then every BUDGET_KEEPALIVE_INTERVAL_SECONDS.
    budget.touch_store() is one read-only GET and never raises."""
    while True:
        await asyncio.to_thread(budget.touch_store)
        await asyncio.sleep(BUDGET_KEEPALIVE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _prewarm_litellm()
    keepalive = asyncio.create_task(_budget_keepalive_loop())
    try:
        yield
    finally:
        keepalive.cancel()


app = FastAPI(title="Dynasty Report Cards API", version="2.0", lifespan=lifespan)

# The read-only data endpoints stay open — they return public Sleeper and
# FantasyCalc data and cost nothing to serve. /chat spends an Anthropic key,
# so it is additionally gated in chat_guard, including an Origin check that
# does not depend on the browser honouring this header.
app.add_middleware(
    CORSMiddleware,
    allow_origins=chat_guard.ALLOWED_ORIGINS,
    allow_origin_regex=chat_guard.ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


# ── Fast endpoints (no LLM) ─────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    """Render's liveness probe, polled every few seconds, and the warm-up
    ping GM Command fires on load and on opening chat — both callers only
    check for a response, never read the body, so this must answer "is the
    process alive enough to route traffic" and nothing more. It used to
    also make two Upstash reads on every hit (via chat_budget/warnings
    below), which ran that Redis round-trip continuously for as long as
    the process was up. That diagnostic payload still exists at /health/deep.
    """
    return {"status": "ok"}


@app.get("/health/deep")
def health_deep():
    """Control-plane check: is spend accounting working? Not polled by
    Render (that's /health) — fetched manually, by the smoke script, or by a
    deploy check.

    Spend accounting gates every billable request (the report and chat
    guards fail closed without it), so if the store can't be built or
    reached this answers 503 "degraded", not 200 "ok". The diagnostic body
    (chat_budget + warnings) is returned either way. Other warnings, such as
    an unset GM_CHAT_SECRET, are reported but don't change the status."""
    budget_status = chat_guard.budget_status()
    body = {
        "status": "ok",
        "chat_budget": budget_status,
        "warnings": chat_guard.config_warnings(),
    }
    if budget_status.get("configured") is not True or budget_status.get("reachable") is not True:
        body["status"] = "degraded"
        return JSONResponse(status_code=503, content=body)
    return body


@app.get("/league")
def league_info() -> dict:
    return dict(LEAGUE)


@app.get("/players/trending")
def trending_players(limit: int = Query(default=25, ge=1, le=100)) -> dict:
    adds = get_trending_adds(limit=limit)
    all_p = get_all_players()
    result = []
    for t in adds:
        p = all_p.get(t["player_id"], {})
        if p.get("position") in ("QB", "RB", "WR", "TE"):
            result.append({
                "player_id": t["player_id"],
                "name": p.get("full_name"),
                "position": p.get("position"),
                "team": p.get("team"),
                "add_count": t.get("count"),
            })
    return {"trending_adds": result}


_league_roster_cache: tuple[float, dict[str, str]] | None = None
_LEAGUE_ROSTER_TTL_SECONDS = 5 * 60


def _league_rostered_owners() -> dict[str, str]:
    """player_id -> the display name of whoever rosters him in this league.

    A name is only ambiguous if nothing else identifies the player. When
    somebody in the league owns a player by that name, his player_id is a
    settled fact and there is nothing left to guess — which is the whole
    problem with resolving a roster player by name search.
    """
    global _league_roster_cache
    now = time.time()
    if _league_roster_cache and (now - _league_roster_cache[0]) < _LEAGUE_ROSTER_TTL_SECONDS:
        return _league_roster_cache[1]
    try:
        users = {u["user_id"]: u.get("display_name") for u in get_league_users(LEAGUE_ID)}
        index: dict[str, str] = {}
        for roster in get_league_rosters(LEAGUE_ID):
            owner = users.get(roster.get("owner_id")) or f"roster {roster.get('roster_id')}"
            for pid in roster.get("players") or []:
                index[pid] = owner
    except Exception:  # noqa: BLE001 — identity hinting must never break a lookup
        return _league_roster_cache[1] if _league_roster_cache else {}
    _league_roster_cache = (now, index)
    return index


def _search_relevance(entry: dict) -> tuple:
    """Rank name-search hits by real-world relevance, most relevant first.

    Sorting on dynasty value alone is wrong for name collisions: two obscure
    players who share a name both score 0, so the tie broke arbitrarily —
    which is how an unsigned veteran nobody has heard of outranked a rostered
    rookie playing this season.

    Being on an NFL roster is the strongest relevance signal available. A
    player with no team is not playing this season; a rostered one is,
    regardless of whether FantasyCalc tracks them yet (it lags on rookies and
    late-round picks). Ordering, most to least significant:

      1. Tier: rostered-and-valued > valued-but-unsigned > rostered >
         neither. Tiers 1 and 2 are the interesting boundary — a player
         FantasyCalc actually prices is a real dynasty asset even between
         contracts, so that outranks rostered-but-untracked depth.
      2. Dynasty value, descending.
      3. Sleeper's own search_rank — what Sleeper orders its player search
         by; lower is more prominent, and it assigns huge values to
         irrelevant players. Missing is treated as least relevant, so this
         degrades safely if the field ever disappears.
    """
    # Rostered in THIS league beats every heuristic below it. Those are
    # guesses about who is more relevant in general; this is knowing which
    # player the conversation is actually about.
    if entry.get("rostered_by"):
        return (-1, -(entry.get("dynasty_value") or 0), 0)

    has_team = bool(entry.get("team"))
    value = entry.get("dynasty_value") or 0
    has_value = value > 0

    if has_team and has_value:
        tier = 0
    elif has_value:
        tier = 1
    elif has_team:
        tier = 2
    else:
        tier = 3

    search_rank = entry.get("search_rank")
    if search_rank is None:
        search_rank = 10 ** 9

    return (tier, -value, search_rank)


_DEPTH_CHART_MAX = 6

# Named assets per position group in the league summary. Enough for the model
# to cite a roster's real top pieces without dumping all 12 rosters in full.
_SUMMARY_TOP_N = 3


def _experience(years_exp) -> dict:
    """Turn Sleeper's raw years_exp into something that can't be misread.

    Shipping the bare integer meant the model had to infer rookie status, and
    it inferred wrong — calling second-year players (2025 draftees) "rookies"
    in a 2026 conversation. Sleeper counts completed NFL seasons, so 0 is a
    true rookie and 1 is a player in his second season. Return both the flag
    and a ready-made label so a description never has to do that arithmetic.
    """
    if years_exp is None:
        return {"is_rookie": False, "experience_label": "experience unknown"}
    try:
        n = int(years_exp)
    except (TypeError, ValueError):
        return {"is_rookie": False, "experience_label": "experience unknown"}
    if n <= 0:
        return {"is_rookie": True, "experience_label": "rookie (1st NFL season)"}
    ordinals = {1: "2nd", 2: "3rd", 3: "4th", 4: "5th"}
    if n in ordinals:
        return {"is_rookie": False, "experience_label": f"{ordinals[n]} NFL season"}
    return {"is_rookie": False, "experience_label": f"veteran ({n} seasons played)"}


def _positional_depth_chart(all_players: dict, team: str | None, position: str | None) -> list[dict]:
    """The live position group a player actually competes with on his current team.

    This ships inside every player lookup on purpose. Describing a player's role
    ("TE3 behind X and Y", "WR4 in Tampa") requires knowing his teammates, and if
    the lookup doesn't carry them the model fills the gap from training data —
    which is a full season stale and will name players who have since been traded
    or cut. Returning the real group closes that gap by construction instead of
    depending on a second tool call being made.

    Capped at the top few by depth_chart_order: past that it's camp bodies, not
    competition. Players with no depth_chart_order sort last.
    """
    if not team or not position:
        return []
    group = [
        {
            "player_id": pid,
            "name": tp.get("full_name"),
            "depth_chart_order": tp.get("depth_chart_order"),
            "status": tp.get("status"),
            "injury_status": tp.get("injury_status"),
        }
        for pid, tp in all_players.items()
        if (tp.get("team") or "").upper() == team.upper()
        and tp.get("position") == position
    ]
    group.sort(key=lambda x: x["depth_chart_order"] if x["depth_chart_order"] is not None else 99)
    return group[:_DEPTH_CHART_MAX]


def _team_quarterbacks(all_players: dict, team: str | None) -> list[dict]:
    """The QBs currently on a skill player's team, by depth chart order.

    Almost every fantasy narrative about a receiver or back runs through who
    is throwing him the ball, and nothing in the payload answered that — so
    the model answered from training data and put Aaron Rodgers in Green Bay
    three years after he left. The QB room is one filter away in data already
    loaded, and shipping it means that sentence has a source instead of a
    memory. Same reasoning as team_depth_chart: close the gap rather than ask
    the model to remember to make another call.
    """
    if not team:
        return []
    qbs = [
        {
            "player_id": pid,
            "name": p.get("full_name"),
            "depth_chart_order": p.get("depth_chart_order"),
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
        }
        for pid, p in all_players.items()
        if (p.get("team") or "").upper() == team.upper() and p.get("position") == "QB"
    ]
    qbs.sort(key=lambda x: x["depth_chart_order"] if x["depth_chart_order"] is not None else 99)
    return qbs[:3]


@app.get("/players/search")
def search_players(q: str = Query(..., min_length=2)) -> dict:
    """Case-insensitive substring search across all Sleeper skill-position players.
    Returns up to 20 results ordered by real-world relevance — rostered players
    ahead of unsigned ones, then by dynasty value. See _search_relevance.

    Each result carries team_depth_chart: the player's live position group on his
    current team, so role/competition claims never have to come from stale
    training data. See _positional_depth_chart."""
    all_p = get_all_players()
    fc_values = get_dynasty_values()
    fc_index = index_by_sleeper_id(fc_values)
    rostered = _league_rostered_owners()
    q_lower = q.lower()
    matches = []
    for pid, p in all_p.items():
        if p.get("position") not in ("QB", "RB", "WR", "TE"):
            continue
        name = p.get("full_name") or ""
        if q_lower not in name.lower():
            continue
        fc = fc_index.get(pid, {})
        matches.append({
            "player_id": pid,
            "name": name,
            "position": p.get("position"),
            "team": p.get("team"),
            "age": p.get("age"),
            "years_exp": p.get("years_exp"),
            **_experience(p.get("years_exp")),
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
            "depth_chart_order": p.get("depth_chart_order"),
            "depth_chart_position": p.get("depth_chart_position"),
            "search_rank": p.get("search_rank"),
            "dynasty_value": fc.get("value"),
            "dynasty_pos_rank": fc.get("positionRank"),
            "redraft_value": fc.get("redraftValue"),
            "trend_30day": fc.get("trend30Day"),
            "team_depth_chart": _positional_depth_chart(all_p, p.get("team"), p.get("position")),
            "team_qbs": _team_quarterbacks(all_p, p.get("team")) if p.get("position") != "QB" else [],
            "rostered_by": rostered.get(pid),
        })
    matches.sort(key=_search_relevance)

    result = {"results": matches[:20]}
    owned = [m for m in matches if m.get("rostered_by")]
    if len(owned) == 1:
        # Say it outright rather than leaving it to be inferred from sort
        # order. A player somebody in the league rosters is the player being
        # discussed; asking the user to pick between him and a namesake
        # nobody owns is not caution, it is discarding the answer.
        result["identity_resolved"] = (
            f"{owned[0]['name']} ({owned[0]['player_id']}) is rostered by "
            f"{owned[0]['rostered_by']} in this league, so this is the player meant. "
            "Do not ask which one; use this player_id."
        )
    return result


@app.get("/players/{sleeper_id}")
def player_info(sleeper_id: str) -> dict:
    """Sleeper player metadata + FantasyCalc dynasty/redraft values for one player."""
    all_p = get_all_players()
    meta = all_p.get(sleeper_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"No player with id {sleeper_id!r}")
    fc = get_value_for_sleeper_id(sleeper_id)
    return {
        "player_id": sleeper_id,
        "name": meta.get("full_name"),
        "position": meta.get("position"),
        "team": meta.get("team"),
        "age": meta.get("age"),
        "years_exp": meta.get("years_exp"),
        **_experience(meta.get("years_exp")),
        "status": meta.get("status"),
        "injury_status": meta.get("injury_status"),
        "depth_chart_order": meta.get("depth_chart_order"),
        "depth_chart_position": meta.get("depth_chart_position"),
        "injury_notes": meta.get("injury_notes"),
        "practice_status": meta.get("practice_status"),
        "dynasty_value": fc["value"] if fc else None,
        "dynasty_pos_rank": fc["positionRank"] if fc else None,
        "redraft_value": fc["redraftValue"] if fc else None,
        "trend_30day": fc["trend30Day"] if fc else None,
    }


@app.get("/players/{sleeper_id}/news")
def player_news(sleeper_id: str) -> dict:
    """Current NFL status snapshot from Sleeper: team, depth chart, injury, practice.
    Use to verify a player's present-day situation when training-data knowledge may be stale."""
    all_p = get_all_players()
    meta = all_p.get(sleeper_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"No player with id {sleeper_id!r}")
    return {
        "player_id": sleeper_id,
        "full_name": meta.get("full_name"),
        "team": meta.get("team"),
        "position": meta.get("position"),
        "status": meta.get("status"),
        "injury_status": meta.get("injury_status"),
        "injury_body_part": meta.get("injury_body_part"),
        "injury_notes": meta.get("injury_notes"),
        "practice_status": meta.get("practice_status"),
        "practice_description": meta.get("practice_description"),
        "depth_chart_order": meta.get("depth_chart_order"),
        "depth_chart_position": meta.get("depth_chart_position"),
        "news_updated": meta.get("news_updated"),
        "team_depth_chart": _positional_depth_chart(all_p, meta.get("team"), meta.get("position")),
        "team_qbs": _team_quarterbacks(all_p, meta.get("team")) if meta.get("position") != "QB" else [],
    }


def _current_nfl_season() -> int:
    """The season now in progress, or the last one if we're before kickoff.
    An NFL season is named for the calendar year it starts in."""
    today = datetime.date.today()
    return today.year if today.month >= 9 else today.year - 1


def _espn_athlete_id(sleeper_id: str, meta: dict | None = None) -> str | None:
    """Sleeper -> ESPN athlete id, through two sources.

    Sleeper's espn_id is null for a lot of recent rookies — exactly the
    players a dynasty manager asks about — so FantasyCalc gets a turn too.
    Both coming back empty is a real "no ESPN data" case, not an error.
    """
    if meta is None:
        meta = get_all_players().get(sleeper_id) or {}
    espn_id = meta.get("espn_id")
    if not espn_id:
        try:
            fc = get_value_for_sleeper_id(sleeper_id)
            espn_id = (fc.get("player") or {}).get("espnId") if fc else None
        except Exception:  # noqa: BLE001
            espn_id = None
    # A missing id has reached ESPN as the literal string "null" before and
    # come back a 400 instead of a clean empty result.
    if not espn_id or str(espn_id).lower() in ("null", "none"):
        return None
    return str(espn_id)


@app.get("/players/{sleeper_id}/stats")
def player_stats(sleeper_id: str, games: int = Query(default=3, ge=1, le=6)) -> dict:
    """On-field production: season-to-date totals plus the last few game lines.

    Nothing in this build could see a snap count. Values and trends said a
    player was rising; they could not say he caught 6 for 88, or that he has
    not played. So every performance claim came from somewhere other than
    data, and a manager asking "how did he look last week" got a hedge.
    """
    meta = get_all_players().get(sleeper_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"No player with id {sleeper_id!r}")

    espn_id = _espn_athlete_id(sleeper_id, meta)
    season = _current_nfl_season()
    out = {
        "player_id": sleeper_id,
        "name": meta.get("full_name"),
        "position": meta.get("position"),
        "team": meta.get("team"),
        "season": season,
        "espn_id": espn_id,
    }
    if not espn_id:
        out["available"] = False
        out["note"] = (
            "No ESPN athlete id on file from Sleeper or FantasyCalc, so no "
            "stats are available for this player. Say that plainly — do not substitute "
            "an impression of how he has played."
        )
        return out

    try:
        season_stats = fantasy_relevant_stats(
            flatten_statistics(get_season_statistics(espn_id, season)),
            meta.get("position"),
        )
    except Exception as exc:  # noqa: BLE001 — stats are additive, never fatal
        season_stats = {}
        out["season_error"] = str(exc)[:200]
    try:
        recent = get_recent_game_logs(espn_id, limit=games, position=meta.get("position"))
    except Exception as exc:  # noqa: BLE001
        recent = {"games": [], "games_played": 0, "games_missed": 0, "available": False}
        out["games_error"] = str(exc)[:200]

    out["season_totals"] = season_stats
    out["recent_games"] = recent["games"]
    out["games_played"] = recent["games_played"]
    out["games_missed"] = recent["games_missed"]
    out["available"] = bool(season_stats or recent["games"])
    if not out["available"]:
        out["note"] = (
            f"ESPN has no {season} stats on file for this player — most likely he has not "
            "played a regular-season snap this year. That is itself the answer; do not "
            "describe production he does not have."
        )
    return out


@app.get("/teams/{team}/roster")
def team_roster(team: str) -> dict:
    """All current skill-position players on an NFL team, sorted by position + depth chart.
    Use to verify who is actually on a roster — avoids hallucinating cut/traded players."""
    all_p = get_all_players()
    fc_values = get_dynasty_values()
    fc_index = index_by_sleeper_id(fc_values)
    team_upper = team.upper()
    players = []
    for pid, p in all_p.items():
        if (p.get("team") or "").upper() != team_upper:
            continue
        if p.get("position") not in ("QB", "RB", "WR", "TE"):
            continue
        fc = fc_index.get(pid, {})
        players.append({
            "player_id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "depth_chart_order": p.get("depth_chart_order"),
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
            "age": p.get("age"),
            "years_exp": p.get("years_exp"),
            **_experience(p.get("years_exp")),
            "dynasty_value": fc.get("value"),
            "dynasty_pos_rank": fc.get("positionRank"),
        })
    players.sort(key=lambda x: (x["position"], x["depth_chart_order"] or 99))
    if not players:
        raise HTTPException(status_code=404, detail=f"No skill-position players found for team {team_upper!r}")
    return {"team": team_upper, "player_count": len(players), "players": players}


@app.get("/roster/{owner}")
def roster_data(owner: str) -> dict:
    """Full Sleeper roster for a display name with FantasyCalc values attached.
    No LLM — always fast. Skill-position players only, sorted by dynasty value."""
    try:
        roster_obj = get_roster_by_display_name(LEAGUE_ID, owner)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    players = resolve_roster_players(roster_obj)
    fc_values = get_dynasty_values()
    fc_index = index_by_sleeper_id(fc_values)
    enriched = []
    for p in players:
        if p.get("position") not in ("QB", "RB", "WR", "TE"):
            continue
        pid = p.get("player_id")
        fc = fc_index.get(pid, {})
        enriched.append({
            "player_id": pid,
            "name": p.get("full_name"),
            "position": p.get("position"),
            "team": p.get("team"),
            "age": p.get("age"),
            "years_exp": p.get("years_exp"),
            **_experience(p.get("years_exp")),
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
            "dynasty_value": fc.get("value"),
            "dynasty_pos_rank": fc.get("positionRank"),
            "redraft_value": fc.get("redraftValue"),
            "trend_30day": fc.get("trend30Day"),
        })
    enriched.sort(key=lambda x: x["dynasty_value"] or 0, reverse=True)
    # Picks ship inside the roster rather than behind a second tool call.
    # A roster is players AND picks; returning only players is how the
    # assistant spent a whole conversation discussing a rebuild without once
    # knowing what draft capital the rebuild actually had.
    try:
        pick_data = picks_module.picks_for_owner(LEAGUE_ID, owner)
        draft_picks = None if "error" in pick_data else {
            "pick_count": pick_data["pick_count"],
            "own_picks": pick_data["own_picks"],
            "picks_from_others": pick_data["picks_from_others"],
            "total_pick_value": pick_data["total_pick_value"],
            "picks": pick_data["picks"],
        }
    except Exception:  # noqa: BLE001 — a pick lookup must never break a roster read
        draft_picks = None

    return {
        "owner": owner,
        "league_id": LEAGUE_ID,
        "player_count": len(enriched),
        "players": enriched,
        "draft_picks": draft_picks,
    }


@app.get("/league/rosters/summary")
def league_rosters_summary() -> dict:
    """Position-group depth and dynasty value for every team in the league, one call.
    Use to find trade partners: a team thin (low count/value) at a position you're
    deep in is a target; a team overloaded at a position you need may be sellers there."""
    users = get_league_users(LEAGUE_ID)
    rosters = get_league_rosters(LEAGUE_ID)
    all_p = get_all_players()
    fc_index = index_by_sleeper_id(get_dynasty_values())
    owner_by_user_id = {u["user_id"]: u.get("display_name") for u in users}

    teams = []
    for roster in rosters:
        owner = owner_by_user_id.get(roster.get("owner_id"), "Unknown")
        # Carry names alongside values. Returning only counts and a bare
        # top_value gave the model numbers with nobody attached to them, so
        # when it described a roster it had to pair those figures with names
        # from somewhere else — and it paired them wrong, naming middling
        # players as the top assets and omitting the actual best ones.
        pos_players: dict[str, list[dict]] = {"QB": [], "RB": [], "WR": [], "TE": []}
        for pid in roster.get("players") or []:
            meta = all_p.get(pid)
            if not meta or meta.get("position") not in pos_players:
                continue
            fc = fc_index.get(pid)
            pos_players[meta["position"]].append({
                "player_id": pid,
                "name": meta.get("full_name"),
                "team": meta.get("team"),
                "dynasty_value": (fc.get("value") if fc else 0) or 0,
            })

        positions = {}
        for pos, plist in pos_players.items():
            plist.sort(key=lambda x: x["dynasty_value"], reverse=True)
            positions[pos] = {
                "count": len(plist),
                "total_value": sum(p["dynasty_value"] for p in plist),
                "top_value": plist[0]["dynasty_value"] if plist else 0,
                "top_players": plist[:_SUMMARY_TOP_N],
            }
        teams.append({
            "owner": owner,
            "total_dynasty_value": sum(p["total_value"] for p in positions.values()),
            "positions": positions,
        })

    teams.sort(key=lambda t: t["total_dynasty_value"], reverse=True)
    return {"teams": teams}


@app.get("/league/picks")
def league_picks() -> dict:
    """Every future draft pick in the league and who currently holds it, priced
    with FantasyCalc's generic round values. Sleeper only records picks that
    changed hands, so this reconstructs the full board from 'everyone owns
    their own' plus those trades."""
    return picks_module.build_pick_inventory(LEAGUE_ID)


@app.get("/picks/{owner}")
def owner_picks(owner: str) -> dict:
    """One team's draft pick inventory by Sleeper display name."""
    result = picks_module.picks_for_owner(LEAGUE_ID, owner)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


class TradeEvaluateRequest(BaseModel):
    team_a_sends: list[str]
    team_b_sends: list[str]
    # Free text, e.g. "2029 2nd", "2028 3rd Rd". Draft picks have no Sleeper
    # id and no value source, so they cannot ride in the id lists — but a
    # trade that contains them is not the trade the player lists describe.
    team_a_picks: list[str] = []
    team_b_picks: list[str] = []


_ORDINALS = {
    "1st": "1st", "first": "1st", "2nd": "2nd", "second": "2nd",
    "3rd": "3rd", "third": "3rd", "4th": "4th", "fourth": "4th",
    "5th": "5th", "fifth": "5th", "6th": "6th", "sixth": "6th",
    "7th": "7th", "seventh": "7th",
}
_ORD_ALT = "|".join(_ORDINALS)
# "2029 2nd Rd" and "2nd round 2029" are the two ways this actually gets typed.
_PICK_YEAR_FIRST = re.compile(rf"\b(20\d{{2}})\s*(?:season\s+)?({_ORD_ALT})\b", re.I)
_PICK_ORD_FIRST = re.compile(
    rf"\b({_ORD_ALT})\s*(?:round|rd\.?)?\s*(?:pick\s*)?(?:in\s+)?(20\d{{2}})\b", re.I
)


def _normalize_pick(text: str) -> str | None:
    """Turn a written pick reference into a canonical 'YYYY Nth', or None."""
    m = _PICK_YEAR_FIRST.search(text or "")
    if m:
        return f"{m.group(1)} {_ORDINALS[m.group(2).lower()]}"
    m = _PICK_ORD_FIRST.search(text or "")
    if m:
        return f"{m.group(2)} {_ORDINALS[m.group(1).lower()]}"
    return None


def find_picks_in_text(text: str) -> list[str]:
    """Every distinct pick mentioned in a block of prose, in order of appearance.

    Used to catch a trade evaluated without its picks: if the conversation
    names picks and the tool call did not carry any, the numbers describe a
    different trade than the one asked about.
    """
    found: list[str] = []
    for regex, order in ((_PICK_YEAR_FIRST, "year"), (_PICK_ORD_FIRST, "ord")):
        for m in regex.finditer(text or ""):
            year, ordinal = (m.group(1), m.group(2)) if order == "year" else (m.group(2), m.group(1))
            pick = f"{year} {_ORDINALS[ordinal.lower()]}"
            if pick not in found:
                found.append(pick)
    return found


def _describe_trade_side(
    sleeper_ids: list[str],
    all_players: dict,
    fc_index: dict,
    picks: list[str] | None = None,
    pick_values: dict | None = None,
) -> dict:
    players = []
    total_value = 0
    unknown_ids = []
    for pid in sleeper_ids:
        meta = all_players.get(pid)
        if not meta:
            unknown_ids.append(pid)
            continue
        fc = fc_index.get(pid)
        value = fc.get("value") if fc else None
        # `or 0` keeps the running total usable, but a missing value is NOT a
        # zero-valued player — see unvalued_players below, which stops that
        # partial total from ever becoming a verdict.
        total_value += value or 0
        players.append({
            "player_id": pid,
            "name": meta.get("full_name"),
            "position": meta.get("position"),
            "dynasty_value": value,
            "dynasty_pos_rank": fc.get("positionRank") if fc else None,
            "unvalued": fc is None,
        })
    described_picks = []
    for raw in picks or []:
        label = _normalize_pick(raw) or raw
        fc = (pick_values or {}).get(label)
        described_picks.append({
            "as_written": raw,
            "pick": label,
            # None means "not priced", which is not the same as worthless.
            "dynasty_value": fc["value"] if fc else None,
        })
    priced_picks = sum(p["dynasty_value"] for p in described_picks if p["dynasty_value"])
    return {
        "players": players,
        "total_value": total_value,
        "unknown_ids": unknown_ids,
        "picks": described_picks,
        "pick_value": priced_picks,
        "unpriced_picks": [p["pick"] for p in described_picks if p["dynasty_value"] is None],
        # A rostered player FantasyCalc does not price — common for rookies,
        # which is exactly who dynasty trades are about. Counted separately
        # from unknown_ids because the failure is different: we know who he
        # is and not what he is worth.
        "unvalued_players": [p["name"] or p["player_id"] for p in players if p["unvalued"]],
        # Named so it cannot be read as the value of everything on this side.
        "player_value_only": total_value,
        "total_value_with_picks": total_value + priced_picks,
    }


_user_roster_cache: tuple[float, set[str]] | None = None
_USER_ROSTER_TTL_SECONDS = 5 * 60


def _user_roster_ids() -> set[str]:
    """Sleeper player_ids currently on the user's own roster.

    Used to settle which direction a trade runs. Cached briefly because a
    trade evaluation shouldn't re-fetch the league on every call.
    """
    global _user_roster_cache
    now = time.time()
    if _user_roster_cache and (now - _user_roster_cache[0]) < _USER_ROSTER_TTL_SECONDS:
        return _user_roster_cache[1]
    try:
        roster = get_roster_by_display_name(LEAGUE_ID, LEAGUE["my_display_name"])
        ids = set(roster.get("players") or [])
    except Exception:  # noqa: BLE001 — direction hinting must never break trade math
        return _user_roster_cache[1] if _user_roster_cache else set()
    _user_roster_cache = (now, ids)
    return ids


def _trade_direction(team_a_ids: list[str], team_b_ids: list[str]) -> dict:
    """Work out which side of a proposed trade is the user's, from roster facts.

    "A, B for C, D" doesn't say who sends what — the same sentence reads as
    buying or selling depending on the speaker, and the model was deciding
    from grammar alone and getting opposite verdicts on identical input.
    Roster membership settles it: a player already on the user's roster can
    only be leaving, and one who isn't can only be arriving.

    Returns the inference plus the evidence, so a caller can state which way
    it read the trade rather than assuming silently.
    """
    owned = _user_roster_ids()
    if not owned:
        return {"user_side": "unknown", "reason": "Could not load your roster to check ownership."}

    a_owned = [pid for pid in team_a_ids if pid in owned]
    b_owned = [pid for pid in team_b_ids if pid in owned]

    if a_owned and not b_owned:
        side, reason = "team_a", f"{len(a_owned)} of the team_a players are on your roster, so team_a is you sending."
    elif b_owned and not a_owned:
        side, reason = "team_b", f"{len(b_owned)} of the team_b players are on your roster, so team_b is you sending."
    elif a_owned and b_owned:
        side, reason = "conflict", (
            "Players from BOTH sides are on your roster — the split is wrong. "
            "Re-check which players you are actually sending before judging this trade."
        )
    else:
        side, reason = "unknown", (
            "Neither side's players are on your roster. This may be a trade between two other teams, "
            "or the roster is stale."
        )
    return {
        "user_side": side,
        "reason": reason,
        "team_a_on_your_roster": a_owned,
        "team_b_on_your_roster": b_owned,
    }


def _fairness_label(delta_pct: float, winner: str | None) -> str:
    if winner is None or delta_pct < 5:
        return "even trade"
    if delta_pct < 15:
        return f"slight edge to {winner}"
    if delta_pct < 30:
        return f"{winner} wins this trade"
    return f"lopsided in {winner}'s favor"


@app.post("/trade/evaluate")
def evaluate_trade(body: TradeEvaluateRequest) -> dict:
    """Deterministic trade math: dynasty value sent by each side (real FantasyCalc
    numbers, not an estimate), the net value delta, and a fairness read. Use this
    for any trade-fairness question instead of eyeballing values from memory."""
    if not body.team_a_sends or not body.team_b_sends:
        raise HTTPException(status_code=400, detail="Both team_a_sends and team_b_sends must be non-empty")

    all_p = get_all_players()
    fc_index = index_by_sleeper_id(get_dynasty_values())

    pick_values = index_picks_by_label(get_dynasty_values())

    team_a = _describe_trade_side(body.team_a_sends, all_p, fc_index, body.team_a_picks, pick_values)
    team_b = _describe_trade_side(body.team_b_sends, all_p, fc_index, body.team_b_picks, pick_values)

    a_value, b_value = team_a["total_value"], team_b["total_value"]
    net_to_a = b_value - a_value
    bigger_side = max(a_value, b_value, 1)
    delta_pct = round(abs(net_to_a) / bigger_side * 100, 1)
    winner = "team_a" if net_to_a > 0 else "team_b" if net_to_a < 0 else None

    warnings = []
    for label, side in (("team_a_sends", team_a), ("team_b_sends", team_b)):
        for pid in side["unknown_ids"]:
            warnings.append(f"Unknown sleeper_id {pid!r} in {label} — not a tracked skill-position player")

    # Totals that include the picks. Picks are priced from FantasyCalc's own
    # pick entries, so the verdict finally describes the whole trade rather
    # than the players-only version of it — which is what produced "lopsided
    # in your favor, and you're keeping three picks" when the picks were
    # going out. The only way to reconcile a players-only surplus with picks
    # in the question was to invent that they stayed.
    a_total = team_a["total_value_with_picks"]
    b_total = team_b["total_value_with_picks"]
    net_total_to_a = b_total - a_total
    bigger_total = max(a_total, b_total, 1)
    total_delta_pct = round(abs(net_total_to_a) / bigger_total * 100, 1)
    total_winner = "team_a" if net_total_to_a > 0 else "team_b" if net_total_to_a < 0 else None

    all_picks = team_a["picks"] + team_b["picks"]
    # One completeness rule covering every asset class, because the failure is
    # identical in all three: a total that silently omits something, presented
    # as if it covered the whole trade. Picks already behaved this way; players
    # did not. An unvalued player contributed 0 to the total and a verdict came
    # back anyway — and FantasyCalc lags hardest on rookies, who are precisely
    # what dynasty trades are made of.
    incomplete: list[str] = []
    for label, side in (("team_a", team_a), ("team_b", team_b)):
        for pick in side["unpriced_picks"]:
            incomplete.append(f"{pick} (pick, no FantasyCalc value)")
        for name in side["unvalued_players"]:
            incomplete.append(f"{name} (player, no FantasyCalc value)")
        for pid in side["unknown_ids"]:
            incomplete.append(f"player_id {pid} (not a tracked player — unresolvable)")

    result = {
        "team_a_sends": team_a["players"],
        "team_a_picks_sent": team_a["picks"],
        "team_a_player_value_sent": a_value,
        "team_a_pick_value_sent": team_a["pick_value"],
        "team_a_total_value_sent": a_total,
        "team_b_sends": team_b["players"],
        "team_b_picks_sent": team_b["picks"],
        "team_b_player_value_sent": b_value,
        "team_b_pick_value_sent": team_b["pick_value"],
        "team_b_total_value_sent": b_total,
        "net_player_value_to_team_a": net_to_a,
        "player_value_delta_pct": delta_pct,
        "net_total_value_to_team_a": net_total_to_a,
        "total_value_delta_pct": total_delta_pct,
        "direction": _trade_direction(body.team_a_sends, body.team_b_sends),
        "warnings": warnings,
    }

    if incomplete:
        # Any unvalued asset makes every total above a floor rather than a
        # total. The known values are kept — they are still useful — but they
        # can no longer be turned into a verdict.
        result["fairness"] = None
        result["incomplete_assets"] = incomplete
        result["verdict_withheld"] = (
            "NO fairness verdict is available: these assets in the trade have no value on "
            f"file — {'; '.join(incomplete)}. Every total above therefore excludes them and "
            "is a floor, not a total. Name the assets that are missing values, give the "
            "figures you do have and say what they cover, and let him weigh the rest. Do "
            "not assign any of them a value yourself, in either direction, and do not treat "
            "a missing value as zero."
        )
    else:
        result["fairness"] = _fairness_label(total_delta_pct, total_winner)
        if all_picks:
            result["fairness_basis"] = (
                "Includes pick values. These are FantasyCalc generic round values — a "
                "league-average pick of that round and year, not this specific one. Whose "
                "pick it is can matter more than the round; call get_pick_inventory if that "
                "is material to the read."
            )
    if all_picks:
        result["picks_in_trade"] = [p["pick"] for p in all_picks]

    return result


# ── Chat endpoint (Claude tool-calling loop) ────────────────────────────────
#
# Moved here from GM Command's Netlify function. Netlify's free-tier functions
# hard-cap at 10s; a tool-calling conversation can chain 3-5 round trips
# (each a full Claude API call), which blew past that ceiling on anything
# needing more than one or two tool calls. Render has no such ceiling, and
# tool execution below calls the route functions above *directly* — no HTTP
# hop back out to this same server like the old JS version had to do.

_anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

CHAT_TOOLS = [
    {
        "name": "get_player_value",
        "description": (
            "Look up dynasty and redraft fantasy value for a player by name. "
            "Returns FantasyCalc dynasty value, position rank, redraft value, 30-day trend, "
            "AND current Sleeper data: team, depth_chart_order (1=starter), status, and injury_status. "
            "Use for any question about what a player is worth in dynasty. "
            "Each result includes a player_id — pass it to get_player_news for full injury/practice details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {
                    "type": "string",
                    "description": "Player's full or partial name (e.g. 'Ja'Marr Chase', 'Josh Allen', 'Pollard')",
                },
            },
            "required": ["player_name"],
        },
    },
    {
        "name": "get_roster",
        "description": (
            "Get the full skill-position roster for a dynasty team owner, with FantasyCalc dynasty "
            "values attached to each player. Sorted by dynasty value. Use to answer questions about "
            "roster composition, depth by position, or overall asset value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Sleeper display name of the roster owner (e.g. 'TitansTrev55')",
                },
            },
            "required": ["owner"],
        },
    },
    {
        "name": "get_pick_inventory",
        "description": (
            "Draft pick inventory: who holds which future picks in this league, priced with "
            "FantasyCalc's generic round values. Pass owner for one team (his own display name "
            "for his picks), omit it for the whole league. Every pick carries the team it "
            "ORIGINATED with and that team's current standing — a 2029 2nd from the last-place "
            "team is a very different asset from the champion's, and this league drafts in "
            "reverse standings order. Use this for any question about draft capital, what he "
            "has to trade, or what a pick in an offer is actually worth. Picks with "
            "dynasty_value null are unpriced — unknown, NOT worthless."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Sleeper display name. Omit for every team in the league.",
                }
            },
        },
    },
    {
        "name": "get_league_rosters_summary",
        "description": (
            "Get position-group depth (player count, total dynasty value, top single-asset value at "
            "QB/RB/WR/TE) for every team in the league in one call. Use this to find trade partners: a "
            "team thin at a position you're deep in is a target to sell to; a team overloaded at a "
            "position you need may be willing to move a piece there. Also useful for ranking teams by "
            "overall asset value."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_trending_players",
        "description": (
            "Get current trending add activity on Sleeper — players being picked up most in "
            "the last 24 hours. Skill-position players only (QB/RB/WR/TE). "
            "Use for waiver wire and speculative add questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of trending players to return (1-100, default 25)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_league_info",
        "description": (
            "Get the canonical league settings for Dynasty Daddies: scoring format, roster slots, "
            "number of teams, PPR setting, superflex configuration, dynasty rules."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_team_roster",
        "description": (
            "Get all current skill-position players on an NFL team, sorted by position and depth chart order. "
            "depth_chart_order=1 means the starter. "
            "ALWAYS call this before naming a player's competition or backfield situation — "
            "never assume from training data who is on a team, because players get cut and traded. "
            "Example: call get_team_roster(\"LAC\") before saying who the Chargers RB1 is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {
                    "type": "string",
                    "description": 'NFL team abbreviation (e.g. "LAC", "JAX", "WAS", "KC", "SF")',
                },
            },
            "required": ["team"],
        },
    },
    {
        "name": "get_player_news",
        "description": (
            "Get a player's CURRENT NFL status from live Sleeper data: team, depth chart position "
            "(depth_chart_order=1 means starter), injury status, and practice participation. "
            "Call this whenever you need to verify present-day facts — roster cuts, team changes, "
            "depth chart shifts — because your training data is ~1 year behind. "
            "Requires the Sleeper player_id returned by get_player_value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sleeper_id": {
                    "type": "string",
                    "description": "Sleeper player ID (the player_id field from get_player_value results)",
                },
            },
            "required": ["sleeper_id"],
        },
    },
    {
        "name": "get_player_stats",
        "description": (
            "On-field production for a player: this season's totals so far, his last few "
            "game lines (receptions, yards, touchdowns, carries, targets where ESPN has them), "
            "and how many games he has played versus missed. This is the ONLY source of "
            "performance data in this toolset — dynasty value and 30-day trend tell you what "
            "the market thinks, not what he did on the field. Call it for any claim about how "
            "a player has played, looked, or produced, and for 'did he do anything last week'. "
            "available:false with a note means there is genuinely nothing on file — usually he "
            "has not played — and that is the answer, not a reason to estimate. "
            "Requires the Sleeper player_id from get_player_value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sleeper_id": {
                    "type": "string",
                    "description": "Sleeper player ID (the player_id field from get_player_value results)",
                },
                "games": {
                    "type": "integer",
                    "description": "How many recent games to return, 1-6. Default 3.",
                },
            },
            "required": ["sleeper_id"],
        },
    },
    {
        "name": "evaluate_trade",
        "description": (
            "Evaluate a proposed trade using actual FantasyCalc dynasty values — not a guess. "
            "Give the sleeper_ids each side is sending away; returns each side's total value sent, "
            "the net value delta, and a fairness read (even trade / slight edge / lopsided). "
            "ALWAYS use this for any trade-fairness question instead of estimating values from memory — "
            "get sleeper_ids from get_player_value or get_roster first. "
            "team_a/team_b are just labels — the response's `direction` field resolves which side is "
            "actually the user by checking who is on his roster, and flags a conflict if you split the "
            "players the wrong way. Read it before saying who wins. "
            "COMPLETENESS: if any asset on either side has no FantasyCalc value on file — an "
            "unpriced pick, an unpriced player, or an unresolvable player_id — fairness comes "
            "back null with incomplete_assets listing them. That is the correct outcome, not a "
            "failure, and the partial totals must not be presented as the trade's value. "
            "DRAFT PICKS: if the trade includes any, you MUST pass them in team_a_picks/team_b_picks "
            "on the correct sides. Picks have no value source, so including them makes the tool "
            "withhold the fairness verdict — that is the correct outcome, not a failure. Omitting "
            "them does not get you a usable verdict either: the tool checks what the user wrote and "
            "withholds it anyway."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_a_sends": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sleeper player_ids that side A is giving up (going to side B)",
                },
                "team_b_sends": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sleeper player_ids that side B is giving up (going to side A)",
                },
                "team_a_picks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Draft picks side A is giving up, as written, e.g. [\"2029 2nd\", "
                        "\"2028 3rd\"]. Required whenever the trade involves picks."
                    ),
                },
                "team_b_picks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Draft picks side B is giving up, as written, e.g. [\"2027 4th\"]. "
                        "Required whenever the trade involves picks."
                    ),
                },
            },
            "required": ["team_a_sends", "team_b_sends"],
        },
    },
]

# Serialised once: the fallback token estimate needs its size, and
# re-serialising 8KB of tool definitions per failed count is wasteful.
_CHAT_TOOLS_JSON = json.dumps(CHAT_TOOLS)


def _execute_chat_tool(name: str, tool_input: dict, latest_user_text: str = "") -> dict:
    """Dispatch a Claude tool call to the matching route function above,
    in-process — no HTTP round-trip. Route functions raise HTTPException on
    a bad lookup (unknown player/team); convert that to an {"error": ...}
    dict instead of letting it propagate and abort the whole chat turn.

    latest_user_text is the user's most recent message — the trade actually
    under discussion. It exists so a trade evaluation can be checked against
    what was proposed, rather than trusting that every part of it made it into
    the call. Deliberately not the whole conversation: an earlier, unrelated
    mention of a pick would otherwise withhold the verdict on a trade that
    never contained one."""
    try:
        if name == "get_player_value":
            return search_players(q=tool_input.get("player_name", ""))
        if name == "get_roster":
            return roster_data(owner=tool_input.get("owner", ""))
        if name == "get_league_rosters_summary":
            return league_rosters_summary()
        if name == "get_pick_inventory":
            owner = (tool_input.get("owner") or "").strip()
            if owner:
                result = picks_module.picks_for_owner(LEAGUE_ID, owner)
                if "error" in result:
                    return result
                return result
            return picks_module.build_pick_inventory(LEAGUE_ID)
        if name == "get_trending_players":
            limit = min(100, max(1, tool_input.get("limit") or 25))
            return trending_players(limit=limit)
        if name == "get_league_info":
            return league_info()
        if name == "get_team_roster":
            return team_roster(team=(tool_input.get("team") or "").upper())
        if name == "get_player_news":
            return player_news(sleeper_id=tool_input.get("sleeper_id", ""))
        if name == "get_player_stats":
            games = tool_input.get("games") or 3
            return player_stats(
                sleeper_id=tool_input.get("sleeper_id", ""),
                games=min(6, max(1, int(games))),
            )
        if name == "evaluate_trade":
            a_picks = tool_input.get("team_a_picks") or []
            b_picks = tool_input.get("team_b_picks") or []
            req = TradeEvaluateRequest(
                team_a_sends=tool_input.get("team_a_sends") or [],
                team_b_sends=tool_input.get("team_b_sends") or [],
                team_a_picks=a_picks,
                team_b_picks=b_picks,
            )
            result = evaluate_trade(req)

            # A pick left out of the call is invisible to everything above —
            # the math would come back clean and describe a trade nobody
            # proposed. Compare against what the user actually wrote and
            # pull the verdict if anything is missing, rather than relying
            # on the call having been complete.
            if not (a_picks or b_picks):
                mentioned = find_picks_in_text(latest_user_text)
                if mentioned:
                    result["fairness"] = None
                    result["verdict_withheld"] = (
                        "This evaluation was run with NO picks, but his message "
                        f"mentions {', '.join(mentioned)}. The numbers above therefore "
                        "describe a different trade than the one asked about, and no "
                        "fairness verdict is available. Call evaluate_trade again with "
                        "team_a_picks and team_b_picks filled in on the correct sides."
                    )
                    result["picks_mentioned_but_not_evaluated"] = mentioned
            return result
        return {"error": f"Unknown tool: {name}"}
    except HTTPException as exc:
        return {"error": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 — tool errors must not kill the chat turn
        return {"error": str(exc)}


from chat_prompt import CHAT_SYSTEM_PROMPT, _conversation_stance, build_chat_system  # noqa: E402,F401


@app.post("/chat")
async def chat(request: Request) -> dict:
    """GM Command's assistant. Accepts the same body shape the frontend has
    always sent {model, max_tokens, messages, system?} and returns the same
    Anthropic Messages API response shape — the frontend needs no changes.
    Runs the tool-calling loop server-side (up to 5 rounds), same as the old
    Netlify function, but without Netlify's 10s ceiling and without the extra
    HTTP hop for each tool call."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is the caller's problem
        raise HTTPException(status_code=400, detail="Body must be valid JSON.")

    # Rate limit, budget, origin, secret, and per-request caps. The caller no
    # longer picks the model or the token ceiling — those set the price of the
    # call, and this endpoint is public.
    try:
        params = chat_guard.enforce(request, body)
    except chat_guard.ChatRefused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail)

    messages = list(params["messages"])
    model = params["model"]
    max_tokens = params["max_tokens"]
    frontend_system = params["system"]

    system = build_chat_system(frontend_system, messages, datetime.date.today().isoformat())
    return await run_chat_loop(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
        client=_anthropic_client, execute_tool=_execute_chat_tool,
    )


async def run_chat_loop(*, model: str, max_tokens: int, system: str, messages: list,
                        client, execute_tool, tools: list | None = None) -> dict:
    """The /chat tool-calling loop, moved here unchanged so the evaluation
    harness runs exactly what production runs. /chat passes the module's
    client and tool executor; the harness passes its own client (with SDK
    retries off) and a record/replay tool executor. Every round still
    reserves and settles against budget, so spend is capped the same way."""
    tools = CHAT_TOOLS if tools is None else tools
    final_response = None

    # Tool-calling loop — continue until Claude returns a text response.
    # Safety cap of 5 rounds prevents runaway loops.
    for _ in range(5):
        # Every round reserves its own upper bound before the call and
        # reconciles afterwards. Reserving per round rather than per request
        # keeps the stranded amount small if this process dies mid-turn.
        #
        # There is deliberately nothing to salvage when a reservation is
        # refused mid-loop: reaching that point means the previous round
        # returned stop_reason "tool_use", and a request for tools is not an
        # answer — the only rounds that produce one assign final_response and
        # break immediately. So the failure is surfaced rather than handing
        # the frontend a partial turn dressed up as a reply.
        # Count the exact payload rather than estimating it. This is a free
        # endpoint — no tokens are billed — and it turns the input half of the
        # reservation from a guess into a measurement.
        try:
            counted = await asyncio.to_thread(
                client.messages.count_tokens,
                model=model, system=system, messages=messages, tools=tools,
            )
            counted_input = int(getattr(counted, "input_tokens", 0) or 0)
        except Exception:  # noqa: BLE001
            # Degrade toward over-charging, never toward unmetered: a
            # deliberately pessimistic character estimate, not a skipped
            # reservation.
            #
            # It must cover everything the real count covers. Omitting
            # CHAT_TOOLS left out ~8.3KB of tool definitions that go up on
            # every round — more than the system prompt — so the "pessimistic"
            # path was quietly the optimistic one.
            fallback_chars = len(str(messages)) + len(system) + len(_CHAT_TOOLS_JSON)
            counted_input = -(-fallback_chars // 3)

        try:
            reservation = budget.reserve(model, counted_input, max_tokens)
        except budget.BudgetExhausted as exc:
            raise HTTPException(status_code=429, detail=str(exc))
        except budget.BudgetUnavailable:
            raise HTTPException(
                status_code=503,
                detail="Spend accounting is unavailable, so this request "
                       "cannot be authorised. Try again shortly.",
            )
        except budget.UnpricedModel as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        try:
            resp = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a normal error response
            # The reservation is NOT released. From here we cannot tell a
            # connection that never opened from a response that was generated
            # and billed before the socket died, and the safe reading of an
            # ambiguous failure is that the money left the account. A smaller
            # daily allowance is an acceptable error; an unrecorded charge is
            # not. A stranded reservation expires with the day key.
            return {"error": str(exc)}

        # Reconcile to truth. Normally negative, because output was reserved
        # at the max_tokens ceiling and real replies are shorter.
        usage = getattr(resp, "usage", None)
        budget.settle(
            reservation,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )

        if resp.stop_reason != "tool_use":
            final_response = resp
            break

        # Execute all tool_use blocks in this turn concurrently (thread pool,
        # since the underlying Sleeper/FantasyCalc/ESPN calls are sync I/O).
        tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]
        # The trade being asked about is the one in his LATEST message, not
        # anything he mentioned earlier in the conversation. Scanning the whole
        # history meant a 2027 2nd named five messages ago withheld the verdict
        # on a later, player-only trade — the check firing on a trade that was
        # not under discussion. Tool results are appended as user-role messages
        # with list content, so the isinstance filter keeps this to what he
        # actually typed.
        latest_user_text = next(
            (m["content"] for m in reversed(messages)
             if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )
        tool_results = await asyncio.gather(
            *[
                asyncio.to_thread(execute_tool, b.name, b.input, latest_user_text)
                for b in tool_use_blocks
            ]
        )

        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(r)}
                for b, r in zip(tool_use_blocks, tool_results)
            ],
        })

    if final_response is None:
        return {"error": "Tool-calling loop reached maximum rounds without a final text response."}

    # Say so when the turn produced no text. Thinking blocks are content too,
    # so a response can come back well-formed and carry nothing to show —
    # most often because the token ceiling ran out mid-thought. Returning it
    # as-is leaves the client to render an empty bubble and guess why.
    payload = final_response.model_dump()
    blocks = payload.get("content") or []
    if not any(b.get("type") == "text" and (b.get("text") or "").strip() for b in blocks):
        stop = payload.get("stop_reason")
        payload["error"] = (
            "The reply hit its length cap before producing any text — the model spent the "
            "whole budget thinking. Try a narrower question."
            if stop == "max_tokens" else
            f"The model returned no text (stop_reason: {stop})."
        )
    return payload


def _budget_http_error(exc: Exception) -> HTTPException:
    """Give a budget refusal the status it means.

    The agent callbacks now refuse model calls from inside a running pipeline,
    which means these can surface well after the endpoint's own preflight has
    passed. Without this they escaped as 500s, so a working spend control
    looked like a broken service.
    """
    if isinstance(exc, budget.BudgetExhausted):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, budget.UnpricedModel):
        # Server misconfiguration, not the caller's fault: an agent is wired to
        # a model the spend system cannot cost. No provider call happened.
        return HTTPException(
            status_code=500,
            detail="This report is configured to use a model with no verified "
                   "price, so it cannot be run. " + str(exc),
        )
    # BudgetUnavailable and BudgetMisconfigured both land here.
    return HTTPException(
        status_code=503,
        detail="Spend accounting is unavailable, so this report cannot be "
               "authorised. Try again shortly.",
    )


def _is_budget_control(exc: object) -> bool:
    return isinstance(exc, (budget.BudgetExhausted, budget.BudgetUnavailable,
                            budget.UnpricedModel))


# ── Pipeline endpoints (LLM calls) ─────────────────────────────────────────
#
# synthesis_agent uses _run_in_thread() internally to dodge Streamlit's
# nested-asyncio constraint. In FastAPI's pure-async context that helper is
# harmless — each thread creates its own event loop, no collision.

# ── League Intel (no LLM) ───────────────────────────────────────────────────
#
# Server-side persistence for GM Command's League Intel: a real name, an
# AI-synthesized breakdown and add-only observations per leaguemate. The
# storage contract (server revisions, compare-and-set, write-once
# observations, key isolation) lives in intel_store; this layer adds access
# control, validation and HTTP status mapping. Nothing here calls a model.
#
# Status codes: 401 bad/missing key; 403 write from a non-production origin;
# 404 owner not in the league; 409 stale base_rev or reused observation id
# (with the current value, never an overwrite); 422 invalid input; 503 store,
# Sleeper or configuration unavailable. A 200 is only ever a complete answer.

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_EXCEPT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
_intel_members_cache: tuple[float, frozenset[str]] | None = None


class IntelNameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=40)
    base_rev: int = Field(ge=0)


class IntelBreakdownIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=4000)
    base_rev: int = Field(ge=0)
    covers: list[str] = Field(default_factory=list, max_length=intel_store.MAX_OBSERVATIONS)


class IntelObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    date_label: str = Field(default="", max_length=20)


def _intel_gate(request: Request, write: bool) -> None:
    try:
        chat_guard.enforce_intel(request, write=write)
    except chat_guard.ChatRefused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail)


def _intel_store() -> intel_store.IntelStore:
    try:
        return intel_store.store()
    except intel_store.IntelUnavailable:
        raise HTTPException(status_code=503, detail="Intel storage is unavailable.")


def _intel_league_members() -> frozenset[str]:
    global _intel_members_cache
    now = time.time()
    if _intel_members_cache and now - _intel_members_cache[0] < _LEAGUE_ROSTER_TTL_SECONDS:
        return _intel_members_cache[1]
    try:
        members = frozenset(str(u["user_id"]) for u in get_league_users(LEAGUE_ID))
    except Exception as exc:  # noqa: BLE001 — unknown is not "not a member"
        log.warning("intel_members_unavailable error=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="League membership is unavailable; try again.")
    _intel_members_cache = (now, members)
    return members


def _intel_owner(owner_id: str) -> str:
    if not intel_store.OWNER_ID_RE.fullmatch(owner_id):
        raise HTTPException(status_code=422, detail="Invalid owner id.")
    if owner_id not in _intel_league_members():
        raise HTTPException(status_code=404, detail="Not a member of this league.")
    return owner_id


def _intel_write(op: str, owner_id: str, fn):
    try:
        result = fn()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid intel data.")
    except intel_store.IntelUnavailable:
        log.warning("intel_write op=%s owner=%s outcome=unavailable", op, owner_id)
        raise HTTPException(status_code=503, detail="Intel storage is unavailable.")
    log.info("intel_write op=%s owner=%s outcome=%s rev=%s", op, owner_id,
             "ok" if result.get("ok") else "refused", result.get("rev", "-"))
    return result


@app.get("/intel")
def get_intel(request: Request) -> dict:
    _intel_gate(request, write=False)
    s = _intel_store()
    try:
        data = s.read_all()
    except intel_store.IntelUnavailable:
        raise HTTPException(status_code=503, detail="Intel storage is unavailable.")
    log.info("intel_read leaguemates=%d rev=%d", len(data["leaguemates"]), data["rev"])
    return data


@app.put("/intel/{owner_id}/name")
def put_intel_name(owner_id: str, body: IntelNameIn, request: Request) -> dict:
    _intel_gate(request, write=True)
    _intel_owner(owner_id)
    if body.value != body.value.strip() or _CONTROL.search(body.value):
        raise HTTPException(status_code=422, detail="Name must be one trimmed line.")
    s = _intel_store()
    result = _intel_write("name", owner_id, lambda: s.set_name(owner_id, body.value, body.base_rev))
    if not result["ok"]:
        raise HTTPException(status_code=409, detail={"conflict": "name", "current": result["current"]})
    return {"ok": True, "rev": result["rev"], "value": body.value}


@app.put("/intel/{owner_id}/breakdown")
def put_intel_breakdown(owner_id: str, body: IntelBreakdownIn, request: Request) -> dict:
    _intel_gate(request, write=True)
    _intel_owner(owner_id)
    if _CONTROL_EXCEPT_NEWLINE.search(body.value):
        raise HTTPException(status_code=422, detail="Breakdown contains control characters.")
    if len(set(body.covers)) != len(body.covers):
        raise HTTPException(status_code=422, detail="Duplicate observation ids in covers.")
    s = _intel_store()
    result = _intel_write("breakdown", owner_id,
                          lambda: s.set_breakdown(owner_id, body.value, body.base_rev, body.covers))
    if result.get("unknown_observation"):
        raise HTTPException(status_code=422, detail="covers names an observation that is not stored.")
    if not result["ok"]:
        raise HTTPException(status_code=409, detail={"conflict": "breakdown", "current": result["current"]})
    return {"ok": True, "rev": result["rev"]}


@app.put("/intel/{owner_id}/observations/{obs_id}")
def put_intel_observation(owner_id: str, obs_id: str, body: IntelObservationIn, request: Request) -> dict:
    _intel_gate(request, write=True)
    _intel_owner(owner_id)
    if not intel_store.OBS_ID_RE.fullmatch(obs_id):
        raise HTTPException(status_code=422, detail="Observation id must be a lowercase UUID.")
    if _CONTROL.search(body.date_label) or _CONTROL_EXCEPT_NEWLINE.search(body.text) or not body.text.strip():
        raise HTTPException(status_code=422, detail="Invalid observation text or date label.")
    s = _intel_store()
    result = _intel_write("observation", owner_id,
                          lambda: s.add_observation(owner_id, obs_id, body.text, body.date_label))
    if result.get("limit"):
        raise HTTPException(status_code=422, detail="Observation limit reached for this leaguemate.")
    if not result["ok"]:
        raise HTTPException(status_code=409, detail={"conflict": "observation", "rev": result["rev"]})
    return {"ok": True, "created": result["created"], "rev": result["rev"]}


@app.post("/report/player/{sleeper_id}")
async def report_player(sleeper_id: str, request: Request) -> dict:
    """Full per-player pipeline: situation + production + market + synthesis agents.
    Returns a complete player card. Typically 20–60 s per player."""
    try:
        chat_guard.enforce_report(request)
    except chat_guard.ChatRefused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail)
    from agents.synthesis_agent import run_synthesis_agent

    t0 = time.perf_counter()
    rss_before = _current_rss_mb()

    def log_outcome(event: str) -> None:
        duration_ms = (time.perf_counter() - t0) * 1000
        rss_after = _current_rss_mb()
        rss_delta = (
            rss_after - rss_before if rss_before is not None and rss_after is not None else None
        )
        log.info(
            "%s duration_ms=%.1f rss_before_mb=%s rss_after_mb=%s rss_delta_mb=%s",
            event,
            duration_ms,
            f"{rss_before:.1f}" if rss_before is not None else "unknown",
            f"{rss_after:.1f}" if rss_after is not None else "unknown",
            f"{rss_delta:.1f}" if rss_delta is not None else "unknown",
        )

    try:
        card = await run_synthesis_agent(sleeper_id)
    except (budget.BudgetExhausted, budget.BudgetUnavailable, budget.UnpricedModel) as exc:
        # Raised by the agent callbacks mid-pipeline, after preflight passed.
        log_outcome("player_report_failed")
        raise _budget_http_error(exc)
    log_outcome("player_report_complete")
    return card


@app.post("/report/roster/{owner}")
async def report_roster(owner: str, request: Request) -> dict:
    """Full roster pipeline: synthesis agent for every skill-position player, then
    roster_agent for the overall grade.

    Synthesis calls run concurrently (capped at 4 at a time to stay within
    Anthropic rate limits). Typically 2–5 minutes for a 24-player roster.
    """
    try:
        chat_guard.enforce_report(request)
    except chat_guard.ChatRefused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail)

    from agents.synthesis_agent import run_synthesis_agent
    from agents.roster_agent import run_roster_agent

    try:
        roster_obj = get_roster_by_display_name(LEAGUE_ID, owner)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    players = [
        p for p in resolve_roster_players(roster_obj)
        if p.get("position") in ("QB", "RB", "WR", "TE")
    ]

    sem = asyncio.Semaphore(4)

    async def _bounded(player_id: str) -> dict:
        async with sem:
            return await run_synthesis_agent(player_id)

    results = await asyncio.gather(
        *[_bounded(p["player_id"]) for p in players],
        return_exceptions=True,
    )

    # A budget refusal is not "this player failed". return_exceptions=True
    # flattens everything into the results list, so a spend control doing its
    # job would otherwise have been filed as a per-player data error and the
    # report would have carried on to grade a partial roster — and then made
    # one more model call to do it.
    budget_stops = [c for c in results if _is_budget_control(c)]
    if budget_stops:
        raise _budget_http_error(budget_stops[0])

    valid_cards = [c for c in results if isinstance(c, dict) and "error" not in c]
    error_entries = [
        {"player_id": players[i]["player_id"], "name": players[i].get("full_name"), "error": str(c)}
        for i, c in enumerate(results)
        if not isinstance(c, dict) or "error" in c
    ]

    # Reservations already taken by the concurrent batch are left alone: some
    # of those calls may have reached the provider, and releasing them would
    # under-count real spend.
    try:
        roster_grade = await run_roster_agent(valid_cards)
    except (budget.BudgetExhausted, budget.BudgetUnavailable, budget.UnpricedModel) as exc:
        raise _budget_http_error(exc)

    return {
        "owner": owner,
        "player_count": len(valid_cards),
        "player_cards": valid_cards,
        "errors": error_entries,
        "roster_grade": roster_grade,
    }
