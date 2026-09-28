"""Ddreportcards' app-level league metadata for Dynasty Daddies.

Deployment identity and league facts this app uses directly (e.g. league_id
and my_display_name in api.py, and the /league endpoint). GM Command keeps
its own mirror of these values in dynasty_config.json.

FantasyCalc provider-query defaults are NOT sourced from here: they are
owned by the shared package, in dynasty_core.settings. Some values overlap
today (num_teams, num_qbs, ppr); that duplication is a known, deliberately
deferred item.
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
