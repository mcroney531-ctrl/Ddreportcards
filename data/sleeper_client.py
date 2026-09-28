"""Ddreportcards Sleeper compatibility surface.

Combines two sources under the names existing callers already use:
  - Sleeper provider/domain primitives, re-exported from dynasty_core.sleeper
  - Ddreportcards-owned workflows (cross-season trades, owner resolution,
    roster enrichment), re-exported from data.sleeper_workflows
"""
from dynasty_core.sleeper import (  # noqa: F401
    get_league_users,
    get_league_rosters,
    get_league_info,
    get_league_season_chain,
    get_transactions,
    get_all_trades,
    get_all_players,
    get_trending_adds,
)
from data.sleeper_workflows import (  # noqa: F401
    get_all_trades_all_seasons,
    get_roster_by_display_name,
    resolve_roster_players,
)
