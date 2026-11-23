"""
League Intel storage contract, run against a real Redis.

The contract GPT approved for GM Command's League Intel:
  * name, breakdown and observations are independent per leaguemate;
  * conflicts use server revisions (compare-and-set on base_rev), never clocks;
  * a never-written field (no revision) differs from a deliberately empty one;
  * the revision and its data are written in one Lua script, so a refused,
    duplicate or failed write consumes no revision;
  * observations are write-once by id; same id + same content is idempotent,
    same id + different content is a conflict, never an overwrite;
  * a breakdown records which observation ids it covers, and may only cover
    stored observations;
  * intel keys are isolated from the spend budget's keys.
"""
import unittest
import unittest.mock
import uuid

import budget
import intel_store
from intel_store import IntelStore
from tests._local_redis import REDIS_BIN, LocalRedis

A, B = "111", "222"


def oid() -> str:
    return str(uuid.uuid4())


@unittest.skipUnless(REDIS_BIN, "redis-server not installed")
class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.redis = LocalRedis()
        self.store = IntelStore(self.redis)

    def tearDown(self):
        self.redis.close()

    def counter(self) -> int:
        return int(self.redis("GET", intel_store.REV_KEY) or 0)


class EmptyStoreTest(StoreTestCase):
    def test_empty_store_is_uninitialized_not_an_error(self):
        data = self.store.read_all()
        self.assertEqual(data, {"schema": 1, "initialized": False, "rev": 0, "leaguemates": {}})

    def test_first_write_initializes(self):
        self.store.add_observation(A, oid(), "note", "Nov 18")
        self.assertTrue(self.store.read_all()["initialized"])


class NameTest(StoreTestCase):
    def test_never_set_vs_deliberately_empty(self):
        self.store.add_observation(A, oid(), "note", "")
        self.assertIsNone(self.store.read_all()["leaguemates"][A]["name"])
        r1 = self.store.set_name(A, "John", 0)
        r2 = self.store.set_name(A, "", r1["rev"])
        self.assertTrue(r2["ok"])
        self.assertEqual(self.store.read_all()["leaguemates"][A]["name"], {"value": "", "rev": r2["rev"]})

    def test_migration_base_rev_0_never_overwrites_a_cleared_name(self):
        r1 = self.store.set_name(A, "John", 0)
        r2 = self.store.set_name(A, "", r1["rev"])
        again = self.store.set_name(A, "Old local name", 0)
        self.assertFalse(again["ok"])
        self.assertEqual(again["current"], {"value": "", "rev": r2["rev"]})

    def test_stale_write_is_refused_with_current_value(self):
        r1 = self.store.set_name(A, "John", 0)
        self.store.set_name(A, "Johnny", r1["rev"])
        stale = self.store.set_name(A, "Jon", r1["rev"])
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["current"]["value"], "Johnny")
        self.assertEqual(self.store.read_all()["leaguemates"][A]["name"]["value"], "Johnny")

    def test_refused_write_consumes_no_revision(self):
        self.store.set_name(A, "John", 0)
        before = self.counter()
        self.store.set_name(A, "Jon", 0)
        self.assertEqual(self.counter(), before)


class IndependenceTest(StoreTestCase):
    def test_name_and_breakdown_edits_do_not_clobber_each_other(self):
        o = oid()
        self.store.add_observation(A, o, "Lowballs on WRs", "")
        self.store.set_name(A, "John", 0)                     # device 1
        self.assertTrue(self.store.set_breakdown(A, "Cheap on WRs.", 0, [o])["ok"])  # device 2
        lm = self.store.read_all()["leaguemates"][A]
        self.assertEqual(lm["name"]["value"], "John")
        self.assertEqual(lm["breakdown"]["value"], "Cheap on WRs.")

    def test_leaguemates_are_separate(self):
        self.store.set_name(A, "John", 0)
        self.assertTrue(self.store.set_name(B, "Sam", 0)["ok"])


class BreakdownTest(StoreTestCase):
    def test_breakdown_records_covered_observations(self):
        o1, o2 = oid(), oid()
        self.store.add_observation(A, o1, "one", "")
        self.store.add_observation(A, o2, "two", "")
        self.store.set_breakdown(A, "summary", 0, [o1])
        bd = self.store.read_all()["leaguemates"][A]["breakdown"]
        self.assertEqual(bd["covers"], [o1])   # o2 is outstanding, and visibly so

    def test_concurrent_synthesis_loser_is_refused_and_both_notes_survive(self):
        o1, o2 = oid(), oid()
        self.store.add_observation(A, o1, "phone note", "")
        self.store.add_observation(A, o2, "laptop note", "")
        win = self.store.set_breakdown(A, "from phone", 0, [o1])
        lose = self.store.set_breakdown(A, "from laptop", 0, [o2])   # slow AI reply
        self.assertTrue(win["ok"])
        self.assertFalse(lose["ok"])
        self.assertEqual(lose["current"], {"value": "from phone", "rev": win["rev"], "covers": [o1]})
        lm = self.store.read_all()["leaguemates"][A]
        self.assertEqual({o["id"] for o in lm["observations"]}, {o1, o2})
        self.assertNotIn(o2, lm["breakdown"]["covers"])

    def test_cannot_cover_an_unstored_observation(self):
        before = self.counter()
        res = self.store.set_breakdown(A, "summary", 0, [oid()])
        self.assertEqual(set(res), {"ok", "unknown_observation"})
        self.assertEqual(self.counter(), before)
        self.assertNotIn(A, self.store.read_all()["leaguemates"])


