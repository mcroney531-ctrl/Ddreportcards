"""Durable, atomic spend budget.

Three defects motivated this, and only the first was about persistence:

  1. ``_budget_used`` was a module global. Render runs this on the free plan,
     which spins down after ~15 minutes idle, so the "daily" budget reset on
     every cold start — a per-uptime-window budget, on an app used in bursts.
  2. Check and record were separate operations. Three concurrent requests at
     1.9M all passed the check and then all three spent. Making the counter
     durable does not fix that; read-total then increment-actual is still
     raceable. The check and the reservation have to be one operation.
  3. The counter was in TOKENS. The endpoint permits several models, input and
     output are priced differently, and the old 2,000,000-token budget was
     worth anywhere between $4.00 and $20.00 depending purely on the mix. It
     was never a spend cap.

So: integer microdollars, reserved atomically before the call and reconciled
to truth after it, in a store that outlives the process.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
from dataclasses import dataclass

import httpx

log = logging.getLogger("gm.budget")

USD = 1_000_000  # microdollars per dollar


# ── Pricing ──────────────────────────────────────────────────────────────────
# Microdollars per 1,000,000 tokens. Exact integers; no float ever touches a
# budget figure.
#
# This table is AUTHORITATIVE. A model that is not priced here cannot be spent
# on, whatever an allowlist env var says — otherwise an operator adds a model
# and it bills against the budget at zero. The chat allowlist may only narrow
# this set, never widen it.
#
# Adding a model is deliberately a paired edit: price it here, then permit it.
MODEL_PRICING: dict[str, dict[str, int]] = {
    # Served to /chat callers.
    "claude-sonnet-5": {"input": 2 * USD, "output": 10 * USD},
    # Not offered to /chat, but the six report-pipeline agents run on it and
    # their spend lands on the same daily key, so it has to be priced.
    "claude-sonnet-4-6": {"input": 3 * USD, "output": 15 * USD},
}


class UnpricedModel(ValueError):
    """Raised for a model the spend system cannot cost."""


def price_of(model: str) -> dict[str, int]:
    try:
        return MODEL_PRICING[model]
    except KeyError:
        raise UnpricedModel(
            f"Model {model!r} has no verified price, so its spend cannot be "
            f"bounded. Priced models: {sorted(MODEL_PRICING)}."
        ) from None


def cost_micro(tokens: int, micro_per_mtok: int) -> int:
    """Cost of ``tokens``, rounded UP.

    Rounding down would under-count spend, which is the one direction this
    must never fail in.
    """
    tokens = max(0, int(tokens))
    return -(-tokens * micro_per_mtok // 1_000_000)


def usage_cost_micro(model: str, input_tokens: int, output_tokens: int) -> int:
    p = price_of(model)
    return cost_micro(input_tokens, p["input"]) + cost_micro(output_tokens, p["output"])


# ── Configuration ────────────────────────────────────────────────────────────

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DAILY_LIMIT_MICRO = _int_env("GM_CHAT_DAILY_USD_MICROS", 5 * USD)  # $5.00/day
KEY_PREFIX = os.environ.get("GM_BUDGET_KEY_PREFIX", "gm:spend:v1")
TTL_SECONDS = _int_env("GM_BUDGET_TTL_SECONDS", 172_800)  # 48h
# Durable is the default. An unset variable must not quietly reinstate the
# restart-resetting counter this module exists to replace — that is the exact
# weakness the work was for, and a /health warning is not a control.
BACKEND = (os.environ.get("GM_BUDGET_BACKEND") or "redis").strip().lower()

UPSTASH_URL = (os.environ.get("GM_UPSTASH_REST_URL") or "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("GM_UPSTASH_REST_TOKEN") or ""


def day_key(now: datetime.datetime | None = None) -> str:
    """The UTC date in the key IS the accounting boundary. TTL is only garbage
    collection, and it is computed app-side so the budget never depends on the
    store's clock."""
    d = (now or datetime.datetime.now(datetime.timezone.utc)).date()
    return f"{KEY_PREFIX}:{d.isoformat()}"


class BudgetUnavailable(RuntimeError):
    """The store is required but unusable. Callers must fail closed."""


class BudgetMisconfigured(BudgetUnavailable):
    """The backend is not a recognised value, or its settings are missing.

    A subclass of BudgetUnavailable so every existing fail-closed path already
    handles it: a service that cannot tell what its budget backend is has no
    business spending money.
    """


class BudgetExhausted(RuntimeError):
    """The day's ceiling would be exceeded."""


