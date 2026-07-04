"""LeagueLogs Developer API client — free, no-auth, read-only.

https://developer.leaguelogs.com — used here for two things only:
1. A second ESPN athlete id source (get_espn_id), since Sleeper's and
   FantasyCalc's ids both go missing for deep-roster players.
2. LLM-rewritten injury/status blurbs (get_player_blurb), a cleaner signal
   than scraping ESPN's team injury feed.

Deliberately NOT using their Market Index — FantasyCalc is the sole value
anchor for this project; same reason KeepTradeCut was excluded.

Attribution is required by their terms whenever this data is displayed —
see ATTRIBUTION_HTML.

Part of dynasty_core — shared data layer for the Dynasty Umbrella.
"""
from __future__ import annotations

import time

import httpx

BASE_URL = "https://developer.leaguelogs.com/v1"

ATTRIBUTION_HTML = (
    '<a href="https://leaguelogs.com" target="_blank" rel="noopener" '
    "style=\"display:inline-flex;align-items:baseline;gap:6px;"
    "font-family:Georgia,'Times New Roman',serif;font-size:14px;line-height:1;"
    'color:currentColor;text-decoration:none;">'
    "<span>Powered by</span>"
    '<em style="font-weight:600;letter-spacing:-0.01em;">LeagueLogs</em>'
    '<span aria-hidden="true" style="width:5px;height:5px;border-radius:50%;'
    'background:#ffe24c;display:inline-block;"></span>'
    "</a>"
)

_players_cache: dict[str, dict] | None = None
_players_cache_time: float = 0.0
_PLAYERS_TTL_SECONDS = 24 * 60 * 60


def get_all_players() -> dict[str, dict]:
    """All tracked NFL skill-position players, indexed by sleeperPlayerId."""
    global _players_cache, _players_cache_time
    now = time.time()
    if _players_cache is None or (now - _players_cache_time) > _PLAYERS_TTL_SECONDS:
        resp = httpx.get(f"{BASE_URL}/players", timeout=20)
        resp.raise_for_status()
        data = resp.json()
        _players_cache = {p["sleeperPlayerId"]: p for p in data.get("data", [])}
        _players_cache_time = now
    return _players_cache


def get_espn_id(sleeper_player_id: str) -> str | None:
    """LeagueLogs' ESPN id sync — fallback when Sleeper's and FantasyCalc's ids are both missing."""
    player = get_all_players().get(sleeper_player_id)
    return player.get("espnId") if player else None


def get_player_blurb(sleeper_player_id: str) -> dict | None:
    """1-3 sentence LLM-rewritten status note, or None if no recent material."""
    resp = httpx.get(f"{BASE_URL}/players/{sleeper_player_id}/blurb", timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return {"blurb": data.get("blurb"), "signals": data.get("signals", [])}
