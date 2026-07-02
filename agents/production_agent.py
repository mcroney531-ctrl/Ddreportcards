"""
Production Agent — evaluates production/talent only, independent of situation,
for a ROSTERED dynasty player. Adapted from Scout's rookie production_agent:
college stats are swapped for current + prior NFL season production via ESPN's
Core API. The Risk Modifier still combines durability (1-5) + injury probability,
now also folding in an age-curve component (years_exp/age from Sleeper), since a
rostered veteran's dynasty risk includes "how much window is left."

Inputs:  Sleeper player_id
Outputs: structured production assessment + risk modifier
"""

import os, sys, json, re, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from data import sleeper_client, espn_client

# ── Age-curve calibration (position-specific dynasty decline windows) ─────────

AGE_CURVES = {
    "RB": {"decline_age": 27, "cliff_age": 29},
    "WR": {"decline_age": 30, "cliff_age": 32},
    "TE": {"decline_age": 30, "cliff_age": 32},
    "QB": {"decline_age": 36, "cliff_age": 39},
}


def _most_recent_completed_season() -> int:
    """NFL season year = the calendar year it kicks off in (September)."""
    today = datetime.date.today()
    return today.year - 1 if today.month < 9 else today.year


# ── ADK tool functions ────────────────────────────────────────────────────────

