#!/usr/bin/env python3
"""
Live-production smoke test for the deployed Ddreportcards service.

This is NOT part of the offline test suite and never runs automatically --
`python -m unittest discover -s tests` (and CI) only ever touch tests/. This
script makes real HTTP requests against a live, already-deployed instance
and is meant to be run by hand, after a deploy, to confirm the service
actually answers the way it's supposed to.

Default mode is read-only and free: liveness, the rich health payload, a
couple of public data endpoints, and that missing/wrong-secret requests
actually get rejected before any real work happens. Nothing here spends
money unless you explicitly pass --billable-player-report.

Required environment:
  GM_SMOKE_BASE_URL   e.g. https://ddreportcards.onrender.com
  GM_CHAT_SECRET      the real X-GM-Key value -- read from the environment
                       only, never printed or logged by this script

Usage:
  python scripts/smoke_production.py
  python scripts/smoke_production.py --billable-player-report 12501
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _env_or_die(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def _request(method: str, url: str, headers=None, body=None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
    except urllib.error.URLError as exc:
        return None, str(exc.reason)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = raw
    return status, parsed


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    line = f"[{mark}] {label}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    return condition


def run_default_smoke(base_url: str, secret: str) -> bool:
    ok = True

    status, _ = _request("GET", f"{base_url}/health")
    ok &= check("GET /health is 200", status == 200, f"got {status}")

    status, body = _request("GET", f"{base_url}/health/deep")
    ok &= check("GET /health/deep is 200", status == 200, f"got {status}")
    ok &= check(
        "GET /health/deep has chat_budget payload",
        isinstance(body, dict) and "chat_budget" in body,
        f"body keys: {list(body) if isinstance(body, dict) else type(body)}",
    )

    status, _ = _request("GET", f"{base_url}/league")
    ok &= check("GET /league (public, read-only) is 200", status == 200, f"got {status}")

    status, _ = _request("GET", f"{base_url}/players/trending")
    ok &= check("GET /players/trending (public, read-only) is 200", status == 200, f"got {status}")

    status, _ = _request(
        "POST", f"{base_url}/chat", body={"model": "x", "max_tokens": 10, "messages": []}
    )
    ok &= check("POST /chat with no key is 401", status == 401, f"got {status}")

    status, _ = _request(
        "POST", f"{base_url}/chat",
        headers={"X-GM-Key": "deliberately-wrong"},
        body={"model": "x", "max_tokens": 10, "messages": []},
    )
    ok &= check("POST /chat with wrong key is 401", status == 401, f"got {status}")

    status, _ = _request("POST", f"{base_url}/report/player/12501")
    ok &= check("POST /report/player with no key is 401 (no billable work triggered)", status == 401, f"got {status}")

    for path in (
        "/internal/memory-profile",
        "/internal/memory-profile-agent",
        "/internal/memory-profile-agent/late",
        "/internal/memory-profile-runner-setup",
    ):
        status, _ = _request("POST", f"{base_url}{path}", headers={"X-GM-Key": secret})
        ok &= check(f"removed profiling route {path} stays gone (404)", status == 404, f"got {status}")

    return ok


def run_billable_player_report(base_url: str, secret: str, sleeper_id: str) -> bool:
    print(f"\n--- Billable report smoke: player {sleeper_id} (real Anthropic spend) ---")
    headers = {"X-GM-Key": secret}

    status_before, before = _request("GET", f"{base_url}/health/deep", headers=headers)
    if status_before != 200 or not isinstance(before, dict):
        print(f"[FAIL] could not read /health/deep before the report (status {status_before})")
        return False
    spent_before = before.get("chat_budget", {}).get("spent_micro")

    t0 = time.perf_counter()
    status, card = _request(
        "POST", f"{base_url}/report/player/{sleeper_id}", headers=headers, timeout=180.0
    )
    duration_s = time.perf_counter() - t0

    status_after, after = _request("GET", f"{base_url}/health/deep", headers=headers)
    spent_after = after.get("chat_budget", {}).get("spent_micro") if isinstance(after, dict) else None

    ok = True
    ok &= check("POST /report/player is 200", status == 200, f"got {status}")
    ok &= check(
        "response has a valid card shape (_detail present, no raw_output fallback)",
        isinstance(card, dict) and "_detail" in card and "raw_output" not in card,
        f"body keys: {list(card) if isinstance(card, dict) else type(card)}",
    )

    risk = card.get("risk_modifier", {}) if isinstance(card, dict) else {}
    ok &= check(
        "risk_modifier.current_health_score is present",
        isinstance(risk, dict) and "current_health_score" in risk,
        f"risk_modifier keys: {list(risk) if isinstance(risk, dict) else type(risk)}",
    )
    ok &= check(
        "risk_modifier has no injury_chance_pct",
        isinstance(risk, dict) and "injury_chance_pct" not in risk,
        f"risk_modifier: {risk}",
    )
    ok &= check(
        "risk_modifier has no durability_score",
        isinstance(risk, dict) and "durability_score" not in risk,
        f"risk_modifier: {risk}",
    )
    market_detail = (card.get("_detail") or {}).get("market") if isinstance(card, dict) else None
    ok &= check(
        "Market stage completed (no error/raw_output fallback in _detail.market)",
        isinstance(market_detail, dict) and "error" not in market_detail and "raw_output" not in market_detail,
        f"_detail.market: {market_detail}",
    )

    ok &= check("post-report /health/deep is 200", status_after == 200, f"got {status_after}")

    print(f"duration_s={duration_s:.1f}")
    print(f"spent_micro_before={spent_before}")
    print(f"spent_micro_after={spent_after}")
    if isinstance(spent_before, int) and isinstance(spent_after, int):
        print(f"spent_micro_delta={spent_after - spent_before}")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--billable-player-report",
        metavar="SLEEPER_ID",
        default=None,
        help="Opt-in: also run one real POST /report/player/<id>. Spends real money.",
    )
    args = parser.parse_args()

    base_url = _env_or_die("GM_SMOKE_BASE_URL").rstrip("/")
    secret = _env_or_die("GM_CHAT_SECRET")

    print(f"Smoke testing {base_url} (secret loaded from GM_CHAT_SECRET, never printed)\n")
    ok = run_default_smoke(base_url, secret)

    if args.billable_player_report:
        ok = run_billable_player_report(base_url, secret, args.billable_player_report) and ok
    else:
        print("\n(skipping billable report smoke -- pass --billable-player-report <sleeper_id> to run it)")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
