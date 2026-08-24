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
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.dynasty_config import LEAGUE
from dynasty_core.sleeper import (
    get_all_players,
    get_league_users,
    get_league_rosters,
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
        pos_values: dict[str, list[int]] = {"QB": [], "RB": [], "WR": [], "TE": []}
        for pid in roster.get("players") or []:
            meta = all_p.get(pid)
            if not meta or meta.get("position") not in pos_values:
                continue
            fc = fc_index.get(pid)
            pos_values[meta["position"]].append((fc.get("value") if fc else 0) or 0)

        positions = {
            pos: {"count": len(values), "total_value": sum(values), "top_value": max(values, default=0)}
            for pos, values in pos_values.items()
        }
        teams.append({
            "owner": owner,
            "total_dynasty_value": sum(p["total_value"] for p in positions.values()),
            "positions": positions,
        })

    teams.sort(key=lambda t: t["total_dynasty_value"], reverse=True)
    return {"teams": teams}


class TradeEvaluateRequest(BaseModel):
    team_a_sends: list[str]
    team_b_sends: list[str]


def _describe_trade_side(sleeper_ids: list[str], all_players: dict, fc_index: dict) -> dict:
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
        total_value += value or 0
        players.append({
            "player_id": pid,
            "name": meta.get("full_name"),
            "position": meta.get("position"),
            "dynasty_value": value,
            "dynasty_pos_rank": fc.get("positionRank") if fc else None,
            "unvalued": fc is None,
        })
    return {"players": players, "total_value": total_value, "unknown_ids": unknown_ids}


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

    team_a = _describe_trade_side(body.team_a_sends, all_p, fc_index)
    team_b = _describe_trade_side(body.team_b_sends, all_p, fc_index)

    a_value, b_value = team_a["total_value"], team_b["total_value"]
    net_to_a = b_value - a_value
    bigger_side = max(a_value, b_value, 1)
    delta_pct = round(abs(net_to_a) / bigger_side * 100, 1)
    winner = "team_a" if net_to_a > 0 else "team_b" if net_to_a < 0 else None

    warnings = []
    for label, side in (("team_a_sends", team_a), ("team_b_sends", team_b)):
        for pid in side["unknown_ids"]:
            warnings.append(f"Unknown sleeper_id {pid!r} in {label} — not a tracked skill-position player")

    return {
        "team_a_sends": team_a["players"],
        "team_a_value_sent": a_value,
        "team_b_sends": team_b["players"],
        "team_b_value_sent": b_value,
        "net_value_to_team_a": net_to_a,
        "value_delta_pct": delta_pct,
        "fairness": _fairness_label(delta_pct, winner),
        "warnings": warnings,
    }


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
        "name": "get_player_blurb",
        "description": (
            "Get a short LLM-written narrative blurb about a player from LeagueLogs — context on "
            "role changes, injury notes, or hot/cold streaks beyond raw stats. Not every player has "
            "one; blurb will be null if there is no recent material. "
            "LeagueLogs attribution is required whenever you use this data: if blurb is non-null, "
            'end your response with a short plain-text attribution line, e.g. "Powered by LeagueLogs (leaguelogs.com)". '
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
        "name": "evaluate_trade",
        "description": (
            "Evaluate a proposed trade using actual FantasyCalc dynasty values — not a guess. "
            "Give the sleeper_ids each side is sending away; returns each side's total value sent, "
            "the net value delta, and a fairness read (even trade / slight edge / lopsided). "
            "ALWAYS use this for any trade-fairness question instead of estimating values from memory — "
            "get sleeper_ids from get_player_value or get_roster first."
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
            },
            "required": ["team_a_sends", "team_b_sends"],
        },
    },
]