@dataclass(frozen=True)
class Reservation:
    """Carries the key it charged.

    A round that reserves at 23:59:58 and settles at 00:00:03 must settle
    against the day it reserved from; settling against "today" would credit
    yesterday's over-reservation to today and silently inflate the allowance.
    """

    key: str
    model: str
    micros: int


# ── Lua ──────────────────────────────────────────────────────────────────────
# Check and increment as ONE operation. Deliberately not INCRBY-then-inspect:
# that inflates the counter for the window before the compensating decrement,
# so concurrent refusals can hold it above the limit at once — and a caller
# that dies before rolling back leaves the inflation until the key expires.

RESERVE_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local res = tonumber(ARGV[1])
local lim = tonumber(ARGV[2])
if res <= 0 then
  return {0, cur}
end
if cur + res > lim then
  return {0, cur}
end
local total = redis.call('INCRBY', KEYS[1], res)
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
end
return {1, total}
"""

# Reconciliation is unconditional: if the money was spent it is recorded, even
# when that pushes the day over its ceiling. The next reservation then fails,
# which is the correct consequence. Only the aggregate is floored at zero.
SETTLE_LUA = """
local total = redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
if total < 0 then
  redis.call('SET', KEYS[1], '0')
  total = 0
end
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return total
"""


class MemoryBudgetStore:
    """Development and test only.

    Selected only by explicit configuration. It is never automatic production
    failover — that would reinstate the restartable spend path this module
    exists to remove.
    """

    durable = False
    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[str, int] = {}

    def reachable(self) -> bool:
        return True

    def reserve(self, key: str, micros: int, limit: int) -> tuple[bool, int]:
        with self._lock:
            cur = self._totals.get(key, 0)
            if micros <= 0:
                return False, cur
            if cur + micros > limit:
                return False, cur
            self._totals[key] = cur + micros
            return True, self._totals[key]

    def settle(self, key: str, delta: int) -> int:
        with self._lock:
            total = self._totals.get(key, 0) + delta
            if total < 0:
                total = 0
            self._totals[key] = total
            return total

    def get(self, key: str) -> int:
        with self._lock:
            return self._totals.get(key, 0)


class UpstashBudgetStore:
    """Upstash Redis over its REST API.

    REST rather than a Redis driver on purpose: ``httpx`` is already a
    dependency, and there is no connection pool to re-establish every time a
    free-plan service wakes from sleep.
    """

    durable = True
    name = "redis"

    def __init__(self, url: str, token: str, timeout: float = 4.0) -> None:
        if not url or not token:
            raise BudgetUnavailable("Upstash URL/token not configured.")
        self._url = url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout
        self._last_error: str | None = None

    def _cmd(self, *args) -> object:
        try:
            r = httpx.post(
                self._url,
                headers=self._headers,
                json=[str(a) for a in args],
                timeout=self._timeout,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as exc:  # noqa: BLE001 — every failure is fail-closed
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise BudgetUnavailable(self._last_error) from exc
        if isinstance(body, dict) and body.get("error"):
            self._last_error = str(body["error"])
            raise BudgetUnavailable(self._last_error)
        self._last_error = None
        return body.get("result") if isinstance(body, dict) else body

    def reachable(self) -> bool:
        try:
            self._cmd("PING")
            return True
        except BudgetUnavailable:
            return False

    def reserve(self, key: str, micros: int, limit: int) -> tuple[bool, int]:
        res = self._cmd("EVAL", RESERVE_LUA, 1, key, micros, limit, TTL_SECONDS)
        granted, total = int(res[0]), int(res[1])
        return bool(granted), total

    def settle(self, key: str, delta: int) -> int:
        return int(self._cmd("EVAL", SETTLE_LUA, 1, key, delta, TTL_SECONDS))

    def get(self, key: str) -> int:
        v = self._cmd("GET", key)
        return int(v or 0)

    @property
    def last_error(self) -> str | None:
        return self._last_error


_store = None
_store_lock = threading.Lock()


def store():
    """The configured store, or an exception.

    Deliberately no fallback path. Falling back from redis to memory on a
    missing variable or a typo would turn a configuration mistake into a
    silently unmetered service, which is the failure this whole module is
    built to prevent.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                if BACKEND == "redis":
                    if not UPSTASH_URL or not UPSTASH_TOKEN:
                        raise BudgetMisconfigured(
                            "GM_BUDGET_BACKEND=redis but GM_UPSTASH_REST_URL / "
                            "GM_UPSTASH_REST_TOKEN are not set."
                        )
                    _store = UpstashBudgetStore(UPSTASH_URL, UPSTASH_TOKEN)
                elif BACKEND == "memory":
                    # Development and tests only, and only when asked for by name.
                    _store = MemoryBudgetStore()
                else:
                    raise BudgetMisconfigured(
                        f"GM_BUDGET_BACKEND={BACKEND!r} is not a known backend "
                        f"(expected 'redis' or 'memory')."
                    )
    return _store


