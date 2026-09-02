"""League-wide draft pick inventory.

Nothing in this build knew a draft pick existed. The assistant could not say
how many picks the user held, who held his, or whose pick it was that a
trade partner was offering — so when picks came up it either went silent or
made something up ("a 2029 2nd is basically worthless" and "your future 2nds
are gold", in the same conversation).

The inventory is a reconstruction, not a fetch. Sleeper records only the
picks that have MOVED; a team's untouched picks appear in no endpoint at
all. So the full picture is "every team owns its own pick in every round of
every tradeable season", with the traded_picks list applied on top as
overrides.

Values come from FantasyCalc's own pick entries — the same payload the
player values already come from, so there is no second source of truth to
reconcile. They are generic round values: a "2029 2nd" is priced as a
league-average 2029 2nd. Which team's 2029 2nd it actually is often matters
more than the round, so every pick carries its origin team and how that
team looks right now. That much is sourced. Projecting where a team will
finish three seasons out is not, and nothing here pretends to.
"""

from __future__ import annotations

from dynasty_core.fantasycalc import get_dynasty_values, index_picks_by_label
from dynasty_core.sleeper import (
    get_league_drafts,
    get_league_info,
    get_league_rosters,
    get_league_users,
    get_traded_picks,
)

# Sleeper allows trading picks three drafts out.
_SEASONS_AHEAD = 3
# Fallback only — the real number comes from the league's draft settings.
_DEFAULT_ROUNDS = 4
_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def pick_label(season: str | int, round_num: int) -> str:
    return f"{season} {_ORDINALS.get(round_num, f'{round_num}th')}"


def _draft_rounds(league_id: str) -> int:
    try:
        drafts = get_league_drafts(league_id) or []
        for d in drafts:
            rounds = (d.get("settings") or {}).get("rounds")
            if rounds:
                return int(rounds)
    except Exception:  # noqa: BLE001 — a missing draft object must not sink the inventory
        pass
    return _DEFAULT_ROUNDS


def _future_seasons(league_id: str, traded: list[dict]) -> list[str]:
    """The draft seasons still tradeable.

    Anchored on the league's current season rather than the calendar: during
    the 2026 season the 2026 rookie draft has already happened, so the next
    three are 2027-2029. Any season that shows up in traded_picks is unioned
    in, so an unusual league setting cannot silently drop picks.
    """
    seasons: set[str] = set()
    try:
        current = int(get_league_info(league_id).get("season"))
        seasons.update(str(current + n) for n in range(1, _SEASONS_AHEAD + 1))
    except Exception:  # noqa: BLE001
        pass
    seasons.update(str(t["season"]) for t in traded if t.get("season"))
    return sorted(seasons)


def _standings(rosters: list[dict]) -> dict[int, dict]:
    """Current record and league position, by roster_id.

    Ordered by wins then points for — the same reverse-standings order this
    league drafts in, so "1st of 12" here is also "last pick" there.
    """
    ranked = sorted(
        rosters,
        key=lambda r: (
            (r.get("settings") or {}).get("wins", 0),
            (r.get("settings") or {}).get("fpts", 0),
        ),
        reverse=True,
    )
    out = {}
    for i, r in enumerate(ranked, start=1):
        st = r.get("settings") or {}
        out[r["roster_id"]] = {
            "standing": i,
            "of": len(ranked),
            "wins": st.get("wins", 0),
            "losses": st.get("losses", 0),
            "ties": st.get("ties", 0),
        }
    return out


