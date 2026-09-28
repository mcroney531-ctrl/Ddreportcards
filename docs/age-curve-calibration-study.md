# Stage 3D-1 — Age-curve calibration study

Research only. No runtime threshold, penalty or behavior was changed by this
study. Recommendations at the end are proposals for Stage 3D-2, pending review.

Reproduce: `python research/age_curve_analysis.py` from the repository root
(standard library only; downloads cached in `research/.cache/`, which is
gitignored). Every number below comes from `research/output/age_curve_results.json`,
and the appendix tables are generated verbatim into `research/output/age_tables.md`.
A rerun with the same inputs is byte-identical (fixed bootstrap seed 3, 1,000
resamples).

## 1. Question

Are the runtime aging thresholds and penalties defensible *for the two jobs
they actually do*?

| Constant | Where | Value (read from source by the script, not re-typed) |
|---|---|---|
| `AGE_CURVES` | `agents/production_agent.py` | RB 27/29 · WR 30/32 · TE 30/32 · QB 36/39 |
| `AGING_RISK_PENALTY` | `agents/market_agent.py` | low 0 · moderate −5 · high −12 · unknown 0 |
| `MAX_BLEND_ADJUSTMENT` | `agents/market_agent.py` | 0.25 |
| `SELL_HIGH_MARKET_FLOOR` | `agents/trade_agent.py` | 55 |

Band boundaries match `compute_age_curve_signal`: age < decline → low;
decline ≤ age < cliff → moderate; age ≥ cliff → high.

## 2. Production architecture (why only two jobs)

Stages 3B–3C.7 established that aging risk does **not** feed production_score,
the Trade Value Grade or roster quality_score. It has exactly two effects:

- **Job A: Hybrid Market Value.** Age enters `proprietary_composite` via
  `AGING_RISK_PENALTY`. The composite nudges FantasyCalc's value by
  `(composite − percentile)/100 × 0.25`, so k composite points move the
  hybrid value by k × 0.25 %. Moderate (−5) is about −1.25 %, and high (−12)
  is about −3.0 %, all else equal.
- **Job B: sell_high.** `aging_risk == "high"` alone satisfies the internal-risk
  side of the Trade Agent's sell-high flag when `trade_value_score ≥ 55`. This
  binary decision matters more than the Job A nudge.

This is therefore not a "player quality by age" model. The question for the
high threshold is whether it marks a materially riskier cohort. The question
for the penalties is whether they are proportionate *overlays* on a market
anchor that already prices age.

## 3. Sources (source data)

nflverse, `https://github.com/nflverse/nflverse-data` release assets,
downloaded 2026-09-28 (UTC):

| File | Use |
|---|---|
| `players/players.csv` | `gsis_id` → `birth_date` |
| `stats_player/stats_player_reg_{2010…2025}.csv` | regular-season player totals (`season_type` = REG only) |

The byte sizes and SHA-256 hashes of every file are recorded in
`research/output/age_curve_results.json` (`sources`). The fields used are
`player_id` (= gsis_id), `position`, `games`, `fantasy_points`
(nflverse standard scoring) and `receptions`. Raw files are not committed.

The 2026 regular season is in progress, so its file is never read. The 2025
file is complete: 250 players have all 17 games. Each season file has one
blank-id aggregate row (e.g. `games` = 272), which is excluded.

## 4. Methodology (derived calculations)

- **Half-PPR:** `half_ppr = fantasy_points + 0.5 × receptions`. The
  identity `fantasy_points_ppr − fantasy_points = receptions` holds on every
  2025 row, so this is exactly the midpoint of standard and PPR.
- **Historical age:** completed years on **September 1 of season t**, from
  nflverse `birth_date`. It is an integer, like runtime age, and is not
  rounded from any other date. No cohort observation lacked a birth date (0
  missing at every position, in all three cohorts).
- **Window:** season pairs t → t+1 for t = 2010 … 2024, so the last pair is
  2024 → 2025.
  - Recent-era sensitivity: t ≥ 2016.
  - Temporal split: development t = 2010–2019, validation t = 2020–2024,
    also shown with the 2020 → 2021 transition excluded.
- **Positions:** QB, RB, WR and TE are analyzed independently. The runtime
  29/31 fallback is not a football cohort and is not fitted.
- **Cohorts** (by the season-t half-PPR position rank):
  - primary: QB 36 · RB 60 · WR 72 · TE 36;
  - tight: 24 / 36 / 48 / 24;
  - broad participation: ≥ 8 games and ≥ 5.0 half-PPR PPG, regardless of rank.
- **Survivorship:** every season-t cohort member is followed into t+1. A
  player absent from t+1 counts as **0** next-season points. PPG retention is
  reported separately for players who played in both seasons ("returners").
- **Outcomes per observation:**
  - A. total retention = next half-PPR ÷ season-t half-PPR;
  - B. PPG retention (returners only);
  - C. relevance survival = in the same cohort, at the same position, in t+1;
  - D. severe loss = next-season total ≤ 50 % of season t, disappearances
    included. The 50 % cut is descriptive only.
- **Uncertainty:** 95 % percentile bootstrap intervals, resampling *players*
  (clusters), not player-seasons, because one player contributes several
  correlated seasons.
- **Candidate thresholds:**
  - Search space: only the brief's neighborhood (RB decline 25–29 / cliff
    27–31; WR and TE 27–32 / 29–34; QB 32–38 / 35–41), with cliff > decline,
    at least 20 development observations in every band, and monotone
    severe-loss risk (low ≤ moderate ≤ high).
  - Fit on development data only: pick the pair whose three-band step model
    has the lowest Brier score for severe loss.
  - Judge on validation: score each band's development-period rate against
    validation outcomes, for severe loss and for survival, for the current
    thresholds, the best candidate and a no-age baseline. The difference gets
    a player-clustered bootstrap CI.
  - A candidate is only recommended if it beats the current thresholds out of
    sample with a CI that excludes zero.

