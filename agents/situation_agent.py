"""
Situation Agent — evaluates opportunity only, independent of talent, for a
ROSTERED dynasty player. Adapted from Scout's rookie-draft situation_agent:
draft capital is dropped (not meaningful once a player is rostered and
established), depth chart + competition-quality assessment carries over.

Inputs:  Sleeper player_id
Outputs: structured opportunity assessment with Opportunity Grade (0-100 -> letter)
"""

import os, sys, json, re
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from data import sleeper_client, fantasycalc_client

# ── Competition grade tiers (12-team superflex calibration) ───────────────────

GRADE_TIERS = {
    "QB": [(12, "A"), (20, "B"), (28, "C"), (36, "D")],
    "RB": [(12, "A"), (24, "B"), (36, "C"), (48, "D")],
    "WR": [(12, "A"), (24, "B"), (36, "C"), (54, "D")],
    "TE": [(6, "A"), (12, "B"), (18, "C"), (24, "D")],
}


def _value_grade(position: str, redraft_position_rank: int | None) -> str:
    if redraft_position_rank is None:
        return "F"
    for thresh, grade in GRADE_TIERS.get(position, []):
        if redraft_position_rank <= thresh:
            return grade
    return "F"


# ── ADK tool functions ────────────────────────────────────────────────────────

def lookup_player_situation(player_id: str) -> dict:
    """
    Look up a rostered player's opportunity signals from Sleeper:
    depth_chart_order (1 = starter, higher = deeper backup), depth_chart_position,
    team, status, injury_status.
    player_id: Sleeper player_id (e.g. '5967' for Tony Pollard)
    """
    all_players = sleeper_client.get_all_players()
    p = all_players.get(player_id)
    if not p:
        return {"error": f"No Sleeper player found for player_id={player_id!r}"}
    return {
        "player_id": player_id,
        "full_name": p.get("full_name"),
        "position": p.get("position"),
        "team": p.get("team"),
        "depth_chart_order": p.get("depth_chart_order"),
        "depth_chart_position": p.get("depth_chart_position"),
        "status": p.get("status"),
        "injury_status": p.get("injury_status"),
        "years_exp": p.get("years_exp"),
        "age": p.get("age"),
    }


def get_position_depth(team: str, position: str) -> dict:
    """
    Return all players at a given position on a given team, ordered by depth_chart_order.
    team: NFL team abbreviation, Sleeper convention (e.g. 'TEN', 'WAS')
    position: 'QB', 'RB', 'WR', or 'TE'
    """
    all_players = sleeper_client.get_all_players()
    depth = [
        {
            "player_id": pid,
            "name": p.get("full_name"),
            "depth_chart_order": p.get("depth_chart_order"),
            "years_exp": p.get("years_exp"),
            "status": p.get("status"),
        }
        for pid, p in all_players.items()
        if p.get("team") == team
        and p.get("position") == position
        and p.get("depth_chart_order") is not None
    ]
    depth.sort(key=lambda x: x["depth_chart_order"] or 99)
    return {"team": team, "position": position, "depth_chart": depth}


def assess_position_competition(team: str, position: str, exclude_player_id: str) -> dict:
    """
    Grade the QUALITY of every other active player at this team+position (rookies
    and veterans alike), not just count bodies. Competitors are graded by their
    FantasyCalc REDRAFT position rank (who is actually eating snaps now — a
    prospect with high dynasty value but no current role isn't a real block yet).

    team: NFL team abbreviation (e.g. 'TEN')
    position: 'QB', 'RB', 'WR', or 'TE'
    exclude_player_id: the rostered player being evaluated, excluded from their
      own competition list.
    """
    all_players = sleeper_client.get_all_players()
    fc_values = fantasycalc_client.get_dynasty_values()
    fc_by_sleeper = fantasycalc_client.index_by_sleeper_id_with_redraft_rank(fc_values)

    competitors = []
    for pid, p in all_players.items():
        if pid == exclude_player_id:
            continue
        if p.get("team") != team or p.get("position") != position or not p.get("active"):
            continue
        fc = fc_by_sleeper.get(pid)
        if fc:
            grade = _value_grade(position, fc.get("redraftPositionRank"))
            competitors.append({
                "name": p.get("full_name"),
                "years_exp": p.get("years_exp"),
                "grade": grade,
                "dynasty_value": fc.get("value"),
                "redraft_position_rank": fc.get("redraftPositionRank"),
            })
        else:
            competitors.append({
                "name": p.get("full_name"),
                "years_exp": p.get("years_exp"),
                "grade": "F",
                "dynasty_value": 0,
                "note": "outside FantasyCalc's dynasty-relevant pool — replaceable depth",
            })

    competitors.sort(key=lambda c: c.get("dynasty_value") or 0, reverse=True)
    grade_counts = {g: sum(1 for c in competitors if c["grade"] == g) for g in ["A", "B", "C", "D", "F"]}
    grade_order = ["A", "B", "C", "D", "F"]
    strongest = next((g for g in grade_order if grade_counts[g] > 0), None)

    if not competitors:
        room_strength = "no other rostered-relevant competition at this position on the team"
    elif strongest in ("A", "B"):
        room_strength = "strong — at least one entrenched starter-quality player pushes back"
    elif strongest == "C":
        room_strength = "moderate — a flex-level competitor, but no entrenched star"
    else:
        room_strength = "soft — only replaceable depth (grade D/F) around them"

    return {
        "team": team,
        "position": position,
        "competitor_count": len(competitors),
        "grade_counts": grade_counts,
        "strongest_competitor_grade": strongest,
        "room_strength": room_strength,
        "competitors": competitors[:8],
    }


