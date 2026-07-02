"""Sleeper API client — league rosters/users, player metadata, trending adds.

No auth required. https://docs.sleeper.com/
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
    """Resolve a roster by the owner's Sleeper display name (e.g. 'BCNH')."""
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
    all_players = get_all_players()
    return [
        {"player_id": pid, **all_players.get(pid, {})}
        for pid in roster.get("players", [])
    ]
