"""Sleeper fantasy API client — dynasty league rosters, users, players, and trades.

No auth required. https://docs.sleeper.com/
Part of dynasty_core — shared data layer for the Dynasty Umbrella.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

BASE_URL = "https://api.sleeper.app/v1"

# get_all_players() is a player catalog / identity map, not a live-status
# feed. Sleeper's own docs say the full /players/nfl payload should be
# fetched sparingly -- at most about once a day -- and stored rather than
# re-fetched per lookup, with filtered position/active variants documented
# as the better option when only a subset is needed. 24h matches that
# guidance. This is a process-local cache: a worker restart clears it and
# can trigger another full fetch inside 24h -- that's accepted for the
# current single-user deployment footprint, not a claim of enforcing
# Sleeper's once/day recommendation globally across restarts. A consumer
# that needs sub-24h freshness (e.g. injury_status, practice_participation,
# depth-chart fields) should use a smaller filtered-provider primitive
# instead of shortening this TTL -- none exists yet.
_players_cache: dict[str, Any] | None = None
_players_cache_time: float = 0.0
_PLAYERS_TTL_SECONDS = 24 * 60 * 60


def get_user(username: str) -> dict:
    resp = httpx.get(f"{BASE_URL}/user/{username}", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_leagues(user_id: str, season: str) -> list[dict]:
    resp = httpx.get(f"{BASE_URL}/user/{user_id}/leagues/nfl/{season}", timeout=15)
    resp.raise_for_status()
    return resp.json()


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


def get_all_players() -> dict[str, dict]:
    """Full Sleeper player catalog keyed by player_id. ~14MB; cached
    process-local for 24h (see the cache comment above _players_cache).

    This is identity/metadata (name, position, team, college, age, etc.),
    not a guarantee of real-time injury/practice/depth-chart freshness --
    those fields are present but can be up to 24h stale.
    """
    global _players_cache, _players_cache_time
    now = time.time()
    if _players_cache is None or (now - _players_cache_time) > _PLAYERS_TTL_SECONDS:
        resp = httpx.get(f"{BASE_URL}/players/nfl", timeout=30)
        resp.raise_for_status()
        _players_cache = resp.json()
        _players_cache_time = now
    return _players_cache


def get_player(player_id: str) -> dict | None:
    """Look up one player from the cached full Sleeper player catalog.

    Uses get_all_players(); does not call Sleeper's differently-shaped
    /players/nfl/{player_id} endpoint.
    """
    return get_all_players().get(str(player_id))


def get_trending(
    type: str = "add",
    sport: str = "nfl",
    limit: int = 25,
    lookback_hours: int = 24,
) -> list[dict]:
    # type/sport/limit keep scoutcap tools.sleeper.get_trending's positional
    # order so its facade can swap onto this; lookback_hours is appended.
    resp = httpx.get(
        f"{BASE_URL}/players/{sport}/trending/{type}",
        params={
            "lookback_hours": lookback_hours,
            "limit": limit,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_trending_adds(
    lookback_hours: int = 24,
    limit: int = 25,
) -> list[dict]:
    return get_trending(
        type="add",
        sport="nfl",
        limit=limit,
        lookback_hours=lookback_hours,
    )
