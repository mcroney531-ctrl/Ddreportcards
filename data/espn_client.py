"""ESPN Core API client — re-exports from dynasty_core.espn.
Stage 1 of the Dynasty Umbrella: ESPN logic consolidated in dynasty_core/.
All external agent APIs preserved; no agent changes needed.
"""
from dynasty_core.espn import (  # noqa: F401
    TEAM_ESPN_IDS,
    TEAM_ABBR_ALIASES,
    team_espn_id,
    get_season_statistics,
    get_career_statistics,
    get_event_log,
    flatten_statistics,
    get_injury_detail,
    get_player_injury_notes,
)
