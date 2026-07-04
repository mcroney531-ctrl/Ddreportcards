"""FantasyCalc client — re-exports from dynasty_core.fantasycalc.
Stage 1 of the Dynasty Umbrella: FantasyCalc logic consolidated in dynasty_core/.
All external agent APIs preserved; no agent changes needed.
"""
from dynasty_core.fantasycalc import (  # noqa: F401
    get_dynasty_values,
    index_by_sleeper_id,
    get_value_for_sleeper_id,
    index_by_sleeper_id_with_redraft_rank,
    get_player_value,
    value_grade,
    GRADE_TIERS,
)
