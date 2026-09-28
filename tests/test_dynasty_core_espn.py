"""
Stage 2C-1: behavioral baseline for dynasty_core.espn (active-NFL layer),
written before the package boundary changes so extraction is checked
against a specification.

Covers the team map, the stat flattener, the season/career stat requests
(including ESPN's 400-as-404 quirk), and the working team-feed injury path
-- the only verified ESPN injury source (Stage 2 plan §8.1).

Mocked network only; no live ESPN calls.
"""
import unittest
from unittest import mock

import httpx

import dynasty_core.espn as espn

BASE = espn.BASE_URL


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


class TeamEspnIdTest(unittest.TestCase):
    def test_normal_abbreviation(self):
        self.assertEqual(espn.team_espn_id("KC"), "12")
        self.assertEqual(espn.team_espn_id("TEN"), "10")

    def test_sleeper_was_maps_to_espn_wsh(self):
        self.assertEqual(espn.team_espn_id("WAS"), "28")
        self.assertEqual(espn.team_espn_id("WSH"), "28")

    def test_unknown_team_is_none(self):
        self.assertIsNone(espn.team_espn_id("XYZ"))
        self.assertIsNone(espn.team_espn_id(""))

    def test_map_covers_all_32_teams(self):
        self.assertEqual(len(espn.TEAM_ESPN_IDS), 32)
        self.assertEqual(len(set(espn.TEAM_ESPN_IDS.values())), 32)


class FlattenStatisticsTest(unittest.TestCase):
    PAYLOAD = {"splits": {"categories": [
        {"name": "receiving", "abbreviation": "rec", "stats": [
            {"name": "receptions", "abbreviation": "REC", "value": 88.0, "displayValue": "88"},
            {"name": "receivingYards", "abbreviation": "YDS", "value": 1204.0, "displayValue": "1,204"},
        ]},
        {"name": "defensive", "abbreviation": "def", "stats": [
            {"name": "totalTackles", "abbreviation": "TOT", "value": 1.0, "displayValue": "1"},
        ]},
        {"name": "general", "stats": [{"name": "gamesPlayed", "displayValue": "17"}]},
    ]}}

    def test_exact_name_to_value_contract(self):
        self.assertEqual(espn.flatten_statistics(self.PAYLOAD), {
            "receptions": 88.0,
            "receivingYards": 1204.0,
            "totalTackles": 1.0,
            "gamesPlayed": None,  # no "value" key -> None, displayValue is never used
        })

    def test_distinct_from_scout_college_transform(self):
        # Scout's tools.espn.get_college_stats builds "<cat_abbr>_<stat_abbr>"
        # keys from displayValue and keeps only rush/rec/gen/s categories.
        # This flattener must stay unprefixed, value-based and unfiltered.
        flat = espn.flatten_statistics(self.PAYLOAD)
        self.assertNotIn("rec_rec", flat)
        self.assertNotIn("rec_yds", flat)
        self.assertIn("totalTackles", flat)            # "def" category not filtered out
        self.assertIsInstance(flat["receivingYards"], float)  # not the "1,204" display string

    def test_empty_and_missing_structure(self):
        self.assertEqual(espn.flatten_statistics({}), {})
        self.assertEqual(espn.flatten_statistics({"splits": {}}), {})


class StatRequestsTest(unittest.TestCase):
    CASES = [
        ("season", lambda: espn.get_season_statistics("3916148", 2025),
         f"{BASE}/seasons/2025/types/2/athletes/3916148/statistics"),
        ("career", lambda: espn.get_career_statistics("3916148"),
         f"{BASE}/athletes/3916148/statistics"),
    ]

    def test_url_timeout_and_passthrough(self):
        for label, call, url in self.CASES:
            with self.subTest(label), mock.patch.object(espn.httpx, "get", return_value=_resp({"splits": {}})) as m:
                self.assertEqual(call(), {"splits": {}})
                m.assert_called_once_with(url, timeout=15)

    def test_400_and_404_return_empty_dict(self):
        for label, call, _ in self.CASES:
            for status in (400, 404):
                with self.subTest(label, status=status), \
                     mock.patch.object(espn.httpx, "get", return_value=_resp(None, status)):
                    self.assertEqual(call(), {})

    def test_other_errors_propagate(self):
        for label, call, _ in self.CASES:
            with self.subTest(label), mock.patch.object(espn.httpx, "get", return_value=_resp(None, 500)):
                with self.assertRaises(httpx.HTTPStatusError):
                    call()


