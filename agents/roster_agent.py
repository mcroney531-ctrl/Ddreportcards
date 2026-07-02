"""
Roster Agent — aggregates every player card on BCNH's roster into one overall
roster grade. Reuses Scout's quality-weighted philosophy: grade starters by
quality, not headcount, apply the superflex QB premium when filling the SF
slot, and flag positional thinness deterministically. The LLM's job is
narrative synthesis over numbers computed in code, not the grading math itself.

Inputs:  list of player cards (as produced by synthesis_agent.run_synthesis_agent)
Outputs: overall letter grade, starter quality score, positional breakdown, flags
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

# League format: 12-team, superflex (2 QB starts), PPR dynasty.
BASE_STARTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
FLEX_SLOTS = 2       # RB/WR/TE eligible
SF_SLOTS = 1          # QB/RB/WR/TE eligible (superflex)

# Superflex positional value multiplier — QBs get a real premium when
# competing for the SF slot, mirroring Scout's draft-side POSITION_VALUE.
POSITION_VALUE = {"QB": 1.18, "RB": 1.0, "WR": 1.0, "TE": 0.95}

QUALITY_WEIGHTS = {"opportunity": 0.30, "production": 0.40, "trade_value": 0.30}

# Below this quality_score, a player isn't a real dynasty contributor —
# used for "thin at position" flags.
CONTRIBUTOR_THRESHOLD = 50
MIN_CONTRIBUTORS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}

LETTER_GRADE_TABLE = [
    (97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
    (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"), (63, "D"), (60, "D-"), (0, "F"),
]


def _letter_grade(score: float) -> str:
    for threshold, letter in LETTER_GRADE_TABLE:
        if score >= threshold:
            return letter
    return "F"


def _quality_score(card: dict) -> float:
    # `or 0` (not just .get(key, 0)) because a sub-agent can return an explicit
    # null for a score field, not just omit the key — .get's default only
    # covers the latter.
    return (
        (card.get("opportunity_score") or 0) * QUALITY_WEIGHTS["opportunity"]
        + (card.get("production_score") or 0) * QUALITY_WEIGHTS["production"]
        + (card.get("trade_value_score") or 0) * QUALITY_WEIGHTS["trade_value"]
    )


# ── ADK tool function ─────────────────────────────────────────────────────────

def compute_roster_grade(player_cards_json: str) -> dict:
    """
    Compute the overall roster grade from a JSON array of player cards (as
    produced by the Synthesis Agent — each with position, opportunity_score,
    production_score, trade_value_score, player name).

    Assigns starters by quality (not raw headcount): base position slots first
    (QB1/RB2/WR2/TE1), then FLEX (best remaining RB/WR/TE), then the superflex
    SF slot (best remaining player at ANY position, with a QB value premium
    applied since QBs carry real superflex scarcity value). Everyone else is
    bench. Overall grade is the average quality_score across all starters.

    player_cards_json: JSON-encoded list of player card dicts
    """
    cards = json.loads(player_cards_json)
    for c in cards:
        c["quality_score"] = round(_quality_score(c), 1)

    by_position: dict[str, list[dict]] = {"QB": [], "RB": [], "WR": [], "TE": []}
    for c in cards:
        pos = c.get("position")
        if pos in by_position:
            by_position[pos].append(c)
    for players in by_position.values():
        players.sort(key=lambda c: c["quality_score"], reverse=True)

    starters: list[dict] = []
    used_ids: set[str] = set()

    def _player_key(c: dict) -> str:
        return c.get("player") or c.get("player_id") or str(id(c))

    # 1. Base position slots
    for pos, slots in BASE_STARTER_SLOTS.items():
        for c in by_position[pos][:slots]:
            starters.append({**c, "starter_slot": pos})
            used_ids.add(_player_key(c))

    # 2. FLEX (RB/WR/TE), best remaining by quality_score
    flex_pool = [
        c for pos in ("RB", "WR", "TE") for c in by_position[pos] if _player_key(c) not in used_ids
    ]
    flex_pool.sort(key=lambda c: c["quality_score"], reverse=True)
    for c in flex_pool[:FLEX_SLOTS]:
        starters.append({**c, "starter_slot": "FLEX"})
        used_ids.add(_player_key(c))

    # 3. Superflex SF slot — best remaining at ANY position, QB premium applied
    sf_pool = [c for players in by_position.values() for c in players if _player_key(c) not in used_ids]
    sf_pool.sort(key=lambda c: c["quality_score"] * POSITION_VALUE.get(c.get("position"), 1.0), reverse=True)
    for c in sf_pool[:SF_SLOTS]:
        starters.append({**c, "starter_slot": "SF"})
        used_ids.add(_player_key(c))

    starter_quality_score = round(sum(c["quality_score"] for c in starters) / len(starters), 1) if starters else 0.0

    positional_breakdown = {}
    flags = []
    for pos, players in by_position.items():
        contributors = [p for p in players if p["quality_score"] >= CONTRIBUTOR_THRESHOLD]
        pos_starters = [c for c in starters if c.get("position") == pos]
        avg_starter_quality = (
            round(sum(c["quality_score"] for c in pos_starters) / len(pos_starters), 1) if pos_starters else 0.0
        )
        positional_breakdown[pos] = {
            "roster_count": len(players),
            "real_contributors": len(contributors),
            "starters_assigned": len(pos_starters),
            "avg_starter_quality": avg_starter_quality,
            "top_players": [{"name": p.get("player"), "quality_score": p["quality_score"]} for p in players[:3]],
        }
        if len(contributors) < MIN_CONTRIBUTORS.get(pos, 1):
            flags.append(
                f"thin at {pos} — only {len(contributors)} real contributor(s) "
                f"(quality_score >= {CONTRIBUTOR_THRESHOLD}) on the roster"
            )

    return {
        "overall_grade": _letter_grade(starter_quality_score),
        "starter_quality_score": starter_quality_score,
        "starters": [
            {"name": c.get("player"), "position": c.get("position"), "starter_slot": c["starter_slot"], "quality_score": c["quality_score"]}
            for c in starters
        ],
        "positional_breakdown": positional_breakdown,
        "flags": flags,
        "bench_count": len(cards) - len(starters),
    }


# ── System prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Roster Agent for a dynasty fantasy football roster report card tool.

You receive a JSON array of player cards for one team's full roster (already produced by
the Synthesis Agent, each with opportunity/production/trade-value scores) and must produce
the overall roster grade.

League format: 12-team, 4-round, SUPERFLEX (2 QB starts), PPR dynasty.

Steps:
1. Call compute_roster_grade with the player_cards_json you were given, verbatim as a
   JSON string
2. Use its output (overall_grade, starter_quality_score, positional_breakdown, flags)
   to write the final roster report

CRITICAL — the grading math (starter assignment, quality scores, flags) is already done in
compute_roster_grade. Do NOT recompute or override those numbers. Your job is narrative:
explain WHY the roster grades where it does, referencing specific players and positions
from positional_breakdown, and elaborate on the flags with concrete context.

Output format — return a JSON object with these exact keys:
{
  "overall_grade": "B-",
  "starter_quality_score": 78.4,
  "positional_breakdown": { ...pass through compute_roster_grade's positional_breakdown... },
  "flags": ["thin at TE — only 1 real contributor..."],
  "narrative": "3-5 sentence summary of overall roster strength and weakness, naming specific players/positions.",
  "strongest_position": "WR",
  "weakest_position": "TE"
}
"""