class ObservationTest(StoreTestCase):
    def test_add_only_and_ordered_by_server_revision(self):
        ids = [oid() for _ in range(3)]
        for i, o in enumerate(ids):
            self.store.add_observation(A, o, f"note {i}\nsecond line", f"Nov {i}")
        obs = self.store.read_all()["leaguemates"][A]["observations"]
        self.assertEqual([o["id"] for o in obs], ids)
        self.assertEqual(obs[0]["text"], "note 0\nsecond line")
        self.assertEqual(obs[0]["date_label"], "Nov 0")

    def test_retry_is_idempotent_and_consumes_no_revision(self):
        o = oid()
        first = self.store.add_observation(A, o, "note", "Nov 1")
        before = self.counter()
        again = self.store.add_observation(A, o, "note", "Nov 1")
        self.assertEqual(again, {"ok": True, "created": False, "rev": first["rev"]})
        self.assertEqual(self.counter(), before)

    def test_reused_id_with_other_content_is_a_conflict_not_an_overwrite(self):
        o = oid()
        self.store.add_observation(A, o, "original", "")
        before = self.counter()
        res = self.store.add_observation(A, o, "different", "")
        self.assertFalse(res["ok"])
        self.assertTrue(res["conflict"])
        self.assertEqual(self.counter(), before)
        self.assertEqual(self.store.read_all()["leaguemates"][A]["observations"][0]["text"], "original")

    def test_limit_refuses_without_a_revision(self):
        with unittest.mock.patch.object(intel_store, "MAX_OBSERVATIONS", 2):
            self.store.add_observation(A, oid(), "1", "")
            self.store.add_observation(A, oid(), "2", "")
            before = self.counter()
            self.assertEqual(self.store.add_observation(A, oid(), "3", ""), {"ok": False, "limit": True})
            self.assertEqual(self.counter(), before)

    def test_invalid_ids_never_reach_redis(self):
        n = len(self.redis.log)
        for bad in ("not-a-uuid", "../x", oid().upper()):
            with self.assertRaises(ValueError):
                self.store.add_observation(A, bad, "t", "")
        with self.assertRaises(ValueError):
            self.store.set_name("12a", "x", 0)
        self.assertEqual(len(self.redis.log), n)


class KeyIsolationTest(StoreTestCase):
    def test_every_key_intel_touches_is_under_its_prefix(self):
        o = oid()
        self.store.add_observation(A, o, "n", "")
        self.store.set_name(A, "J", 0)
        self.store.set_breakdown(A, "b", 0, [o])
        self.store.read_all()
        for args in self.redis.log:
            cmd = args[0]
            if cmd == "EVAL":
                keys = args[3:3 + int(args[2])]
            else:
                keys = [args[1]]
            for k in keys:
                with self.subTest(cmd=cmd, key=k):
                    self.assertTrue(k.startswith(intel_store.INTEL_PREFIX))

    def test_budget_keys_are_untouched(self):
        self.redis("SET", budget.KEEPALIVE_KEY, "1")
        day = f"{budget.KEY_PREFIX}:2026-11-23"
        self.redis("SET", day, "12345")
        o = oid()
        self.store.add_observation(A, o, "n", "")
        self.store.set_name(A, "J", 0)
        self.store.set_breakdown(A, "b", 0, [o])
        self.assertEqual(self.redis("GET", day), "12345")
        self.assertEqual(self.redis("GET", budget.KEEPALIVE_KEY), "1")
        intel_keys = self.redis("KEYS", "*")
        self.assertEqual(sorted(k for k in intel_keys if not k.startswith(intel_store.INTEL_PREFIX)),
                         sorted([day, budget.KEEPALIVE_KEY]))

    def test_overlapping_budget_prefix_refuses_to_start(self):
        for prefix in ("gm:intel:v1", "gm:intel", "", "gm"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(intel_store.IntelMisconfigured):
                    intel_store.check_key_isolation(prefix, f"{prefix}:keepalive")
        intel_store.check_key_isolation(budget.KEY_PREFIX, budget.KEEPALIVE_KEY)


class FailClosedConfigTest(unittest.TestCase):
    def tearDown(self):
        intel_store._set_store_for_tests(None)

    def test_missing_upstash_config_raises_instead_of_memory_fallback(self):
        intel_store._set_store_for_tests(None)
        with unittest.mock.patch.dict("os.environ", {"GM_UPSTASH_REST_URL": "", "GM_UPSTASH_REST_TOKEN": ""}):
            with self.assertRaises(intel_store.IntelMisconfigured):
                intel_store.store()

    def test_transport_failure_is_unavailable_without_detail(self):
        def broken(*args):
            raise ConnectionError("https://secret-host.example token=abc")
        with self.assertRaises(intel_store.IntelUnavailable) as ctx:
            IntelStore(broken).read_all()
        self.assertNotIn("secret-host", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
