"""
League Intel HTTP endpoints: access, validation and status mapping.

GET  /intel                                 read everything (consistent snapshot)
PUT  /intel/{owner}/name                    {value, base_rev}
PUT  /intel/{owner}/breakdown               {value, base_rev, covers}
PUT  /intel/{owner}/observations/{obs_id}   {text, date_label}

Access: x-gm-key always required, and fail-closed (503) when the service has
no GM_CHAT_SECRET. Writes only from the exact production Origin; previews,
localhost and Origin-less callers may read but not write. Store, Sleeper or
config failures are 503 with no partial body. Intel text never reaches logs.
"""
import logging
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

import api
import chat_guard
import intel_store
from intel_store import IntelStore
from tests._local_redis import REDIS_BIN, LocalRedis

SECRET = "test-intel-secret"
PROD = "https://gm-command.netlify.app"
PREVIEW = "https://deploy-preview-14--gm-command.netlify.app"
MEMBER, OTHER_MEMBER, STRANGER = "1001", "1002", "9999"
MEMBERS = [{"user_id": MEMBER}, {"user_id": OTHER_MEMBER}]


def H(origin=PROD, key=SECRET):
    h = {"X-GM-Key": key} if key is not None else {}
    if origin:
        h["Origin"] = origin
    return h


@unittest.skipUnless(REDIS_BIN, "redis-server not installed")
class IntelApiTestCase(unittest.TestCase):
    def setUp(self):
        self.redis = LocalRedis()
        intel_store._set_store_for_tests(IntelStore(self.redis))
        api._intel_members_cache = None
        chat_guard._hits.clear()
        self.patches = [
            mock.patch.object(chat_guard, "SHARED_SECRET", SECRET),
            mock.patch.object(api, "get_league_users", return_value=MEMBERS),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        intel_store._set_store_for_tests(None)
        api._intel_members_cache = None
        self.redis.close()

    def put_obs(self, owner=MEMBER, obs_id=None, text="Lowballs on WRs", origin=PROD, key=SECRET):
        obs_id = obs_id or str(uuid.uuid4())
        return obs_id, self.client.put(f"/intel/{owner}/observations/{obs_id}",
                                       json={"text": text, "date_label": "Nov 23"}, headers=H(origin, key))


class RoundTripTest(IntelApiTestCase):
    def test_empty_then_full_round_trip(self):
        r = self.client.get("/intel", headers=H())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"schema": 1, "initialized": False, "rev": 0, "leaguemates": {}})

        o, r = self.put_obs()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["created"], True)
        r = self.client.put(f"/intel/{MEMBER}/name", json={"value": "John", "base_rev": 0}, headers=H())
        self.assertEqual(r.status_code, 200)
        r = self.client.put(f"/intel/{MEMBER}/breakdown",
                            json={"value": "Cheap on WRs.", "base_rev": 0, "covers": [o]}, headers=H())
        self.assertEqual(r.status_code, 200)

        data = self.client.get("/intel", headers=H()).json()
        lm = data["leaguemates"][MEMBER]
        self.assertTrue(data["initialized"])
        self.assertEqual(lm["name"]["value"], "John")
        self.assertEqual(lm["breakdown"]["covers"], [o])
        self.assertEqual([x["text"] for x in lm["observations"]], ["Lowballs on WRs"])

    def test_stale_name_is_409_with_current_value(self):
        r1 = self.client.put(f"/intel/{MEMBER}/name", json={"value": "John", "base_rev": 0}, headers=H()).json()
        self.client.put(f"/intel/{MEMBER}/name", json={"value": "Johnny", "base_rev": r1["rev"]}, headers=H())
        r = self.client.put(f"/intel/{MEMBER}/name", json={"value": "Jon", "base_rev": r1["rev"]}, headers=H())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["current"]["value"], "Johnny")

    def test_stale_breakdown_is_409_with_current_covers(self):
        o1, _ = self.put_obs()
        o2, _ = self.put_obs(text="second")
        self.client.put(f"/intel/{MEMBER}/breakdown", json={"value": "a", "base_rev": 0, "covers": [o1]}, headers=H())
        r = self.client.put(f"/intel/{MEMBER}/breakdown", json={"value": "b", "base_rev": 0, "covers": [o2]}, headers=H())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["current"]["covers"], [o1])

    def test_observation_retry_is_200_reuse_is_409(self):
        o, _ = self.put_obs()
        _, again = self.put_obs(obs_id=o)
        self.assertEqual((again.status_code, again.json()["created"]), (200, False))
        _, clash = self.put_obs(obs_id=o, text="other text")
        self.assertEqual(clash.status_code, 409)