def lookup_player_info(player_id: str) -> dict:
    """
    Look up a rostered player's basic profile and current injury/practice status
    from Sleeper. Returns name, position, team, age, years_exp, status,
    injury_status, and their ESPN athlete id (needed for production/injury lookups).
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
        "age": p.get("age"),
        "years_exp": p.get("years_exp"),
        "status": p.get("status"),
        "injury_status": p.get("injury_status"),
        "injury_start_date": p.get("injury_start_date"),
        "practice_participation": p.get("practice_participation"),
        "espn_id": p.get("espn_id"),
    }


def get_current_season_production(espn_athlete_id: str, season: int | None = None) -> dict:
    """
    Fetch one NFL regular season's production stats from ESPN (rushing/receiving/
    passing/scoring, flattened to {stat_name: value}). season defaults to the
    most recently completed NFL season.
    espn_athlete_id: the player's ESPN athlete id
    """
    season = season or _most_recent_completed_season()
    stats = espn_client.get_season_statistics(str(espn_athlete_id), season)
    return {"espn_id": espn_athlete_id, "season": season, "stats": espn_client.flatten_statistics(stats)}


def get_prior_season_production(espn_athlete_id: str) -> dict:
    """Fetch the season before the most recently completed one, for trend comparison."""
    season = _most_recent_completed_season() - 1
    stats = espn_client.get_season_statistics(str(espn_athlete_id), season)
    return {"espn_id": espn_athlete_id, "season": season, "stats": espn_client.flatten_statistics(stats)}


def get_career_production(espn_athlete_id: str) -> dict:
    """Fetch career totals (all NFL seasons combined) from ESPN."""
    stats = espn_client.get_career_statistics(str(espn_athlete_id))
    return {"espn_id": espn_athlete_id, "stats": espn_client.flatten_statistics(stats)}


def get_injury_notes(espn_athlete_id: str, team: str) -> dict:
    """
    Fetch recent injury/status notes for the player from ESPN's team injury feed.
    team: NFL team abbreviation, Sleeper convention (e.g. 'TEN', 'WAS')
    """
    team_id = espn_client.team_espn_id(team)
    if not team_id:
        return {"error": f"Unknown team abbreviation {team!r}"}
    notes = espn_client.get_player_injury_notes(str(espn_athlete_id), team_id)
    return {
        "espn_id": espn_athlete_id,
        "notes": [
            {"status": n.get("status"), "date": n.get("date"), "comment": n.get("shortComment")}
            for n in notes
        ],
    }


def compute_age_curve_signal(position: str, age: int | None, years_exp: int | None) -> dict:
    """
    Estimate dynasty career-window remaining from position-specific aging curves.
    Purely age/experience-based risk context — not a talent judgment.
    position: 'QB', 'RB', 'WR', or 'TE'
    """
    if age is None:
        return {"aging_risk": "unknown", "note": "no age on file"}
    c = AGE_CURVES.get(position, {"decline_age": 29, "cliff_age": 31})
    if age < c["decline_age"]:
        risk = "low"
        note = f"{c['decline_age'] - age}+ years before the typical {position} decline age"
    elif age < c["cliff_age"]:
        risk = "moderate"
        note = f"entering the typical {position} decline window ({c['decline_age']}-{c['cliff_age']})"
    else:
        risk = "high"
        note = f"past the typical {position} cliff age ({c['cliff_age']}) — short window remaining"
    return {
        "position": position,
        "age": age,
        "years_exp": years_exp,
        "aging_risk": risk,
        "career_window_note": note,
    }


# ── Calibration anchors ───────────────────────────────────────────────────────

PRODUCTION_CALIBRATION = """
Production Grade calibration anchors (0-100) — evaluate on-field NFL production
efficiency and volume ONLY, ignore landing spot/opportunity (that's a separate agent):
- 95-100 (A+): Historically elite season production, top-3 at position, workhorse volume
- 85-94  (A/A-): Clear top-12 producer at position, strong efficiency and volume both
- 75-84  (B+/B): Solid top-24 producer, above-average efficiency or volume
- 65-74  (B-/C+): Usable committee/complementary producer, decent efficiency, capped volume
- 50-64  (C/C-): Replacement-level production, inconsistent, or limited sample (injury/role)
- 35-49  (D+/D): Thin production even accounting for role, concerning efficiency
- 0-34   (D-/F): Minimal production evidence, buried or non-functional as a producer

Letter grade conversion:
97-100→A+, 93-96→A, 90-92→A-, 87-89→B+, 83-86→B, 80-82→B-,
77-79→C+, 73-76→C, 70-72→C-, 67-69→D+, 63-66→D, 60-62→D-, <60→F

Risk Modifier — durability score 1-5 (5 = most durable) and injury chance %,
now also folding in the age-curve signal (a rostered veteran's dynasty risk
includes how much productive window is left, not just injury history):
- 5 / <10%: No injury history, full practice participation, low aging risk
- 4 / 10-20%: Minor injury history or moderate aging risk, otherwise healthy
- 3 / 20-35%: Moderate injury history OR high aging risk (past position cliff age)
- 2 / 35-50%: Recurrent injuries or current significant limitation
- 1 / >50%: Chronic durability concerns or clearly declining/injured with little window left
"""

SYSTEM_PROMPT = f"""You are the Production Agent for a dynasty fantasy football roster report card tool.

Your job: evaluate a ROSTERED player's on-field PRODUCTION only — completely independent
of their situation/opportunity (a separate agent handles that). Focus on: NFL production
volume and efficiency (most recent completed season, with prior-season trend context),
positional value, and durability (Risk Modifier, including the age-curve signal).

{PRODUCTION_CALIBRATION}

Tools available:
- lookup_player_info: Basic profile + live injury/practice status from Sleeper, + ESPN athlete id
- get_current_season_production: Most recently completed NFL season's stats
- get_prior_season_production: The season before that, for trend comparison
- get_career_production: Career totals for broader context
- get_injury_notes: Recent injury/status notes from ESPN's team feed
- compute_age_curve_signal: Position-specific aging-window risk from age/years_exp

Steps:
1. Call lookup_player_info for basic profile, live injury status, and ESPN athlete id
2. Call get_current_season_production and get_prior_season_production to see the trend
3. Call get_career_production for broader context
4. Call get_injury_notes for recent injury/status signal
5. Call compute_age_curve_signal using the player's position/age/years_exp
6. Combine injury notes + age-curve signal into the Risk Modifier
7. Synthesize all into a Production Grade

Key stats to weight for skill positions (field names come from ESPN's flattened stats):
- RB: rushingYards, rushingAttempts, yardsPerRushAttempt, rushingTouchdowns, receptions, receivingYards
- WR/TE: receptions, receivingYards, yardsPerReception, receivingTouchdowns, longReception
- QB: completionPct, passingYards, yardsPerPassAttempt, passingTouchdowns, interceptions, rushingYards

Output format — always return a JSON object with these exact keys:
{{
  "player": "Full Name",
  "position": "RB",
  "espn_athlete_id": "3916148",
  "current_season": 2025,
  "key_stats": {{
    "rushing_yards": 1082,
    "rushing_attempts": 242,
    "yards_per_carry": 4.5,
    "rushing_tds": 5,
    "receptions": 33,
    "receiving_yards": 206
  }},
  "trend_note": "One sentence comparing current season to prior season.",
  "risk_modifier": {{
    "durability_score": 4,
    "injury_chance_pct": 15,
    "injury_notes": "Brief description of any relevant recent injury/status notes",
    "aging_risk": "moderate",
    "career_window_note": "entering the typical RB decline window (27-29)"
  }},
  "production_score": 78,
  "production_grade": "B",
  "key_factors": ["bullet points on what drives the grade"],
  "concerns": ["any production or durability concerns"],
  "summary": "Two-sentence plain-English summary of production outlook."
}}
"""

# ── Agent + runner ────────────────────────────────────────────────────────────

def build_production_agent() -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        name="production_agent",
        instruction=SYSTEM_PROMPT,
        tools=[
            lookup_player_info,
            get_current_season_production,
            get_prior_season_production,
            get_career_production,
            get_injury_notes,
            compute_age_curve_signal,
        ],
    )


async def run_production_agent(player_id: str) -> dict:
    """Run the Production Agent for a given Sleeper player_id. Returns parsed JSON output."""
    agent = build_production_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="dynasty_report_cards", session_service=session_service)

    session_id = f"prod_session_{player_id}"
    await session_service.create_session(
        app_name="dynasty_report_cards", user_id="user", session_id=session_id
    )

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"Evaluate the production for player_id {player_id}")]
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


if __name__ == "__main__":
    import asyncio
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running Production Agent for player_id: {player_id}\n")
    result = asyncio.run(run_production_agent(player_id))
    print(json.dumps(result, indent=2))