def _set_store_for_tests(s) -> None:
    global _store
    _store = s


# ── Public API ───────────────────────────────────────────────────────────────

def reserve(model: str, input_tokens: int, max_tokens: int,
            now: datetime.datetime | None = None) -> Reservation:
    """Atomically reserve an UPPER BOUND for one model round.

    Input is counted, not guessed. Output is reserved at the full permitted
    ``max_tokens``, so the reservation can only ever be too large — which is
    the safe direction, and why an actual above it is logged loudly.

    Raises BudgetExhausted (-> 429) or BudgetUnavailable (-> 503).
    """
    p = price_of(model)  # unpriced model raises before anything is reserved
    micros = cost_micro(input_tokens, p["input"]) + cost_micro(max_tokens, p["output"])
    key = day_key(now)
    granted, total = store().reserve(key, micros, DAILY_LIMIT_MICRO)
    if not granted:
        raise BudgetExhausted(
            f"Daily spend budget for this service is spent "
            f"({total / USD:.2f} of {DAILY_LIMIT_MICRO / USD:.2f} USD). "
            f"It resets at 00:00 UTC."
        )
    return Reservation(key=key, model=model, micros=micros)


def settle(res: Reservation, input_tokens: int, output_tokens: int) -> int:
    """Reconcile a reservation to what was actually spent."""
    actual = usage_cost_micro(res.model, input_tokens, output_tokens)
    delta = actual - res.micros
    if delta > 0:
        # The reservation is meant to be an upper bound, so this should not
        # happen. It is recorded in full anyway — the money left the account,
        # and accounting has to reflect that even if it puts the day over its
        # ceiling. Subsequent reservations then fail, which is correct.
        log.warning(
            "budget under-reservation: model=%s reserved=%d actual=%d over=%d",
            res.model, res.micros, actual, delta,
        )
    return store().settle(res.key, delta)


def release(res: Reservation) -> int:
    """Give back a reservation for a call that provably never reached the
    provider. Only for definite pre-send failures; an ambiguous network error
    leaves the reservation charged."""
    return store().settle(res.key, -res.micros)


def record_actual(model: str, input_tokens: int, output_tokens: int,
                  now: datetime.datetime | None = None) -> int:
    """Record spend that could not be reserved in advance.

    The report agents run inside ADK, which calls the model itself and reports
    usage afterwards, so there is no point at which this process could reserve.
    Their spend still consumes the same daily key, so later reservations see it.
    """
    return store().settle(day_key(now), usage_cost_micro(model, input_tokens, output_tokens))


def has_room(now: datetime.datetime | None = None) -> bool:
    """Cheap 'is there anything left today' check for paths that cannot reserve."""
    return store().get(day_key(now)) < DAILY_LIMIT_MICRO


def status(now: datetime.datetime | None = None) -> dict:
    """Safe to expose.

    Classifications only. The underlying exception is NOT included: it comes
    from an HTTP client and can carry the Upstash endpoint in its message, so
    the detail is logged server-side and the caller gets a code.
    """
    out = {
        "backend": BACKEND,
        "configured": bool(UPSTASH_URL and UPSTASH_TOKEN) if BACKEND == "redis" else True,
        "day": day_key(now).rsplit(":", 1)[-1],
        "limit_micro": DAILY_LIMIT_MICRO,
        "limit_usd": round(DAILY_LIMIT_MICRO / USD, 2),
    }
    try:
        s = store()
    except BudgetUnavailable as exc:
        # Must still render: /health is how a misconfiguration gets noticed.
        log.error("budget store unavailable: %s", exc)
        out.update(durable=(BACKEND == "redis"), reachable=False,
                   error="budget_store_unavailable")
        return out

    out["durable"] = s.durable
    try:
        spent = s.get(day_key(now))
        out.update(reachable=True, spent_micro=spent,
                   remaining_micro=max(0, DAILY_LIMIT_MICRO - spent))
    except BudgetUnavailable as exc:
        log.error("budget store unreachable: %s", exc)
        out.update(reachable=False, error="budget_store_unavailable")
    return out
