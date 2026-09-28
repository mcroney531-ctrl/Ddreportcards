"""
Stage 2C-1: behavioral baseline for dynasty_core.fantasycalc, written before
the package boundary changes (2C-2) so the extraction is checked against a
specification rather than against itself.

Deliberately independent of the league defaults' actual values: those come
from an ambient config module today (audit B1) and are fixed in 2C-2. Tests
use fc._default_params_key() / no-argument calls for "the default parameter
set" and explicit arguments for everything else.

Mocked network and time only; no live FantasyCalc calls.
"""
import unittest
from unittest import mock

import httpx

import dynasty_core.fantasycalc as fc

TTL = fc._VALUES_TTL_SECONDS


def _entry(sleeper_id, name, position, value, redraft, pos_rank=None, team="KC", age=25, yoe=3):
    player = {"name": name, "position": position, "maybeTeam": team, "maybeAge": age, "maybeYoe": yoe}
    if sleeper_id is not None:
        player["sleeperId"] = sleeper_id
    return {"player": player, "value": value, "redraftValue": redraft, "positionRank": pos_rank}


PAYLOAD = [
    _entry("100", "WR High Redraft", "WR", 5000, 900, pos_rank=2),
    _entry("101", "WR Top Dynasty", "WR", 6000, 700, pos_rank=1),
    _entry("200", "QB One", "QB", 7000, 950, pos_rank=1),
    _entry(None, "2027 1st", "PICK", 3000, 0),
    _entry(None, "2028 2nd", "PICK", 1500, 0),
    _entry(None, "2027 Mid 1st", "PICK", 3100, 0),
    _entry(None, "2029 5th", "PICK", 100, 0),
    _entry(None, "No Id Player", "RB", 900, 500),
]


def _fake_response(payload, status_ok=True):
    resp = mock.Mock()
    resp.json.return_value = payload
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=mock.Mock(), response=mock.Mock(status_code=503)
        )
    return resp


class _CacheReset(unittest.TestCase):
    def setUp(self):
        self._orig_values = dict(fc._values_cache)
        self._orig_index = fc._index_cache
        fc._values_cache.clear()
        fc._index_cache = None

    def tearDown(self):
        fc._values_cache.clear()
        fc._values_cache.update(self._orig_values)
        fc._index_cache = self._orig_index


class RequestContractTest(_CacheReset):
    def test_url_params_timeout_and_passthrough(self):
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response(PAYLOAD)) as m, \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            result = fc.get_dynasty_values(is_dynasty=True, num_qbs=2, num_teams=12, ppr=0.5)
        m.assert_called_once_with(
            "https://api.fantasycalc.com/values/current",
            params={"isDynasty": "true", "numQbs": 2, "numTeams": 12, "ppr": 0.5},
            timeout=20,
        )
        self.assertIs(result, PAYLOAD)

    def test_is_dynasty_false_is_lowercase_string(self):
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response([])) as m, \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            fc.get_dynasty_values(is_dynasty=False, num_qbs=1, num_teams=10, ppr=1.0)
        self.assertEqual(m.call_args.kwargs["params"]["isDynasty"], "false")

    def test_non_2xx_propagates(self):
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response([], status_ok=False)), \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            with self.assertRaises(httpx.HTTPStatusError):
                fc.get_dynasty_values(num_qbs=2, num_teams=12, ppr=0.5)


