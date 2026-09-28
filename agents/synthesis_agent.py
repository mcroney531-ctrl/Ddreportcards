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

from agents.usage import (
    AGENT_MAX_OUTPUT_TOKENS,
    check_budget_before_model,
    clear_invocation_reservation,
    record_model_usage,
)

from agents.situation_agent import run_situation_agent
from agents.production_agent import run_production_agent
from agents.market_agent import run_market_agent

# Card fields each sub-agent owns; copied from that sub-agent's result in code
# (see run_synthesis_agent), never from the Synthesis model's copy. The
# Synthesis model authors only key_strengths, key_concerns and narrative.
SITUATION_CARD_FIELDS = ("player", "position", "team", "opportunity_score", "opportunity_grade")
PRODUCTION_CARD_FIELDS = ("production_score", "production_grade", "risk_modifier")
MARKET_CARD_FIELDS = ("dynasty_value", "hybrid_market_value", "trend_30day", "trade_value_score", "trade_value_grade")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unusable(result, label: str) -> str | None:
    """Why a sub-agent result can't be used, or None if it can."""
    if not isinstance(result, dict):
        return f"{label} agent did not run"
    if "error" in result:
        return f"{label}: {result['error']}"
    if "raw_output" in result:
        return f"unstructured {label} output"
    return None


def _market_inputs(sub_results: dict) -> tuple[tuple | None, str | None]:
    """(opportunity_score, production_score, current_health_score, aging_risk)
    taken from the Situation/Production results, or (None, reason). Nothing is
    substituted for a missing or non-numeric value."""
    situation, production = sub_results.get("situation"), sub_results.get("production")
    problem = _unusable(situation, "situation") or _unusable(production, "production")
    if problem:
        return None, problem
    risk = production.get("risk_modifier")
    if not _is_number(situation.get("opportunity_score")):
        return None, "situation result has no numeric opportunity_score"
    if not _is_number(production.get("production_score")):
        return None, "production result has no numeric production_score"
    if not isinstance(risk, dict):
        return None, "production result has no risk_modifier"
    if not _is_number(risk.get("current_health_score")):
        return None, "production risk_modifier has no numeric current_health_score"
    if not risk.get("aging_risk"):
        return None, "production risk_modifier has no aging_risk"
    return (situation["opportunity_score"], production["production_score"],
            risk["current_health_score"], risk["aging_risk"]), None

# ── Thread helper (avoids nested-asyncio issue under Streamlit) ───────────────

