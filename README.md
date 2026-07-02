# Dynasty Report Cards

Roster report card agent for a 12-team superflex PPR dynasty league. Grades every
player on a roster (Opportunity, Production, Risk, Trade Value), rolls those grades
into an overall roster grade, and flags sell candidates for the trade section.

Phase 1: single team (owner `BCNH`, team "Pain"). Expands to the full league later.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY, GOOGLE_API_KEY, SLEEPER_LEAGUE_ID
streamlit run app.py
```

## Architecture

- `data/` — API clients: Sleeper (rosters/players), ESPN hidden Core API (current
  production, injuries), FantasyCalc (dynasty values).
- `agents/situation_agent.py` — opportunity grade (depth chart, competition quality).
- `agents/production_agent.py` — production grade + risk modifier (durability,
  injury probability, age curve).
- `agents/market_agent.py` — hybrid trade value (proprietary composite blended with
  FantasyCalc dynasty consensus).
- `agents/synthesis_agent.py` — orchestrates the three into one player card.
- `agents/roster_agent.py` — aggregates player cards into the overall roster grade.
- `agents/trade_agent.py` — flags sell candidates with reasoning.
- `app.py` — Streamlit frontend (roster overview, player card, trade section).
