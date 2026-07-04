"""Canonical league settings for The Psych Ward — single source of truth.

Stage 0 of the Dynasty Umbrella project. Both Python engines import LEAGUE
from here; GM Command mirrors these values as dynasty_config.json.

All FantasyCalc queries (ppr, numQbs, numTeams) must come from this module.
"""

LEAGUE = {
    "league_id": "1312201057664786432",
    "league_name": "The Psych Ward",
    "my_display_name": "TitansTrev55",
    "num_teams": 12,
    "num_qbs": 2,           # superflex counts the QB slot twice
    "ppr": 0.5,             # half-PPR — 0=standard, 0.5=half, 1=full
    "scoring_label": "half-PPR",
    "is_dynasty": True,
    "roster_size": 27,
}