class ParameterKeyedCacheTest(_CacheReset):
    SF = dict(is_dynasty=True, num_qbs=2, num_teams=12, ppr=0.5)
    ONE_QB = dict(is_dynasty=True, num_qbs=1, num_teams=10, ppr=1.0)

    def test_distinct_parameter_sets_never_share_an_entry(self):
        sf_payload, one_qb_payload = [{"list": "sf"}], [{"list": "1qb"}]
        with mock.patch.object(fc.httpx, "get",
                               side_effect=[_fake_response(sf_payload), _fake_response(one_qb_payload)]) as m, \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            first = fc.get_dynasty_values(**self.SF)
            second = fc.get_dynasty_values(**self.ONE_QB)
            # Both now cached; each must still return its own list.
            again_sf = fc.get_dynasty_values(**self.SF)
            again_one_qb = fc.get_dynasty_values(**self.ONE_QB)
        self.assertEqual(m.call_count, 2)
        self.assertIs(first, sf_payload)
        self.assertIs(second, one_qb_payload)
        self.assertIs(again_sf, sf_payload)
        self.assertIs(again_one_qb, one_qb_payload)
        self.assertEqual(m.call_args_list[1].kwargs["params"],
                         {"isDynasty": "true", "numQbs": 1, "numTeams": 10, "ppr": 1.0})

    def test_same_parameters_inside_ttl_do_not_refetch(self):
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response(PAYLOAD)) as m, \
             mock.patch.object(fc.time, "time", side_effect=[1000.0, 1000.0 + TTL]):
            first = fc.get_dynasty_values(**self.SF)
            second = fc.get_dynasty_values(**self.SF)  # exactly at TTL is still fresh (<=)
        m.assert_called_once()
        self.assertIs(first, second)

    def test_expired_entry_refetches(self):
        new_payload = [{"list": "new"}]
        with mock.patch.object(fc.httpx, "get",
                               side_effect=[_fake_response(PAYLOAD), _fake_response(new_payload)]) as m, \
             mock.patch.object(fc.time, "time", side_effect=[1000.0, 1000.0 + TTL + 1]):
            fc.get_dynasty_values(**self.SF)
            second = fc.get_dynasty_values(**self.SF)
        self.assertEqual(m.call_count, 2)
        self.assertIs(second, new_payload)

    def test_failed_refresh_keeps_previous_good_entry(self):
        key = (True, 2, 12, 0.5)
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response(PAYLOAD)), \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            fc.get_dynasty_values(**self.SF)
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response([], status_ok=False)), \
             mock.patch.object(fc.time, "time", return_value=1000.0 + TTL + 1):
            with self.assertRaises(httpx.HTTPStatusError):
                fc.get_dynasty_values(**self.SF)
        self.assertEqual(fc._values_cache[key], (1000.0, PAYLOAD))


class CondensedIndexInvalidationTest(_CacheReset):
    def test_index_reused_while_default_values_unchanged(self):
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response(PAYLOAD)) as m, \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            first = fc._build_condensed_index()
            second = fc._build_condensed_index()
            fc.get_player_value("100")
        m.assert_called_once()
        self.assertIs(first, second)

    def test_index_invalidated_when_default_parameter_set_refreshes(self):
        refreshed = [_entry("100", "WR Renamed", "WR", 1, 1)]
        with mock.patch.object(fc.httpx, "get",
                               side_effect=[_fake_response(PAYLOAD), _fake_response(refreshed)]), \
             mock.patch.object(fc.time, "time", side_effect=[1000.0, 1000.0 + TTL + 1, 1000.0 + TTL + 1]):
            self.assertEqual(fc.get_player_value("100")["name"], "WR High Redraft")
            fc.get_dynasty_values()  # default parameter set, expired -> refetch -> index dropped
            self.assertIsNone(fc._index_cache)
            self.assertEqual(fc.get_player_value("100")["name"], "WR Renamed")

    def test_index_survives_refresh_of_a_non_default_parameter_set(self):
        default_key = fc._default_params_key()
        other = dict(is_dynasty=not default_key[0], num_qbs=default_key[1] + 1,
                     num_teams=default_key[2] + 2, ppr=default_key[3] + 0.5)
        with mock.patch.object(fc.httpx, "get",
                               side_effect=[_fake_response(PAYLOAD), _fake_response([{"x": 1}])]) as m, \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            index = fc._build_condensed_index()
            fc.get_dynasty_values(**other)
            self.assertIs(fc._index_cache, index)
            self.assertIs(fc._build_condensed_index(), index)
        self.assertEqual(m.call_count, 2)