## 5. Results

The full age-by-age and band tables for every position, sample and cohort are
in the appendix. The key numbers (primary cohort, all 2010–2024 pairs) are
below; "sev." is the severe-loss rate and "surv." is relevance survival.

### Out-of-sample: do age bands help at all? (validation Brier; lower is better)

| Pos | Outcome | No age | Current bands | Current − no age [95 % CI] | Best dev candidate | Candidate − current [95 % CI] |
|---|---|---:|---:|---|---|---|
| RB | severe loss | 0.2227 | 0.2106 | **[−0.021, −0.004]** | 25/28 | [−0.006, 0.003] |
| RB | survival | 0.2177 | 0.2110 | **[−0.011, −0.003]** | 25/28 | [−0.005, 0.002] |
| WR | severe loss | 0.1856 | 0.1839 | [−0.006, 0.002] | 27/32 | [−0.006, 0.002] |
| WR | survival | 0.2251 | 0.2235 | [−0.004, 0.001] | 27/32 | [−0.003, 0.001] |
| TE | severe loss | 0.2146 | 0.2118 | [−0.006, 0.001] | 29/33 | [−0.005, 0.001] |
| TE | survival | 0.2250 | 0.2235 | [−0.004, 0.002] | 29/33 | [−0.008, 0.002] |
| QB | severe loss | 0.2055 | 0.2039 | [−0.007, 0.003] | 32/37 | [−0.005, 0.002] |
| QB | survival | 0.1929 | 0.1905 | [−0.006, 0.001] | 32/37 | [−0.005, 0.003] |

- **RB:** the only position where the current bands reliably beat "ignore age"
  out of sample.
- **WR, TE and QB:** the current bands point the right way, but the one-year
  validation window has too few old players to confirm them statistically.
- **No position:** the best development candidate never beats the current
  thresholds out of sample with a CI that excludes zero.

### RB — current 27/29

| Band | n | Median total ret. | Median PPG ret. | Surv. | Sev. |
|---|---:|---:|---:|---:|---:|
| low (<27) | 640 | 0.78 [0.71, 0.81] | 0.85 | 0.69 [0.65, 0.72] | 0.30 |
| moderate (27–28) | 149 | 0.62 [0.47, 0.78] | 0.80 | 0.62 [0.55, 0.70] | 0.44 |
| high (29+) | 111 | 0.43 [0.34, 0.60] | 0.72 | 0.50 [0.39, 0.59] | 0.56 |

- Outcomes worsen in order from low to moderate to high on all four measures.
- The ordering holds in the recent era (severe loss 0.27 / 0.46 / 0.58), in
  validation (0.27 / 0.44 / 0.63) and without 2020 → 2021, and in the tight
  (0.26 / 0.39 / 0.44) and broad (0.31 / 0.43 / 0.56) cohorts.
- Returners' PPG also falls (0.85 → 0.72), so the drop is partly real
  performance erosion and not only disappearance.
- Age by age, median retention steps down between 27 (0.71) and 28 (0.48), and
  severe loss rises from 0.38 to 0.51. So the top development candidates all put the high
  boundary at 28 (25/28, 26/28, 27/28), but none beats 27/29 out of sample.

### WR — current 30/32

| Band | n | Median total ret. | Median PPG ret. | Surv. | Sev. |
|---|---:|---:|---:|---:|---:|
| low (<30) | 931 | 0.80 [0.76, 0.84] | 0.87 | 0.66 [0.62, 0.70] | 0.24 |
| moderate (30–31) | 83 | 0.66 [0.53, 0.76] | 0.80 | 0.63 [0.51, 0.73] | 0.33 |
| high (32+) | 66 | 0.53 [0.27, 0.69] | 0.76 | 0.49 [0.34, 0.58] | 0.49 |

- The high band is clearly worse, and that holds in the recent era, the tight
  and broad cohorts, and validation (severe loss 0.54, n = 13).
- The moderate band isn't distinct in validation (severe loss 0.20 against 0.24
  for low, n = 25).
- Age by age, the decline is gradual from about 27 (retention 0.67; 0.66–0.80
  across 27–31) rather than a step at 30. The development search prefers
  27/32, which moves only the moderate boundary and keeps the cliff at 32,
  but that isn't distinguishable out of sample.

### TE — current 30/32

| Band | n | Median total ret. | Median PPG ret. | Surv. | Sev. |
|---|---:|---:|---:|---:|---:|
| low (<30) | 439 | 0.73 [0.70, 0.81] | 0.87 | 0.66 [0.60, 0.71] | 0.30 |
| moderate (30–31) | 52 | 0.77 [0.48, 0.85] | 0.89 | 0.56 [0.41, 0.69] | 0.40 |
| high (32+) | 49 | 0.66 [0.37, 0.75] | 0.92 | 0.59 [0.45, 0.69] | 0.41 |

- Both older bands are worse than low on survival and severe loss, but
  **moderate and high are indistinguishable**: severe loss 0.40 against 0.41
  (all years), 0.46 against 0.46 (recent), and survival is actually higher in
  the high band.
- In development the current pair isn't even monotone (moderate 0.38 > high
  0.37), so it fails the search's validity rule.
- Returners' PPG shows no age erosion (0.87 / 0.89 / 0.92).
- Nothing in these data separates a TE "cliff" at 32 from the moderate band,
  so the sell_high trigger for TEs is not supported as a *distinct* high-risk
  cohort.
- The best development candidate, 29/33, doesn't validate (CI includes zero).

### QB — current 36/39

