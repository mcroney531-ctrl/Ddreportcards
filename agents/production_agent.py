"""
Production Agent — evaluates production/talent only, independent of situation,
for a ROSTERED dynasty player. Adapted from Scout's rookie production_agent:
college stats are swapped for the most recently completed NFL regular season
(the historical production baseline) + the prior completed season via ESPN's
Core API -- never partial in-progress totals. production_score/production_grade
measure that demonstrated production only. The Risk Modifier is reported
alongside it, not folded into it: a current-health/availability score (1-5,
from Sleeper's live injury/practice status + ESPN's team injury feed) and a
separately-computed age-curve signal (years_exp/age from Sleeper), since a
rostered veteran's dynasty risk includes "how much window is left." Those two
signals are applied numerically once, downstream, by the Market agent's
proprietary composite -- never inside production_score. There is
no predictive model behind a numeric injury probability, and current
injury/status evidence does not establish a historical durability record, so
neither is claimed.

Inputs:  Sleeper player_id
Outputs: structured production assessment + risk modifier
"""

import os, sys, json, re, datetime, functools
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agents.usage import (
    AGENT_MAX_OUTPUT_TOKENS,
    check_budget_before_model,
    clear_invocation_reservation,
    record_model_usage,
)

from data import sleeper_client, espn_client, fantasycalc_client

# ── Age-curve calibration (position-specific dynasty decline windows) ─────────

AGE_CURVES = {
    "RB": {"decline_age": 27, "cliff_age": 29},
    "WR": {"decline_age": 30, "cliff_age": 32},
    "TE": {"decline_age": 30, "cliff_age": 32},
    "QB": {"decline_age": 36, "cliff_age": 39},
}


def _most_recent_completed_season(as_of: datetime.date | None = None) -> int:
    """The most recently COMPLETED NFL regular season, as of `as_of` (default today).

    An NFL season is named for the calendar year it kicks off in, and its
    regular season ends in January of the next year. Season N counts as
    completed from February 1 of N+1:
      January       -> year - 2   (season year - 1 may still be finishing)
      February-Dec  -> year - 1   (September kickoff does NOT make the new,
                                   in-progress season "completed")
    Deliberately conservative: one season stale for part of January beats
    ever feeding partial totals to a tool described as completed-season data.
    (api._current_nfl_season is different on purpose: chat wants the season
    in progress.)
    """
    today = as_of or datetime.date.today()
    return today.year - 2 if today.month == 1 else today.year - 1


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

    # Sleeper's espn_id is null for some recent rookies — FantasyCalc carries
    # its own ESPN sync as a fallback, matched by sleeperId. Deep-roster
    # players can still come back null from both; that's a legitimate
    # "no ESPN data" case, not a bug.
    espn_id = p.get("espn_id")
    if not espn_id:
        fc = fantasycalc_client.get_value_for_sleeper_id(player_id)
        espn_id = fc["player"].get("espnId") if fc else None

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
        "espn_id": espn_id,
    }


def _valid_espn_id(espn_athlete_id) -> bool:
    """
    Guards against the LLM passing through a missing id as the literal string
    "null"/"none" (seen when lookup_player_info's espn_id comes back None for
    a player too deep on the roster for both Sleeper and FantasyCalc to have
    an ESPN id on file) — without this, that string reaches ESPN's API as a
    URL segment and comes back a 400, not a clean "no data" case.
    """
    return bool(espn_athlete_id) and str(espn_athlete_id).lower() not in ("null", "none")


def get_current_season_production(espn_athlete_id: str, season: int | None = None) -> dict:
    """
    Fetch one NFL regular season's production stats from ESPN (rushing/receiving/
    passing/scoring, flattened to {stat_name: value}). season defaults to the
    most recently completed NFL regular season -- the historical production
    baseline, not season-to-date totals for a season still in progress.
    espn_athlete_id: the player's ESPN athlete id
    """
    if not _valid_espn_id(espn_athlete_id):
        return {"error": "no ESPN athlete id on file for this player — likely too deep on the roster to have NFL stats tracked"}
    if season is None:
        season = _most_recent_completed_season()
    stats = espn_client.get_season_statistics(str(espn_athlete_id), season)
    return {"espn_id": espn_athlete_id, "season": season, "stats": espn_client.flatten_statistics(stats)}


