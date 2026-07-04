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
