#!/usr/bin/env python3
"""
Stage 3D-1 age-curve calibration study (research only — changes no runtime code).

Question: are the runtime aging thresholds (agents/production_agent.py
AGE_CURVES) and penalties (agents/market_agent.py AGING_RISK_PENALTY)
defensible for their two actual jobs — the small hybrid_market_value overlay
and the binary sell_high trigger?

Data: nflverse regular-season player stats and player birth dates
(https://github.com/nflverse/nflverse-data). Standard library only; nothing
here is a runtime dependency. Raw downloads go to research/.cache/
(gitignored); derived summaries go to research/output/.

Usage (from the repository root):
  python research/age_curve_analysis.py           # historical study
  python research/age_curve_analysis.py --live    # + current-player impact
      (needs network access to api.sleeper.app and api.fantasycalc.com;
       free endpoints, no model calls)

Deterministic: fixed bootstrap seed, fixed season window, and the runtime
constants are read from source with ast (not re-typed), so the study always
evaluates what production actually uses.
"""
import argparse
import ast
import csv
import datetime
import hashlib
import json
import math
import pathlib
import random
import statistics
import sys
import urllib.request
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "research" / ".cache"
OUT = ROOT / "research" / "output"

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
STATS_URL = NFLVERSE + "/stats_player/stats_player_reg_{season}.csv"
PLAYERS_URL = NFLVERSE + "/players/players.csv"

FIRST_T, LAST_T = 2010, 2024          # season t; outcome season t+1 (last pair 2024 -> 2025)
RECENT_FIRST_T = 2016                 # recent-era sensitivity: 2016->2017 .. 2024->2025
DEV_T = range(2010, 2020)             # development: 2010->2011 .. 2019->2020
VAL_T = range(2020, 2025)             # validation:  2020->2021 .. 2024->2025
POSITIONS = ("QB", "RB", "WR", "TE")
PRIMARY_CAP = {"QB": 36, "RB": 60, "WR": 72, "TE": 36}
TIGHT_CAP = {"QB": 24, "RB": 36, "WR": 48, "TE": 24}
BROAD_MIN_GAMES, BROAD_MIN_PPG = 8, 5.0   # broad participation cohort (not rank-based)
SEVERE_LOSS = 0.5                     # descriptive: next-year total <= 50% of season t
GRID = {"RB": ((25, 29), (27, 31)), "WR": ((27, 32), (29, 34)),
        "TE": ((27, 32), (29, 34)), "QB": ((32, 38), (35, 41))}
MIN_BAND_N = 20                       # a candidate needs this many dev observations per band
SEED, BOOT = 3, 1000


# ── runtime constants, read (not imported) from source ────────────────────────

def _literal(path: pathlib.Path, name: str):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(f"{name} not found in {path}")


AGE_CURVES = _literal(ROOT / "agents" / "production_agent.py", "AGE_CURVES")
AGING_RISK_PENALTY = _literal(ROOT / "agents" / "market_agent.py", "AGING_RISK_PENALTY")
MAX_BLEND_ADJUSTMENT = _literal(ROOT / "agents" / "market_agent.py", "MAX_BLEND_ADJUSTMENT")
SELL_HIGH_MARKET_FLOOR = _literal(ROOT / "agents" / "trade_agent.py", "SELL_HIGH_MARKET_FLOOR")


def band(age: int, decline: int, cliff: int) -> str:
    """Same boundaries as compute_age_curve_signal: < decline low, < cliff moderate, else high."""
    return "low" if age < decline else "moderate" if age < cliff else "high"


# ── data ──────────────────────────────────────────────────────────────────────

def fetch(url: str, dest: pathlib.Path, manifest: list) -> pathlib.Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as r:
            dest.write_bytes(r.read())
    data = dest.read_bytes()
    manifest.append({"url": url, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                     "fetched_utc": datetime.datetime.fromtimestamp(dest.stat().st_mtime, datetime.timezone.utc)
                     .strftime("%Y-%m-%d %H:%M")})
    return dest


