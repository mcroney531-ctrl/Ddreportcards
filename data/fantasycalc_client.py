"""FantasyCalc client — dynasty value consensus, matched by Sleeper player id.

Open, unauthenticated. https://api.fantasycalc.com/values/current
"""
from __future__ import annotations

import time

import httpx

BASE_URL = "https://api.fantasycalc.com/values/current"

_values_cache: list[dict] | None = None
_values_cache_time: float = 0.0
_VALUES_TTL_SECONDS = 6 * 60 * 60


def get_dynasty_values(is_dynasty: bool = True, num_qbs: int = 2, num_teams: int = 12, ppr: float = 1) -> list[dict]:
    """Full league-wide value list. Cached in-process since it's the same call every time for this league."""
    global _values_cache, _values_cache_time
    now = time.time()
    if _values_cache is None or (now - _values_cache_time) > _VALUES_TTL_SECONDS:
        resp = httpx.get(
            BASE_URL,
            params={"isDynasty": str(is_dynasty).lower(), "numQbs": num_qbs, "numTeams": num_teams, "ppr": ppr},
            timeout=20,
        )
        resp.raise_for_status()
        _values_cache = resp.json()
        _values_cache_time = now
    return _values_cache


def index_by_sleeper_id(values: list[dict]) -> dict[str, dict]:
    return {
        entry["player"]["sleeperId"]: entry
        for entry in values
        if entry.get("player", {}).get("sleeperId")
    }


def get_value_for_sleeper_id(sleeper_id: str) -> dict | None:
    values = get_dynasty_values()
    return index_by_sleeper_id(values).get(sleeper_id)