class AccessTest(IntelApiTestCase):
    def test_missing_or_wrong_key_is_401_for_read_and_write(self):
        for key in (None, "wrong"):
            with self.subTest(key=key):
                self.assertEqual(self.client.get("/intel", headers=H(key=key)).status_code, 401)
                self.assertEqual(self.put_obs(key=key)[1].status_code, 401)

    def test_unset_secret_is_fail_closed(self):
        with mock.patch.object(chat_guard, "SHARED_SECRET", ""):
            self.assertEqual(self.client.get("/intel", headers=H()).status_code, 503)
            self.assertEqual(self.put_obs()[1].status_code, 503)
        self.assertFalse(self.client.get("/intel", headers=H()).json()["initialized"])

    def test_previews_and_other_origins_can_read_but_not_write(self):
        self.assertEqual(self.client.get("/intel", headers=H(origin=PREVIEW)).status_code, 200)
        for origin in (PREVIEW, "http://localhost:8888", None, "https://evil.example",
                       PROD + ".evil.example", PROD + "/"):
            with self.subTest(origin=origin):
                _, r = self.put_obs(origin=origin)
                self.assertEqual(r.status_code, 403)
                r = self.client.put(f"/intel/{MEMBER}/name", json={"value": "x", "base_rev": 0}, headers=H(origin))
                self.assertEqual(r.status_code, 403)
        self.assertFalse(self.client.get("/intel", headers=H()).json()["initialized"])

    def test_unknown_origin_cannot_read(self):
        self.assertEqual(self.client.get("/intel", headers=H(origin="https://evil.example")).status_code, 403)

    def test_cors_preflight_allows_put_from_production(self):
        r = self.client.options(f"/intel/{MEMBER}/name", headers={
            "Origin": PROD, "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-gm-key,content-type"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("PUT", r.headers["access-control-allow-methods"])

    def test_rate_limited_in_its_own_bucket(self):
        with mock.patch.object(chat_guard, "INTEL_RATE_LIMIT_REQUESTS", 2):
            codes = [self.client.get("/intel", headers=H()).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertFalse(any(k.startswith("chat:") for k in chat_guard._hits))


class ValidationTest(IntelApiTestCase):
    def test_owner_must_be_a_league_member(self):
        self.assertEqual(self.put_obs(owner=STRANGER)[1].status_code, 404)
        self.assertEqual(self.put_obs(owner="abc")[1].status_code, 422)

    def test_membership_unknown_is_503_not_404(self):
        with mock.patch.object(api, "get_league_users", side_effect=RuntimeError("sleeper down")):
            self.assertEqual(self.put_obs()[1].status_code, 503)

    def test_bad_inputs_are_422_and_write_nothing(self):
        cases = [
            ("/name", {"value": "x" * 41, "base_rev": 0}),
            ("/name", {"value": " John", "base_rev": 0}),
            ("/name", {"value": "Jo\nhn", "base_rev": 0}),
            ("/name", {"value": "John", "base_rev": -1}),
            ("/name", {"value": "John", "base_rev": 0, "extra": 1}),
            ("/breakdown", {"value": "x" * 4001, "base_rev": 0}),
            ("/breakdown", {"value": "ok", "base_rev": 0, "covers": [str(uuid.uuid4())]}),
            ("/breakdown", {"value": "ok", "base_rev": 0, "covers": ["nope"]}),
            ("/observations/" + str(uuid.uuid4()), {"text": "", "date_label": ""}),
            ("/observations/" + str(uuid.uuid4()), {"text": "x" * 1001}),
            ("/observations/" + str(uuid.uuid4()), {"text": "ok", "date_label": "a\nb"}),
            ("/observations/NOT-A-UUID", {"text": "ok"}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=str(body)[:60]):
                r = self.client.put(f"/intel/{MEMBER}{path}", json=body, headers=H())
                self.assertEqual(r.status_code, 422)
        self.assertFalse(self.client.get("/intel", headers=H()).json()["initialized"])
        self.assertIsNone(self.redis("GET", intel_store.REV_KEY))


class FailureTest(IntelApiTestCase):
    def test_store_down_is_503_with_no_body_data(self):
        def broken(*args):
            raise ConnectionError("down")
        intel_store._set_store_for_tests(IntelStore(broken))
        r = self.client.get("/intel", headers=H())
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("leaguemates", r.text)
        self.assertEqual(self.put_obs()[1].status_code, 503)

    def test_unconfigured_store_is_503(self):
        intel_store._set_store_for_tests(None)
        with mock.patch.dict("os.environ", {"GM_UPSTASH_REST_URL": "", "GM_UPSTASH_REST_TOKEN": ""}):
            self.assertEqual(self.client.get("/intel", headers=H()).status_code, 503)


class LoggingTest(IntelApiTestCase):
    def test_intel_content_never_reaches_logs(self):
        secret_bits = ("SecretRealName", "PrivateNoteText", "HiddenBreakdown")
        # gm.* has its own handler and does not propagate, so capture it and
        # the root (uvicorn, httpx, ...) separately.
        with self.assertLogs(level=logging.DEBUG) as root_logs, \
                self.assertLogs("gm", level=logging.DEBUG) as logs:
            o, _ = self.put_obs(text=secret_bits[1])
            self.client.put(f"/intel/{MEMBER}/name", json={"value": secret_bits[0], "base_rev": 0}, headers=H())
            self.client.put(f"/intel/{MEMBER}/name", json={"value": secret_bits[0], "base_rev": 0}, headers=H())
            self.client.put(f"/intel/{MEMBER}/breakdown",
                            json={"value": secret_bits[2], "base_rev": 0, "covers": [o]}, headers=H())
            self.client.get("/intel", headers=H())
        text = "\n".join(logs.output + root_logs.output)
        self.assertIn("intel_write op=name", text)
        for bit in secret_bits:
            self.assertNotIn(bit, text)


class ExistingBehaviourTest(unittest.TestCase):
    def test_chat_still_allows_requests_when_secret_unset(self):
        # Intel is fail-closed; chat's existing behaviour is not changed.
        with mock.patch.object(chat_guard, "SHARED_SECRET", ""):
            chat_guard.check_secret(mock.Mock(headers={}))


if __name__ == "__main__":
    unittest.main()