| Band | n | Median total ret. | Median PPG ret. | Surv. | Sev. |
|---|---:|---:|---:|---:|---:|
| low (<36) | 490 | 0.83 [0.78, 0.89] | 0.95 | 0.75 [0.70, 0.80] | 0.28 |
| moderate (36–38) | 34 | 0.67 [0.29, 0.79] | 0.82 | 0.62 [0.45, 0.76] | 0.44 |
| high (39+) | 16 | 0.81 [0.00, 0.94] | 0.94 | 0.63 [0.25, 0.80] | 0.38 |

- The high band has 16 observations in 15 seasons (9 in development), so it
  fails the 20-per-band minimum. Its intervals are uninformative
  (median retention CI 0.00–0.94), and it isn't worse than moderate.
- Ages 37+ look worse than younger QBs (severe loss 0.50–0.55 at 37–39, all
  n ≤ 11), and the development search prefers 32/37. But that doesn't
  validate, and it rests on about 35 observations.
- QB ages 21–36 show no consistent age gradient.

## 6. Candidate comparison — summary

| Pos | Current | Rank of current among valid dev candidates | Best dev candidate | Validates? |
|---|---|---|---|---|
| RB | 27/29 | 4 of 19 | 25/28 | no (CI includes 0) |
| WR | 30/32 | 7 of 19 | 27/32 | no |
| TE | 30/32 | invalid (non-monotone in dev) | 29/33 | no |
| QB | 36/39 | invalid (high band n = 9 in dev) | 32/37 | no |

In every position, neighboring cut points give nearly identical development
Brier scores (differences in the fourth decimal). The data don't identify
precise one-year cliffs.

## 7. Current-player sell_high impact — NOT RUN

This needs today's Sleeper ages and FantasyCalc values. The research sandbox's
network policy blocks `api.sleeper.app` and `api.fantasycalc.com`: `--live`
fails with `URLError: Tunnel connection failed: 403 Forbidden`, and the failure
is recorded in the results file rather than filled in.

The analysis is implemented in the script (`--live`) and is ready to run from
any machine with access. It uses free endpoints and makes no model calls.
- It classifies every FantasyCalc-relevant QB/RB/WR/TE by Sleeper age under the
  current thresholds and under each position's best development candidate.
- It counts low / moderate / high / unknown, and high ∧
  `trade_value_score ≥ 55`.
- It lists every player entering or leaving the age-driven sell_high set. The
  score uses the runtime math: position percentile, rounded half-up.

Because no threshold change is recommended (section 10), this analysis is not
needed to decide 3D-2 as proposed. It becomes necessary if a threshold move is
ever reconsidered.

## 8. Unknown age

- **Historical (nflverse):** every cohort player-season had a birth date
  (24,801 of 24,833 nflverse player records carry one).
- **Current (Sleeper / FantasyCalc / DD roster):** not measured, for the same
  network reason. `--live` reports Sleeper missing-age counts for active
  QB/RB/WR/TE players and for the FantasyCalc pool.
- **Current behavior** (unknown → no penalty and never an age-driven
  sell_high) is left unchanged, and the question is flagged for 3D-2
  pending that count.

## 9. Penalty sensitivity (Job A)

| Penalty | Hybrid effect (all else equal) |
|---|---|
| moderate −3 / **−5** / −8 | −0.75 % / **−1.25 %** / −2.0 % |
| high −8 / **−12** / −16 | −2.0 % / **−3.0 %** / −4.0 % |

- These are internal overlays on FantasyCalc, which already prices age and
  career horizon. The historical production drop (for example, RB high-band
  median retention of 0.43) must **not** be read as a size for this penalty;
  that would double-count the market's own age pricing.
- What the study can support is the *ordering and rough spacing*. Where age
  signal exists (RB, and WR at the high end), the extra severe-loss risk over
  the low band is about 0.13 moderate and 0.26 high for RB, and about 0.08 and
  0.24 for WR. The moderate/high ratio of about 0.35–0.5 is consistent with
  −5/−12 (≈ 0.42).
- The size of the overlay is a product choice. At ±3 % it stays small relative
  to the market anchor. Nothing in the data argues for a larger internal
  overlay.

## 10. Recommendations (interpretation → product recommendation)

| Pos | Recommendation | Why |
|---|---|---|
| RB | **KEEP 27/29** | Monotone and validated against no-age; the 25/28 alternative doesn't beat it out of sample. Age 28 is a watch item. |
| WR | **KEEP 30/32** | The high band (32+) is clearly riskier across samples; the moderate boundary (27 vs 30) isn't identifiable out of sample. |
| TE | **DATA INSUFFICIENT — do not change** | The age effect is real versus <30, but moderate and high are indistinguishable, so the high threshold doesn't mark a distinct cohort. See 3D-2 item 2. |
| QB | **DATA INSUFFICIENT — do not change** | The high band (39+) has 16 observations and isn't worse than moderate; 37+ looks worse but doesn't validate. |

- **Penalty:** KEEP −5 / −12. It is a small (≤ 3 %), conservative internal
  overlay whose ordering and spacing match the empirical risk ordering. It is
  not a production-loss forecast, and making it bigger would double-count
  FantasyCalc.
- **Fallback curve (29/31):** it is not reachable in graded flows. `app.py`
  and every `api.py` report path filter to QB/RB/WR/TE before any card is
  built. Keep it, or in 3D-2 return `unknown` for unsupported positions
  rather than an arbitrary curve. This is not a calibration question.
- **Unknown age:** no change until `--live` measures how often Sleeper age is
  missing. Keep neutral handling if it's rare; revisit in 3D-2 if not.

## 11. Limitations

- **Outcome horizon:** one season ahead. Dynasty value is multi-year, so these
  results bound the short-horizon risk that sell_high implies, not career
  value.
- **Age definition mismatch:** the study uses age at Sep 1 of season t. Runtime
  uses Sleeper's live integer `age` field, so during a season a player can be
  one year older at runtime than his study age, and Sleeper's refresh cadence
  isn't controlled here.
