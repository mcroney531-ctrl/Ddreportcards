Yep. One hard gate in this: capture Sleeper's live roster_positions before changing starter math. I couldn't reach that endpoint from here either, so Scout's WR3/FLEX1 config stays supporting evidence, not the authority.

D2 is next. Work only in "mcroney531-ctrl/Ddreportcards".

Current accepted/live DD commit is "219bca59a22fe790e19db6cc166f471b32e28263" on "claude/dynasty-umbrella-consolidation-4waag0".

Before changing anything, verify that branch HEAD is still exactly that commit. If it has moved, stop and report the new head/diff before editing.

D2 has two specific pieces:

1. Remove DD's duplicate FantasyCalc competition-grade policy.
2. Correct DD roster grading to the league's actual starter structure, but only after verifying it from Sleeper.

Do not expand this into general cleanup.

1. Duplicate FantasyCalc grade policy

Current problem:

"agents/situation_agent.py" owns this local copy:

- "GRADE_TIERS"
- "_value_grade(...)"

Those thresholds are an exact duplicate of the canonical policy already present in the pinned "dynasty-core" package:

"dynasty_core.fantasycalc.GRADE_TIERS"
"dynasty_core.fantasycalc.value_grade"

DD's own compatibility facade already re-exports both through:

"data/fantasycalc_client.py"

This duplication was deliberately deferred as D2. The grade policy belongs in one place.

Required change

Remove the local "GRADE_TIERS" and "_value_grade" implementation from "agents/situation_agent.py".

Use the existing DD facade instead:

"fantasycalc_client.value_grade(position, redraft_position_rank)"

Do not import directly from "dynasty_core" inside the agent. Preserve the compatibility boundary through "data/fantasycalc_client.py".

This is consolidation only. The actual thresholds and resulting A/B/C/D/F grades must remain byte-for-byte behaviorally equivalent.

Tests — fail first

Add narrow tests that fail on "219bca5" and prove the ownership change, not just today's numeric results.

At minimum prove:

- "situation_agent.py" no longer defines its own "GRADE_TIERS".
- it no longer defines "_value_grade".
- competition grading delegates through "data.fantasycalc_client.value_grade".
- monkeypatching the facade's "value_grade" is observed by "assess_position_competition", proving there is no hidden local copy.
- canonical boundary cases still produce the existing grades for QB/RB/WR/TE.
- "None" rank still produces "F".

Do not modify "dynasty-core", its pin, or its grade tiers.

2. Roster starter-slot mismatch

Current DD policy in "agents/roster_agent.py":

- QB 1
- RB 2
- WR 2
- TE 1
- FLEX 2
- SF 1

Scout's previously verified league config says:

- QB 1
- RB 2
- WR 3
- TE 1
- FLEX 1
- SF 1
- K 1

But Scout is not the authority for this change.

Hard verification gate

Before editing roster-slot policy, fetch:

"https://api.sleeper.app/v1/league/1312201057664786432"

and record the exact "roster_positions" array returned by Sleeper.

That live league field is the authority.

If the code environment cannot reach Sleeper, use a browser-capable surface if available. If you still cannot retrieve the actual endpoint response, STOP the starter-slot portion and report the blocker. Do not infer the lineup from Scout and do not silently implement WR3/FLEX1 from the existing config.

Show the exact returned "roster_positions" in your report.

If Sleeper confirms the expected lineup

Correct DD's roster grading policy to represent the verified lineup.

Preferred architecture:

- Put the verified DD roster-position fact in "config/dynasty_config.py" as a DD-owned constant outside the existing "LEAGUE" dict.
- Do NOT add it to "LEAGUE"; D4's GM mirror and "/league" contract should remain unchanged.
- "roster_agent.py" should derive its gradeable starter allocation from that one DD config source rather than owning independent "BASE_STARTER_SLOTS" / "FLEX_SLOTS" / "SF_SLOTS" facts that can drift.
- Normalize Sleeper's "SUPER_FLEX" spelling to the existing internal "SF" label only at the application boundary.
- K is a real starter slot but DD does not generate player cards for kickers. Keep kickers outside "starter_quality_score"; document that explicitly.
- BN/TAXI/IR are not starter slots and do not participate in starter grading.

The corrected skill-position starter set should therefore be whatever the verified Sleeper data actually implies. If it is the expected league shape, that is 9 gradeable starters:

QB1 / RB2 / WR3 / TE1 / FLEX1 / SF1

Thin-position policy

"MIN_CONTRIBUTORS" currently mirrors the old base positional starter counts.

Do not leave a hidden WR2 assumption after changing the lineup.

Preserve "CONTRIBUTOR_THRESHOLD = 50", but make positional minimum-contributor requirements derive from the verified base positional starter requirements where appropriate. If Sleeper confirms WR3, "not thin at WR" cannot still require only two contributors without an explicit product-policy reason.

