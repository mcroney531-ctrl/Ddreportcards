"""Canonical league settings for Dynasty Daddies — single source of truth.

Stage 0 of the Dynasty Umbrella project. Both Python engines import LEAGUE
from here; GM Command mirrors these values as dynasty_config.json.

All FantasyCalc queries (ppr, numQbs, numTeams) must come from this module.
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