def load(manifest: list):
    births = {}
    with open(fetch(PLAYERS_URL, CACHE / "players.csv", manifest), newline="") as f:
        for r in csv.DictReader(f):
            if r["gsis_id"] and r["birth_date"]:
                births[r["gsis_id"]] = datetime.date.fromisoformat(r["birth_date"][:10])
    seasons = {}
    for s in range(FIRST_T, LAST_T + 2):
        rows = {}
        with open(fetch(STATS_URL.format(season=s), CACHE / f"stats_player_reg_{s}.csv", manifest), newline="") as f:
            for r in csv.DictReader(f):
                if not r["player_id"] or r["season_type"] != "REG" or r["position"] not in POSITIONS:
                    continue  # blank-id aggregate rows, non-REG, non-skill positions
                games = int(float(r["games"] or 0))
                half = float(r["fantasy_points"] or 0) + 0.5 * float(r["receptions"] or 0)
                rows[r["player_id"]] = {"id": r["player_id"], "name": r["player_display_name"],
                                        "pos": r["position"], "games": games, "half": half}
        for pos in POSITIONS:  # half-PPR position rank within the season
            ranked = sorted((p for p in rows.values() if p["pos"] == pos), key=lambda p: (-p["half"], p["id"]))
            for i, p in enumerate(ranked, 1):
                p["rank"] = i
        seasons[s] = rows
    return births, seasons


def age_on_sep1(birth: datetime.date, season: int) -> int:
    ref = datetime.date(season, 9, 1)
    return ref.year - birth.year - ((ref.month, ref.day) < (birth.month, birth.day))


def in_cohort(p: dict, cohort: str) -> bool:
    if cohort == "broad":
        return p["games"] >= BROAD_MIN_GAMES and p["half"] / p["games"] >= BROAD_MIN_PPG
    cap = (PRIMARY_CAP if cohort == "primary" else TIGHT_CAP)[p["pos"]]
    return p["rank"] <= cap and p["half"] > 0


def observations(births, seasons, cohort: str):
    """One row per (player, season t) in the cohort, with t+1 outcomes. Absent in t+1 = 0."""
    obs, missing_birth = [], defaultdict(int)
    for t in range(FIRST_T, LAST_T + 1):
        nxt = seasons[t + 1]
        for p in seasons[t].values():
            if not in_cohort(p, cohort):
                continue
            if p["id"] not in births:
                missing_birth[p["pos"]] += 1
                continue
            q = nxt.get(p["id"])
            next_half = q["half"] if q else 0.0
            returner = bool(q and q["games"] > 0 and p["games"] > 0)
            obs.append({
                "id": p["id"], "name": p["name"], "pos": p["pos"], "t": t,
                "age": age_on_sep1(births[p["id"]], t),
                "total_ret": next_half / p["half"],
                "ppg_ret": (q["half"] / q["games"]) / (p["half"] / p["games"]) if returner and p["half"] > 0 else None,
                "survive": bool(q and q["pos"] == p["pos"] and in_cohort(q, cohort)),
                "severe": next_half <= SEVERE_LOSS * p["half"],
            })
    return obs, dict(missing_birth)


# ── statistics ────────────────────────────────────────────────────────────────

def cluster_boot(rows, stat, seed=SEED, n=BOOT):
    """95% percentile interval, resampling players (clusters), not rows."""
    by_player = defaultdict(list)
    for r in rows:
        by_player[r["id"]].append(r)
    clusters = list(by_player.values())
    if len(clusters) < 5:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        sample = [r for _ in clusters for r in rng.choice(clusters)]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    vals.sort()
    return (round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3))


def med_ret(rows):
    return statistics.median(r["total_ret"] for r in rows) if rows else None


def surv(rows):
    return sum(r["survive"] for r in rows) / len(rows) if rows else None


def summarize(rows, with_ci=True):
    ppg = [r["ppg_ret"] for r in rows if r["ppg_ret"] is not None]
    s = {
        "n": len(rows), "players": len({r["id"] for r in rows}),
        "median_total_ret": round(med_ret(rows), 3) if rows else None,
        "mean_total_ret": round(statistics.fmean(r["total_ret"] for r in rows), 3) if rows else None,
        "returners": len(ppg),
        "median_ppg_ret_returners": round(statistics.median(ppg), 3) if ppg else None,
        "survival": round(surv(rows), 3) if rows else None,
        "severe_loss": round(sum(r["severe"] for r in rows) / len(rows), 3) if rows else None,
    }
    if with_ci and rows:
        s["median_total_ret_ci"] = cluster_boot(rows, med_ret)
        s["survival_ci"] = cluster_boot(rows, surv)
    return s


def brier(rows, probs, outcome, d, h):
    return statistics.fmean((probs[band(r["age"], d, h)] - r[outcome]) ** 2 for r in rows)


def band_probs(rows, outcome, d, h):
    out = {}
    for b in ("low", "moderate", "high"):
        sub = [r[outcome] for r in rows if band(r["age"], d, h) == b]
        out[b] = sum(sub) / len(sub) if sub else None
    return out