- **Scoring:** nflverse standard fantasy points may differ from this league's
  exact settings (e.g. passing TD value, 2-point conversions). The half-PPR
  construction follows the brief.
- **Selection and regression:** the cohort is chosen on season-t production,
  so every age regresses (median retention is below 1 even for young players).
  The comparisons are between ages, not absolute forecasts.
- **Confounding:** injuries, contracts and depth-chart changes aren't
  controlled. Position labels come from nflverse per season.
- **Power:** the validation window (5 seasons) has few old players, especially
  for QB and TE. "No reliable improvement" partly reflects that.
- **Not measured:** the current-player sell_high impact and current
  unknown-age prevalence (section 7). No model calls or paid APIs were used.

## 12. Proposed Stage 3D-2 changes (for review; none implemented)

1. No threshold or penalty change for RB, WR, QB or the penalties.
2. **TE:** decide the product question the data raise: should a 32+ TE
   trigger sell_high by age alone when 32+ TEs aren't measurably riskier than
   30–31? Options:
   - (a) keep it (conservative, and no evidence of harm);
   - (b) make the TE high band unreachable for the age-only sell_high trigger
     while keeping the −12 overlay.

   Option (b) is a behavior change and needs the `--live` impact list first.
3. Run `python research/age_curve_analysis.py --live` from a machine with
   Sleeper and FantasyCalc access. It measures the current sell_high set and
   unknown-age prevalence.
4. Optional cleanup: return `unknown` (not the 29/31 curve) for positions
   outside QB/RB/WR/TE.
5. Re-run the study after the 2026 season completes, adding 2025 → 2026. RB
   age 28 and WR 27+ are the watch items.

---

## Appendix — generated tables

Generated by `research/age_curve_analysis.py` (`research/output/age_tables.md`);
⚠ marks ages with fewer than 20 observations, where estimates are unstable.
"Survival" is relevance survival (same cohort cut-off in t+1).

#### QB — age by age (primary cohort, 2010→2011 … 2024→2025)

| age | n | median total ret. [95% CI] | mean total ret. | median PPG ret. (returners, n) | relevance survival [95% CI] | severe loss | current band |
|---:|---:|---|---:|---|---|---:|---|
| 21 ⚠ | 11 | 1.12 [0.80, 1.49] | 1.12 | 0.93 (11) | 0.82 [0.55, 1.00] | 0.18 | low |
| 22 | 37 | 0.89 [0.79, 1.16] | 1.06 | 1.00 (35) | 0.84 [0.70, 0.95] | 0.16 | low |
| 23 | 52 | 0.91 [0.80, 1.15] | 0.99 | 1.00 (52) | 0.81 [0.69, 0.90] | 0.19 | low |
| 24 | 53 | 0.76 [0.55, 0.96] | 0.75 | 0.96 (48) | 0.77 [0.66, 0.89] | 0.30 | low |
| 25 | 50 | 0.73 [0.48, 0.81] | 0.69 | 0.95 (45) | 0.60 [0.46, 0.74] | 0.36 | low |
| 26 | 38 | 0.97 [0.72, 1.07] | 0.97 | 1.00 (34) | 0.74 [0.58, 0.87] | 0.26 | low |
| 27 | 41 | 0.86 [0.65, 1.01] | 0.85 | 0.89 (41) | 0.76 [0.63, 0.88] | 0.32 | low |
| 28 | 39 | 0.62 [0.42, 0.96] | 0.68 | 0.95 (34) | 0.64 [0.49, 0.80] | 0.44 | low |
| 29 | 29 | 0.81 [0.64, 0.91] | 0.78 | 0.95 (29) | 0.76 [0.59, 0.90] | 0.24 | low |
| 30 | 28 | 0.86 [0.78, 1.20] | 0.95 | 0.95 (28) | 0.79 [0.64, 0.93] | 0.25 | low |
| 31 | 29 | 0.76 [0.66, 1.10] | 0.86 | 0.88 (27) | 0.79 [0.62, 0.93] | 0.28 | low |
| 32 | 24 | 0.77 [0.63, 0.97] | 0.77 | 0.97 (22) | 0.79 [0.62, 0.96] | 0.25 | low |
| 33 | 21 | 0.75 [0.55, 1.03] | 0.87 | 0.93 (20) | 0.91 [0.76, 1.00] | 0.29 | low |
| 34 | 24 | 0.73 [0.23, 0.98] | 0.80 | 0.96 (20) | 0.58 [0.38, 0.79] | 0.38 | low |
| 35 ⚠ | 14 | 0.94 [0.79, 1.12] | 0.94 | 0.83 (14) | 0.93 [0.79, 1.00] | 0.14 | low |
| 36 ⚠ | 15 | 0.70 [0.42, 1.09] | 0.73 | 0.94 (14) | 0.73 [0.53, 0.93] | 0.33 | moderate |
| 37 ⚠ | 11 | 0.23 [0.01, 0.79] | 0.45 | 0.76 (9) | 0.46 [0.18, 0.73] | 0.55 | moderate |
| 38 ⚠ | 8 | 0.52 [0.00, 0.90] | 0.51 | 0.70 (7) | 0.62 [0.25, 0.88] | 0.50 | moderate |
| 39 ⚠ | 6 | 0.37 [0.00, 1.31] | 0.56 | 0.86 (3) | 0.50 [0.17, 0.83] | 0.50 | high |
| 40 ⚠ | 4 | 0.91 n/a | 0.69 | 0.94 (3) | 0.75 n/a | 0.25 | high |
| 41 ⚠ | 2 | 0.47 n/a | 0.47 | 0.94 (1) | 0.50 n/a | 0.50 | high |
| 42 ⚠ | 1 | 1.28 n/a | 1.28 | 1.28 (1) | 1.00 n/a | 0.00 | high |
| 43 ⚠ | 1 | 1.11 n/a | 1.11 | 1.04 (1) | 1.00 n/a | 0.00 | high |
| 44 ⚠ | 1 | 0.72 n/a | 0.72 | 0.72 (1) | 1.00 n/a | 0.00 | high |
| 45 ⚠ | 1 | 0.00 n/a | 0.00 | — (0) | 0.00 n/a | 1.00 | high |