Do NOT change Trade Agent surplus thresholds. "SURPLUS_MIN_CONTRIBUTORS" is separate trade policy.

Starter-assignment tests — fail first

Use synthetic cards only. No model or network calls.

Include a case deliberately shaped so the old WR2/FLEX2 policy gives a different answer:

- enough QB/RB/WR/TE players to fill every slot;
- WR3 has lower "quality_score" than an extra RB;
- under the verified WR3/FLEX1 lineup, WR3 must still occupy the third required WR slot;
- only one remaining RB/WR/TE player gets FLEX;
- SF behavior remains unchanged.

Also prove:

- exact count of gradeable starters;
- required base slots are filled before FLEX;
- one SF slot remains;
- QB "POSITION_VALUE" premium for SF selection is unchanged;
- bench count remains correct;
- thin-position flags reflect the corrected base positional requirements.

Preserve exactly

This is not a recalibration.

Do not change:

- "POSITION_VALUE"
- "QUALITY_WEIGHTS"
- "CONTRIBUTOR_THRESHOLD"
- "LETTER_GRADE_TABLE"
- Situation opportunity calibration
- Production calibration or scoring semantics
- "AGE_CURVES"
- "AGING_RISK_PENALTY"
- Market "WEIGHTS"
- "MAX_BLEND_ADJUSTMENT"
- "MARKET_GRADE_TABLE"
- "SELL_HIGH_MARKET_FLOOR"
- "SELL_HIGH_CURRENT_HEALTH_MAX"
- "SELL_BEFORE_DROP_GAP"
- Trade "SURPLUS_MIN_CONTRIBUTORS"
- models ("anthropic/claude-sonnet-4-6")
- token budgets
- agent output schemas
- FantasyCalc request settings
- dynasty-core or its exact pin
- GM Command
- Scout
- DRAFT_CLASS_YEAR
- API auth/security/budget controls

Do not touch the retired Streamlit cleanup; that is a separate queued batch after D2.

Do not touch D5.

Do not touch the parked items: Streamlit component deprecation, snake-vs-linear draft order, README historical cleanup, GM UI/feed/version issues, or DD keepalive cleanup.

Model-facing text

Do not opportunistically rewrite prompts.

The half-PPR / BCNH corrections are already complete from D4.

If correcting the starter policy requires changing text that is actually sent to an LLM, isolate that in a separate commit explicitly labeled "[model-facing]", add exact string tests, and make no model calls.

Comments/docstrings that merely describe deterministic code can change with the implementation commit.

Compatibility requirements

After D2:

- "/league" JSON must remain byte-identical to the D4 baseline.
- "scripts/check_league_profile.py --gm-config ../gm-command/dynasty_config.json" must still report the same 13-field match when the sibling GM repo is available.
- DD's existing FantasyCalc facade remains the provider boundary.
- no new network dependency in tests.
- no paid/model calls.
- exact dynasty-core SHA stays "cef3c3d2b7120825110235eb2c30b4f8dd9a0247".
- "requirements.txt" and "requirements.lock.txt" should not change unless you discover an actual dependency requirement; if so, stop and report instead of expanding scope.

Verification

Baseline before edits:

"python -m unittest discover -s tests"

Expected current baseline: 292 passing.

Demonstrate fail-first for the new D2 tests against "219bca5".

After implementation run:

- full unittest suite;
- dependency checker;
- "pip check";
- "/league" compatibility test;
- GM profile drift checker if sibling repo is present;
- direct deterministic roster-grade tests;
- any existing source-ownership tests.

No live roster report generation is needed; do not spend model tokens just to canary deterministic slot math.

Because Render auto-deploys the hardened branch, do all local verification before pushing. After push, confirm the resulting Render deploy is live and "/health" remains 200. Do not invoke a paid report endpoint.

Commit structure

Prefer two implementation commits if both pieces are completed:

1. "D2: use canonical FantasyCalc competition grading"
2. "D2: grade the verified Dynasty Daddies starter lineup"

Only add a third "[model-facing]" commit if truly required by the rule above.

Do not combine unrelated cleanup.

Return to me

Report:

- starting SHA;
- exact Sleeper "roster_positions" response used as authority;
- fail-first results;
- exact files changed;
- how duplicate grade ownership was removed;
- how starter slots are now represented/derived;
- before/after synthetic assignment proving WR/FLEX behavior changed correctly;
- confirmation all preserved constants remained unchanged;
- full final test count;
- dependency checker result;
- "pip check";
- "/league" compatibility result;
- GM drift-check result;
- dynasty-core resolved SHA;
- commit SHA(s);
- pushed branch head;
- Render deploy ID/status and "/health";
- anything you could not verify.

Stop after D2. Do not start the retired Streamlit cleanup or D5.

Bring me Claude's result after that and I'll audit the exact commits before we close D2.