def get_prior_season_production(espn_athlete_id: str) -> dict:
    """Fetch the completed regular season before the most recently completed one,
    for trend comparison."""
    if not _valid_espn_id(espn_athlete_id):
        return {"error": "no ESPN athlete id on file for this player — likely too deep on the roster to have NFL stats tracked"}
    season = _most_recent_completed_season() - 1
    stats = espn_client.get_season_statistics(str(espn_athlete_id), season)
    return {"espn_id": espn_athlete_id, "season": season, "stats": espn_client.flatten_statistics(stats)}


def get_career_production(espn_athlete_id: str) -> dict:
    """Fetch career totals (all NFL seasons combined) from ESPN."""
    if not _valid_espn_id(espn_athlete_id):
        return {"error": "no ESPN athlete id on file for this player — likely too deep on the roster to have NFL stats tracked"}
    stats = espn_client.get_career_statistics(str(espn_athlete_id))
    return {"espn_id": espn_athlete_id, "stats": espn_client.flatten_statistics(stats)}


def get_injury_notes(espn_athlete_id: str, team: str) -> dict:
    """
    Fetch recent injury/status notes for the player from ESPN's team injury feed.
    team: NFL team abbreviation, Sleeper convention (e.g. 'TEN', 'WAS')
    """
    if not _valid_espn_id(espn_athlete_id):
        return {"error": "no ESPN athlete id on file for this player"}
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
    Classify aging risk against this model's configured position aging thresholds
    (AGE_CURVES). Purely age/experience-based risk context — not a talent
    judgment. The thresholds are configured values, not cited research.
    position: 'QB', 'RB', 'WR', or 'TE'
    """
    if age is None:
        return {"aging_risk": "unknown", "note": "no age on file"}
    c = AGE_CURVES.get(position, {"decline_age": 29, "cliff_age": 31})
    if age < c["decline_age"]:
        risk = "low"
        note = (f"{c['decline_age'] - age}+ years before this model's configured {position} "
                f"aging-risk threshold ({c['decline_age']})")
    elif age < c["cliff_age"]:
        risk = "moderate"
        note = f"within this model's configured {position} moderate aging-risk window ({c['decline_age']}-{c['cliff_age']})"
    else:
        risk = "high"
        note = f"at or past this model's configured {position} high aging-risk threshold ({c['cliff_age']})"
    return {
        "position": position,
        "age": age,
        "years_exp": years_exp,
        "aging_risk": risk,
        "career_window_note": note,
    }


# ── Calibration anchors ───────────────────────────────────────────────────────

PRODUCTION_CALIBRATION = """
Production Grade calibration anchors (0-100) — evaluate demonstrated on-field NFL
production efficiency and volume ONLY, ignore landing spot/opportunity (that's a separate
agent). Age and current health do not move this grade: they are scored separately in
risk_modifier below. Games actually missed in the graded seasons still count as they show
up in the production record (volume, sample size) — that is evidence, not a risk estimate:
- 95-100 (A+): Historically elite season production, top-3 at position, workhorse volume
- 85-94  (A/A-): Clear top-12 producer at position, strong efficiency and volume both
- 75-84  (B+/B): Solid top-24 producer, above-average efficiency or volume
- 65-74  (B-/C+): Usable committee/complementary producer, decent efficiency, capped volume
- 50-64  (C/C-): Replacement-level production, inconsistent, or limited sample (games played/role)
- 35-49  (D+/D): Thin production even accounting for role, concerning efficiency
- 0-34   (D-/F): Minimal production evidence, buried or non-functional as a producer

Letter grade conversion:
97-100→A+, 93-96→A, 90-92→A-, 87-89→B+, 83-86→B, 80-82→B-,
77-79→C+, 73-76→C, 70-72→C-, 67-69→D+, 63-66→D, 60-62→D-, <60→F