#### QB — current bands 36/39 across samples

| sample | band | n | median total ret. [95% CI] | median PPG ret. | survival [95% CI] | severe loss |
|---|---|---:|---|---:|---|---:|
| all_2010_2024 | low | 490 | 0.83 [0.78, 0.89] | 0.95 | 0.75 [0.69, 0.80] | 0.28 |
| all_2010_2024 | moderate | 34 | 0.67 [0.29, 0.79] | 0.82 | 0.62 [0.45, 0.76] | 0.44 |
| all_2010_2024 | high | 16 | 0.81 [0.00, 0.94] | 0.94 | 0.62 [0.25, 0.80] | 0.38 |
| recent_2016_2024 | low | 288 | 0.81 [0.75, 0.89] | 0.94 | 0.74 [0.67, 0.80] | 0.27 |
| recent_2016_2024 | moderate | 23 | 0.64 [0.23, 0.82] | 0.83 | 0.61 [0.48, 0.73] | 0.43 |
| recent_2016_2024 | high | 13 | 0.93 [0.00, 1.11] | 0.94 | 0.77 [0.43, 0.91] | 0.23 |
| development | low | 329 | 0.84 [0.79, 0.90] | 0.96 | 0.75 [0.69, 0.80] | 0.28 |
| development | moderate | 22 | 0.69 [0.20, 0.83] | 0.90 | 0.64 [0.33, 0.85] | 0.46 |
| development | high | 9 | 0.93 [0.00, 0.95] | 0.94 | 0.67 [0.00, 0.93] | 0.33 |
| validation | low | 161 | 0.79 [0.73, 0.90] | 0.93 | 0.76 [0.67, 0.83] | 0.27 |
| validation | moderate | 12 | 0.64 [0.00, 0.84] | 0.77 | 0.58 [0.36, 0.80] | 0.42 |
| validation | high | 7 | 0.72 [0.00, 1.48] | 0.87 | 0.57 [0.20, 0.86] | 0.43 |
| validation_excl_2020_to_2021 | low | 132 | 0.77 [0.67, 0.90] | 0.94 | 0.75 [0.66, 0.82] | 0.28 |
| validation_excl_2020_to_2021 | moderate | 7 | 0.70 [0.59, 1.31] | 0.76 | 0.71 [0.56, 1.00] | 0.29 |
| validation_excl_2020_to_2021 | high | 5 | 0.72 n/a | 0.80 | 0.60 n/a | 0.40 |
| tight_cohort | low | 322 | 0.88 [0.82, 0.92] | 0.93 | 0.73 [0.66, 0.78] | 0.20 |
| tight_cohort | moderate | 26 | 0.76 [0.59, 0.83] | 0.90 | 0.58 [0.37, 0.73] | 0.31 |
| tight_cohort | high | 12 | 0.91 n/a | 0.94 | 0.75 n/a | 0.25 |
| broad_cohort | low | 466 | 0.82 [0.77, 0.88] | 0.94 | 0.73 [0.67, 0.78] | 0.29 |
| broad_cohort | moderate | 32 | 0.67 [0.28, 0.79] | 0.83 | 0.59 [0.41, 0.76] | 0.44 |
| broad_cohort | high | 15 | 0.74 [0.00, 0.94] | 0.94 | 0.60 [0.00, 0.79] | 0.40 |

#### RB — age by age (primary cohort, 2010→2011 … 2024→2025)

| age | n | median total ret. [95% CI] | mean total ret. | median PPG ret. (returners, n) | relevance survival [95% CI] | severe loss | current band |
|---:|---:|---|---:|---|---|---:|---|
| 20 ⚠ | 1 | 0.19 n/a | 0.19 | 0.80 (1) | 0.00 n/a | 1.00 | low |
| 21 | 42 | 0.88 [0.68, 1.08] | 0.93 | 0.89 (41) | 0.76 [0.62, 0.88] | 0.21 | low |
| 22 | 96 | 0.84 [0.74, 0.96] | 0.94 | 0.98 (92) | 0.74 [0.65, 0.82] | 0.22 | low |
| 23 | 140 | 0.83 [0.67, 0.97] | 0.92 | 0.84 (138) | 0.74 [0.66, 0.81] | 0.27 | low |
| 24 | 143 | 0.72 [0.57, 0.81] | 0.78 | 0.82 (136) | 0.65 [0.57, 0.73] | 0.36 | low |
| 25 | 126 | 0.70 [0.60, 0.88] | 0.85 | 0.82 (117) | 0.64 [0.56, 0.72] | 0.33 | low |
| 26 | 92 | 0.73 [0.53, 0.87] | 0.75 | 0.83 (85) | 0.65 [0.55, 0.75] | 0.35 | low |
| 27 | 80 | 0.70 [0.52, 0.88] | 0.79 | 0.81 (75) | 0.69 [0.57, 0.79] | 0.38 | moderate |
| 28 | 69 | 0.48 [0.40, 0.79] | 0.64 | 0.80 (63) | 0.55 [0.43, 0.67] | 0.51 | moderate |
| 29 | 40 | 0.46 [0.17, 0.77] | 0.50 | 0.68 (32) | 0.47 [0.33, 0.62] | 0.57 | high |
| 30 | 26 | 0.56 [0.40, 0.81] | 0.57 | 0.80 (23) | 0.61 [0.42, 0.81] | 0.46 | high |
| 31 | 21 | 0.23 [0.00, 0.46] | 0.43 | 0.62 (14) | 0.33 [0.14, 0.52] | 0.71 | high |
| 32 ⚠ | 10 | 0.48 [0.26, 1.02] | 0.71 | 0.84 (9) | 0.60 [0.30, 0.90] | 0.50 | high |
| 33 ⚠ | 7 | 0.40 [0.00, 0.77] | 0.39 | 0.64 (5) | 0.43 [0.14, 0.71] | 0.57 | high |
| 34 ⚠ | 3 | 0.61 n/a | 0.49 | 0.75 (2) | 0.67 n/a | 0.33 | high |
| 35 ⚠ | 2 | 0.56 n/a | 0.56 | 0.84 (2) | 0.50 n/a | 0.50 | high |
| 36 ⚠ | 1 | 1.04 n/a | 1.04 | 1.04 (1) | 1.00 n/a | 0.00 | high |
| 37 ⚠ | 1 | 0.00 n/a | 0.00 | — (0) | 0.00 n/a | 1.00 | high |

