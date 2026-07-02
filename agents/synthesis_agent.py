"""
Synthesis Agent — per-player orchestrator. Calls Situation, Production, and
Market as Agent Tools for one rostered player and combines them into a single
player card: Opportunity grade, Production grade, Risk modifier, Trade Value,
plus a short narrative. Adapted from Scout's synthesis_agent, minus the
roster-need/sentiment/pick-recommendation logic (that's the rookie-draft use
case) — those responsibilities move to roster_agent.py and trade_agent.py here.
"""

import os, sys, json, re, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agents.situation_agent import run_situation_agent
from agents.production_agent import run_production_agent
from agents.market_agent import run_market_agent

# ── Thread helper (avoids nested-asyncio issue under Streamlit) ───────────────

def _run_in_thread(coro):
    """Run an async coroutine in a fresh thread with its own event loop."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(asyncio.run, coro)
        return future.result()


# ── ADK tool functions ────────────────────────────────────────────────────────

def evaluate_situation(player_id: str) -> dict:
    """
    Call the Situation Agent to evaluate opportunity for a rostered player.
    Returns opportunity_score (0-100), opportunity_grade (letter), team,
    depth_chart_order, competition breakdown, key_factors, concerns, summary.
    player_id: Sleeper player_id
    """
    return _run_in_thread(run_situation_agent(player_id))


def evaluate_production(player_id: str) -> dict:
    """
    Call the Production Agent to evaluate on-field production and compute the
    Risk Modifier for a rostered player. Returns production_score (0-100),
    production_grade, key_stats, risk_modifier (durability_score 1-5,
    injury_chance_pct, aging_risk), key_factors, concerns, summary.
    player_id: Sleeper player_id
    """
    return _run_in_thread(run_production_agent(player_id))


def evaluate_market(
    player_id: str, opportunity_score: int, production_score: int, durability_score: int, aging_risk: str
) -> dict:
    """
    Call the Market Agent to compute the player's dynasty Trade Value — a hybrid
    of our own proprietary composite (built from the opportunity/production/risk
    scores you pass in) and FantasyCalc's dynasty value consensus.
    player_id: Sleeper player_id
    opportunity_score, production_score: from evaluate_situation/evaluate_production
    durability_score, aging_risk: from evaluate_production's risk_modifier
    """
    return _run_in_thread(
        run_market_agent(player_id, opportunity_score, production_score, durability_score, aging_risk)
    )


# ── System prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Synthesis Agent for a dynasty fantasy football roster report card tool.

You orchestrate the full per-player evaluation pipeline for one rostered player:
1. Call evaluate_situation → get Opportunity grade
2. Call evaluate_production → get Production grade + Risk Modifier
3. Call evaluate_market, passing opportunity_score, production_score, durability_score,
   and aging_risk from steps 1-2 → get Trade Value
4. Synthesize everything into one player card

This is a 12-team, 4-round, superflex (2 QB starts), PPR dynasty league. Weigh QB value
accordingly in your narrative — a rostered QB carries a real superflex premium.

CRITICAL — for the "player" and "team" fields in your output, use the values returned by
evaluate_situation or evaluate_production verbatim. Never guess a name from the player_id
itself — Sleeper player_ids are internal identifiers, not something to infer identity from.

Output format — return a JSON object with these exact keys:
{
  "player": "Full Name",
  "position": "RB",
  "team": "TEN",
  "opportunity_grade": "C-",
  "opportunity_score": 72,
  "production_grade": "D+",
  "production_score": 68,
  "risk_modifier": {
    "durability_score": 2,
    "injury_chance_pct": 40,
    "aging_risk": "high",
    "career_window_note": "..."
  },
  "trade_value_grade": "B-",
  "trade_value_score": 59,
  "hybrid_market_value": 1479,
  "dynasty_value": 1539,
  "trend_30day": -81,
  "key_strengths": ["bullet points pulled from the sub-agent outputs"],
  "key_concerns": ["bullet points pulled from the sub-agent outputs"],
  "narrative": "3-4 sentence synthesis of this player's overall dynasty standing on the roster — where the grades tell a consistent story vs. where they pull in different directions (e.g. strong production but declining opportunity), and what that means for whether to hold, buy-low, or sell-high."
}
"""

# ── Agent + runner ────────────────────────────────────────────────────────────

def build_synthesis_agent() -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        name="synthesis_agent",
        instruction=SYSTEM_PROMPT,
        tools=[evaluate_situation, evaluate_production, evaluate_market],
    )


async def run_synthesis_agent(player_id: str) -> dict:
    """Run the full player-card pipeline for a given Sleeper player_id."""
    agent = build_synthesis_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="dynasty_report_cards", session_service=session_service)

    session_id = f"synth_session_{player_id}"
    await session_service.create_session(
        app_name="dynasty_report_cards", user_id="user", session_id=session_id
    )

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"Give me the full player report card for player_id {player_id}")]
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
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running full player-card pipeline for player_id: {player_id}\n")
    result = asyncio.run(run_synthesis_agent(player_id))
    print(json.dumps(result, indent=2))
