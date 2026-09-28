"""Package-owned defaults for FantasyCalc queries.

These values define dynasty_core's default FantasyCalc query profile.
They are provider-query defaults, not deployment identity: league_id,
owner/display name, draft year, roster identity, etc. remain app-owned.
"""

FANTASYCALC_IS_DYNASTY = True
FANTASYCALC_NUM_QBS = 2
FANTASYCALC_NUM_TEAMS = 12
FANTASYCALC_PPR = 0.5