Current Health Score — 1-5 (5 = best), scored ONLY from lookup_player_info's
current Sleeper injury/practice status and get_injury_notes's current ESPN
team-feed notes. This is a current-status/availability signal only: it is
not a historical durability assessment and is not a forecast of future
injury probability. There is no verified source of historical injury data
here -- do not infer "no injury history" from its absence, and do not invent
past injuries. Do not output a numeric injury probability of any kind.
- 5: No current limitation -- full practice participation, no current
  injury_status, no current ESPN note
- 4: Minor current designation (e.g. "Questionable" with a non-structural
  note) or minor current limitation
- 3: Meaningful current limitation -- limited practice participation, or a
  current injury_status/ESPN note suggesting a moderate issue
- 2: Significant current availability concern -- a significant injury
  designation (e.g. "IR", "PUP") or a structural issue noted currently
- 1: Currently unavailable / severe present concern -- out with a
  serious/structural injury per current status

Aging Risk is a SEPARATE signal, computed by compute_age_curve_signal against
this model's configured position aging thresholds. It does not change the
meaning of the Current Health Score above -- a young, currently-healthy player
and an old, currently-healthy player can both score 5 on health while
differing on aging_risk.

Neither Current Health Score nor Aging Risk changes production_score or
production_grade. Both go in risk_modifier; the Market agent applies them to
dynasty value once, deterministically.
"""

SYSTEM_PROMPT = f"""You are the Production Agent for a dynasty fantasy football roster report card tool.

Your job: evaluate a ROSTERED player's on-field PRODUCTION only — completely independent
of their situation/opportunity (a separate agent handles that). Focus on: NFL production
volume and efficiency (most recently completed regular season as the historical baseline,
with prior-completed-season trend context — never partial in-progress totals) and positional
value of that production.

Separately, report the Risk Modifier: current health/availability (Current Health Score)
and aging risk (career-window signal). production_score and production_grade measure
demonstrated on-field production only. current_health_score and aging_risk are reported
separately in risk_modifier and are applied to dynasty value downstream by the Market agent.
Do not lower or raise production_score because of age or current health — an older or
currently injured player with the same production record gets the same production_score.

{PRODUCTION_CALIBRATION}

Tools available:
- lookup_player_info: Basic profile + live injury/practice status from Sleeper, + ESPN athlete id
- get_current_season_production: Most recently completed NFL regular season's stats (the
  historical baseline; its "season" field says which year — not the season in progress)
- get_prior_season_production: The completed season before that, for trend comparison
- get_career_production: Career totals for broader context
- get_injury_notes: Recent injury/status notes from ESPN's team feed
- compute_age_curve_signal: Position-specific aging-window risk from age/years_exp

Steps:
1. Call lookup_player_info for basic profile, live injury status, and ESPN athlete id
2. Call get_current_season_production and get_prior_season_production to see the trend
3. Call get_career_production for broader context
4. Call get_injury_notes for recent injury/status signal
5. Call compute_age_curve_signal using the player's position/age/years_exp (code re-derives
   aging_risk/career_window_note from lookup_player_info's result; report them as returned)
6. Score Current Health Score from current Sleeper status + current ESPN injury notes only,
   per the calibration above. Never output a numeric injury probability. Aging Risk comes
   straight from compute_age_curve_signal and stays a separate field.
7. Grade production_score/production_grade from the production evidence in steps 2-3 only;
   put current health and aging in risk_modifier (and concerns), not in the grade

CRITICAL — lookup_player_info's espn_id can be null for a player too deep on the roster to
have an ESPN athlete id on file (neither Sleeper nor FantasyCalc tracks one). When that
happens, get_current_season_production/get_prior_season_production/get_career_production/
get_injury_notes will each return {{"error": "no ESPN athlete id..."}} instead of stats —
still call them if you want, they're safe to call, but do NOT retry with a made-up id or
treat the null as a value to pass elsewhere. Fall back to a pure no-evidence F grade when
that happens.

