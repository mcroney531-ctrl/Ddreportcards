"""ESPN hidden Core API client — active NFL player stats, injuries, and team map.

Undocumented, no auth. Two gotchas documented here for both engines:
1. GET .../athletes/{id}/statistics returns CAREER totals, not current season.
   Use get_season_statistics() (seasons/{year}/types/2) for single-season stats.
2. ESPN uses 'WSH' for Washington where Sleeper uses 'WAS' — see TEAM_ABBR_ALIASES.
   ESPN also 400s on a malformed/unknown athlete id rather than 404ing —
   get_season_statistics() and get_career_statistics() treat 400 as 404.

Part of dynasty_core — shared data layer for the Dynasty Umbrella.
NOTE: draft/college ESPN functions (get_college_stats, search_draft_prospects)
are scout-specific and live in scoutcap/tools/espn.py, not here.
"""
from __future__ import annotations

import re

import httpx

BASE_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"

# Static abbreviation -> ESPN team id map (32 teams, stable across seasons).
TEAM_ESPN_IDS: dict[str, str] = {
    "ARI": "22", "ATL": "1",  "BAL": "33", "BUF": "2",  "CAR": "29", "CHI": "3",
    "CIN": "4",  "CLE": "5",  "DAL": "6",  "DEN": "7",  "DET": "8",  "GB": "9",
    "HOU": "34", "IND": "11", "JAX": "30", "KC": "12",  "LAC": "24", "LAR": "14",
    "LV": "13",  "MIA": "15", "MIN": "16", "NE": "17",  "NO": "18",  "NYG": "19",
    "NYJ": "20", "PHI": "21", "PIT": "23", "SEA": "26", "SF": "25",  "TB": "27",
    "TEN": "10", "WSH": "28",
}

# Sleeper -> ESPN team abbreviation aliases where the two providers disagree.
TEAM_ABBR_ALIASES: dict[str, str] = {"WAS": "WSH"}


def team_espn_id(sleeper_team_abbr: str) -> str | None:
    """Convert a Sleeper team abbreviation to the ESPN team id, handling aliases."""
    abbr = TEAM_ABBR_ALIASES.get(sleeper_team_abbr, sleeper_team_abbr)
    return TEAM_ESPN_IDS.get(abbr)


def get_season_statistics(espn_athlete_id: str, season: int) -> dict:
    """Current-season stats for an active NFL player.

    Returns {} if the athlete has no stats for that season, hasn't played yet,
    or the id is missing/malformed (ESPN 400s on bad ids rather than 404ing).
    """
    url = f"{BASE_URL}/seasons/{season}/types/2/athletes/{espn_athlete_id}/statistics"
    resp = httpx.get(url, timeout=15)
    if resp.status_code in (400, 404):
        return {}
    resp.raise_for_status()
    return resp.json()


def get_career_statistics(espn_athlete_id: str) -> dict:
    """Career stat totals. Returns {} if no data on file or bad id."""
    url = f"{BASE_URL}/athletes/{espn_athlete_id}/statistics"
    resp = httpx.get(url, timeout=15)
    if resp.status_code in (400, 404):
        return {}
    resp.raise_for_status()
    return resp.json()


def get_event_log(espn_athlete_id: str) -> dict:
    """Per-game refs, most recent first. Each item's 'played' flag marks missed games."""
    url = f"{BASE_URL}/athletes/{espn_athlete_id}/eventlog"
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


_WEEK_IN_REF = re.compile(r"/weeks/(\d+)")


def _week_number(event: dict):
    """Week number from an ESPN event, without a second request.

    `week` comes back as a $ref rather than an inline {"number": n}, so
    reading .number off it silently yielded nothing. The number is already in
    the ref's URL (.../types/2/weeks/3/...), so pull it from there rather than
    spend another round trip per game on a single integer.
    """
    week = event.get("week")
    if isinstance(week, dict):
        if isinstance(week.get("number"), int):
            return week["number"]
        m = _WEEK_IN_REF.search(week.get("$ref") or "")
        if m:
            return int(m.group(1))
    return None


# Stats kept even when zero, because at zero they are the story: a receiver
# who was not targeted is a fact worth stating, where a receiver with no
# passing yards is just a receiver. Scoped by position so the zero-keeping
# does not drag a quarterback's stat line onto a wideout.
_RECEIVING_CORE = {"receptions", "receivingTargets", "receivingYards", "receivingTouchdowns"}
_RUSHING_CORE = {"rushingAttempts", "rushingYards", "rushingTouchdowns"}
_PASSING_CORE = {"passingAttempts", "completions", "passingYards", "passingTouchdowns", "interceptions"}
_ALWAYS_CORE = {"gamesPlayed", "fumblesLost"}

