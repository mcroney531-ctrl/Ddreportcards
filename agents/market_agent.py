"""
Market Agent — Trade Value / Market category. New agent, no Scout equivalent.

Computes a proprietary composite from our own Situation/Production/Risk grades
(Talent/Opportunity/Risk spirit of Scout's composite, reweighted — no draft
capital and no sentiment, since trending adds are color only, not scored),
then blends it with FantasyCalc's dynasty value consensus into one hybrid
market value. FantasyCalc is the anchor (KeepTradeCut is intentionally not
used — no public API, ToS forbids scraping); our composite can only nudge the
number, reflecting where our own grading agrees or diverges from the market.

Inputs:  Sleeper player_id + the Opportunity/Production/Risk outputs already
         computed by situation_agent and production_agent for this player.
Outputs: structured market assessment with a hybrid Trade Value + trend note.
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

from data import fantasycalc_client

# Proprietary composite weights — Production carries the most weight (what a
# player actually does on the field), Opportunity next (their path to keep
# doing it), Risk last (durability/aging headwinds). No sentiment: trending
# adds are explicitly not scored per project scope, only color.
WEIGHTS = {"production": 0.45, "opportunity": 0.35, "risk": 0.20}

AGING_RISK_PENALTY = {"low": 0, "moderate": -5, "high": -12, "unknown": 0}

# How far our proprietary view is allowed to move the market-anchored value.
# +/-25% is a deliberate ceiling — FantasyCalc stays the anchor, we nudge it.
MAX_BLEND_ADJUSTMENT = 0.25


# ── ADK tool functions ────────────────────────────────────────────────────────

def get_market_consensus(player_id: str) -> dict:
    """
    FantasyCalc dynasty value consensus for a player: name, dynasty value,
    position rank, overall rank, 30-day trend, and redraft value for context.
    player_id: Sleeper player_id
    """
    fc = fantasycalc_client.get_value_for_sleeper_id(player_id)
    if not fc:
        return {
            "in_pool": False,
            "dynasty_value": 0,
            "note": "outside FantasyCalc's dynasty-relevant pool (~460 players) — minimal market value",
        }
    return {
        "in_pool": True,
        "name": fc["player"]["name"],
        "dynasty_value": fc["value"],
        "position": fc["player"]["position"],
        "position_rank": fc["positionRank"],
        "overall_rank": fc["overallRank"],
        "trend_30day": fc["trend30Day"],
        "redraft_value": fc["redraftValue"],
    }


def compute_proprietary_composite(
    opportunity_score: int, production_score: int, durability_score: int, aging_risk: str
) -> dict:
    """
    Weighted composite (0-100) from our own agents' grades only — no market
    data. This is our independent view of the player's dynasty desirability.
    opportunity_score, production_score: 0-100 from situation_agent/production_agent
    durability_score: 1-5 from production_agent's risk_modifier
    aging_risk: 'low' | 'moderate' | 'high' | 'unknown' from production_agent's risk_modifier
    """
    risk_score = ((durability_score - 1) / 4) * 100
    composite = (
        production_score * WEIGHTS["production"]
        + opportunity_score * WEIGHTS["opportunity"]
        + risk_score * WEIGHTS["risk"]
        + AGING_RISK_PENALTY.get(aging_risk, 0)
    )
    composite = max(0.0, min(100.0, composite))
    return {
        "proprietary_composite": round(composite, 1),
        "risk_score": round(risk_score, 1),
        "weights_applied": WEIGHTS,
    }


def blend_with_market(proprietary_composite: float, player_id: str) -> dict:
    """
    Blend the proprietary composite with FantasyCalc's dynasty value into one
    hybrid market value, expressed on FantasyCalc's native scale so every
    player on the roster stays directly comparable.

    FantasyCalc is the anchor. We compute this player's percentile within
    their position by dynasty value, compare it to our own 0-100 composite,
    and let the divergence nudge the anchor value by up to +/-25%. If our
    agents and the market fully agree, hybrid_value == dynasty_value.
    proprietary_composite: 0-100 from compute_proprietary_composite
    player_id: Sleeper player_id
    """
    fc = fantasycalc_client.get_value_for_sleeper_id(player_id)
    if not fc:
        return {
            "hybrid_market_value": 0,
            "dynasty_value": 0,
            "market_percentile": 0,
            "divergence": 0,
            "note": "outside FantasyCalc's pool — hybrid value floored at 0",
        }

    position = fc["player"]["position"]
    dynasty_value = fc["value"]

    all_values = fantasycalc_client.get_dynasty_values()
    position_values = sorted(
        (e["value"] for e in all_values if e["player"].get("position") == position),
        reverse=True,
    )
    rank = position_values.index(dynasty_value) if dynasty_value in position_values else len(position_values) - 1
    market_percentile = round((1 - rank / max(len(position_values) - 1, 1)) * 100, 1)

    divergence = proprietary_composite - market_percentile
    blend_multiplier = 1 + (divergence / 100) * MAX_BLEND_ADJUSTMENT
    hybrid_value = round(dynasty_value * blend_multiplier)

    return {
        "hybrid_market_value": hybrid_value,
        "dynasty_value": dynasty_value,
        "market_percentile": market_percentile,
        "proprietary_composite": proprietary_composite,
        "divergence": round(divergence, 1),
        "blend_multiplier": round(blend_multiplier, 3),
        "note": (
            "We're more bullish than the market — nudged the value up."
            if divergence > 5
            else "We're more bearish than the market — nudged the value down."
            if divergence < -5
            else "Our view and the market broadly agree on this player."
        ),
    }


# ── Calibration anchors ────────────────────────────────────────────────────────

MARKET_CALIBRATION = """
Trade Value Grade calibration anchors, based on market_percentile (position rank,
where 100 = the top player at the position league-wide):
- 90-100: A+/A — top-tier dynasty asset at the position, elite trade chip
- 75-89:  A-/B+ — clear top-12 startable asset
- 55-74:  B/B- — solid starter-level trade value
- 35-54:  C+/C — depth/flex-level trade value
- 15-34:  D+/D — replaceable, minimal trade appeal
- 0-14:   D-/F — negligible trade value

