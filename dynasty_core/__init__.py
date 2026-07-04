"""Shared data + domain layer for the Dynasty Umbrella.

Both Python engines (scoutcap and ddreportcards) ship an identical copy of
this package. Stage 2 will promote it to a proper installable package once
the engines have callable APIs.

Modules:
  sleeper      — Sleeper fantasy API (league, rosters, players, trades)
  espn         — ESPN Core API (active NFL: stats, injuries, team map)
  fantasycalc  — FantasyCalc dynasty values + grade logic
  leaguelogs   — LeagueLogs blurbs and ESPN id fallback
"""