#### RB — current bands 27/29 across samples

| sample | band | n | median total ret. [95% CI] | median PPG ret. | survival [95% CI] | severe loss |
|---|---|---:|---|---:|---|---:|
| all_2010_2024 | low | 640 | 0.78 [0.71, 0.81] | 0.84 | 0.69 [0.65, 0.72] | 0.30 |
| all_2010_2024 | moderate | 149 | 0.62 [0.47, 0.78] | 0.80 | 0.62 [0.55, 0.70] | 0.44 |
| all_2010_2024 | high | 111 | 0.43 [0.34, 0.60] | 0.71 | 0.49 [0.39, 0.58] | 0.56 |
| recent_2016_2024 | low | 404 | 0.80 [0.74, 0.87] | 0.88 | 0.72 [0.67, 0.77] | 0.27 |
| recent_2016_2024 | moderate | 76 | 0.54 [0.40, 0.72] | 0.75 | 0.57 [0.45, 0.68] | 0.46 |
| recent_2016_2024 | high | 60 | 0.43 [0.22, 0.58] | 0.69 | 0.48 [0.35, 0.59] | 0.58 |
| development | low | 416 | 0.73 [0.65, 0.81] | 0.83 | 0.66 [0.61, 0.70] | 0.32 |
| development | moderate | 103 | 0.57 [0.46, 0.80] | 0.83 | 0.63 [0.54, 0.72] | 0.44 |
| development | high | 81 | 0.46 [0.29, 0.76] | 0.79 | 0.52 [0.39, 0.63] | 0.53 |
| validation | low | 224 | 0.84 [0.72, 0.91] | 0.88 | 0.74 [0.67, 0.80] | 0.27 |
| validation | moderate | 46 | 0.64 [0.35, 0.84] | 0.73 | 0.61 [0.46, 0.73] | 0.43 |
| validation | high | 30 | 0.37 [0.22, 0.52] | 0.61 | 0.43 [0.24, 0.58] | 0.63 |
| validation_excl_2020_to_2021 | low | 179 | 0.83 [0.74, 0.91] | 0.88 | 0.73 [0.66, 0.79] | 0.26 |
| validation_excl_2020_to_2021 | moderate | 36 | 0.71 [0.40, 0.94] | 0.75 | 0.67 [0.51, 0.81] | 0.39 |
| validation_excl_2020_to_2021 | high | 25 | 0.36 [0.23, 0.49] | 0.61 | 0.44 [0.25, 0.61] | 0.68 |
| tight_cohort | low | 388 | 0.78 [0.71, 0.82] | 0.86 | 0.61 [0.55, 0.66] | 0.26 |
| tight_cohort | moderate | 89 | 0.63 [0.48, 0.76] | 0.80 | 0.51 [0.40, 0.60] | 0.39 |
| tight_cohort | high | 63 | 0.60 [0.43, 0.78] | 0.80 | 0.46 [0.32, 0.57] | 0.44 |
| broad_cohort | low | 609 | 0.76 [0.69, 0.81] | 0.83 | 0.66 [0.62, 0.70] | 0.31 |
| broad_cohort | moderate | 150 | 0.62 [0.50, 0.77] | 0.80 | 0.61 [0.54, 0.69] | 0.43 |
| broad_cohort | high | 109 | 0.43 [0.23, 0.59] | 0.70 | 0.48 [0.37, 0.57] | 0.56 |

#### WR — age by age (primary cohort, 2010→2011 … 2024→2025)