def evaluate(rows, d, h):
    return {b: summarize([r for r in rows if band(r["age"], d, h) == b]) for b in ("low", "moderate", "high")}


def candidate_search(dev, pos):
    """Step-model fit on development data: choose (decline, cliff) minimising the
    Brier score for severe loss, subject to monotone risk and MIN_BAND_N per band."""
    (d_lo, d_hi), (h_lo, h_hi) = GRID[pos]
    results = []
    for d in range(d_lo, d_hi + 1):
        for h in range(h_lo, h_hi + 1):
            if h <= d:
                continue
            counts = {b: sum(band(r["age"], d, h) == b for r in dev) for b in ("low", "moderate", "high")}
            if min(counts.values()) < MIN_BAND_N:
                continue
            p = band_probs(dev, "severe", d, h)
            if not (p["low"] <= p["moderate"] <= p["high"]):
                continue
            results.append({"decline": d, "cliff": h, "counts": counts,
                            "dev_brier_severe": round(brier(dev, p, "severe", d, h), 5),
                            "dev_severe_by_band": {k: round(v, 3) for k, v in p.items()}})
    results.sort(key=lambda c: c["dev_brier_severe"])
    return results


def validation_compare(dev, val, current, cand):
    """Out-of-sample: probabilities from dev bands, scored on validation rows.
    Returns Brier scores and a player-cluster bootstrap CI of (candidate - current)
    (negative = candidate better)."""
    out = {}
    for outcome in ("severe", "survive"):
        pc, pk = band_probs(dev, outcome, *current), band_probs(dev, outcome, *cand)
        p0 = sum(r[outcome] for r in dev) / len(dev)
        def diff(rows):
            return brier(rows, pk, outcome, *cand) - brier(rows, pc, outcome, *current)

        def vs_no_age(rows):
            return brier(rows, pc, outcome, *current) - statistics.fmean((p0 - r[outcome]) ** 2 for r in rows)
        out[outcome] = {
            "no_age_model": round(statistics.fmean((p0 - r[outcome]) ** 2 for r in val), 5),
            "current": round(brier(val, pc, outcome, *current), 5),
            "candidate": round(brier(val, pk, outcome, *cand), 5),
            "current_minus_no_age_ci": cluster_boot(val, vs_no_age),
        }
        if cand != current:
            out[outcome]["candidate_minus_current_ci"] = cluster_boot(val, diff)
    return out


def markdown_tables(results) -> str:
    """Generated tables for docs/age-curve-calibration-study.md (derived data only)."""
    def ci(x):
        return f"[{x[0]:.2f}, {x[1]:.2f}]" if x else "n/a"

    def f(x, nd=2):
        return "—" if x is None else f"{x:.{nd}f}"
    lines = []
    for pos, e in results["positions"].items():
        d, h = e["current"]
        lines += [f"#### {pos} — age by age (primary cohort, 2010→2011 … 2024→2025)", "",
                  "| age | n | median total ret. [95% CI] | mean total ret. | median PPG ret. (returners, n) "
                  "| relevance survival [95% CI] | severe loss | current band |",
                  "|---:|---:|---|---:|---|---|---:|---|"]
        for age, s in e["by_age_primary"].items():
            thin = " ⚠" if s["n"] < 20 else ""
            lines.append(f"| {age}{thin} | {s['n']} | {f(s['median_total_ret'])} {ci(s.get('median_total_ret_ci'))} "
                         f"| {f(s['mean_total_ret'])} | {f(s['median_ppg_ret_returners'])} ({s['returners']}) "
                         f"| {f(s['survival'])} {ci(s.get('survival_ci'))} | {f(s['severe_loss'])} "
                         f"| {band(int(age), d, h)} |")
        lines += ["", f"#### {pos} — current bands {d}/{h} across samples", "",
                  "| sample | band | n | median total ret. [95% CI] | median PPG ret. | survival [95% CI] | severe loss |",
                  "|---|---|---:|---|---:|---|---:|"]
        for sample, bands in e["current_bands"].items():
            for b, s in bands.items():
                lines.append(f"| {sample} | {b} | {s['n']} | {f(s['median_total_ret'])} {ci(s.get('median_total_ret_ci'))} "
                             f"| {f(s['median_ppg_ret_returners'])} | {f(s['survival'])} {ci(s.get('survival_ci'))} "
                             f"| {f(s['severe_loss'])} |")
        lines.append("")
    return "\n".join(lines)