Letter grade conversion (same scale used by the other agents):
97-100→A+, 93-96→A, 90-92→A-, 87-89→B+, 83-86→B, 80-82→B-,
77-79→C+, 73-76→C, 70-72→C-, 67-69→D+, 63-66→D, 60-62→D-, <60→F
"""

SYSTEM_PROMPT = f"""You are the Market Agent for a dynasty fantasy football roster report card tool.

Your job: determine a player's dynasty TRADE VALUE by blending our own independent
grading (their Opportunity and Production grades, plus Risk) with FantasyCalc's dynasty
value consensus — the sole market-consensus anchor for this project (KeepTradeCut is
deliberately excluded, no public API).

{MARKET_CALIBRATION}

Tools available:
- get_market_consensus: player name, FantasyCalc dynasty value, position/overall rank, 30-day trend
- compute_proprietary_composite: Our own 0-100 composite from Opportunity/Production/Risk
- blend_with_market: Blends the composite with FantasyCalc's value into one hybrid market value

CRITICAL — the "name" field returned by get_market_consensus is the ONLY source of truth
for the player's identity. Always use it verbatim for the "player" field in your output.
Never guess or recall a name from your own knowledge of the player_id — player_ids are
Sleeper's internal identifiers and are not something you can reliably infer a name from.

Steps:
1. Call get_market_consensus for the player's FantasyCalc standing
2. Call compute_proprietary_composite using the opportunity_score, production_score,
   durability_score, and aging_risk provided to you in the user message
3. Call blend_with_market with the proprietary composite to get the final hybrid market value
4. Convert the resulting market_percentile into a letter grade using the calibration above
5. Write a short trend note: is the market moving on this player (trend_30day), and does
   our internal view agree or diverge from consensus (the divergence/note from blend_with_market)?

Output format — always return a JSON object with these exact keys:
{{
  "player": "Full Name",
  "position": "RB",
  "dynasty_value": 1539,
  "hybrid_market_value": 1476,
  "market_percentile": 59.0,
  "trade_value_score": 59,
  "trade_value_grade": "B-",
  "trend_30day": -81,
  "trend_note": "One sentence on recent market movement and whether our internal view agrees or diverges.",
  "key_factors": ["bullet points on what drives the trade value"],
  "summary": "Two-sentence plain-English summary of trade value outlook — is this a buy-low, hold, or sell-high?"
}}
"""

# ── Agent + runner ────────────────────────────────────────────────────────────

def build_market_agent() -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        name="market_agent",
        instruction=SYSTEM_PROMPT,
        tools=[get_market_consensus, compute_proprietary_composite, blend_with_market],
    )


async def run_market_agent(
    player_id: str, opportunity_score: int, production_score: int, durability_score: int, aging_risk: str
) -> dict:
    """Run the Market Agent for a given player. Returns parsed JSON output."""
    agent = build_market_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="dynasty_report_cards", session_service=session_service)

    session_id = f"market_session_{player_id}"
    await session_service.create_session(
        app_name="dynasty_report_cards", user_id="user", session_id=session_id
    )

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=(
            f"Evaluate the trade value for player_id {player_id}. "
            f"opportunity_score={opportunity_score}, production_score={production_score}, "
            f"durability_score={durability_score}, aging_risk={aging_risk!r}."
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
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running Market Agent for player_id: {player_id}\n")
    # Sample inputs approximating Pollard's situation/production agent outputs
    result = asyncio.run(run_market_agent(player_id, opportunity_score=72, production_score=68, durability_score=2, aging_risk="high"))
    print(json.dumps(result, indent=2))