Key stats to weight for skill positions (field names come from ESPN's flattened stats):
- RB: rushingYards, rushingAttempts, yardsPerRushAttempt, rushingTouchdowns, receptions, receivingYards
- WR/TE: receptions, receivingYards, yardsPerReception, receivingTouchdowns, longReception
- QB: completionPct, passingYards, yardsPerPassAttempt, passingTouchdowns, interceptions, rushingYards

Output format — always return a JSON object with these exact keys. "current_season" is the
"season" value get_current_season_production returned (the completed baseline season), not the
season currently being played:
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
  "trend_note": "One sentence comparing the baseline season to the prior completed season.",
  "risk_modifier": {{
    "current_health_score": 4,
    "injury_notes": "Brief description of any relevant recent injury/status notes",
    "aging_risk": "moderate",
    "career_window_note": "entering the typical RB decline window (27-29)"
  }},
  "production_score": 78,
  "production_grade": "B",
  "key_factors": ["bullet points on what drives the grade"],
  "concerns": ["any production, current-health, or aging concerns"],
  "summary": "Two-sentence plain-English summary of production outlook."
}}
"""

# ── Agent + runner ────────────────────────────────────────────────────────────

def _capturing_lookup(capture: dict):
    """lookup_player_info, recording each result by player_id so the run can
    recompute the aging signal from the actual lookup. Same name, docstring
    and schema for ADK."""
    @functools.wraps(lookup_player_info)
    def wrapped(player_id: str) -> dict:
        result = lookup_player_info(player_id)
        capture.setdefault("lookup", {})[str(player_id)] = result
        return result
    return wrapped


def _apply_deterministic_aging(result: dict, capture: dict, player_id: str) -> dict:
    """Set risk_modifier.aging_risk / career_window_note from
    compute_age_curve_signal on the captured lookup_player_info result for this
    player_id — never from the model's copy, or from a model-supplied age or
    position. current_health_score and injury_notes stay model-authored.
    Fails with {"error": ...} if the lookup never ran for this player or
    returned an error."""
    lookup = (capture.get("lookup") or {}).get(str(player_id))
    if not lookup:
        return {"error": f"production evaluation incomplete: lookup_player_info did not run for player_id {player_id}"}
    if "error" in lookup:
        return {"error": f"production evaluation failed: {lookup['error']}"}
    signal = compute_age_curve_signal(lookup.get("position"), lookup.get("age"), lookup.get("years_exp"))
    risk = result.get("risk_modifier")
    if not isinstance(risk, dict):
        risk = result["risk_modifier"] = {}
    risk["aging_risk"] = signal["aging_risk"]
    risk["career_window_note"] = signal.get("career_window_note") or signal.get("note")
    return result


def build_production_agent(capture: dict | None = None) -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        generate_content_config=genai_types.GenerateContentConfig(
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
        ),
        before_model_callback=check_budget_before_model,
        after_model_callback=record_model_usage,
        on_model_error_callback=clear_invocation_reservation,
        name="production_agent",
        instruction=SYSTEM_PROMPT,
        tools=[
            _capturing_lookup(capture) if capture is not None else lookup_player_info,
            get_current_season_production,
            get_prior_season_production,
            get_career_production,
            get_injury_notes,
            compute_age_curve_signal,
        ],
    )


async def run_production_agent(player_id: str) -> dict:
    """Run the Production Agent for a given Sleeper player_id. Returns parsed JSON
    output with the aging signal set in code (see _apply_deterministic_aging),
    or {"error": ...}."""
    capture: dict = {}
    agent = build_production_agent(capture)
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
        return _apply_deterministic_aging(json.loads(json_match.group()), capture, player_id)
    return {"raw_output": result_text}


if __name__ == "__main__":
    import asyncio
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running Production Agent for player_id: {player_id}\n")
    result = asyncio.run(run_production_agent(player_id))
    print(json.dumps(result, indent=2))
