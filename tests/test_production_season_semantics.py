"""
Stage 3A: the Production agent's season tools must feed a COMPLETED
regular-season baseline, never partial in-progress totals.

_most_recent_completed_season() used to return the kickoff year as soon as
the calendar reached September, so on 2026-09-28 (2026 season in progress)
get_current_season_production pulled partial 2026 totals while the tool,
docstring and prompt all called it the "most recently completed" season.

Rule (no reliable, tested season-completion source exists in this repo, so
a conservative deterministic cutoff): the NFL regular season named N ends in
January of N+1, so a season counts as completed from February 1 of N+1.
  January       -> year - 2
  February-Dec  -> year - 1
Being one season stale for part of January is preferred over ever labeling
a possibly unfinished regular season as completed.

api._current_nfl_season is deliberately different (it wants the season in
progress, for chat's season-to-date stats) and is not covered here.

Dates are injected, never read from the real clock.
"""
import datetime
import unittest
from unittest import mock

import agents.production_agent as production_agent


def _as_of(y, m, d):
    """Patch the clock production_agent reads, for code paths that take no date."""
    fake = mock.Mock(wraps=datetime)
    fake.date = mock.Mock(wraps=datetime.date)
    fake.date.today = mock.Mock(return_value=datetime.date(y, m, d))
    return mock.patch.object(production_agent, "datetime", fake)


BOUNDARIES = {
    datetime.date(2026, 1, 1): 2024,
    datetime.date(2026, 1, 15): 2024,
    datetime.date(2026, 1, 31): 2024,
    datetime.date(2026, 2, 1): 2025,
    datetime.date(2026, 3, 1): 2025,
    datetime.date(2026, 8, 31): 2025,
    datetime.date(2026, 9, 1): 2025,
    datetime.date(2026, 9, 28): 2025,
    datetime.date(2026, 12, 31): 2025,
    datetime.date(2024, 2, 29): 2023,  # leap day: nothing special
    datetime.date(2025, 2, 28): 2024,  # non-leap year: nothing special
}


class MostRecentCompletedSeasonTest(unittest.TestCase):
    def test_in_season_date_selects_the_last_completed_season(self):
        # The critical regression: 2026 season in progress -> baseline is 2025.
        with _as_of(2026, 9, 28):
            self.assertEqual(production_agent._most_recent_completed_season(), 2025)

    def test_boundaries_via_the_clock(self):
        for day, expected in BOUNDARIES.items():
            with self.subTest(day=day), _as_of(day.year, day.month, day.day):
                self.assertEqual(production_agent._most_recent_completed_season(), expected)

    def test_boundaries_via_explicit_as_of(self):
        for day, expected in BOUNDARIES.items():
            with self.subTest(day=day):
                self.assertEqual(production_agent._most_recent_completed_season(as_of=day), expected)


class SeasonToolsRequestCompletedSeasonsTest(unittest.TestCase):
    def _call(self, fn, *args, **kwargs):
        with mock.patch.object(
            production_agent.espn_client, "get_season_statistics", return_value={"raw": True}
        ) as get_stats, mock.patch.object(
            production_agent.espn_client, "flatten_statistics", return_value={"receivingYards": 1.0}
        ):
            result = fn(*args, **kwargs)
        return result, get_stats

    def test_current_and_prior_in_september_2026(self):
        with _as_of(2026, 9, 28):
            current, current_stats = self._call(production_agent.get_current_season_production, "123")
            prior, prior_stats = self._call(production_agent.get_prior_season_production, "123")

        current_stats.assert_called_once_with("123", 2025)
        prior_stats.assert_called_once_with("123", 2024)
        self.assertEqual(current, {"espn_id": "123", "season": 2025, "stats": {"receivingYards": 1.0}})
        self.assertEqual(prior, {"espn_id": "123", "season": 2024, "stats": {"receivingYards": 1.0}})
        self.assertEqual(current["season"] - prior["season"], 1)

    def test_current_and_prior_stay_consecutive_across_boundaries(self):
        for day in BOUNDARIES:
            with self.subTest(day=day), _as_of(day.year, day.month, day.day):
                current, _ = self._call(production_agent.get_current_season_production, "123")
                prior, _ = self._call(production_agent.get_prior_season_production, "123")
                self.assertEqual(current["season"], BOUNDARIES[day])
                self.assertEqual(prior["season"], BOUNDARIES[day] - 1)

    def test_explicit_season_override_wins_regardless_of_date(self):
        for day in (datetime.date(2026, 9, 28), datetime.date(2026, 1, 15)):
            with self.subTest(day=day), _as_of(day.year, day.month, day.day):
                result, get_stats = self._call(
                    production_agent.get_current_season_production, "123", season=2023
                )
            get_stats.assert_called_once_with("123", 2023)
            self.assertEqual(result["season"], 2023)


if __name__ == "__main__":
    unittest.main()
