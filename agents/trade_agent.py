"""
Trade Agent — flags sell candidates on BCNH's roster. Same division of labor
as the rest of this project: deterministic signals are computed in code, the
LLM only writes the specific "why" narrative for each flagged player. Phase 1
scope is sell-candidates only — no cross-roster buy targets yet (that needs
every team's data, which comes in a later phase).

Three signal types, all computed from the player cards produced by
synthesis_agent + roster_agent's shared quality_score:
  - sell_high: risk is rising (low durability or high aging risk) while the
    market hasn't caught down yet — trade value is still elevated.
  - sell_before_drop: opportunity_score trails trade_value_score by a wide
    margin — the market hasn't priced in a declining role yet.
  - positional_surplus: enough real contributors at one position that the
    weakest of the group is a reasonable candidate to deal from depth.

Inputs:  list of player cards (as produced by synthesis_agent) for BCNH's roster
Outputs: flagged sell candidates with signal type(s) and a written "why"
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

from agents.roster_agent import _quality_score, CONTRIBUTOR_THRESHOLD

# Thresholds for each signal type.
SELL_HIGH_MARKET_FLOOR = 55       # trade_value_score at/above this = "still elevated"
SELL_HIGH_DURABILITY_MAX = 2       # durability_score <= this = risk rising
SELL_BEFORE_DROP_GAP = 15          # opportunity_score trailing trade_value_score by this much
# Superflex bumps the QB surplus bar up since a 2nd/3rd QB has real SF-slot value.
SURPLUS_MIN_CONTRIBUTORS = {"QB": 3, "RB": 4, "WR": 4, "TE": 3}


# ── ADK tool function ─────────────────────────────────────────────────────────

def detect_sell_signals(player_cards_json: str) -> dict:
    """
    Deterministic sell-candidate signal detection across the full roster.
    player_cards_json: JSON-encoded list of player card dicts (from synthesis_agent)

    Returns each flagged player with the signal(s) that triggered and the
    supporting numbers, plus a positional_surplus summary.
    """
    cards = json.loads(player_cards_json)
    for c in cards:
        c.setdefault("quality_score", round(_quality_score(c), 1))

    flagged: dict[str, dict] = {}

    def _flag(name: str, signal: str, detail: str) -> None:
        entry = flagged.setdefault(name, {"player": name, "signals": []})
        entry["signals"].append({"type": signal, "detail": detail})

    for c in cards:
        name = c.get("player")
        risk = c.get("risk_modifier", {}) or {}
        durability = risk.get("durability_score")
        aging_risk = risk.get("aging_risk")
        trade_value_score = c.get("trade_value_score", 0) or 0
        opportunity_score = c.get("opportunity_score", 0) or 0

        risk_rising = (durability is not None and durability <= SELL_HIGH_DURABILITY_MAX) or aging_risk == "high"
        if risk_rising and trade_value_score >= SELL_HIGH_MARKET_FLOOR:
            _flag(name, "sell_high", (
                f"durability_score={durability}, aging_risk={aging_risk!r} (risk rising) but "
                f"trade_value_score={trade_value_score} is still elevated (>= {SELL_HIGH_MARKET_FLOOR}) "
                "— the market hasn't caught down to the risk yet."
            ))

        gap = trade_value_score - opportunity_score
        if gap >= SELL_BEFORE_DROP_GAP:
            _flag(name, "sell_before_drop", (
                f"opportunity_score={opportunity_score} trails trade_value_score={trade_value_score} "
                f"by {gap} pts — the market hasn't priced in the declining role yet."
            ))

    by_position: dict[str, list[dict]] = {}
    for c in cards:
        by_position.setdefault(c.get("position"), []).append(c)

    positional_surplus: dict[str, dict] = {}
    for pos, players in by_position.items():
        contributors = [p for p in players if p.get("quality_score", 0) >= CONTRIBUTOR_THRESHOLD]
        min_needed = SURPLUS_MIN_CONTRIBUTORS.get(pos, 3)
        if len(contributors) >= min_needed:
            weakest = min(contributors, key=lambda p: p["quality_score"])
            _flag(weakest.get("player"), "positional_surplus", (
                f"{len(contributors)} real contributors at {pos} (surplus threshold {min_needed}) — "
                f"{weakest.get('player')} grades weakest of the group, a candidate to deal from depth."
            ))
            positional_surplus[pos] = {"contributor_count": len(contributors), "min_needed": min_needed}

    return {"flagged_players": list(flagged.values()), "positional_surplus": positional_surplus}


# ── System prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Trade Agent for a dynasty fantasy football roster report card tool.

You receive a JSON array of player cards for BCNH's full roster and must flag sell
candidates. Phase 1 scope is sell-candidates only — do not suggest buy targets or
name players from other rosters, that data isn't available yet.

Steps:
1. Call detect_sell_signals with the player_cards_json you were given, verbatim as
   a JSON string
2. For each entry in flagged_players, write a specific "why" — reference the actual
   signal detail(s) returned AND pull supporting context from that player's own card
   (team, position, key_concerns, narrative) to make it concrete, not generic.

CRITICAL — do NOT invent signals or flag players detect_sell_signals didn't flag. The
scoring/thresholds are fixed in code; your job is explaining the specific "why" in
plain English, not deciding who qualifies.

A player can have more than one signal — if so, lead with the strongest one but
mention both in the why. Recommendation should map from signal type:
sell_high -> "sell-high", sell_before_drop -> "sell before it drops",
positional_surplus -> "deal from depth".

Output format — return a JSON object with these exact keys:
{
  "sell_candidates": [
    {
      "player": "Tony Pollard",
      "position": "RB",
      "signals": ["sell_high"],
      "recommendation": "sell-high",
      "why": "2-3 sentences, specific to this player, using the signal detail and their card context."
    }
  ],
  "positional_surplus_summary": "One sentence on any position(s) with real depth surplus, or empty string if none.",
  "summary": "1-2 sentence overview of the trade section — how many candidates, and the strongest single sell case."
}

If flagged_players is empty, return sell_candidates: [] and say so plainly in summary.
"""

# ── Agent + runner ────────────────────────────────────────────────────────────

def build_trade_agent() -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        name="trade_agent",
        instruction=SYSTEM_PROMPT,
        tools=[detect_sell_signals],
    )


async def run_trade_agent(player_cards: list[dict]) -> dict:
    """Run the Trade Agent over the full set of player cards for BCNH's roster."""
    agent = build_trade_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="dynasty_report_cards", session_service=session_service)

    session_id = "trade_session"
    await session_service.create_session(
        app_name="dynasty_report_cards", user_id="user", session_id=session_id
    )

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=(
            "Find sell candidates on this roster. player_cards_json:\n" + json.dumps(player_cards)
        ))]
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
    sample_cards = [
        {
            "player": "Tony Pollard", "position": "RB",
            "opportunity_score": 72, "production_score": 68, "trade_value_score": 59,
            "risk_modifier": {"durability_score": 2, "aging_risk": "high"},
        },
        {
            "player": "Kirk Cousins", "position": "QB",
            "opportunity_score": 35, "production_score": 50, "trade_value_score": 55,
            "risk_modifier": {"durability_score": 4, "aging_risk": "high"},
        },
    ]
    result = asyncio.run(run_trade_agent(sample_cards))
    print(json.dumps(result, indent=2))