def build_pick_inventory(league_id: str) -> dict:
    """Who holds which future picks, priced and attributed."""
    rosters = get_league_rosters(league_id)
    users = get_league_users(league_id)
    traded = get_traded_picks(league_id) or []
    pick_values = index_picks_by_label(get_dynasty_values())

    owner_name = {u["user_id"]: u.get("display_name") for u in users}
    team_name = {
        r["roster_id"]: owner_name.get(r.get("owner_id")) or f"Roster {r['roster_id']}"
        for r in rosters
    }
    standings = _standings(rosters)
    rounds = _draft_rounds(league_id)
    seasons = _future_seasons(league_id, traded)

    # {(season, round, origin_roster_id): current_holder_roster_id}
    holder = {
        (str(season), rnd, rid): rid
        for season in seasons
        for rnd in range(1, rounds + 1)
        for rid in team_name
    }
    unknown_seasons = []
    for t in traded:
        key = (str(t.get("season")), t.get("round"), t.get("roster_id"))
        if key in holder:
            holder[key] = t.get("owner_id")
        elif key[0] not in seasons:
            unknown_seasons.append(key[0])

    by_team: dict[int, list[dict]] = {rid: [] for rid in team_name}
    for (season, rnd, origin), held_by in holder.items():
        if held_by not in by_team:
            continue
        label = pick_label(season, rnd)
        fc = pick_values.get(label)
        origin_standing = standings.get(origin, {})
        by_team[held_by].append({
            "label": label,
            "season": season,
            "round": rnd,
            "origin_roster_id": origin,
            "origin_team": team_name.get(origin),
            "is_own": origin == held_by,
            # None, not 0 — an unpriced pick is unknown, not worthless, and
            # the difference is the whole point.
            "dynasty_value": fc["value"] if fc else None,
            "origin_team_standing": origin_standing.get("standing"),
            "origin_team_record": (
                f"{origin_standing.get('wins')}-{origin_standing.get('losses')}"
                if origin_standing else None
            ),
        })

    teams = []
    for rid, picks in by_team.items():
        picks.sort(key=lambda p: (p["season"], p["round"], p["origin_team"] or ""))
        priced = [p["dynasty_value"] for p in picks if p["dynasty_value"] is not None]
        teams.append({
            "roster_id": rid,
            "owner": team_name.get(rid),
            "pick_count": len(picks),
            "own_picks": sum(1 for p in picks if p["is_own"]),
            "picks_from_others": sum(1 for p in picks if not p["is_own"]),
            "total_pick_value": sum(priced) if priced else None,
            "unpriced_picks": len(picks) - len(priced),
            "standing": standings.get(rid, {}).get("standing"),
            "record": (
                f"{standings[rid]['wins']}-{standings[rid]['losses']}"
                if rid in standings else None
            ),
            "picks": picks,
        })
    teams.sort(key=lambda t: t["total_pick_value"] or 0, reverse=True)

    notes = [
        "Pick values are FantasyCalc's generic round values — a league-average "
        f"{seasons[0] if seasons else 'future'} 2nd, not this specific one. Whose pick it is "
        "usually matters more than the round: each pick carries its origin team and that "
        "team's current standing.",
        "This league drafts in reverse standings order, so an origin team sitting last is "
        "an early pick and one sitting first is a late one — for the NEXT draft. Standings "
        "two or three seasons out are not knowable and are not projected here.",
    ]
    if any(p["dynasty_value"] is None for t in teams for p in t["picks"]):
        notes.append(
            "Some picks have no FantasyCalc value (it prices rounds 1-4 and only a few "
            "seasons out). Those show dynasty_value null — unknown, not zero."
        )
    if unknown_seasons:
        notes.append(
            f"Traded picks exist for season(s) {sorted(set(unknown_seasons))} outside the "
            "tradeable window this built; they are not in the inventory."
        )

    return {
        "league_id": league_id,
        "seasons": seasons,
        "rounds_per_draft": rounds,
        "draft_order": "reverse standings, no lottery",
        "teams": teams,
        "notes": notes,
    }


def picks_for_owner(league_id: str, owner: str) -> dict:
    """One team's pick inventory, matched on Sleeper display name."""
    inv = build_pick_inventory(league_id)
    target = (owner or "").strip().lower()
    for team in inv["teams"]:
        if (team["owner"] or "").lower() == target:
            return {
                "owner": team["owner"],
                "seasons": inv["seasons"],
                "rounds_per_draft": inv["rounds_per_draft"],
                "draft_order": inv["draft_order"],
                **{k: v for k, v in team.items() if k != "owner"},
                "notes": inv["notes"],
            }
    known = [t["owner"] for t in inv["teams"]]
    return {"error": f"No team with owner {owner!r}. Owners in this league: {known}"}
