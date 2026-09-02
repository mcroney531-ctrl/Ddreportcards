"""FantasyCalc dynasty value consensus — merged client for the Dynasty Umbrella.

Open, unauthenticated. https://api.fantasycalc.com/values/current

Merges scoutcap/tools/fantasycalc.py (grade logic + condensed index accessor)
and ddreportcards/data/fantasycalc_client.py (raw list interface + full caching).
League params driven by config/dynasty_config.py; falls back to half-PPR defaults.

Two accessor styles, both supported:
  Raw entry (Report Cards style): get_value_for_sleeper_id() -> full FC dict
  Condensed record (Scout style):  get_player_value()          -> flat dict
"""
from __future__ import annotations

import re
import time
from collections import defaultdict

import httpx

try:
    from config.dynasty_config import LEAGUE as _LEAGUE
    _DEFAULT_PPR: float = _LEAGUE["ppr"]
    _DEFAULT_NUM_QBS: int = _LEAGUE["num_qbs"]
    _DEFAULT_NUM_TEAMS: int = _LEAGUE["num_teams"]
except ImportError:
    _DEFAULT_PPR = 0.5
    _DEFAULT_NUM_QBS = 2
    _DEFAULT_NUM_TEAMS = 12

BASE_URL = "https://api.fantasycalc.com/values/current"

# Grade tiers by redraft position rank within each position group.
# Calibrated for 12-team superflex (from scoutcap).
GRADE_TIERS: dict[str, list[tuple[int, str]]] = {
    "QB": [(12, "A"), (20, "B"), (28, "C"), (36, "D")],
    "RB": [(12, "A"), (24, "B"), (36, "C"), (48, "D")],
    "WR": [(12, "A"), (24, "B"), (36, "C"), (54, "D")],
    "TE": [(6,  "A"), (12, "B"), (18, "C"), (24, "D")],
}

_values_cache: list[dict] | None = None
_values_cache_time: float = 0.0
_VALUES_TTL_SECONDS = 6 * 60 * 60
_index_cache: dict[str, dict] | None = None  # condensed index; invalidated with values cache


def get_dynasty_values(
    is_dynasty: bool = True,
    num_qbs: int = _DEFAULT_NUM_QBS,
    num_teams: int = _DEFAULT_NUM_TEAMS,
    ppr: float = _DEFAULT_PPR,
) -> list[dict]:
    """Full league-wide value list from FantasyCalc. Cached 6 h in-process."""
    global _values_cache, _values_cache_time, _index_cache
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
        _index_cache = None
    return _values_cache


def index_by_sleeper_id(values: list[dict]) -> dict[str, dict]:
    """Raw FantasyCalc entries indexed by Sleeper player_id."""
    return {
        entry["player"]["sleeperId"]: entry
        for entry in values
        if entry.get("player", {}).get("sleeperId")
    }


_PICK_LABEL_RE = re.compile(r"^\d{4} (1st|2nd|3rd|4th)$")


def index_picks_by_label(values: list[dict]) -> dict[str, dict]:
    """FantasyCalc draft-pick entries keyed by their round-only label.

    Picks come back in the same /values/current payload as players, marked
    position "PICK" and named like "2029 2nd". index_by_sleeper_id drops them
    because a pick has no sleeperId, so this is the other half of that split.

    Round-only is as precise as it gets and as precise as is useful: Sleeper's
    traded-pick data is also only {season, round}, since the actual slot isn't
    known until the draft order is set.
    """
    return {
        entry["player"]["name"]: entry
        for entry in values
        if entry.get("player", {}).get("position") == "PICK"
        and _PICK_LABEL_RE.match(entry["player"].get("name", ""))
    }


def get_value_for_sleeper_id(sleeper_id: str) -> dict | None:
    """Raw FantasyCalc entry for a player (as returned by the API), or None."""
    values = get_dynasty_values()
    return index_by_sleeper_id(values).get(sleeper_id)


def index_by_sleeper_id_with_redraft_rank(values: list[dict]) -> dict[str, dict]:
    """Like index_by_sleeper_id, but each entry also gets 'redraftPositionRank'.

    Derived by sorting each position group by redraftValue — reflects who is
    actually eating snaps now, the relevant signal for grading on-field competition.
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


def _build_condensed_index() -> dict[str, dict]:
    """Build {sleeper_id: condensed_rec} used by get_player_value(). Cached alongside values."""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    data = get_dynasty_values()
    by_sleeper: dict[str, dict] = {}
    pos_players: dict[str, list[str]] = defaultdict(list)

    for d in data:
        p = d.get("player", {})
        sid = p.get("sleeperId")
        if not sid:
            continue
        rec = {
            "name": p.get("name"),
            "position": p.get("position"),
            "team": p.get("maybeTeam"),
            "age": p.get("maybeAge"),
            "years_exp": p.get("maybeYoe"),
            "dynasty_value": d.get("value") or 0,
            "redraft_value": d.get("redraftValue") or 0,
            "dynasty_pos_rank": d.get("positionRank"),
            "redraft_pos_rank": None,
        }
        by_sleeper[sid] = rec
        pos_players[p.get("position")].append(sid)

    for pos, sids in pos_players.items():
        sids.sort(key=lambda s: by_sleeper[s]["redraft_value"], reverse=True)
        for i, sid in enumerate(sids):
            by_sleeper[sid]["redraft_pos_rank"] = i + 1

    _index_cache = by_sleeper
    return _index_cache


def get_player_value(sleeper_id) -> dict | None:
    """Condensed FantasyCalc record for a player (Scout-style accessor).

    Returns None if the player is outside the dynasty-relevant pool (~460 players).
    Keys: name, position, team, age, years_exp, dynasty_value, redraft_value,
    dynasty_pos_rank, redraft_pos_rank.
    """
    return _build_condensed_index().get(str(sleeper_id))


def value_grade(position: str, redraft_pos_rank: int | None) -> str:
    """Letter grade A–F for a competitor based on redraft rank within their position."""
    if redraft_pos_rank is None:
        return "F"
    for thresh, grade in GRADE_TIERS.get(position, []):
        if redraft_pos_rank <= thresh:
            return grade
    return "F"
