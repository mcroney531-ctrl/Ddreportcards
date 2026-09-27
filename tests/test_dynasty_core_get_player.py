"""
Stage 2B Batch 4: dynasty_core.sleeper.get_player is a lookup over the
cached full-player catalog (get_all_players), not a request to Sleeper's
single-player /players/nfl/{id} route -- that route exists but returns a
different shape from the full-map entry (verified in Batch 1), so shared
core doesn't take on a second provider contract for the same data.

Mocked only; no live Sleeper calls.
"""
import unittest
from unittest import mock

import dynasty_core.sleeper as sleeper

_CATALOG = {
    "4046": {"player_id": "4046", "full_name": "Patrick Mahomes", "position": "QB"},
    "12501": {"player_id": "12501", "full_name": "Test Player", "position": "WR"},
}


class GetPlayerContractTest(unittest.TestCase):
    def test_known_string_id_returns_exact_catalog_entry(self):
        with mock.patch.object(sleeper, "get_all_players", return_value=_CATALOG):
            self.assertIs(sleeper.get_player("4046"), _CATALOG["4046"])

    def test_integer_like_input_is_normalized_to_string(self):
        with mock.patch.object(sleeper, "get_all_players", return_value=_CATALOG):
            self.assertIs(sleeper.get_player(12501), _CATALOG["12501"])

    def test_missing_id_returns_none(self):
        with mock.patch.object(sleeper, "get_all_players", return_value=_CATALOG):
            self.assertIsNone(sleeper.get_player("does-not-exist"))

    def test_delegates_to_get_all_players_without_its_own_http(self):
        with mock.patch.object(sleeper, "get_all_players", return_value=_CATALOG) as all_players, \
             mock.patch.object(sleeper.httpx, "get") as http_get:
            sleeper.get_player("4046")
        all_players.assert_called_once_with()
        http_get.assert_not_called()


class GetPlayerUsesSharedCacheTest(unittest.TestCase):
    def setUp(self):
        self._orig = (sleeper._players_cache, sleeper._players_cache_time)
        sleeper._players_cache = None
        sleeper._players_cache_time = 0.0

    def tearDown(self):
        sleeper._players_cache, sleeper._players_cache_time = self._orig

    def test_repeated_lookups_on_warm_cache_make_no_extra_fetches(self):
        resp = mock.Mock()
        resp.json.return_value = _CATALOG
        resp.raise_for_status.return_value = None
        with mock.patch.object(sleeper.httpx, "get", return_value=resp) as http_get, \
             mock.patch.object(sleeper.time, "time", return_value=1000.0):
            sleeper.get_player("4046")
            sleeper.get_player("12501")
            sleeper.get_player("missing")
        http_get.assert_called_once()
        self.assertEqual(http_get.call_args.args[0], f"{sleeper.BASE_URL}/players/nfl")


if __name__ == "__main__":
    unittest.main()
