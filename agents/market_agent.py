"""
Market Agent — Trade Value / Market category. New agent, no Scout equivalent.

Two distinct outputs, deliberately kept apart:
  - Trade Value Grade (trade_value_score / trade_value_grade): the player's
    FantasyCalc market-consensus standing, i.e. market_percentile within
    their position. Our own grading does not change it. This is what the
    roster grade and the Trade Agent read.
  - Hybrid Market Value (hybrid_market_value): our internal valuation. It
    computes a proprietary composite from our own Opportunity/Production
    grades plus current health and aging risk (Talent/Opportunity/Risk spirit
    of Scout's composite, reweighted — no draft capital and no sentiment,
    since trending adds are color only, not scored), then nudges
    FantasyCalc's dynasty value by at most +/-25% toward it. That nudge is the
    only place the composite lands.
FantasyCalc is the anchor (KeepTradeCut is intentionally not used — no public
API, ToS forbids scraping).

Inputs:  Sleeper player_id + the Opportunity/Production/Risk outputs already
         computed by situation_agent and production_agent for this player.
Outputs: market-consensus Trade Value Grade, Hybrid Market Value, trend note.
"""

import os, sys, json, re, math, functools
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

from data import fantasycalc_client

# Proprietary composite weights — Production carries the most weight (what a
# player actually does on the field), Opportunity next (their path to keep
# doing it), Risk last (current health + aging risk). No sentiment: trending
# adds are explicitly not scored per project scope, only color.
WEIGHTS = {"production": 0.45, "opportunity": 0.35, "risk": 0.20}

AGING_RISK_PENALTY = {"low": 0, "moderate": -5, "high": -12, "unknown": 0}

# How far our proprietary view is allowed to move the market-anchored value.
# +/-25% is a deliberate ceiling — FantasyCalc stays the anchor, we nudge it.
MAX_BLEND_ADJUSTMENT = 0.25

# Trade Value Grade: relative FantasyCalc standing within the position, not the
# Production/Opportunity 0-100 scale. The six market tiers below (90-100, 75-89,
# 55-74, 35-54, 15-34, 0-14) each named two letters; each is split at its
# midpoint. (lower bound, grade), checked top-down.
MARKET_GRADE_TABLE = [
    (96, "A+"), (90, "A"),
    (83, "A-"), (75, "B+"),
    (65, "B"), (55, "B-"),
    (45, "C+"), (35, "C"),
    (25, "D+"), (15, "D"),
    (8, "D-"), (0, "F"),
]


def market_grade_from_percentile(percentile: float) -> str:
    """Letter for a raw market_percentile (0-100). Graded BEFORE rounding, so
    89.6 is an A-, not a 90th-percentile A."""
    for lower, grade in MARKET_GRADE_TABLE:
        if percentile >= lower:
            return grade
    return "F"


def market_score_from_percentile(percentile: float) -> int:
    """trade_value_score: market_percentile rounded half-up for display and
    roster math (Python's round() would send 59.5 and 60.5 both to 60)."""
    return int(math.floor(percentile + 0.5))


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
    opportunity_score: int, production_score: int, current_health_score: int, aging_risk: str
) -> dict:
    """
    Weighted composite (0-100) from our own agents' grades only — no market
    data. This is our independent view of the player's dynasty desirability.
    It feeds blend_with_market (hybrid_market_value) only; it does not change
    market_percentile or the Trade Value Grade.
    opportunity_score, production_score: 0-100 from situation_agent/production_agent
    current_health_score: 1-5 from production_agent's risk_modifier -- a current
        health/availability signal only, not a historical durability assessment
        or a forecast of future injury probability
    aging_risk: 'low' | 'moderate' | 'high' | 'unknown' from production_agent's risk_modifier
    """
    risk_score = ((current_health_score - 1) / 4) * 100
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
Trade Value Grade — relative FantasyCalc standing within the position, based on
market_percentile (position rank, where 100 = the top player at the position
league-wide). This is the Market grade scale, not the Production/Opportunity 0-100 scale:
- 90-100: A+/A — top-tier dynasty asset at the position, elite trade chip
- 75-89:  A-/B+ — clear top-12 startable asset
- 55-74:  B/B- — solid starter-level trade value
- 35-54:  C+/C — depth/flex-level trade value
- 15-34:  D+/D — replaceable, minimal trade appeal
- 0-14:   D-/F — negligible trade value

Exact letters (computed in code from the raw percentile, before rounding):
96-100→A+, 90-95→A, 83-89→A-, 75-82→B+, 65-74→B, 55-64→B-,
45-54→C+, 35-44→C, 25-34→D+, 15-24→D, 8-14→D-, 0-7→F
"""

SYSTEM_PROMPT = f"""You are the Market Agent for a dynasty fantasy football roster report card tool.

Your job: report a player's dynasty market standing and our internal valuation of it,
anchored on FantasyCalc's dynasty value consensus — the sole market-consensus anchor for
this project (KeepTradeCut is deliberately excluded, no public API). These are two
different outputs:
- Trade Value Grade: trade_value_score is market_percentile (FantasyCalc position
  standing), rounded to an integer, and trade_value_grade is its letter. Our internal
  composite does not change trade_value_score.
- Hybrid Market Value: hybrid_market_value is the only output our internal composite
  changes. It is FantasyCalc's dynasty_value nudged (at most +/-25%) toward our own
  Opportunity/Production/Risk composite.

{MARKET_CALIBRATION}

Tools available:
- get_market_consensus: player name, FantasyCalc dynasty value, position/overall rank, 30-day trend
- compute_proprietary_composite: Our own 0-100 composite from Opportunity/Production/Risk
  (Risk here is fed by current_health_score, a current health/availability signal --
  not a historical durability assessment or a forecast of future injury probability)