class TransformsTest(unittest.TestCase):
    def test_index_by_sleeper_id_skips_entries_without_sleeper_id(self):
        idx = fc.index_by_sleeper_id(PAYLOAD)
        self.assertEqual(set(idx), {"100", "101", "200"})
        self.assertIs(idx["100"], PAYLOAD[0])

    def test_index_by_sleeper_id_with_redraft_rank_ranks_within_position_by_redraft_value(self):
        payload = [dict(e, player=dict(e["player"])) for e in PAYLOAD]  # don't touch module data
        idx = fc.index_by_sleeper_id_with_redraft_rank(payload)
        # WR "100" has higher redraftValue (900) than "101" (700) despite lower dynasty value.
        self.assertEqual(idx["100"]["redraftPositionRank"], 1)
        self.assertEqual(idx["101"]["redraftPositionRank"], 2)
        self.assertEqual(idx["200"]["redraftPositionRank"], 1)

    def test_index_picks_by_label_keeps_only_round_labelled_picks(self):
        idx = fc.index_picks_by_label(PAYLOAD)
        self.assertEqual(set(idx), {"2027 1st", "2028 2nd"})
        self.assertIs(idx["2027 1st"], PAYLOAD[3])

    def test_value_grade_tiers(self):
        self.assertEqual(fc.value_grade("QB", None), "F")
        self.assertEqual(fc.value_grade("QB", 12), "A")   # thresholds are inclusive
        self.assertEqual(fc.value_grade("QB", 13), "B")
        self.assertEqual(fc.value_grade("WR", 54), "D")
        self.assertEqual(fc.value_grade("WR", 55), "F")
        self.assertEqual(fc.value_grade("TE", 6), "A")
        self.assertEqual(fc.value_grade("K", 1), "F")     # unknown position

    def test_grade_tiers_unchanged(self):
        self.assertEqual(fc.GRADE_TIERS, {
            "QB": [(12, "A"), (20, "B"), (28, "C"), (36, "D")],
            "RB": [(12, "A"), (24, "B"), (36, "C"), (48, "D")],
            "WR": [(12, "A"), (24, "B"), (36, "C"), (54, "D")],
            "TE": [(6, "A"), (12, "B"), (18, "C"), (24, "D")],
        })


class AccessorsTest(_CacheReset):
    def _with_payload(self):
        return mock.patch.object(fc, "get_dynasty_values", return_value=PAYLOAD)

    def test_get_value_for_sleeper_id_returns_raw_entry_or_none(self):
        with self._with_payload() as gdv:
            self.assertIs(fc.get_value_for_sleeper_id("200"), PAYLOAD[2])
            self.assertIsNone(fc.get_value_for_sleeper_id("999"))
        gdv.assert_called_with()  # default parameter set only

    def test_get_player_value_condensed_record(self):
        with self._with_payload():
            rec = fc.get_player_value("100")
        self.assertEqual(rec, {
            "name": "WR High Redraft", "position": "WR", "team": "KC", "age": 25, "years_exp": 3,
            "dynasty_value": 5000, "redraft_value": 900, "dynasty_pos_rank": 2, "redraft_pos_rank": 1,
        })

    def test_get_player_value_normalizes_id_and_misses_return_none(self):
        with self._with_payload():
            self.assertEqual(fc.get_player_value(101)["name"], "WR Top Dynasty")
            self.assertIsNone(fc.get_player_value("999"))


class RedraftRankTransformDoesNotMutateInputTest(_CacheReset):
    """Stage 2C-4.5 (audit O2): index_by_sleeper_id_with_redraft_rank used to
    write redraftPositionRank into the entry dicts it was given. When a caller
    passed the cached get_dynasty_values() list (Ddreportcards' situation
    agent does), that silently mutated the shared FantasyCalc cache."""

    def _payload(self):
        import copy
        return copy.deepcopy(PAYLOAD)

    def test_rankings_unchanged(self):
        idx = fc.index_by_sleeper_id_with_redraft_rank(self._payload())
        self.assertEqual({sid: e["redraftPositionRank"] for sid, e in idx.items()},
                         {"100": 1, "101": 2, "200": 1})

    def test_input_entries_gain_no_rank_field(self):
        payload = self._payload()
        fc.index_by_sleeper_id_with_redraft_rank(payload)
        self.assertFalse(any("redraftPositionRank" in e for e in payload))
        self.assertEqual(payload, PAYLOAD)

    def test_cached_default_payload_is_not_mutated(self):
        with mock.patch.object(fc.httpx, "get", return_value=_fake_response(self._payload())), \
             mock.patch.object(fc.time, "time", return_value=1000.0):
            cached = fc.get_dynasty_values()
            fc.index_by_sleeper_id_with_redraft_rank(cached)
            again = fc.get_dynasty_values()  # cache hit
        self.assertIs(again, cached)
        self.assertFalse(any("redraftPositionRank" in e for e in fc._values_cache[fc._default_params_key()][1]))

    def test_returned_entries_are_distinct_top_level_dicts(self):
        payload = self._payload()
        idx = fc.index_by_sleeper_id_with_redraft_rank(payload)
        by_id = fc.index_by_sleeper_id(payload)
        for sid in idx:
            with self.subTest(sid):
                self.assertIsNot(idx[sid], by_id[sid])

    def test_provider_fields_and_nested_data_intact(self):
        payload = self._payload()
        idx = fc.index_by_sleeper_id_with_redraft_rank(payload)
        source = fc.index_by_sleeper_id(payload)
        for sid, entry in idx.items():
            with self.subTest(sid):
                expected = dict(source[sid], redraftPositionRank=entry["redraftPositionRank"])
                self.assertEqual(entry, expected)
                self.assertEqual(entry["player"], source[sid]["player"])