def _execute_chat_tool(name: str, tool_input: dict) -> dict:
    """Dispatch a Claude tool call to the matching route function above,
    in-process — no HTTP round-trip. Route functions raise HTTPException on
    a bad lookup (unknown player/team); convert that to an {"error": ...}
    dict instead of letting it propagate and abort the whole chat turn."""
    try:
        if name == "get_player_value":
            return search_players(q=tool_input.get("player_name", ""))
        if name == "get_roster":
            return roster_data(owner=tool_input.get("owner", ""))
        if name == "get_league_rosters_summary":
            return league_rosters_summary()
        if name == "get_trending_players":
            limit = min(100, max(1, tool_input.get("limit") or 25))
            return trending_players(limit=limit)
        if name == "get_league_info":
            return league_info()
        if name == "get_team_roster":
            return team_roster(team=(tool_input.get("team") or "").upper())
        if name == "get_player_news":
            return player_news(sleeper_id=tool_input.get("sleeper_id", ""))
        if name == "get_player_blurb":
            return player_blurb(sleeper_id=tool_input.get("sleeper_id", ""))
        if name == "evaluate_trade":
            req = TradeEvaluateRequest(
                team_a_sends=tool_input.get("team_a_sends") or [],
                team_b_sends=tool_input.get("team_b_sends") or [],
            )
            return evaluate_trade(req)
        return {"error": f"Unknown tool: {name}"}
    except HTTPException as exc:
        return {"error": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 — tool errors must not kill the chat turn
        return {"error": str(exc)}


CHAT_SYSTEM_PROMPT = (
    "Today's date is {today}. Your NFL training data has a cutoff around mid-2025 — roughly one full season behind. "
    "CRITICAL RULES — always follow before answering:\n"
    "1. Call get_player_value for any player you discuss to get their live team, depth_chart_order, and status.\n"
    "2. Call get_team_roster before naming ANY player's competition or describing a backfield/WR corps — "
    "never assume from training data who is on a team. Players get cut, traded, and replaced every offseason.\n"
    "3. Call get_player_news for any player whose current depth chart position, injury, or team membership is central to the answer.\n"
    "4. If a player's team in tool results is null or missing, they are a free agent or out of the league — do not claim they compete with anyone.\n"
    "5. If you use a non-null blurb from get_player_blurb, end your response with a short plain-text "
    'attribution line: "Powered by LeagueLogs (leaguelogs.com)" — required by their terms.\n'
    "6. For any question about whether a trade is fair, call evaluate_trade with the sleeper_ids on "
    "each side — never estimate the value delta yourself.\n"
)


@app.post("/chat")
async def chat(request: Request) -> dict:
    """GM Command's assistant. Accepts the same body shape the frontend has
    always sent {model, max_tokens, messages, system?} and returns the same
    Anthropic Messages API response shape — the frontend needs no changes.
    Runs the tool-calling loop server-side (up to 5 rounds), same as the old
    Netlify function, but without Netlify's 10s ceiling and without the extra
    HTTP hop for each tool call."""
    body = await request.json()

    messages = list(body.get("messages") or [])
    model = body.get("model") or "claude-sonnet-4-5"
    max_tokens = body.get("max_tokens") or 1024
    frontend_system = body.get("system")

    today = datetime.date.today().isoformat()
    system = CHAT_SYSTEM_PROMPT.format(today=today)
    if frontend_system:
        system += f"\n\n{frontend_system}"

    final_response = None

    # Tool-calling loop — continue until Claude returns a text response.
    # Safety cap of 5 rounds prevents runaway loops.
    for _ in range(5):
        try:
            resp = await asyncio.to_thread(
                _anthropic_client.messages.create,
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=CHAT_TOOLS,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a normal error response
            return {"error": str(exc)}

        if resp.stop_reason != "tool_use":
            final_response = resp
            break

        # Execute all tool_use blocks in this turn concurrently (thread pool,
        # since the underlying Sleeper/FantasyCalc/ESPN calls are sync I/O).
        tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]
        tool_results = await asyncio.gather(
            *[asyncio.to_thread(_execute_chat_tool, b.name, b.input) for b in tool_use_blocks]
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

    return final_response.model_dump()


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