# ── live current-player impact (optional; needs Sleeper + FantasyCalc access) ─

def live_impact(candidates: dict) -> dict:
    """Classify today's FantasyCalc-relevant QB/RB/WR/TE by Sleeper age under the
    current and candidate thresholds, and list who enters/leaves the age-driven
    sell_high set (high AND trade_value_score >= SELL_HIGH_MARKET_FLOOR).
    trade_value_score follows the runtime math: position percentile of FantasyCalc
    dynasty value, rounded half-up (agents/market_agent.py)."""
    def get(url):
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.load(r)
    sleeper = get("https://api.sleeper.app/v1/players/nfl")
    fc = get("https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=2&numTeams=12&ppr=0.5")
    by_pos = defaultdict(list)
    for e in fc:
        by_pos[e["player"].get("position")].append(e["value"])
    for vals in by_pos.values():
        vals.sort(reverse=True)
    report = {"sleeper_missing_age": {}, "fc_missing_age": {}, "counts": {}, "changes": {}}
    for pos in POSITIONS:
        pop = [p for p in sleeper.values() if p.get("position") == pos and p.get("active")]
        report["sleeper_missing_age"][pos] = {"active": len(pop), "missing_age": sum(p.get("age") is None for p in pop)}
    pool = []
    for e in fc:
        pos, sid = e["player"].get("position"), str(e["player"].get("sleeperId") or "")
        if pos not in POSITIONS:
            continue
        vals = by_pos[pos]
        rank = vals.index(e["value"])
        pct = round((1 - rank / max(len(vals) - 1, 1)) * 100, 1)
        age = (sleeper.get(sid) or {}).get("age")
        pool.append({"name": e["player"].get("name"), "pos": pos, "age": age,
                     "trade_value_score": int(math.floor(pct + 0.5))})
    for pos in POSITIONS:
        rows = [p for p in pool if p["pos"] == pos]
        report["fc_missing_age"][pos] = {"pool": len(rows), "missing_age": sum(p["age"] is None for p in rows)}
    sets = {}
    for label, curves in {"current": AGE_CURVES, **candidates}.items():
        counts = {}
        for pos in POSITIONS:
            c = curves[pos]
            rows = [p for p in pool if p["pos"] == pos]
            bands = [band(p["age"], c["decline_age"], c["cliff_age"]) if p["age"] is not None else "unknown" for p in rows]
            counts[pos] = {b: bands.count(b) for b in ("low", "moderate", "high", "unknown")}
            counts[pos]["high_and_floor"] = sum(b == "high" and p["trade_value_score"] >= SELL_HIGH_MARKET_FLOOR
                                                for b, p in zip(bands, rows))
        report["counts"][label] = counts
        sets[label] = {p["name"] for p in pool if p["age"] is not None
                       and band(p["age"], curves[p["pos"]]["decline_age"], curves[p["pos"]]["cliff_age"]) == "high"
                       and p["trade_value_score"] >= SELL_HIGH_MARKET_FLOOR}
    for label in candidates:
        report["changes"][label] = {"enter": sorted(sets[label] - sets["current"]),
                                    "leave": sorted(sets["current"] - sets[label])}
    return report


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run the current-player impact analysis")
    args = ap.parse_args()

    manifest = []
    births, seasons = load(manifest)
    results = {"generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"),
               "sources": manifest, "runtime_constants": {
                   "AGE_CURVES": AGE_CURVES, "AGING_RISK_PENALTY": AGING_RISK_PENALTY,
                   "MAX_BLEND_ADJUSTMENT": MAX_BLEND_ADJUSTMENT, "SELL_HIGH_MARKET_FLOOR": SELL_HIGH_MARKET_FLOOR},
               "cohorts": {}, "positions": {}}

    cohort_obs = {}
    for cohort in ("primary", "tight", "broad"):
        obs, missing = observations(births, seasons, cohort)
        cohort_obs[cohort] = obs
        results["cohorts"][cohort] = {"observations": len(obs), "missing_birth_date": missing,
                                      "by_position": {p: sum(o["pos"] == p for o in obs) for p in POSITIONS}}

    OUT.mkdir(parents=True, exist_ok=True)
    live_candidates = {}
    for pos in POSITIONS:
        cur = (AGE_CURVES[pos]["decline_age"], AGE_CURVES[pos]["cliff_age"])
        prim = [o for o in cohort_obs["primary"] if o["pos"] == pos]
        dev = [o for o in prim if o["t"] in DEV_T]
        val = [o for o in prim if o["t"] in VAL_T]
        by_age = {}
        for age in sorted({o["age"] for o in prim}):
            by_age[age] = summarize([o for o in prim if o["age"] == age])
        cands = candidate_search(dev, pos)
        best = (cands[0]["decline"], cands[0]["cliff"]) if cands else None
        cur_counts = {b: sum(band(o["age"], *cur) == b for o in dev) for b in ("low", "moderate", "high")}
        cur_ok = min(cur_counts.values()) >= MIN_BAND_N
        cur_rank = next((i for i, c in enumerate(cands, 1) if (c["decline"], c["cliff"]) == cur), None)
        entry = {
            "current": cur,
            "by_age_primary": by_age,
            "current_bands": {
                "all_2010_2024": evaluate(prim, *cur),
                "recent_2016_2024": evaluate([o for o in prim if o["t"] >= RECENT_FIRST_T], *cur),
                "development": evaluate(dev, *cur),
                "validation": evaluate(val, *cur),
                "validation_excl_2020_to_2021": evaluate([o for o in val if o["t"] != 2020], *cur),
                "tight_cohort": evaluate([o for o in cohort_obs["tight"] if o["pos"] == pos], *cur),
                "broad_cohort": evaluate([o for o in cohort_obs["broad"] if o["pos"] == pos], *cur),
            },
            "current_dev_band_counts": cur_counts,
            "current_meets_min_band_n": cur_ok,
            "current_rank_among_valid_candidates": cur_rank,
            "valid_candidates": len(cands),
            "top_candidates_dev": cands[:5],
        }
        if best and best != cur:
            entry["best_dev_candidate"] = best
            entry["best_candidate_bands"] = {
                "all_2010_2024": evaluate(prim, *best),
                "validation": evaluate(val, *best),
                "tight_cohort": evaluate([o for o in cohort_obs["tight"] if o["pos"] == pos], *best),
                "recent_2016_2024": evaluate([o for o in prim if o["t"] >= RECENT_FIRST_T], *best),
            }
            entry["validation_brier"] = validation_compare(dev, val, cur, best)
            live_candidates.setdefault(f"best_dev_{pos}", {**AGE_CURVES, pos: {"decline_age": best[0], "cliff_age": best[1]}})
        else:
            entry["validation_brier"] = validation_compare(dev, val, cur, cur)
        results["positions"][pos] = entry

        with open(OUT / f"age_table_{pos}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["age", "n", "players", "median_total_ret", "median_total_ret_ci_lo", "median_total_ret_ci_hi",
                        "mean_total_ret", "returners", "median_ppg_ret_returners", "survival",
                        "survival_ci_lo", "survival_ci_hi", "severe_loss", "current_band"])
            for age, s in by_age.items():
                mci, sci = s.get("median_total_ret_ci") or (None, None), s.get("survival_ci") or (None, None)
                w.writerow([age, s["n"], s["players"], s["median_total_ret"], *mci, s["mean_total_ret"],
                            s["returners"], s["median_ppg_ret_returners"], s["survival"], *sci,
                            s["severe_loss"], band(age, *cur)])

    results["penalty_sensitivity"] = {
        "hybrid_pct_per_composite_point": MAX_BLEND_ADJUSTMENT,
        "note": "hybrid multiplier = 1 + (composite - percentile)/100 * MAX_BLEND_ADJUSTMENT; a penalty of k "
                "composite points moves hybrid by k * 0.25 % (all else equal, before the 0..100 composite clamp).",
        "moderate": {str(k): round(k * MAX_BLEND_ADJUSTMENT, 2) for k in (-3, -5, -8)},
        "high": {str(k): round(k * MAX_BLEND_ADJUSTMENT, 2) for k in (-8, -12, -16)},
    }

    if args.live:
        try:
            results["live_impact"] = live_impact(live_candidates)
        except Exception as exc:  # noqa: BLE001 — report, never fabricate
            results["live_impact"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        results["live_impact"] = {"skipped": "run with --live (needs api.sleeper.app and api.fantasycalc.com)"}

    (OUT / "age_curve_results.json").write_text(json.dumps(results, indent=1, default=str))
    (OUT / "age_tables.md").write_text(markdown_tables(results) + "\n")
    print(f"wrote {OUT / 'age_curve_results.json'}, age_tables.md and age_table_*.csv")


if __name__ == "__main__":
    sys.exit(main())
