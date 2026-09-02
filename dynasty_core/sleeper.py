"""Sleeper fantasy API client — dynasty league rosters, users, players, and trades.

No auth required. https://docs.sleeper.com/
Part of dynasty_core — shared data layer for the Dynasty Umbrella.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://api.sleeper.app/v1"

_players_cache: dict[str, Any] | None = None
_players_cache_time: float = 0.0
_PLAYERS_TTL_SECONDS = 6 * 60 * 60


def get_league_users(league_id: str) -> list[dict]:
    resp = httpx.get(f"{BASE_URL}/league/{league_id}/users", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_league_rosters(league_id: str) -> list[dict]:
    resp = httpx.get(f"{BASE_URL}/league/{league_id}/rosters", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_league_info(league_id: str) -> dict:
    resp = httpx.get(f"{BASE_URL}/league/{league_id}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_traded_picks(league_id: str) -> list[dict]:
    """Every future draft pick that has changed hands in this league.

    Sleeper only records picks that MOVED. Each entry looks like
    {"season": "2029", "round": 2, "roster_id": 3, "previous_owner_id": 3,
     "owner_id": 5} where roster_id is whose pick it originally is and
    owner_id is who holds it now. A team's untraded picks appear nowhere,
    so a full inventory means starting from "everyone owns their own" and
    applying these as overrides.
    """
    resp = httpx.get(f"{BASE_URL}/league/{league_id}/traded_picks", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_league_drafts(league_id: str) -> list[dict]:
    """Draft objects for this league season, newest first. settings.rounds says
    how many rounds the rookie draft runs, which sets how many picks a team
    owns per future season."""
    resp = httpx.get(f"{BASE_URL}/league/{league_id}/drafts", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_league_season_chain(league_id: str) -> list[dict]:
    """Walk previous_league_id links to collect every season. Returns newest first.

    Each season in Sleeper dynasty is a separate league object linked by
    previous_league_id — this is normal Sleeper dynasty continuity.
    """
    chain = []
    current_id = league_id
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        info = get_league_info(current_id)
        chain.append({"league_id": current_id, "season": info.get("season")})
        current_id = info.get("previous_league_id")
    return chain


def get_transactions(league_id: str, week: int) -> list[dict]:
    resp = httpx.get(f"{BASE_URL}/league/{league_id}/transactions/{week}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_all_trades(league_id: str) -> list[dict]:
    """All completed trades for one season's league_id, deduped by transaction_id.

    Scans weeks 1-18 to catch both offseason (logged under week 1) and
    in-season trades.
    """
    trades: dict[str, dict] = {}
    for week in range(1, 19):
        for txn in get_transactions(league_id, week):
            if txn.get("type") == "trade" and txn.get("status") == "complete":
                trades[txn["transaction_id"]] = txn
    return list(trades.values())


def get_all_trades_all_seasons(league_id: str) -> list[dict]:
    """Every completed trade across this dynasty league's full history."""
    all_trades = []
    for season_info in get_league_season_chain(league_id):
        for trade in get_all_trades(season_info["league_id"]):
            trade["_season"] = season_info["season"]
            trade["_league_id"] = season_info["league_id"]
            all_trades.append(trade)
    all_trades.sort(key=lambda t: t.get("created") or 0, reverse=True)
    return all_trades


def get_all_players() -> dict[str, dict]:
    """Full Sleeper player dictionary keyed by player_id. ~14MB; cached in-process."""
    global _players_cache, _players_cache_time
    now = time.time()
    if _players_cache is None or (now - _players_cache_time) > _PLAYERS_TTL_SECONDS:
        resp = httpx.get(f"{BASE_URL}/players/nfl", timeout=30)
        resp.raise_for_status()
        _players_cache = resp.json()
        _players_cache_time = now
    return _players_cache


def get_trending_adds(lookback_hours: int = 24, limit: int = 25) -> list[dict]:
    resp = httpx.get(
        f"{BASE_URL}/players/nfl/trending/add",
        params={"lookback_hours": lookback_hours, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_roster_by_display_name(league_id: str, display_name: str) -> dict:
    """Resolve a roster by the owner's Sleeper display name (e.g. 'TitansTrev55')."""
    users = get_league_users(league_id)
    user = next((u for u in users if u.get("display_name") == display_name), None)
    if user is None:
        raise ValueError(f"No league user found with display_name={display_name!r}")
    rosters = get_league_rosters(league_id)
    roster = next((r for r in rosters if r.get("owner_id") == user["user_id"]), None)
    if roster is None:
        raise ValueError(f"No roster found for user_id={user['user_id']!r}")
    return roster


def resolve_roster_players(roster: dict) -> list[dict]:
    """Attach Sleeper player metadata to each player_id on a roster."""
    all_players_map = get_all_players()
    return [
        {"player_id": pid, **all_players_map.get(pid, {})}
        for pid in roster.get("players", [])
    ]
