"""
Trade history — normalizes raw Sleeper trade transactions across this
dynasty league's full season chain (walking previous_league_id back to
2022) into a clean structure, with each trade side priced against
FantasyCalc's current values for both players and draft picks.

This is an "internal market" signal, not a replacement for FantasyCalc:
it shows what THIS league's owners actually paid for a player, which can
diverge from broader consensus (a league that's historically bullish or
bearish on a player/position relative to the outside market).

Retroactive-pricing caveat: FantasyCalc has no historical snapshots, so
even a 2023 trade is priced with TODAY's values. This approximates
relative value at trade time, not a literal record of what things were
worth back then.

Pick-value caveat: Sleeper's trade data only gives {round, season} for
traded picks, not an exact draft slot (unknown until the draft order is
set) — so every pick is priced against FantasyCalc's generic round-only
value (e.g. "2027 2nd"), not a specific slot like "2027 2.05".
"""
from __future__ import annotations

import datetime
import re

from data import sleeper_client, fantasycalc_client

ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _pick_label(round_num: int, season: str) -> str:
    return f"{season} {ORDINALS.get(round_num, f'{round_num}th')}"


def _fc_generic_pick_index(fc_values: list[dict]) -> dict[str, dict]:
    """FantasyCalc pick entries keyed by their generic round-only label."""
    index = {}
    for e in fc_values:
        name = e["player"].get("name", "")
        if e["player"].get("position") == "PICK" and re.match(r"^\d{4} (1st|2nd|3rd|4th)$", name):
            index[name] = e
    return index


def _asset_for_player(player_id: str, all_players: dict, fc_by_sleeper: dict) -> dict:
    p = all_players.get(player_id, {})
    fc = fc_by_sleeper.get(player_id)
    return {
        "type": "player",
        "player_id": player_id,
        "name": p.get("full_name") or f"Unknown player ({player_id})",
        "position": p.get("position"),
        "team": p.get("team"),
        "value": fc["value"] if fc else 0,
        "in_fc_pool": fc is not None,
    }


def _asset_for_pick(round_num: int, season: str, fc_picks_by_label: dict) -> dict:
    label = _pick_label(round_num, season)
    fc = fc_picks_by_label.get(label)
    return {
        "type": "pick",
        "label": label,
        "round": round_num,
        "season": season,
        "value": fc["value"] if fc else 0,
        "in_fc_pool": fc is not None,
    }


def _normalize_one_trade(
    raw_trade: dict, roster_names: dict[int, str], all_players: dict, fc_by_sleeper: dict, fc_picks_by_label: dict
) -> dict:
    adds = raw_trade.get("adds") or {}
    drops = raw_trade.get("drops") or {}
    draft_picks = raw_trade.get("draft_picks") or []
    roster_ids = raw_trade.get("roster_ids") or []

    sides = []
    for rid in roster_ids:
        gave, received = [], []

        for pid, to_rid in adds.items():
            from_rid = drops.get(pid)
            if from_rid is None:
                continue
            if to_rid == rid:
                received.append(_asset_for_player(pid, all_players, fc_by_sleeper))
            elif from_rid == rid:
                gave.append(_asset_for_player(pid, all_players, fc_by_sleeper))

        for pick in draft_picks:
            asset = _asset_for_pick(pick["round"], pick["season"], fc_picks_by_label)
            if pick.get("owner_id") == rid:
                received.append(asset)
            elif pick.get("previous_owner_id") == rid:
                gave.append(asset)

        sides.append({
            "roster_id": rid,
            "team": roster_names.get(rid, f"Roster {rid}"),
            "gave": gave,
            "received": received,
            "gave_value": sum(a["value"] for a in gave),
            "received_value": sum(a["value"] for a in received),
        })

    created = raw_trade.get("created")
    return {
        "transaction_id": raw_trade["transaction_id"],
        "season": raw_trade.get("_season"),
        "date": datetime.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %I:%M %p") if created else None,
        "created": created,
        "sides": sides,
    }


def get_trade_history(league_id: str) -> list[dict]:
    """
    Every completed trade across this league's full season history, normalized
    and priced against FantasyCalc's current values. Sorted newest first.
    """
    raw_trades = sleeper_client.get_all_trades_all_seasons(league_id)
    fc_values = fantasycalc_client.get_dynasty_values()
    fc_by_sleeper = fantasycalc_client.index_by_sleeper_id(fc_values)
    fc_picks_by_label = _fc_generic_pick_index(fc_values)
    all_players = sleeper_client.get_all_players()

    # Roster -> team name mapping differs per season (owners can change),
    # so build one lookup per season's league_id, not just the current one.
    roster_names_by_season_league: dict[str, dict[int, str]] = {}
    for season_info in sleeper_client.get_league_season_chain(league_id):
        lid = season_info["league_id"]
        users = sleeper_client.get_league_users(lid)
        rosters = sleeper_client.get_league_rosters(lid)
        uid_to_name = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u.get("display_name") for u in users}
        roster_names_by_season_league[lid] = {
            r["roster_id"]: uid_to_name.get(r.get("owner_id"), f"Roster {r['roster_id']}") for r in rosters
        }

    trades = []
    for raw in raw_trades:
        roster_names = roster_names_by_season_league.get(raw["_league_id"], {})
        trades.append(_normalize_one_trade(raw, roster_names, all_players, fc_by_sleeper, fc_picks_by_label))

    trades.sort(key=lambda t: t.get("created") or 0, reverse=True)
    return trades


def summarize_player_trade_activity(trades: list[dict]) -> dict[str, dict]:
    """
    Per-player trade history: every occurrence of a player being traded,
    with the side ratio (this side's total value / the other side's total
    value) as an "internal market" context signal — not a literal implied
    value for the player alone, since trades often bundle multiple assets.
    Keyed by player_id.
    """
    activity: dict[str, dict] = {}

    for trade in trades:
        sides = trade["sides"]
        if len(sides) != 2:
            continue  # skip 3+-team trades for this simpler pairwise ratio
        side_a, side_b = sides
        ratio_a_perspective = (side_a["received_value"] / side_a["gave_value"]) if side_a["gave_value"] else None
        ratio_b_perspective = (side_b["received_value"] / side_b["gave_value"]) if side_b["gave_value"] else None

        for side, other_side, ratio in ((side_a, side_b, ratio_a_perspective), (side_b, side_a, ratio_b_perspective)):
            for asset in side["gave"]:
                if asset["type"] != "player":
                    continue
                entry = activity.setdefault(asset["player_id"], {"name": asset["name"], "position": asset["position"], "occurrences": []})
                entry["occurrences"].append({
                    "date": trade["date"],
                    "season": trade["season"],
                    "traded_from": side["team"],
                    "traded_to": other_side["team"],
                    "side_value_ratio": round(ratio, 2) if ratio is not None else None,
                })

    for entry in activity.values():
        entry["times_traded"] = len(entry["occurrences"])

    return activity
