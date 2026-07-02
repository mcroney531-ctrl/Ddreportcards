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


def index_by_sleeper_id_with_redraft_rank(values: list[dict]) -> dict[str, dict]:
    """Like index_by_sleeper_id, but each entry also gets 'redraftPositionRank'.

    FantasyCalc's API gives a dynasty positionRank directly but no redraft
    equivalent, so it's derived here by sorting each position group by
    redraftValue. Redraft rank reflects who is actually eating snaps *now*,
    which is the relevant signal for grading current on-field competition.
    """
    by_sleeper = index_by_sleeper_id(values)
    by_position: dict[str, list[str]] = {}
    for sid, entry in by_sleeper.items():
        pos = entry["player"].get("position")
        by_position.setdefault(pos, []).append(sid)
    for sids in by_position.values():
        sids.sort(key=lambda s: by_sleeper[s].get("redraftValue") or 0, reverse=True)
        for i, sid in enumerate(sids):
            by_sleeper[sid]["redraftPositionRank"] = i + 1
    return by_sleeper
