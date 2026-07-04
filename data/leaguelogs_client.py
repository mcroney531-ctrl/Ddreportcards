"""LeagueLogs API client — re-exports from dynasty_core.leaguelogs.
Stage 1 of the Dynasty Umbrella: LeagueLogs logic consolidated in dynasty_core/.
All external agent APIs preserved; no agent changes needed.
"""
from dynasty_core.leaguelogs import (  # noqa: F401
    ATTRIBUTION_HTML,
    get_all_players,
    get_espn_id,
    get_player_blurb,
)
