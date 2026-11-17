"""Ddreportcards' canonical league profile for Dynasty Daddies.

The one source of this app's deployment identity (league_id, league and team
names, the owner's Sleeper display name) and of verified league facts. Both
api.py (every Sleeper call, /league, the chat get_league_info tool) and the
Streamlit app.py read it; neither reads identity from the environment.

GM Command's dynasty_config.json is a checked mirror of this profile:
  python scripts/check_league_profile.py --gm-config ../gm-command/dynasty_config.json

num_teams / num_qbs / ppr / is_dynasty are facts about the league.
dynasty_core.settings holds numerically identical FantasyCalc *query defaults*,
chosen to price this league. They mean different things, so neither is derived
from the other; tests/test_league_identity.py asserts they agree, and a
divergence means FantasyCalc is being queried for the wrong format.
Verified against Sleeper API 2026-07-04.
"""

LEAGUE = {
    "league_id": "1312201057664786432",
    "league_name": "Dynasty Daddies",      # Sleeper league name
    "my_team_name": "The Psych Ward",      # TitansTrev55's team name
    "my_display_name": "TitansTrev55",     # Sleeper display name / username
    "num_teams": 12,
    "num_qbs": 2,           # superflex counts the QB slot twice
    "ppr": 0.5,             # half-PPR — confirmed rec=0.5 in scoring_settings
    "scoring_label": "half-PPR",
    "pass_td_pts": 4.0,     # confirmed pass_td=4.0 in scoring_settings
    "is_dynasty": True,
    "active_roster_size": 24,   # 10 starters + 14 BN
    "taxi_slots": 4,
    "reserve_slots": 4,         # IR slots
}
