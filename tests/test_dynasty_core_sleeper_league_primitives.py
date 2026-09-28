"""
Stage 2C-1: behavioral baseline for the dynasty_core.sleeper league
primitives that survive extraction and had no package-level tests.

Not covered here on purpose: get_all_trades_all_seasons,
get_roster_by_display_name and resolve_roster_players. They are
Ddreportcards-only workflows that move out of the package in 2C-3
(post-convergence audit, F1), so freezing them into the package's test
contract would work against that move.

Mocked network only; no live Sleeper calls.
"""
import unittest
from unittest import mock

import httpx

import dynasty_core.sleeper as sleeper

BASE = sleeper.BASE_URL


def _resp(payload=None, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=mock.Mock(), response=mock.Mock(status_code=status)
        )
    else:
        r.raise_for_status.return_value = None
    return r


class SimpleLeaguePrimitivesTest(unittest.TestCase):
    CASES = [
        ("get_league_users", lambda: sleeper.get_league_users("L1"), f"{BASE}/league/L1/users"),
        ("get_league_rosters", lambda: sleeper.get_league_rosters("L1"), f"{BASE}/league/L1/rosters"),
        ("get_league_info", lambda: sleeper.get_league_info("L1"), f"{BASE}/league/L1"),
        ("get_traded_picks", lambda: sleeper.get_traded_picks("L1"), f"{BASE}/league/L1/traded_picks"),
        ("get_league_drafts", lambda: sleeper.get_league_drafts("L1"), f"{BASE}/league/L1/drafts"),
        ("get_transactions", lambda: sleeper.get_transactions("L1", 7), f"{BASE}/league/L1/transactions/7"),
    ]

    def test_url_timeout_and_json_passthrough(self):
        for name, call, url in self.CASES:
            payload = [{"from": name}]
            with self.subTest(name), mock.patch.object(sleeper.httpx, "get", return_value=_resp(payload)) as m:
                self.assertIs(call(), payload)
                m.assert_called_once_with(url, timeout=15)

    def test_non_2xx_propagates(self):
        for name, call, _ in self.CASES:
            with self.subTest(name), mock.patch.object(sleeper.httpx, "get", return_value=_resp(None, 404)):
                with self.assertRaises(httpx.HTTPStatusError):
                    call()


class LeagueSeasonChainTest(unittest.TestCase):
    def _info(self, table):
        return lambda league_id: table[league_id]

    def test_walks_previous_league_id_newest_first(self):
        table = {
            "L2026": {"season": "2026", "previous_league_id": "L2025"},
            "L2025": {"season": "2025", "previous_league_id": "L2024"},
            "L2024": {"season": "2024", "previous_league_id": None},
        }
        with mock.patch.object(sleeper, "get_league_info", side_effect=self._info(table)) as info:
            chain = sleeper.get_league_season_chain("L2026")
        self.assertEqual(chain, [
            {"league_id": "L2026", "season": "2026"},
            {"league_id": "L2025", "season": "2025"},
            {"league_id": "L2024", "season": "2024"},
        ])
        self.assertEqual(info.call_count, 3)

    def test_stops_on_empty_string_previous_id(self):
        table = {"L1": {"season": "2026", "previous_league_id": ""}}
        with mock.patch.object(sleeper, "get_league_info", side_effect=self._info(table)):
            self.assertEqual(sleeper.get_league_season_chain("L1"), [{"league_id": "L1", "season": "2026"}])

    def test_cycle_is_not_followed_forever(self):
        table = {"A": {"season": "2026", "previous_league_id": "B"},
                 "B": {"season": "2025", "previous_league_id": "A"}}
        with mock.patch.object(sleeper, "get_league_info", side_effect=self._info(table)) as info:
            chain = sleeper.get_league_season_chain("A")
        self.assertEqual([c["league_id"] for c in chain], ["A", "B"])
        self.assertEqual(info.call_count, 2)


class GetAllTradesTest(unittest.TestCase):
    def test_scans_weeks_1_to_18_and_keeps_completed_trades_deduped(self):
        by_week = {
            1: [{"transaction_id": "t1", "type": "trade", "status": "complete", "week": 1},
                {"transaction_id": "w1", "type": "waiver", "status": "complete"}],
            2: [{"transaction_id": "t1", "type": "trade", "status": "complete", "week": 2},  # duplicate id
                {"transaction_id": "t2", "type": "trade", "status": "failed"}],
            18: [{"transaction_id": "t3", "type": "trade", "status": "complete"}],
        }
        with mock.patch.object(sleeper, "get_transactions",
                               side_effect=lambda lid, week: by_week.get(week, [])) as txns:
            trades = sleeper.get_all_trades("L1")
        self.assertEqual([c.args for c in txns.call_args_list], [("L1", w) for w in range(1, 19)])
        self.assertEqual(sorted(t["transaction_id"] for t in trades), ["t1", "t3"])
        # Dedupe keeps the last-seen record for a repeated transaction_id.
        self.assertEqual(next(t for t in trades if t["transaction_id"] == "t1")["week"], 2)

    def test_no_trades_is_empty_list(self):
        with mock.patch.object(sleeper, "get_transactions", return_value=[]):
            self.assertEqual(sleeper.get_all_trades("L1"), [])


if __name__ == "__main__":
    unittest.main()
