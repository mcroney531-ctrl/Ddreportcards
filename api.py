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
  GET  /players/{sleeper_id}/blurb
  GET  /roster/{owner}

Pipeline endpoints (LLM, seconds–minutes):
  POST /report/player/{sleeper_id}   single player full card (synthesis_agent)
  POST /report/roster/{owner}        full roster pipeline + overall grade
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from config.dynasty_config import LEAGUE
from dynasty_core.sleeper import (
    get_all_players,
    get_roster_by_display_name,
    resolve_roster_players,
    get_trending_adds,
)
from dynasty_core.fantasycalc import (
    get_dynasty_values,
    index_by_sleeper_id,
    get_value_for_sleeper_id,
)
from dynasty_core.leaguelogs import ATTRIBUTION_HTML, get_player_blurb

LEAGUE_ID: str = LEAGUE["league_id"]

app = FastAPI(title="Dynasty Report Cards API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Fast endpoints (no LLM) ─────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


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


@app.get("/players/search")
def search_players(q: str = Query(..., min_length=2)) -> dict:
    """Case-insensitive substring search across all Sleeper skill-position players.
    Returns up to 20 results sorted by dynasty value (highest first)."""
    all_p = get_all_players()
    fc_values = get_dynasty_values()
    fc_index = index_by_sleeper_id(fc_values)
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
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
            "depth_chart_order": p.get("depth_chart_order"),
            "depth_chart_position": p.get("depth_chart_position"),
            "dynasty_value": fc.get("value"),
            "dynasty_pos_rank": fc.get("positionRank"),
            "redraft_value": fc.get("redraftValue"),
            "trend_30day": fc.get("trend30Day"),
        })
    matches.sort(key=lambda x: x["dynasty_value"] or 0, reverse=True)
    return {"results": matches[:20]}


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
    }


@app.get("/players/{sleeper_id}/blurb")
def player_blurb(sleeper_id: str) -> dict:
    """LLM-rewritten 1-3 sentence status note from LeagueLogs — narrative context
    (role change, injury note, hot/cold streak) beyond raw stats. blurb is null
    when LeagueLogs has no recent material for the player. Attribution to
    LeagueLogs is required by their terms whenever this data is displayed."""
    result = get_player_blurb(sleeper_id)
    return {
        "player_id": sleeper_id,
        "blurb": result.get("blurb") if result else None,
        "signals": result.get("signals", []) if result else [],
        "attribution_html": ATTRIBUTION_HTML,
    }


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
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
            "dynasty_value": fc.get("value"),
            "dynasty_pos_rank": fc.get("positionRank"),
            "redraft_value": fc.get("redraftValue"),
            "trend_30day": fc.get("trend30Day"),
        })
    enriched.sort(key=lambda x: x["dynasty_value"] or 0, reverse=True)
    return {
        "owner": owner,
        "league_id": LEAGUE_ID,
        "player_count": len(enriched),
        "players": enriched,
    }


# ── Pipeline endpoints (LLM calls) ─────────────────────────────────────────
#
# synthesis_agent uses _run_in_thread() internally to dodge Streamlit's
# nested-asyncio constraint. In FastAPI's pure-async context that helper is
# harmless — each thread creates its own event loop, no collision.

@app.post("/report/player/{sleeper_id}")
async def report_player(sleeper_id: str) -> dict:
    """Full per-player pipeline: situation + production + market + synthesis agents.
    Returns a complete player card. Typically 20–60 s per player."""
    from agents.synthesis_agent import run_synthesis_agent
    return await run_synthesis_agent(sleeper_id)


@app.post("/report/roster/{owner}")
async def report_roster(owner: str) -> dict:
    """Full roster pipeline: synthesis agent for every skill-position player, then
    roster_agent for the overall grade.

    Synthesis calls run concurrently (capped at 4 at a time to stay within
    Anthropic rate limits). Typically 2–5 minutes for a 24-player roster.
    """
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

    valid_cards = [c for c in results if isinstance(c, dict) and "error" not in c]
    error_entries = [
        {"player_id": players[i]["player_id"], "name": players[i].get("full_name"), "error": str(c)}
        for i, c in enumerate(results)
        if not isinstance(c, dict) or "error" in c
    ]

    roster_grade = await run_roster_agent(valid_cards)

    return {
        "owner": owner,
        "player_count": len(valid_cards),
        "player_cards": valid_cards,
        "errors": error_entries,
        "roster_grade": roster_grade,
    }
