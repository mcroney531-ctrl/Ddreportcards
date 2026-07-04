"""Sleeper API client — re-exports from dynasty_core.sleeper.
Stage 1 of the Dynasty Umbrella: Sleeper logic consolidated in dynasty_core/.
All external agent APIs preserved; no agent changes needed.
"""
from dynasty_core.sleeper import (  # noqa: F401
    get_league_users,
    get_league_rosters,
    get_league_info,
    get_league_season_chain,
    get_transactions,
    get_all_trades,
    get_all_trades_all_seasons,
    get_all_players,
    get_trending_adds,
    get_roster_by_display_name,
    resolve_roster_players,
)