def _run_in_thread(coro):
    """Run an async coroutine in a fresh thread with its own event loop."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(asyncio.run, coro)
        return future.result()


# ── ADK tool functions ────────────────────────────────────────────────────────
#
# Each factory closes over a shared `sub_results` dict so the caller can see
# the full, unmediated sub-agent outputs (competition breakdown, key stats,
# market percentile, etc.) after the run — not just whatever the LLM chose to
# echo into its final narrative. This backs the UI's "what's behind this
# score" drill-down without relying on the LLM to faithfully copy large JSON
# blobs verbatim.
#
# The tools are also closed over the one player_id this run evaluates, and take
# no arguments: the model can't pick another player, and Market's inputs come
# from the Situation/Production results, not from values the model passes.

def _make_tools(sub_results: dict, player_id: str):
    async def evaluate_situation() -> dict:
        """
        Call the Situation Agent to evaluate opportunity for the player under review.
        Returns opportunity_score (0-100), opportunity_grade (letter), team,
        depth_chart_order, competition breakdown, key_factors, concerns, summary.
        Takes no arguments.
        """
        result = await asyncio.to_thread(_run_in_thread, run_situation_agent(player_id))
        sub_results["situation"] = result
        return result

    async def evaluate_production() -> dict:
        """
        Call the Production Agent to evaluate on-field production and compute the
        Risk Modifier for the player under review. Returns production_score (0-100),
        production_grade, key_stats, risk_modifier (current_health_score 1-5 --
        a current health/availability signal only, not a historical durability
        assessment or a forecast of future injury probability -- plus aging_risk),
        key_factors, concerns, summary. Takes no arguments.
        """
        result = await asyncio.to_thread(_run_in_thread, run_production_agent(player_id))
        sub_results["production"] = result
        return result

    async def evaluate_market() -> dict:
        """
        Call the Market Agent for the player under review. Returns
        trade_value_score/trade_value_grade -- the player's FantasyCalc
        market-consensus standing (position percentile) -- plus hybrid_market_value:
        FantasyCalc's dynasty value nudged by our own proprietary composite. Takes no
        arguments: its opportunity_score, production_score, current_health_score and
        aging_risk come from the evaluate_situation / evaluate_production results, so
        call those first.
        """
        inputs, problem = _market_inputs(sub_results)
        if problem:
            result = {"error": f"evaluate_market needs usable situation and production results first ({problem})"}
        else:
            result = await asyncio.to_thread(_run_in_thread, run_market_agent(player_id, *inputs))
        sub_results["market"] = result
        return result

    return [evaluate_situation, evaluate_production, evaluate_market]


# ── System prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Synthesis Agent for a dynasty fantasy football roster report card tool.

You orchestrate the full per-player evaluation pipeline for one rostered player:
1. Call evaluate_situation → get Opportunity grade
2. Call evaluate_production → get Production grade + Risk Modifier
3. Call evaluate_market → get Trade Value (it takes the opportunity/production/risk inputs
   from steps 1-2 itself; none of the tools take arguments)
4. Synthesize everything into one player card. The sub-agents' scores, grades,
   risk_modifier, market fields and identity are copied onto the card in code; your job is
   key_strengths, key_concerns and the narrative, consistent with those results

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
    "current_health_score": 2,
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

def build_synthesis_agent(sub_results: dict, player_id: str) -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        generate_content_config=genai_types.GenerateContentConfig(
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
        ),
        before_model_callback=check_budget_before_model,
        after_model_callback=record_model_usage,
        on_model_error_callback=clear_invocation_reservation,
        name="synthesis_agent",
        instruction=SYSTEM_PROMPT,
        tools=_make_tools(sub_results, player_id),
    )


async def run_synthesis_agent(player_id: str) -> dict:
    """
    Run the full player-card pipeline for a given Sleeper player_id.

    The returned card carries a "_detail" key with the raw situation/
    production/market sub-agent outputs, for UI drill-down into what backs
    each score — not part of the LLM-authored schema, attached in code.
    """
    sub_results: dict = {}
    agent = build_synthesis_agent(sub_results, player_id)
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
        card = json.loads(json_match.group())
        # Sub-agent-owned fields are copied from the sub-agent results in code,
        # never from the synthesis model's copy (the card is what the app shows,
        # the roster grades and the Trade Agent flags). Any unusable sub-agent
        # result fails the card, using the {"error", "player", "position"} shape
        # the app and API already treat as a failed card. Identity comes from
        # Situation only, never from the synthesis model's guess.
        situation = sub_results.get("situation")
        _, problem = _market_inputs(sub_results)
        problem = problem or _unusable(sub_results.get("market"), "market")
        if problem:
            identity = situation if not _unusable(situation, "situation") else {}
            return {"error": f"report card unavailable: {problem}", "player": identity.get("player"),
                    "position": identity.get("position"), "_detail": sub_results}
        for fields, source in ((SITUATION_CARD_FIELDS, situation),
                               (PRODUCTION_CARD_FIELDS, sub_results["production"]),
                               (MARKET_CARD_FIELDS, sub_results["market"])):
            for key in fields:
                card[key] = source.get(key)
        card["_detail"] = sub_results
        return card
    return {"raw_output": result_text}


if __name__ == "__main__":
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running full player-card pipeline for player_id: {player_id}\n")
    result = asyncio.run(run_synthesis_agent(player_id))
    print(json.dumps(result, indent=2))