# ── Agent + runner ────────────────────────────────────────────────────────────

def build_roster_agent() -> LlmAgent:
    return LlmAgent(
        model=LiteLlm(model="anthropic/claude-sonnet-4-6", api_key=os.getenv("ANTHROPIC_API_KEY")),
        name="roster_agent",
        instruction=SYSTEM_PROMPT,
        tools=[compute_roster_grade],
    )


async def run_roster_agent(player_cards: list[dict]) -> dict:
    """Run the Roster Agent over a full set of player cards for one team."""
    agent = build_roster_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name="dynasty_report_cards", session_service=session_service)

    session_id = "roster_session"
    await session_service.create_session(
        app_name="dynasty_report_cards", user_id="user", session_id=session_id
    )

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=(
            "Compute the overall roster grade for this team. player_cards_json:\n"
            + json.dumps(player_cards)
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
    # Minimal smoke test with synthetic cards — real usage passes synthesis_agent output.
    sample_cards = [
        {"player": "Tony Pollard", "position": "RB", "opportunity_score": 72, "production_score": 68, "trade_value_score": 59},
        {"player": "Kirk Cousins", "position": "QB", "opportunity_score": 60, "production_score": 55, "trade_value_score": 40},
        {"player": "Tua Tagovailoa", "position": "QB", "opportunity_score": 80, "production_score": 70, "trade_value_score": 65},
    ]
    result = asyncio.run(run_roster_agent(sample_cards))
    print(json.dumps(result, indent=2))
