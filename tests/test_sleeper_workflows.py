"""
Stage 2C-3 (post-convergence audit F1): three Ddreportcards-only workflows
moved out of the shared package into data/sleeper_workflows.py before
extraction, so they never become versioned dynasty_core API:

  get_all_trades_all_seasons   cross-season trade collection + _season/_league_id annotation
  get_roster_by_display_name   owner resolution by display name (identity policy)
  resolve_roster_players       roster enrichment from the player catalog

Ddreportcards-only tests. They are not part of the shared package suite and
are not copied to scoutcap. Behavior tests mock the shared primitives. The
compatibility tests fake Sleeper and FantasyCalc at the network layer and
run real Ddreportcards callers end to end, because the facades bind names at
import time. No live calls.
"""
import importlib
import inspect
import unittest
from unittest import mock

import dynasty_core.fantasycalc as fc_core
import dynasty_core.sleeper as sleeper_core

MOVED = ("get_all_trades_all_seasons", "get_roster_by_display_name", "resolve_roster_players")


def _workflows():
    return importlib.import_module("data.sleeper_workflows")


class BoundaryTest(unittest.TestCase):
    def test_moved_names_are_gone_from_the_shared_package(self):
        for name in MOVED:
            with self.subTest(name):
                self.assertFalse(hasattr(sleeper_core, name))

    def test_workflows_module_defines_all_three(self):
        wf = _workflows()
        for name in MOVED:
            with self.subTest(name):
                fn = getattr(wf, name)
                self.assertEqual(fn.__module__, "data.sleeper_workflows")

    def test_sleeper_client_still_exposes_all_three_from_workflows(self):
        from data import sleeper_client
        wf = _workflows()
        for name in MOVED:
            with self.subTest(name):
                self.assertIs(getattr(sleeper_client, name), getattr(wf, name))

    def test_provider_primitives_stay_in_the_shared_package(self):
        for name in ("get_league_season_chain", "get_transactions", "get_all_trades", "get_all_players",
                     "get_league_users", "get_league_rosters"):
            with self.subTest(name):
                self.assertTrue(callable(getattr(sleeper_core, name)))

    def test_workflows_module_has_no_provider_http(self):
        source = inspect.getsource(_workflows())
        for forbidden in ("httpx", "api.sleeper.app", "https://", "requests"):
            self.assertNotIn(forbidden, source)

    def test_api_imports_roster_workflows_from_ddreportcards_not_the_package(self):
        import api
        wf = _workflows()
        self.assertIs(api.get_roster_by_display_name, wf.get_roster_by_display_name)
        self.assertIs(api.resolve_roster_players, wf.resolve_roster_players)


class AllTradesAllSeasonsTest(unittest.TestCase):
    CHAIN = [{"league_id": "L2026", "season": "2026"}, {"league_id": "L2025", "season": "2025"}]
    TRADES = {
        "L2026": [{"transaction_id": "a", "created": 300}, {"transaction_id": "b", "created": None}],
        "L2025": [{"transaction_id": "c", "created": 500}, {"transaction_id": "d"},
                  {"transaction_id": "e", "created": 0}, {"transaction_id": "f", "created": 100}],
    }

    def _run(self):
        with mock.patch.object(sleeper_core, "get_league_season_chain", return_value=self.CHAIN) as chain, \
             mock.patch.object(sleeper_core, "get_all_trades",
                               side_effect=lambda lid: [dict(t) for t in self.TRADES[lid]]) as all_trades:
            result = _workflows().get_all_trades_all_seasons("L2026")
        return result, chain, all_trades

    def test_walks_chain_and_fetches_each_season(self):
        _, chain, all_trades = self._run()
        chain.assert_called_once_with("L2026")
        self.assertEqual([c.args for c in all_trades.call_args_list], [("L2026",), ("L2025",)])

    def test_annotates_and_combines(self):
        result, _, _ = self._run()
        by_id = {t["transaction_id"]: t for t in result}
        self.assertEqual(set(by_id), set("abcdef"))
        self.assertEqual((by_id["a"]["_season"], by_id["a"]["_league_id"]), ("2026", "L2026"))
        self.assertEqual((by_id["c"]["_season"], by_id["c"]["_league_id"]), ("2025", "L2025"))

    def test_sorts_newest_first_with_falsey_created_as_zero_in_stable_order(self):
        result, _, _ = self._run()
        # created 500, 300, 100, then the falsey ones (None, missing, 0) as 0,
        # keeping chain/insertion order among themselves (stable sort): b, d, e.
        self.assertEqual([t["transaction_id"] for t in result], ["c", "a", "f", "b", "d", "e"])


class RosterByDisplayNameTest(unittest.TestCase):
    USERS = [{"user_id": "u1", "display_name": "TitansTrev55"}, {"user_id": "u2", "display_name": "Other"}]
    ROSTERS = [{"roster_id": 1, "owner_id": "u2", "players": []},
               {"roster_id": 7, "owner_id": "u1", "players": ["100"]}]

    def _patches(self, users=None, rosters=None):
        return (mock.patch.object(sleeper_core, "get_league_users", return_value=self.USERS if users is None else users),
                mock.patch.object(sleeper_core, "get_league_rosters", return_value=self.ROSTERS if rosters is None else rosters))

    def test_resolves_owner_roster_by_exact_display_name(self):
        pu, pr = self._patches()
        with pu as users, pr as rosters:
            roster = _workflows().get_roster_by_display_name("L1", "TitansTrev55")
        self.assertIs(roster, self.ROSTERS[1])
        users.assert_called_once_with("L1")
        rosters.assert_called_once_with("L1")

    def test_display_name_match_is_exact(self):
        pu, pr = self._patches()
        with pu, pr:
            with self.assertRaises(ValueError) as ctx:
                _workflows().get_roster_by_display_name("L1", "titanstrev55")
        self.assertEqual(str(ctx.exception), "No league user found with display_name='titanstrev55'")

    def test_user_without_roster_raises_same_error(self):
        pu, pr = self._patches(rosters=[{"roster_id": 1, "owner_id": "u2"}])
        with pu, pr:
            with self.assertRaises(ValueError) as ctx:
                _workflows().get_roster_by_display_name("L1", "TitansTrev55")
        self.assertEqual(str(ctx.exception), "No roster found for user_id='u1'")