class CondensedIndexFreshnessThroughGetPlayerValueTest(_CacheReset):
    """Stage 2C-1.5 (audit O1): get_player_value() must honour the values
    cache's TTL. _values_cache + _VALUES_TTL_SECONDS is the single freshness
    authority; the condensed index is only reused while that says the
    default values are still fresh. Previously the index short-circuited
    before get_dynasty_values() ran, so a long-lived process (Scout) served
    its first FantasyCalc snapshot forever."""

    OLD = [_entry("100", "WR Old Name", "WR", 5000, 900, pos_rank=2)]
    NEW = [_entry("100", "WR New Name", "WR", 5100, 950, pos_rank=1)]

    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.responses = []
        self.http = mock.patch.object(fc.httpx, "get", side_effect=lambda *a, **k: self.responses.pop(0))
        self.clock = mock.patch.object(fc.time, "time", side_effect=lambda: self.now)
        self.spy = mock.patch.object(fc, "get_dynasty_values", wraps=fc.get_dynasty_values)
        self.http_get = self.http.start()
        self.clock.start()
        self.gdv = self.spy.start()
        self.addCleanup(mock.patch.stopall)

    def test_first_call_fetches_and_builds_index(self):
        self.responses = [_fake_response(self.OLD)]
        self.assertEqual(fc.get_player_value("100")["name"], "WR Old Name")
        self.assertEqual(self.http_get.call_count, 1)
        self.assertIsNotNone(fc._index_cache)

    def test_repeat_inside_ttl_checks_freshness_without_refetching(self):
        self.responses = [_fake_response(self.OLD)]
        fc.get_player_value("100")
        index = fc._index_cache
        self.now += TTL  # still fresh (<=)
        self.assertEqual(fc.get_player_value("100")["name"], "WR Old Name")
        self.assertEqual(self.gdv.call_count, 2)       # freshness path ran both times
        self.assertEqual(self.http_get.call_count, 1)  # but no extra request
        self.assertIs(fc._index_cache, index)

    def test_after_ttl_refetches_once_and_rebuilds_from_new_payload(self):
        self.responses = [_fake_response(self.OLD), _fake_response(self.NEW)]
        fc.get_player_value("100")
        old_index = fc._index_cache
        self.now += TTL + 1
        rec = fc.get_player_value("100")
        self.assertEqual(self.http_get.call_count, 2)
        self.assertEqual(rec["name"], "WR New Name")
        self.assertEqual(rec["dynasty_value"], 5100)
        self.assertIsNot(fc._index_cache, old_index)

    def test_failed_refresh_after_ttl_raises_and_keeps_good_state(self):
        self.responses = [_fake_response(self.OLD), _fake_response([], status_ok=False)]
        fc.get_player_value("100")
        good_entry = fc._values_cache[fc._default_params_key()]
        good_index = fc._index_cache
        self.now += TTL + 1
        with self.assertRaises(httpx.HTTPStatusError):
            fc.get_player_value("100")
        self.assertEqual(fc._values_cache[fc._default_params_key()], good_entry)
        self.assertIs(fc._index_cache, good_index)

    def test_non_default_refresh_does_not_rebuild_default_index(self):
        default_key = fc._default_params_key()
        other = dict(is_dynasty=not default_key[0], num_qbs=default_key[1] + 1,
                     num_teams=default_key[2] + 2, ppr=default_key[3] + 0.5)
        self.responses = [_fake_response(self.OLD), _fake_response([{"other": True}])]
        fc.get_player_value("100")
        index = fc._index_cache
        fc.get_dynasty_values(**other)
        self.assertIs(fc._index_cache, index)
        self.assertEqual(fc.get_player_value("100")["name"], "WR Old Name")
        self.assertIs(fc._index_cache, index)
        self.assertEqual(self.http_get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