| age | n | median total ret. [95% CI] | mean total ret. | median PPG ret. (returners, n) | relevance survival [95% CI] | severe loss | current band |
|---:|---:|---|---:|---|---|---:|---|
| 20 ⚠ | 2 | 1.65 n/a | 1.65 | 1.21 (2) | 1.00 n/a | 0.00 | low |
| 21 | 43 | 0.99 [0.83, 1.18] | 1.04 | 1.08 (43) | 0.79 [0.67, 0.91] | 0.16 | low |
| 22 | 98 | 0.95 [0.89, 1.07] | 0.97 | 1.02 (98) | 0.81 [0.72, 0.88] | 0.12 | low |
| 23 | 149 | 0.76 [0.71, 0.86] | 0.79 | 0.88 (143) | 0.63 [0.56, 0.70] | 0.25 | low |
| 24 | 137 | 0.86 [0.79, 0.97] | 0.87 | 0.93 (128) | 0.71 [0.64, 0.78] | 0.22 | low |
| 25 | 141 | 0.74 [0.66, 0.82] | 0.80 | 0.84 (139) | 0.60 [0.52, 0.67] | 0.27 | low |
| 26 | 111 | 0.82 [0.74, 0.88] | 0.80 | 0.84 (109) | 0.65 [0.55, 0.74] | 0.23 | low |
| 27 | 107 | 0.67 [0.59, 0.79] | 0.71 | 0.82 (103) | 0.56 [0.47, 0.65] | 0.34 | low |
| 28 | 74 | 0.80 [0.63, 0.89] | 0.74 | 0.86 (71) | 0.70 [0.59, 0.81] | 0.28 | low |
| 29 | 69 | 0.72 [0.63, 0.81] | 0.71 | 0.75 (63) | 0.61 [0.49, 0.72] | 0.29 | low |
| 30 | 48 | 0.66 [0.48, 0.80] | 0.68 | 0.85 (44) | 0.56 [0.44, 0.71] | 0.38 | moderate |
| 31 | 35 | 0.66 [0.56, 0.76] | 0.72 | 0.70 (32) | 0.71 [0.57, 0.86] | 0.26 | moderate |
| 32 | 28 | 0.73 [0.39, 0.96] | 0.68 | 0.84 (26) | 0.61 [0.43, 0.79] | 0.43 | high |
| 33 ⚠ | 17 | 0.39 [0.00, 0.73] | 0.43 | 0.80 (12) | 0.41 [0.18, 0.65] | 0.53 | high |
| 34 ⚠ | 9 | 0.54 [0.09, 1.38] | 0.64 | 0.69 (8) | 0.44 [0.11, 0.78] | 0.44 | high |
| 35 ⚠ | 5 | 0.60 [0.00, 0.95] | 0.49 | 0.93 (3) | 0.60 [0.20, 1.00] | 0.40 | high |
| 36 ⚠ | 6 | 0.21 [0.08, 0.81] | 0.41 | 0.43 (5) | 0.17 [0.00, 0.50] | 0.67 | high |
| 37 ⚠ | 1 | 0.00 n/a | 0.00 | — (0) | 0.00 n/a | 1.00 | high |

#### WR — current bands 30/32 across samples

| sample | band | n | median total ret. [95% CI] | median PPG ret. | survival [95% CI] | severe loss |
|---|---|---:|---|---:|---|---:|
| all_2010_2024 | low | 931 | 0.80 [0.76, 0.84] | 0.87 | 0.66 [0.62, 0.70] | 0.24 |
| all_2010_2024 | moderate | 83 | 0.66 [0.53, 0.76] | 0.80 | 0.63 [0.51, 0.72] | 0.33 |
| all_2010_2024 | high | 66 | 0.53 [0.27, 0.69] | 0.76 | 0.48 [0.34, 0.58] | 0.48 |
| recent_2016_2024 | low | 572 | 0.81 [0.76, 0.85] | 0.87 | 0.67 [0.63, 0.72] | 0.23 |
| recent_2016_2024 | moderate | 48 | 0.62 [0.46, 0.76] | 0.79 | 0.54 [0.39, 0.67] | 0.35 |
| recent_2016_2024 | high | 28 | 0.59 [0.22, 0.76] | 0.84 | 0.46 [0.29, 0.59] | 0.46 |
| development | low | 609 | 0.81 [0.76, 0.85] | 0.89 | 0.66 [0.61, 0.70] | 0.25 |
| development | moderate | 58 | 0.63 [0.44, 0.76] | 0.87 | 0.62 [0.49, 0.74] | 0.38 |
| development | high | 53 | 0.54 [0.27, 0.73] | 0.73 | 0.49 [0.33, 0.60] | 0.47 |
| validation | low | 322 | 0.80 [0.73, 0.86] | 0.86 | 0.67 [0.60, 0.73] | 0.24 |
| validation | moderate | 25 | 0.66 [0.53, 0.77] | 0.69 | 0.64 [0.41, 0.81] | 0.20 |
| validation | high | 13 | 0.47 [0.06, 0.82] | 0.82 | 0.46 [0.27, 0.64] | 0.54 |
| validation_excl_2020_to_2021 | low | 258 | 0.80 [0.74, 0.86] | 0.86 | 0.67 [0.61, 0.74] | 0.24 |
| validation_excl_2020_to_2021 | moderate | 20 | 0.66 [0.53, 0.90] | 0.69 | 0.65 [0.41, 0.83] | 0.20 |
| validation_excl_2020_to_2021 | high | 10 | 0.32 [0.02, 0.64] | 0.46 | 0.30 [0.00, 0.58] | 0.70 |
| tight_cohort | low | 616 | 0.83 [0.78, 0.86] | 0.90 | 0.65 [0.60, 0.69] | 0.20 |
| tight_cohort | moderate | 62 | 0.68 [0.61, 0.76] | 0.81 | 0.55 [0.42, 0.66] | 0.27 |
| tight_cohort | high | 42 | 0.61 [0.39, 0.73] | 0.80 | 0.41 [0.22, 0.54] | 0.41 |
| broad_cohort | low | 1118 | 0.80 [0.75, 0.83] | 0.87 | 0.67 [0.64, 0.70] | 0.27 |
| broad_cohort | moderate | 96 | 0.64 [0.54, 0.71] | 0.78 | 0.62 [0.52, 0.72] | 0.35 |
| broad_cohort | high | 74 | 0.46 [0.23, 0.63] | 0.80 | 0.45 [0.33, 0.53] | 0.53 |

#### TE — age by age (primary cohort, 2010→2011 … 2024→2025)