_CORE_BY_POSITION = {
    "QB": _PASSING_CORE | _RUSHING_CORE | _ALWAYS_CORE,
    "RB": _RUSHING_CORE | _RECEIVING_CORE | _ALWAYS_CORE,
    "WR": _RECEIVING_CORE | _ALWAYS_CORE,
    "TE": _RECEIVING_CORE | _ALWAYS_CORE,
}
_CORE_ANY_POSITION = _RECEIVING_CORE | _RUSHING_CORE | _PASSING_CORE | _ALWAYS_CORE


def fantasy_relevant_stats(flat: dict, position: str | None = None) -> dict:
    """Drop the fields ESPN returns at zero for every player regardless of position.

    A receiver's game line comes back with QBRating, fieldGoals, kickExtraPoints,
    stuffs and twoPtRush all sitting at 0 — about ninety fields, of which a
    handful carry the game. Sending all of it to a model costs tokens against
    the daily budget and buries the six receptions that matter.

    Anything non-zero is kept whatever it is, so ESPN's less obvious columns —
    yards per route run, average depth of target — survive when they have a
    value.
    """
    core = _CORE_BY_POSITION.get((position or "").upper(), _CORE_ANY_POSITION)
    out = {}
    for name, value in (flat or {}).items():
        if name in core or (value not in (0, 0.0, None, "")):
            out[name] = value
    return out


def get_recent_game_logs(espn_athlete_id: str, limit: int = 3, position: str | None = None) -> dict:
    """Per-game stat lines for a player's most recent games, newest first.

    get_season_statistics gives season-to-date TOTALS, which cannot answer
    "what did he do last week" — the question that actually moves a dynasty
    read in-season. The eventlog carries one entry per game with a `played`
    flag and a $ref to that game's stat line; this dereferences the tail of
    it.

    Cost is one fetch for the log plus one per game returned, so `limit`
    stays small. Games the player missed are counted but not dereferenced —
    there is nothing to fetch and the absence is itself the signal.

    ESPN returns eventlog items oldest-first, so the most recent games are
    the tail. Results are re-sorted by date where a date comes back, so a
    change in ESPN's ordering degrades to "right games, maybe wrong order"
    rather than silently returning week 1 as the latest game.
    """
    try:
        log = get_event_log(espn_athlete_id)
    except Exception:  # noqa: BLE001 — no log is a legitimate "no data" case
        return {"games": [], "games_played": 0, "games_missed": 0, "available": False}

    items = (log.get("events") or {}).get("items") or []
    played = [i for i in items if i.get("played")]
    missed = len(items) - len(played)

    games = []
    for item in played[-max(1, limit):]:
        entry: dict = {}
        stats_ref = (item.get("statistics") or {}).get("$ref")
        if stats_ref:
            try:
                entry["stats"] = fantasy_relevant_stats(
                    flatten_statistics(get_injury_detail(stats_ref)), position
                )
            except Exception:  # noqa: BLE001 — skip a game rather than lose them all
                continue
        # Date and opponent are a nice-to-have: one extra fetch per game, and
        # a stat line without them is still worth returning.
        event_ref = (item.get("event") or {}).get("$ref")
        if event_ref:
            try:
                ev = get_injury_detail(event_ref)
                entry["date"] = ev.get("date")
                entry["game"] = ev.get("shortName") or ev.get("name")
                entry["week"] = _week_number(ev)
            except Exception:  # noqa: BLE001
                pass
        if entry:
            games.append(entry)

    games.sort(key=lambda g: g.get("date") or "", reverse=True)
    return {
        "games": games,
        "games_played": len(played),
        "games_missed": missed,
        "available": bool(games),
    }


def flatten_statistics(stats_response: dict) -> dict[str, float]:
    """Flatten ESPN's nested category/stat structure into {stat_name: value}."""
    flat: dict[str, float] = {}
    categories = stats_response.get("splits", {}).get("categories", [])
    for cat in categories:
        for stat in cat.get("stats", []):
            flat[stat["name"]] = stat.get("value")
    return flat


def _team_injury_refs(espn_team_id: str) -> list[str]:
    """All injury/status $refs for a team, paginated."""
    url = f"{BASE_URL}/teams/{espn_team_id}/injuries"
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    refs = [item["$ref"] for item in data.get("items", [])]
    for page in range(2, data.get("pageCount", 1) + 1):
        resp = httpx.get(url, params={"page": page}, timeout=15)
        resp.raise_for_status()
        refs.extend(item["$ref"] for item in resp.json().get("items", []))
    return refs


def get_injury_detail(ref_url: str) -> dict:
    resp = httpx.get(ref_url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_player_injury_notes(espn_athlete_id: str, espn_team_id: str) -> list[dict]:
    """Injury/status entries for one player, filtered from the team's feed.

    Filters refs by athlete id before dereferencing, so this costs one fetch
    per matching entry rather than one per player on the team.
    """
    refs = _team_injury_refs(espn_team_id)
    pattern = re.compile(rf"/athletes/{re.escape(espn_athlete_id)}/injuries/")
    matching = [ref for ref in refs if pattern.search(ref)]
    return [get_injury_detail(ref) for ref in matching]