- blend_with_market: Nudges FantasyCalc's value by the composite into the hybrid market value,
  and returns the market_percentile the Trade Value Grade is taken from

CRITICAL — the "name" field returned by get_market_consensus is the ONLY source of truth
for the player's identity. Always use it verbatim for the "player" field in your output.
Never guess or recall a name from your own knowledge of the player_id — player_ids are
Sleeper's internal identifiers and are not something you can reliably infer a name from.

Steps:
1. Call get_market_consensus for the player's FantasyCalc standing
2. Call compute_proprietary_composite using the opportunity_score, production_score,
   current_health_score, and aging_risk provided to you in the user message
3. Call blend_with_market with the proprietary composite to get the final hybrid market value
4. trade_value_score and trade_value_grade are deterministic: code sets them from
   blend_with_market's market_percentile using the table above. Report them as that table
   gives them; do not choose, adjust or re-grade them (not for our composite, not for the
   divergence). Likewise player, position, dynasty_value, trend_30day, hybrid_market_value
   and market_percentile are taken from the tool results in code — pass
   compute_proprietary_composite's result to blend_with_market unchanged
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

def _capturing_consensus(capture: dict):
    """get_market_consensus, recording each result by player_id so
    run_market_agent can take the market facts from the tool output rather
    than the model's copy of it. Same name, docstring and schema for ADK."""
    @functools.wraps(get_market_consensus)
    def wrapped(player_id: str) -> dict:
        result = get_market_consensus(player_id)
        capture.setdefault("consensus", {})[str(player_id)] = result
        return result
    return wrapped


def _capturing_blend(capture: dict):
    """blend_with_market, recording each result (and the composite it was
    given) by player_id. Same name, docstring and schema for ADK."""
    @functools.wraps(blend_with_market)
    def wrapped(proprietary_composite: float, player_id: str) -> dict:
        result = blend_with_market(proprietary_composite, player_id)
        capture.setdefault("blend", {})[str(player_id)] = {
            "proprietary_composite": proprietary_composite, "result": result,
        }
        return result
    return wrapped


def _apply_deterministic_fields(result: dict, capture: dict, player_id: str, expected_composite: float) -> dict:
    """Set every tool-backed Market field from the captured tool results.

      get_market_consensus -> player, position, dynasty_value, trend_30day
      blend_with_market    -> hybrid_market_value, market_percentile
      code                 -> trade_value_score, trade_value_grade
    The model keeps only the narrative (trend_note, key_factors, summary).

    Fails with {"error": ...} (the agents' convention) when either tool never
    ran for this player_id, or when blend_with_market was handed a composite
    other than the one computed from this run's own inputs. Model-authored
    market facts are never a fallback.
    """
    consensus = (capture.get("consensus") or {}).get(str(player_id))
    blend = (capture.get("blend") or {}).get(str(player_id))
    missing = [name for name, got in (("get_market_consensus", consensus), ("blend_with_market", blend)) if not got]
    if missing:
        return {"error": f"market evaluation incomplete: {', '.join(missing)} did not run for player_id {player_id}"}
    if abs(float(blend["proprietary_composite"]) - expected_composite) > 0.05:
        return {"error": (
            f"market evaluation rejected: blend_with_market was given proprietary_composite="
            f"{blend['proprietary_composite']}, but this player's inputs compute {expected_composite}"
        )}

    blended = blend["result"]
    in_pool = consensus.get("in_pool", False)
    percentile = blended["market_percentile"]
    result.update({
        "player": consensus.get("name") if in_pool else None,
        "position": consensus.get("position") if in_pool else None,
        "dynasty_value": consensus["dynasty_value"],
        "trend_30day": consensus.get("trend_30day") if in_pool else None,
        "hybrid_market_value": blended["hybrid_market_value"],
        "market_percentile": percentile,
        "trade_value_score": market_score_from_percentile(percentile),
        "trade_value_grade": market_grade_from_percentile(percentile),
    })
    return result


def build_market_agent(capture: dict | None = None) -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        generate_content_config=genai_types.GenerateContentConfig(
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
        ),
        before_model_callback=check_budget_before_model,
        after_model_callback=record_model_usage,
        on_model_error_callback=clear_invocation_reservation,
        name="market_agent",
        instruction=SYSTEM_PROMPT,
        tools=[
            _capturing_consensus(capture) if capture is not None else get_market_consensus,
            compute_proprietary_composite,
            _capturing_blend(capture) if capture is not None else blend_with_market,
        ],
    )


async def run_market_agent(
    player_id: str, opportunity_score: int, production_score: int, current_health_score: int, aging_risk: str
) -> dict:
    """Run the Market Agent for a given player. Returns parsed JSON output with
    every tool-backed field set from the actual tool results (see
    _apply_deterministic_fields), or {"error": ...} if those results are
    missing or don't match this player's inputs."""
    capture: dict = {}
    agent = build_market_agent(capture)
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
            f"current_health_score={current_health_score}, aging_risk={aging_risk!r}."
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
        expected = compute_proprietary_composite(
            opportunity_score, production_score, current_health_score, aging_risk
        )["proprietary_composite"]
        return _apply_deterministic_fields(json.loads(json_match.group()), capture, player_id, expected)
    return {"raw_output": result_text}


if __name__ == "__main__":
    import asyncio
    player_id = sys.argv[1] if len(sys.argv) > 1 else "5967"  # Tony Pollard
    print(f"Running Market Agent for player_id: {player_id}\n")
    # Sample inputs approximating Pollard's situation/production agent outputs
    result = asyncio.run(run_market_agent(player_id, opportunity_score=72, production_score=68, current_health_score=2, aging_risk="high"))
    print(json.dumps(result, indent=2))