| age | n | median total ret. [95% CI] | mean total ret. | median PPG ret. (returners, n) | relevance survival [95% CI] | severe loss | current band |
|---:|---:|---|---:|---|---|---:|---|
| 20 ⚠ | 2 | 0.95 n/a | 0.95 | 1.10 (2) | 1.00 n/a | 0.50 | low |
| 21 ⚠ | 11 | 1.30 [0.70, 1.80] | 1.27 | 1.06 (11) | 0.82 [0.64, 1.00] | 0.00 | low |
| 22 | 36 | 0.90 [0.45, 1.09] | 0.82 | 1.07 (32) | 0.61 [0.44, 0.75] | 0.39 | low |
| 23 | 47 | 0.81 [0.72, 1.01] | 0.92 | 0.95 (44) | 0.70 [0.57, 0.83] | 0.19 | low |
| 24 | 59 | 0.80 [0.59, 1.07] | 0.88 | 0.84 (58) | 0.68 [0.56, 0.80] | 0.29 | low |
| 25 | 66 | 0.71 [0.63, 0.87] | 0.80 | 0.81 (64) | 0.67 [0.55, 0.77] | 0.27 | low |
| 26 | 66 | 0.72 [0.69, 0.95] | 0.82 | 0.86 (63) | 0.67 [0.55, 0.79] | 0.29 | low |
| 27 | 54 | 0.74 [0.52, 0.93] | 0.76 | 0.88 (54) | 0.69 [0.57, 0.80] | 0.35 | low |
| 28 | 56 | 0.75 [0.56, 0.86] | 0.77 | 0.81 (53) | 0.64 [0.50, 0.77] | 0.32 | low |
| 29 | 42 | 0.56 [0.39, 0.73] | 0.68 | 0.74 (38) | 0.50 [0.36, 0.64] | 0.43 | low |
| 30 | 31 | 0.67 [0.42, 0.89] | 0.65 | 0.93 (25) | 0.52 [0.32, 0.68] | 0.42 | moderate |
| 31 | 21 | 0.79 [0.47, 0.92] | 0.71 | 0.79 (18) | 0.62 [0.43, 0.81] | 0.38 | moderate |
| 32 ⚠ | 14 | 0.81 [0.00, 1.05] | 0.67 | 0.91 (9) | 0.64 [0.43, 0.86] | 0.36 | high |
| 33 ⚠ | 13 | 0.71 [0.63, 0.94] | 0.73 | 0.92 (13) | 0.77 [0.54, 1.00] | 0.23 | high |
| 34 ⚠ | 11 | 0.40 [0.00, 1.00] | 0.47 | 0.93 (8) | 0.36 [0.09, 0.64] | 0.64 | high |
| 35 ⚠ | 3 | 1.00 n/a | 0.70 | 0.98 (2) | 0.67 n/a | 0.33 | high |
| 36 ⚠ | 3 | 0.65 n/a | 0.71 | 0.80 (3) | 1.00 n/a | 0.00 | high |
| 37 ⚠ | 4 | 0.30 n/a | 0.39 | 0.54 (3) | 0.25 n/a | 0.75 | high |
| 38 ⚠ | 1 | 0.00 n/a | 0.00 | — (0) | 0.00 n/a | 1.00 | high |

#### TE — current bands 30/32 across samples

| sample | band | n | median total ret. [95% CI] | median PPG ret. | survival [95% CI] | severe loss |
|---|---|---:|---|---:|---|---:|
| all_2010_2024 | low | 439 | 0.73 [0.70, 0.81] | 0.87 | 0.66 [0.60, 0.71] | 0.30 |
| all_2010_2024 | moderate | 52 | 0.77 [0.47, 0.85] | 0.89 | 0.56 [0.41, 0.69] | 0.40 |
| all_2010_2024 | high | 49 | 0.66 [0.37, 0.75] | 0.92 | 0.59 [0.45, 0.69] | 0.41 |
| recent_2016_2024 | low | 267 | 0.77 [0.68, 0.84] | 0.85 | 0.65 [0.57, 0.72] | 0.31 |
| recent_2016_2024 | moderate | 24 | 0.62 [0.21, 0.79] | 0.92 | 0.50 [0.25, 0.70] | 0.46 |
| recent_2016_2024 | high | 33 | 0.63 [0.37, 0.70] | 0.83 | 0.55 [0.42, 0.67] | 0.46 |
| development | low | 288 | 0.73 [0.68, 0.83] | 0.89 | 0.64 [0.57, 0.69] | 0.32 |
| development | moderate | 37 | 0.79 [0.48, 0.91] | 0.86 | 0.57 [0.39, 0.71] | 0.38 |
| development | high | 35 | 0.71 [0.43, 0.94] | 0.93 | 0.63 [0.47, 0.74] | 0.37 |
| validation | low | 151 | 0.74 [0.68, 0.87] | 0.86 | 0.69 [0.59, 0.77] | 0.28 |
| validation | moderate | 15 | 0.64 [0.21, 0.83] | 0.98 | 0.53 [0.25, 0.78] | 0.47 |
| validation | high | 14 | 0.53 [0.00, 0.71] | 0.85 | 0.50 [0.18, 0.74] | 0.50 |
| validation_excl_2020_to_2021 | low | 122 | 0.75 [0.69, 0.94] | 0.86 | 0.70 [0.60, 0.80] | 0.24 |
| validation_excl_2020_to_2021 | moderate | 11 | 0.43 [0.21, 1.18] | 0.92 | 0.46 [0.18, 0.73] | 0.55 |
| validation_excl_2020_to_2021 | high | 11 | 0.66 [0.00, 0.71] | 0.90 | 0.55 [0.14, 0.80] | 0.46 |
| tight_cohort | low | 287 | 0.77 [0.70, 0.84] | 0.90 | 0.61 [0.55, 0.67] | 0.25 |
| tight_cohort | moderate | 33 | 0.79 [0.64, 0.87] | 0.87 | 0.67 [0.48, 0.82] | 0.30 |
| tight_cohort | high | 40 | 0.70 [0.43, 0.88] | 0.91 | 0.57 [0.43, 0.68] | 0.35 |
| broad_cohort | low | 349 | 0.74 [0.69, 0.83] | 0.86 | 0.66 [0.59, 0.72] | 0.27 |
| broad_cohort | moderate | 45 | 0.74 [0.42, 0.79] | 0.87 | 0.60 [0.44, 0.73] | 0.42 |
| broad_cohort | high | 45 | 0.70 [0.43, 0.85] | 0.91 | 0.58 [0.45, 0.68] | 0.38 |

