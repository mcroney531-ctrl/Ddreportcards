"""
Stage 2B Batch 2: dynasty_core.sleeper.get_user / get_leagues are new,
additive shared-core primitives -- no existing Scout caller is migrated to
them yet (scoutcap/tools/sleeper.py keeps its own implementation until the
facade-conversion batch).

One thing these primitives deliberately do NOT copy from Scout's old
tools.sleeper.get_leagues(): a hardcoded `season: str = "2025"` default.
A provider primitive shouldn't freeze a calendar-sensitive application
default into shared infrastructure, so dynasty_core.sleeper.get_leagues
requires season explicitly -- these tests assert that too.

Mocked network only; no live HTTP calls.
"""
import inspect
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


class GetUserTest(unittest.TestCase):
    def test_correct_url(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response({})) as m:
            sleeper.get_user("someuser")
        m.assert_called_once()
        called_url = m.call_args.args[0]
        self.assertEqual(called_url, f"{sleeper.BASE_URL}/user/someuser")

    def test_timeout_supplied(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response({})) as m:
            sleeper.get_user("someuser")
        self.assertIn("timeout", m.call_args.kwargs)
        self.assertIsNotNone(m.call_args.kwargs["timeout"])

    def test_decoded_object_returned(self):
        payload = {"user_id": "123", "username": "someuser"}
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(payload)):
            result = sleeper.get_user("someuser")
        self.assertEqual(result, payload)

    def test_non_2xx_propagates(self):
        with mock.patch.object(
            sleeper.httpx, "get", return_value=_fake_response({}, status_ok=False)
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                sleeper.get_user("someuser")


class GetLeaguesTest(unittest.TestCase):
    def test_correct_user_id_and_explicit_season_encoded_in_url(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([])) as m:
            sleeper.get_leagues("123", "2026")
        called_url = m.call_args.args[0]
        self.assertEqual(called_url, f"{sleeper.BASE_URL}/user/123/leagues/nfl/2026")

    def test_timeout_supplied(self):
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response([])) as m:
            sleeper.get_leagues("123", "2026")
        self.assertIn("timeout", m.call_args.kwargs)
        self.assertIsNotNone(m.call_args.kwargs["timeout"])

    def test_returns_decoded_list(self):
        payload = [{"league_id": "abc"}, {"league_id": "def"}]
        with mock.patch.object(sleeper.httpx, "get", return_value=_fake_response(payload)):
            result = sleeper.get_leagues("123", "2026")
        self.assertEqual(result, payload)

    def test_non_2xx_propagates(self):
        with mock.patch.object(
            sleeper.httpx, "get", return_value=_fake_response([], status_ok=False)
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                sleeper.get_leagues("123", "2026")

    def test_season_has_no_default_value(self):
        # The stale Scout default (season="2025") must not be promoted into
        # shared core -- season is a required parameter here, not optional.
        params = inspect.signature(sleeper.get_leagues).parameters
        self.assertEqual(params["season"].default, inspect.Parameter.empty)

    def test_calling_without_season_raises_type_error(self):
        with self.assertRaises(TypeError):
            sleeper.get_leagues("123")  # noqa: missing required season


if __name__ == "__main__":
    unittest.main()
