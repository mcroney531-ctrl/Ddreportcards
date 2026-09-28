"""Ddreportcards-owned Sleeper workflows.

Application composition built only from shared dynasty_core.sleeper
primitives: cross-season trade collection, owner resolution by display
name, and roster enrichment. They encode Ddreportcards policy (the
_season/_league_id annotation convention, display-name identity), so they
live here rather than in the shared package. No provider HTTP belongs in
this module.
"""
from __future__ import annotations

import dynasty_core.sleeper as sleeper_core


def get_all_trades_all_seasons(league_id: str) -> list[dict]:
    """Every completed trade across this dynasty league's full history."""
    all_trades = []
    for season_info in sleeper_core.get_league_season_chain(league_id):
        for trade in sleeper_core.get_all_trades(season_info["league_id"]):
            trade["_season"] = season_info["season"]
            trade["_league_id"] = season_info["league_id"]
            all_trades.append(trade)
    all_trades.sort(key=lambda t: t.get("created") or 0, reverse=True)
    return all_trades


def get_roster_by_display_name(league_id: str, display_name: str) -> dict:
    """Resolve a roster by the owner's Sleeper display name (e.g. 'TitansTrev55')."""
    users = sleeper_core.get_league_users(league_id)
    user = next((u for u in users if u.get("display_name") == display_name), None)
    if user is None:
        raise ValueError(f"No league user found with display_name={display_name!r}")
    rosters = sleeper_core.get_league_rosters(league_id)
    roster = next((r for r in rosters if r.get("owner_id") == user["user_id"]), None)
    if roster is None:
        raise ValueError(f"No roster found for user_id={user['user_id']!r}")
    return roster


def resolve_roster_players(roster: dict) -> list[dict]:
    """Attach Sleeper player metadata to each player_id on a roster."""
    all_players_map = sleeper_core.get_all_players()
    return [
        {"player_id": pid, **all_players_map.get(pid, {})}
        for pid in roster.get("players", [])
    ]