class PlayerInjuryNotesTest(unittest.TestCase):
    TEAM = "12"
    FEED = f"{BASE}/teams/{TEAM}/injuries"
    MINE_1 = f"{BASE}/athletes/123/injuries/555"
    MINE_2 = f"{BASE}/athletes/123/injuries/556"
    OTHER = f"{BASE}/athletes/999/injuries/777"
    PREFIX_COLLISION = f"{BASE}/athletes/1234/injuries/888"

    def _router(self, pages):
        def fake_get(url, params=None, timeout=None):
            self.calls.append((url, params, timeout))
            if url == self.FEED:
                return _resp(pages[(params or {}).get("page", 1)])
            return _resp({"ref": url, "status": "Questionable"})
        return fake_get

    def setUp(self):
        self.calls = []

    def test_filters_by_athlete_and_dereferences_only_matches(self):
        pages = {1: {"items": [{"$ref": self.MINE_1}, {"$ref": self.OTHER}, {"$ref": self.PREFIX_COLLISION}],
                     "pageCount": 1}}
        with mock.patch.object(espn.httpx, "get", side_effect=self._router(pages)):
            notes = espn.get_player_injury_notes("123", self.TEAM)
        self.assertEqual(notes, [{"ref": self.MINE_1, "status": "Questionable"}])
        fetched = [c[0] for c in self.calls]
        self.assertEqual(fetched, [self.FEED, self.MINE_1])  # no fetch for other athletes

    def test_follows_feed_pagination(self):
        pages = {1: {"items": [{"$ref": self.OTHER}], "pageCount": 2},
                 2: {"items": [{"$ref": self.MINE_1}, {"$ref": self.MINE_2}], "pageCount": 2}}
        with mock.patch.object(espn.httpx, "get", side_effect=self._router(pages)):
            notes = espn.get_player_injury_notes("123", self.TEAM)
        self.assertEqual([n["ref"] for n in notes], [self.MINE_1, self.MINE_2])
        self.assertEqual(self.calls[0], (self.FEED, None, 15))
        self.assertEqual(self.calls[1], (self.FEED, {"page": 2}, 15))

    def test_no_entries_for_player_is_empty_list(self):
        pages = {1: {"items": [{"$ref": self.OTHER}], "pageCount": 1}}
        with mock.patch.object(espn.httpx, "get", side_effect=self._router(pages)):
            self.assertEqual(espn.get_player_injury_notes("123", self.TEAM), [])

    def test_team_feed_error_propagates(self):
        with mock.patch.object(espn.httpx, "get", return_value=_resp(None, 500)):
            with self.assertRaises(httpx.HTTPStatusError):
                espn.get_player_injury_notes("123", self.TEAM)


class FantasyRelevantStatsTest(unittest.TestCase):
    def test_keeps_position_core_zeros_and_any_nonzero(self):
        flat = {"receptions": 0, "receivingTargets": 0, "passingYards": 0,
                "QBRating": 0.0, "yardsPerRouteRun": 1.9, "gamesPlayed": 1}
        self.assertEqual(espn.fantasy_relevant_stats(flat, "WR"),
                         {"receptions": 0, "receivingTargets": 0, "yardsPerRouteRun": 1.9, "gamesPlayed": 1})

    def test_unknown_position_uses_union_of_cores(self):
        out = espn.fantasy_relevant_stats({"passingYards": 0, "receptions": 0, "QBRating": 0}, None)
        self.assertEqual(out, {"passingYards": 0, "receptions": 0})


if __name__ == "__main__":
    unittest.main()
