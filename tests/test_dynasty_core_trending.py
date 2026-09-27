"""
Stage 2B Batch 5: dynasty_core.sleeper.get_trending is the generalized
trending primitive; get_trending_adds stays as a compatibility wrapper with
its original signature and no HTTP of its own.

get_trending's parameter order (type, sport, limit, lookback_hours) keeps
scoutcap tools.sleeper.get_trending's positional shape, with lookback_hours
appended, so the Batch 6 facade swap is safe for positional callers too.

Mocked network only; no live Sleeper calls.
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
            "error", request=mock.Mock(), response=mock.Mock(status_code=404)
        )
    return resp


class GetTrendingTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([])) as m:
            sleeper.get_trending()
        m.assert_called_once_with(
            f"{sleeper.BASE_URL}/players/nfl/trending/add",
            params={"lookback_hours": 24, "limit": 25},
            timeout=15,
        )

    def test_custom_call_forwards_all_four_values(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([])) as m:
            sleeper.get_trending(type="drop", sport="nfl", limit=100, lookback_hours=6)
        m.assert_called_once_with(
            f"{sleeper.BASE_URL}/players/nfl/trending/drop",
            params={"lookback_hours": 6, "limit": 100},
            timeout=15,
        )

    def test_positional_order_matches_scout_surface(self):
        # scoutcap tools.sleeper.get_trending(type, sport, limit) -- a
        # positional third argument must still mean limit.
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([])) as m:
            sleeper.get_trending("drop", "nfl", 50)
        self.assertEqual(m.call_args.kwargs["params"], {"lookback_hours": 24, "limit": 50})
        self.assertTrue(m.call_args.args[0].endswith("/players/nfl/trending/drop"))

    def test_response_json_returned_unchanged(self):
        payload = [{"player_id": "4046", "count": 312}, {"player_id": "12501", "count": 7}]
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(payload)):
            self.assertIs(sleeper.get_trending(), payload)

    def test_non_2xx_propagates(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([], status_ok=False)):
            with self.assertRaises(httpx.HTTPStatusError):
                sleeper.get_trending(type="bogus")


class GetTrendingAddsWrapperTest(unittest.TestCase):
    def test_signature_unchanged(self):
        import inspect
        params = inspect.signature(sleeper.get_trending_adds).parameters
        self.assertEqual(list(params), ["lookback_hours", "limit"])
        self.assertEqual(params["lookback_hours"].default, 24)
        self.assertEqual(params["limit"].default, 25)

    def test_default_delegates_with_add_and_nfl(self):
        with mock.patch.object(sleeper, "get_trending", return_value=[]) as gt:
            sleeper.get_trending_adds()
        gt.assert_called_once_with(type="add", sport="nfl", limit=25, lookback_hours=24)

    def test_custom_values_forwarded(self):
        with mock.patch.object(sleeper, "get_trending", return_value=[]) as gt:
            sleeper.get_trending_adds(lookback_hours=6, limit=100)
        gt.assert_called_once_with(type="add", sport="nfl", limit=100, lookback_hours=6)

    def test_wrapper_returns_delegate_result(self):
        payload = [{"player_id": "4046", "count": 312}]
        with mock.patch.object(sleeper, "get_trending", return_value=payload):
            self.assertIs(sleeper.get_trending_adds(), payload)

    def test_wrapper_issues_exactly_one_http_request(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([])) as m:
            sleeper.get_trending_adds(lookback_hours=12, limit=40)
        m.assert_called_once_with(
            f"{sleeper.BASE_URL}/players/nfl/trending/add",
            params={"lookback_hours": 12, "limit": 40},
            timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
