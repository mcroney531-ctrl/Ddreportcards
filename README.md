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

## Testing

Offline contract suite — no Anthropic key, no Upstash connection, no
Sleeper/FantasyCalc/ESPN network, no production server required:

```
python -m unittest discover -s tests -v
```

Live-production smoke test — makes real HTTP requests against an already
deployed instance, never runs automatically (not part of the suite above,
not run by CI):

```
export GM_SMOKE_BASE_URL="https://ddreportcards.onrender.com"
export GM_CHAT_SECRET="<the real X-GM-Key value>"

# Read-only, free: liveness, /health/deep, a couple of public data
# endpoints, and that missing/wrong-secret requests actually get rejected.
python scripts/smoke_production.py

# Same as above, plus exactly one real, billable /report/player/<id> call.
python scripts/smoke_production.py --billable-player-report 12501
```

Dependency drift check — requirements.txt pins exact versions verified
against a known-good Render deployment (Phase 4B); this reports whether
what's actually installed still matches:

```
python scripts/check_dependency_versions.py
```