def get_trending_sentiment(limit: int = 50) -> dict:
    """Optional color signal — Sleeper trending adds. Relative rank, not absolute."""
    trending = sleeper_client.get_trending_adds(limit=limit)
    all_players = sleeper_client.get_all_players()
    result = []
    for t in trending:
        pid = t.get("player_id")
        p = all_players.get(pid, {})
        if p.get("position") in ("QB", "RB", "WR", "TE"):
            result.append({
                "player_id": pid,
                "name": p.get("full_name"),
                "position": p.get("position"),
                "team": p.get("team"),
                "add_count": t.get("count"),
            })
    return {
        "trending_adds": result,
        "note": "Counts reflect raw platform activity — interpret relative to each other, not as absolute thresholds.",
    }


# ── Calibration anchors ────────────────────────────────────────────────────────

OPPORTUNITY_CALIBRATION = """
Opportunity Grade calibration anchors (0-100):
- 95-100 (A+): Entrenched lead role on a contending team, no meaningful competition, elite scheme fit
- 85-94  (A/A-): Clear lead role, minimal competition, good scheme, good team context
- 75-84  (B+/B): Solid role but meaningful competition OR suboptimal scheme/team context
- 65-74  (B-/C+): Legitimate role but sharing touches or unclear/committee depth chart
- 50-64  (C/C-): Complementary/backup role with a path to more, or lead role in a low-volume offense
- 35-49  (D+/D): Deep backup or clearly blocked by an established teammate
- 0-34   (D-/F): No clear path to meaningful touches

Letter grade conversion:
97-100→A+, 93-96→A, 90-92→A-, 87-89→B+, 83-86→B, 80-82→B-,
77-79→C+, 73-76→C, 70-72→C-, 67-69→D+, 63-66→D, 60-62→D-, <60→F
"""

SYSTEM_PROMPT = f"""You are the Situation Agent for a dynasty fantasy football roster report card tool.

Your job: evaluate a ROSTERED player's OPPORTUNITY only — completely independent of how
talented they are. Do not factor in career production or athleticism into this grade.
Focus entirely on: depth chart position, scheme fit, team context, and quality of the
competition for touches/targets/snaps.

{OPPORTUNITY_CALIBRATION}

Tools available:
- lookup_player_situation: Sleeper depth chart data, team, and status for the player
- get_position_depth: The full depth chart at their position on their team
- assess_position_competition: Grade the QUALITY of every other player at that
  team+position (FantasyCalc redraft position rank)
- get_trending_sentiment: Optional — Sleeper trending adds for context

CRITICAL — competition is about QUALITY, not headcount. Do NOT simply penalize a player
for having bodies on the depth chart. Use assess_position_competition to grade them:
- Only grade D/F competitors (replaceable depth) is a SOFT room — reflect this as a
  HIGHER opportunity grade, since the path stays clear.
- A single grade A or B competitor (entrenched starter-quality player) is a genuine
  threat to touches — LOWER the grade even if the player is currently "starting."
Always state the competition explicitly, e.g. "1 backup on roster, grade D → soft room."

Steps:
1. Call lookup_player_situation to get depth chart position and team
2. Call get_position_depth to see the full depth chart ordering
3. Call assess_position_competition to grade the quality of the competition
4. Synthesize into an Opportunity Grade (numeric 0-100, then letter), explicitly
   weighing competition QUALITY over raw count

Output format — always return a JSON object with these exact keys:
{{
  "player": "Full Name",
  "position": "RB",
  "team": "TEN",
  "depth_chart_order": 1,
  "opportunity_score": 82,
  "opportunity_grade": "B",
  "competition": {{
    "competitor_count": 2,
    "strongest_competitor_grade": "D",
    "room_strength": "soft — only replaceable depth around them",
    "summary": "2 RBs behind him on the depth chart but both grade D/F — soft room"
  }},
  "key_factors": ["RB1 on depth chart", "pass-catching role adds volume floor"],
  "concerns": ["rookie added in draft could siphon early-down work later in the year"],
  "summary": "Two-sentence plain-English summary of opportunity outlook."
}}
"""

# ── Agent + runner setup ──────────────────────────────────────────────────────

def build_situation_agent() -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        name="situation_agent",
        instruction=SYSTEM_PROMPT,
        tools=[
            lookup_player_situation,
            get_position_depth,
            assess_position_competition,
            get_trending_sentiment,
        ],
    )


async def run_situation_agent(player_id: str) -> dict:
    """Run the Situation Agent for a given Sleeper player_id. Returns parsed JSON output."""
    agent = build_situation_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="dynasty_report_cards", session_service=session_service)

    session_id = f"sit_session_{player_id}"
    await session_service.create_session(
        app_name="dynasty_report_cards", user_id="user", session_id=session_id
    )

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"Evaluate the opportunity for player_id {player_id}")]
    )

    result_text = ""
    async for event in runner.run_async(
        user_id="user", session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    result_text += part.text

    json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group())
    return {"raw_output": result_text}


# ── CLI test entry point ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running Situation Agent for player_id: {player_id}\n")
    result = asyncio.run(run_situation_agent(player_id))
    print(json.dumps(result, indent=2))
