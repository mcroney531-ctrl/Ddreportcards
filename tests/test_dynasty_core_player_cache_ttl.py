"""
Stage 2B Batch 3: dynasty_core.sleeper.get_all_players()'s process-local
cache TTL moved from 6h to 24h.

Sleeper's own docs for GET /players/nfl say the full player map should be
fetched sparingly -- at most about once a day -- and stored client-side
rather than re-fetched per lookup. The architecture this locks in:
get_all_players() is a player catalog / identity map, not a live-status
feed. Consumers needing sub-24h freshness (injury_status,
practice_participation, depth-chart fields) need a different, filtered
primitive -- not a shorter catalog TTL. No such primitive is introduced
in this batch.

These tests use mocked time and network only; no live Sleeper calls.
"""
import unittest
from unittest import mock

import httpx

import dynasty_core.sleeper as sleeper


def _fake_response(payload, status_ok=True):
    resp = mock.Mock()
    resp.json.return_value = payload
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=mock.Mock(), response=mock.Mock(status_code=500)
        )
    return resp


class PlayerCacheTTLTest(unittest.TestCase):
    def setUp(self):
        self._orig_cache = sleeper._players_cache
        self._orig_cache_time = sleeper._players_cache_time
        sleeper._players_cache = None
        sleeper._players_cache_time = 0.0

    def tearDown(self):
        sleeper._players_cache = self._orig_cache
        sleeper._players_cache_time = self._orig_cache_time

    def test_ttl_constant_is_24_hours(self):
        self.assertEqual(sleeper._PLAYERS_TTL_SECONDS, 24 * 60 * 60)

    def test_first_call_fetches_players_endpoint(self):
        payload = {"1": {"full_name": "A"}}
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(payload)) as m, \
             mock.patch.object(sleeper.time, "time", return_value=1000.0):
            result = sleeper.get_all_players()
        m.assert_called_once()
        self.assertEqual(m.call_args.args[0], f"{sleeper.BASE_URL}/players/nfl")
        self.assertEqual(result, payload)

    def test_repeated_calls_inside_24h_do_not_refetch(self):
        payload = {"1": {"full_name": "A"}}
        # second call at +23h -- still inside the 24h TTL
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(payload)) as m, \
             mock.patch.object(sleeper.time, "time", side_effect=[1000.0, 1000.0 + 23 * 60 * 60]):
            first = sleeper.get_all_players()
            second = sleeper.get_all_players()
        m.assert_called_once()
        self.assertIs(first, second)

    def test_call_after_24h_ttl_performs_new_fetch(self):
        payload1 = {"1": {"full_name": "A"}}
        payload2 = {"1": {"full_name": "A-updated"}}
        # second call at +24h+1s -- past the TTL
        with mock.patch.object(
            sleeper.httpx, "get", side_effect=[_fake_response(payload1), _fake_response(payload2)]
        ) as m, mock.patch.object(
            sleeper.time, "time", side_effect=[1000.0, 1000.0 + 24 * 60 * 60 + 1]
        ):
            first = sleeper.get_all_players()
            second = sleeper.get_all_players()
        self.assertEqual(m.call_count, 2)
        self.assertEqual(first, payload1)
        self.assertEqual(second, payload2)

    def test_cache_timestamp_updates_only_after_successful_fetch(self):
        payload = {"1": {"full_name": "A"}}
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(payload)), \
             mock.patch.object(sleeper.time, "time", return_value=5000.0):
            sleeper.get_all_players()
        self.assertEqual(sleeper._players_cache_time, 5000.0)

    def test_failed_refresh_does_not_replace_previous_good_cache(self):
        good_payload = {"1": {"full_name": "A"}}
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(good_payload)), \
             mock.patch.object(sleeper.time, "time", return_value=1000.0):
            sleeper.get_all_players()

        # Past the TTL, so a refresh is attempted -- and fails.
        with mock.patch.object(
            sleeper.httpx, "get", return_value=_fake_response({}, status_ok=False)
        ), mock.patch.object(sleeper.time, "time", return_value=1000.0 + 24 * 60 * 60 + 1):
            with self.assertRaises(httpx.HTTPStatusError):
                sleeper.get_all_players()

        # The failed refresh must not clear, null out, or corrupt the last
        # good cache -- neither the payload nor its timestamp changes.
        self.assertEqual(sleeper._players_cache, good_payload)
        self.assertEqual(sleeper._players_cache_time, 1000.0)


if __name__ == "__main__":
    unittest.main()