class ResolveRosterPlayersTest(unittest.TestCase):
    CATALOG = {"100": {"full_name": "Known Player", "position": "WR"}}

    def _resolve(self, roster):
        with mock.patch.object(sleeper_core, "get_all_players", return_value=self.CATALOG):
            return _workflows().resolve_roster_players(roster)

    def test_attaches_metadata_and_keeps_player_id(self):
        self.assertEqual(self._resolve({"players": ["100"]}),
                         [{"player_id": "100", "full_name": "Known Player", "position": "WR"}])

    def test_unknown_id_still_returns_its_player_id(self):
        self.assertEqual(self._resolve({"players": ["999"]}), [{"player_id": "999"}])

    def test_missing_or_empty_players_is_empty_list(self):
        self.assertEqual(self._resolve({}), [])
        self.assertEqual(self._resolve({"players": []}), [])


class _FakeNetwork(unittest.TestCase):
    """Routes httpx.get by URL for Sleeper + FantasyCalc and resets both
    shared caches, so real Ddreportcards callers can run end to end."""

    BASE = sleeper_core.BASE_URL
    CATALOG = {"100": {"full_name": "Known WR", "position": "WR", "team": "KC", "years_exp": 3},
               "200": {"full_name": "Some K", "position": "K", "team": "KC"}}

    def setUp(self):
        self._saved = (sleeper_core._players_cache, sleeper_core._players_cache_time,
                       dict(fc_core._values_cache), fc_core._index_cache)
        sleeper_core._players_cache, sleeper_core._players_cache_time = None, 0.0
        fc_core._values_cache.clear()
        fc_core._index_cache = None
        routes = {
            f"{self.BASE}/league/L1": {"season": "2026", "previous_league_id": "L0"},
            f"{self.BASE}/league/L0": {"season": "2025", "previous_league_id": None},
            f"{self.BASE}/league/L1/users": [{"user_id": "u1", "display_name": "Owner26"}],
            f"{self.BASE}/league/L0/users": [{"user_id": "u1", "display_name": "Owner25"}],
            f"{self.BASE}/league/L1/rosters": [{"roster_id": 1, "owner_id": "u1", "players": ["100", "200"]}],
            f"{self.BASE}/league/L0/rosters": [{"roster_id": 1, "owner_id": "u1", "players": ["100"]}],
            f"{self.BASE}/players/nfl": self.CATALOG,
        }
        trades = {
            ("L1", 1): [{"transaction_id": "t26", "type": "trade", "status": "complete", "created": 2000,
                         "roster_ids": [1]}],
            ("L0", 3): [{"transaction_id": "t25", "type": "trade", "status": "complete", "created": 1000,
                         "roster_ids": [1]}],
        }

        def fake_get(url, *args, **kwargs):
            resp = mock.Mock(status_code=200)
            resp.raise_for_status.return_value = None
            if "fantasycalc" in url:
                resp.json.return_value = []
            elif "/transactions/" in url:
                lid = url.split("/league/")[1].split("/")[0]
                resp.json.return_value = trades.get((lid, int(url.rsplit("/", 1)[1])), [])
            elif url in routes:
                resp.json.return_value = routes[url]
            else:
                raise AssertionError(f"unexpected URL {url}")
            return resp

        patcher = mock.patch.object(sleeper_core.httpx, "get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        (sleeper_core._players_cache, sleeper_core._players_cache_time, values, fc_core._index_cache) = self._saved
        fc_core._values_cache.clear()
        fc_core._values_cache.update(values)


class DdreportcardsCallersStillWorkTest(_FakeNetwork):
    def test_api_roster_resolution(self):
        import api
        with mock.patch.object(api, "LEAGUE_ID", "L1"):
            result = api.roster_data("Owner26")
        self.assertEqual([p["player_id"] for p in result["players"]], ["100"])  # K filtered out
        self.assertEqual(result["players"][0]["name"], "Known WR")

    def test_api_roster_unknown_owner_is_404_with_same_detail(self):
        import api
        from fastapi import HTTPException
        with mock.patch.object(api, "LEAGUE_ID", "L1"):
            with self.assertRaises(HTTPException) as ctx:
                api.roster_data("Nobody")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "No league user found with display_name='Nobody'")

    def test_sleeper_client_workflow_access(self):
        from data import sleeper_client
        roster = sleeper_client.get_roster_by_display_name("L1", "Owner26")
        self.assertEqual([p["player_id"] for p in sleeper_client.resolve_roster_players(roster)], ["100", "200"])

    def test_trade_history_uses_moved_all_season_workflow(self):
        from data import trade_history
        trades = trade_history.get_trade_history("L1")
        # Newest first across both seasons; _season and _league_id both survive
        # the move (the latter selects each season's roster->team-name map).
        self.assertEqual([(t["transaction_id"], t["season"]) for t in trades], [("t26", "2026"), ("t25", "2025")])
        self.assertEqual([t["sides"][0]["team"] for t in trades], ["Owner26", "Owner25"])


if __name__ == "__main__":
    unittest.main()
