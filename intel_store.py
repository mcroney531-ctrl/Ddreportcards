"""League Intel persistence: the user's notes on leaguemates, stored server-side.

GM Command kept intel only in browser localStorage, so a cleared browser or a
second device lost it. This stores it in the same Upstash Redis the spend
budget uses, under its own fixed key prefix, so it survives both.

Contract (agreed before implementation):

* Three independent kinds of data per leaguemate (Sleeper owner_id):
    - name:        the person's real name, user-edited
    - breakdown:   the AI-synthesized tendencies summary, plus `covers`, the
                   observation ids it was synthesized from
    - observations: add-only, individually identified source notes
* Ordering and conflicts use a server revision counter, never device clocks.
  Name and breakdown writes are compare-and-set against the revision the
  client last saw (base_rev); a stale write is refused with the current value
  rather than overwriting it.
* "Never set" and "deliberately empty" are different states: a field that has
  never been written has no *_rev field at all (reported as rev 0 / null);
  clearing a name stores "" with a real revision.
* Every revision is taken inside the same Lua script that writes the data it
  belongs to, so a refused, duplicate or failed write consumes no revision
  and no write can land without one.
* An observation id is written once (HSETNX semantics). Re-sending the same
  id with the same content is an idempotent success; different content under
  an existing id is a conflict, never an overwrite.

Key isolation: every key is built here, from INTEL_PREFIX and a validated
owner id or the fixed names below. Nothing outside this module passes a key
in. INTEL_PREFIX is a constant, not an environment setting, and store()
refuses to run if the budget's prefix or keepalive key could overlap it.

Logging: owner ids, operation names, outcomes and revisions only. Names,
breakdowns and observation text never reach a log line, and storage errors
are logged by exception class only.
"""
from __future__ import annotations

import logging
import os
import re
import threading

import httpx

log = logging.getLogger("gm.intel")

INTEL_PREFIX = "gm:intel:v1:"
SCHEMA = 1

META_KEY = INTEL_PREFIX + "meta"          # exists once anything has been written
REV_KEY = INTEL_PREFIX + "rev"            # the server revision counter
OWNERS_KEY = INTEL_PREFIX + "owners"      # set of owner ids that have records

OWNER_ID_RE = re.compile(r"^\d{1,32}$")
OBS_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MAX_OBSERVATIONS = 500


def lm_key(owner_id: str) -> str:
    _require_owner(owner_id)
    return f"{INTEL_PREFIX}lm:{owner_id}"


def obs_key(owner_id: str) -> str:
    _require_owner(owner_id)
    return f"{INTEL_PREFIX}obs:{owner_id}"


def _require_owner(owner_id: str) -> None:
    if not isinstance(owner_id, str) or not OWNER_ID_RE.fullmatch(owner_id):
        raise ValueError("invalid owner id")


class IntelUnavailable(RuntimeError):
    """The store could not be reached or answered with an error."""


class IntelMisconfigured(IntelUnavailable):
    """The store must not run: missing config or a key-space overlap."""


# ── Lua: each script bumps the revision and writes its data atomically ──────
#
# KEYS[1]=meta KEYS[2]=rev KEYS[3]=owners, then the owner's keys.
# Returns are flat arrays: {status, rev, ...}. Values are stored as strings;
# an observation is stored as "<rev>:<date_label>\n<text>" so no JSON library
# is needed inside Redis.

SET_FIELD_LUA = """
local cur = tonumber(redis.call('HGET', KEYS[4], ARGV[1] .. '_rev') or '0')
if cur ~= tonumber(ARGV[3]) then
  return {0, cur, redis.call('HGET', KEYS[4], ARGV[1]) or ''}
end
local rev = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[4], ARGV[1], ARGV[2], ARGV[1] .. '_rev', rev)
redis.call('SETNX', KEYS[1], ARGV[4])
redis.call('SADD', KEYS[3], ARGV[5])
return {1, rev}
"""

# ARGV: value, base_rev, covers (space-separated ids), meta, owner_id
SET_BREAKDOWN_LUA = """
local cur = tonumber(redis.call('HGET', KEYS[4], 'breakdown_rev') or '0')
if cur ~= tonumber(ARGV[2]) then
  return {0, cur, redis.call('HGET', KEYS[4], 'breakdown') or '',
          redis.call('HGET', KEYS[4], 'breakdown_covers') or ''}
end
for id in string.gmatch(ARGV[3], '%S+') do
  if redis.call('HEXISTS', KEYS[5], id) == 0 then
    return {-1, cur, id}
  end
end
local rev = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[4], 'breakdown', ARGV[1], 'breakdown_rev', rev,
           'breakdown_covers', ARGV[3])
redis.call('SETNX', KEYS[1], ARGV[4])
redis.call('SADD', KEYS[3], ARGV[5])
return {1, rev}
"""

# ARGV: obs_id, "<date_label>\n<text>", max, meta, owner_id
ADD_OBS_LUA = """
local existing = redis.call('HGET', KEYS[5], ARGV[1])
if existing then
  local sep = string.find(existing, ':', 1, true)
  local rev = tonumber(string.sub(existing, 1, sep - 1))
  if string.sub(existing, sep + 1) == ARGV[2] then
    return {2, rev}
  end
  return {0, rev}
end
if redis.call('HLEN', KEYS[5]) >= tonumber(ARGV[3]) then
  return {-1, 0}
end
local rev = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[5], ARGV[1], rev .. ':' .. ARGV[2])
redis.call('SETNX', KEYS[1], ARGV[4])
redis.call('SADD', KEYS[3], ARGV[5])
return {1, rev}
"""

# Consistent snapshot of everything. KEYS[1]=meta KEYS[2]=rev, then pairs of
# (lm key, obs key) in the order of ARGV owner ids.
READ_ALL_LUA = """
local out = {redis.call('EXISTS', KEYS[1]), tonumber(redis.call('GET', KEYS[2]) or '0')}
local n = 0
for i = 3, #KEYS, 2 do
  n = n + 1
  table.insert(out, ARGV[n])
  table.insert(out, redis.call('HGETALL', KEYS[i]))
  table.insert(out, redis.call('HGETALL', KEYS[i + 1]))
end
return out
"""


def _meta_value() -> str:
    return f'{{"schema":{SCHEMA}}}'


class IntelStore:
    """Intel operations over a Redis command callable: cmd(*args) -> result.

    In production cmd is UpstashTransport; tests drive a real local Redis, so
    the Lua above is what is tested, not a Python imitation of it.
    """

    def __init__(self, cmd) -> None:
        self._cmd = cmd

    def _call(self, op: str, *args):
        try:
            return self._cmd(*args)
        except IntelUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure is fail-closed
            log.warning("intel_store_error op=%s error=%s", op, type(exc).__name__)
            raise IntelUnavailable(type(exc).__name__) from exc

    def _eval(self, op: str, script: str, keys: list[str], args: list) -> list:
        res = self._call(op, "EVAL", script, len(keys), *keys, *[str(a) for a in args])
        if not isinstance(res, list) or not res:
            raise IntelUnavailable("unexpected store reply")
        return res

    # ── writes ──
    def set_name(self, owner_id: str, value: str, base_rev: int) -> dict:
        res = self._eval("set_name", SET_FIELD_LUA,
                         [META_KEY, REV_KEY, OWNERS_KEY, lm_key(owner_id)],
                         ["name", value, int(base_rev), _meta_value(), owner_id])
        if int(res[0]) == 1:
            return {"ok": True, "rev": int(res[1])}
        return {"ok": False, "current": _field(res[2], int(res[1]))}

    def set_breakdown(self, owner_id: str, value: str, base_rev: int, covers: list[str]) -> dict:
        for oid in covers:
            if not OBS_ID_RE.fullmatch(oid):
                raise ValueError("invalid observation id in covers")
        res = self._eval("set_breakdown", SET_BREAKDOWN_LUA,
                         [META_KEY, REV_KEY, OWNERS_KEY, lm_key(owner_id), obs_key(owner_id)],
                         [value, int(base_rev), " ".join(covers), _meta_value(), owner_id])
        status = int(res[0])
        if status == 1:
            return {"ok": True, "rev": int(res[1])}
        if status == -1:
            return {"ok": False, "unknown_observation": _s(res[2])}
        cur_rev = int(res[1])
        current = _field(res[2], cur_rev)
        if current is not None:
            current["covers"] = _s(res[3]).split()
        return {"ok": False, "current": current}

    def add_observation(self, owner_id: str, obs_id: str, text: str, date_label: str) -> dict:
        if not OBS_ID_RE.fullmatch(obs_id):
            raise ValueError("invalid observation id")
        payload = f"{date_label}\n{text}"
        res = self._eval("add_observation", ADD_OBS_LUA,
                         [META_KEY, REV_KEY, OWNERS_KEY, lm_key(owner_id), obs_key(owner_id)],
                         [obs_id, payload, MAX_OBSERVATIONS, _meta_value(), owner_id])
        status = int(res[0])
        if status == 1:
            return {"ok": True, "created": True, "rev": int(res[1])}
        if status == 2:
            return {"ok": True, "created": False, "rev": int(res[1])}
        if status == -1:
            return {"ok": False, "limit": True}
        return {"ok": False, "conflict": True, "rev": int(res[1])}

    # ── read ──
    def read_all(self) -> dict:
        owners = sorted(_s(o) for o in (self._call("read_all", "SMEMBERS", OWNERS_KEY) or []))
        keys = [META_KEY, REV_KEY]
        for o in owners:
            keys += [lm_key(o), obs_key(o)]
        res = self._eval("read_all", READ_ALL_LUA, keys, owners)
        leaguemates = {}
        i = 2
        while i < len(res):
            owner, lm_flat, obs_flat = _s(res[i]), res[i + 1] or [], res[i + 2] or []
            i += 3
            lm = _pairs(lm_flat)
            name = _field(lm.get("name"), int(lm.get("name_rev") or 0))
            breakdown = _field(lm.get("breakdown"), int(lm.get("breakdown_rev") or 0))
            if breakdown is not None:
                breakdown["covers"] = (lm.get("breakdown_covers") or "").split()
            observations = []
            for oid, stored in _pairs(obs_flat).items():
                rev, _, rest = stored.partition(":")
                date_label, _, text = rest.partition("\n")
                observations.append({"id": oid, "text": text, "date_label": date_label, "rev": int(rev)})
            observations.sort(key=lambda o: o["rev"])
            leaguemates[owner] = {"name": name, "breakdown": breakdown, "observations": observations}
        return {"schema": SCHEMA, "initialized": bool(int(res[0])), "rev": int(res[1] or 0),
                "leaguemates": leaguemates}


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else ("" if v is None else str(v))


def _pairs(flat) -> dict[str, str]:
    flat = [_s(x) for x in flat]
    return dict(zip(flat[0::2], flat[1::2]))


def _field(value, rev: int):
    """rev 0 means the field was never written: None, not ''."""
    if not rev:
        return None
    return {"value": _s(value), "rev": rev}


class UpstashTransport:
    """Upstash Redis REST, one command per POST. Same credentials as the
    budget store; a separate client so neither can disturb the other's
    connection or error state."""

    def __init__(self, url: str, token: str, timeout: float = 4.0) -> None:
        self._url = url
        self._client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=timeout)

    def __call__(self, *args):
        try:
            r = self._client.post(self._url, json=[str(a) for a in args])
            r.raise_for_status()
            body = r.json()
        except Exception as exc:  # noqa: BLE001 — never echo the URL or body
            raise IntelUnavailable(type(exc).__name__) from exc
        if isinstance(body, dict) and body.get("error"):
            raise IntelUnavailable("store error")
        return body.get("result") if isinstance(body, dict) else body

    def close(self) -> None:
        self._client.close()


def check_key_isolation(budget_prefix: str, keepalive_key: str) -> None:
    """Budget and intel must not be able to name each other's keys."""
    for other in (budget_prefix, keepalive_key):
        o = other or ""
        if o.startswith(INTEL_PREFIX.rstrip(":")) or INTEL_PREFIX.startswith(o) or not o:
            raise IntelMisconfigured("budget key space overlaps intel key space")


_store: IntelStore | None = None
_lock = threading.Lock()


def store() -> IntelStore:
    """The configured store. Fail-closed: no Upstash config, or a key-space
    overlap with the budget, raises rather than falling back to memory —
    intel kept in process memory would be silently lost on the next sleep."""
    global _store
    if _store is not None:
        return _store
    with _lock:
        if _store is None:
            import budget

            check_key_isolation(budget.KEY_PREFIX, budget.KEEPALIVE_KEY)
            url = (os.environ.get("GM_UPSTASH_REST_URL") or "").rstrip("/")
            token = os.environ.get("GM_UPSTASH_REST_TOKEN") or ""
            if not url or not token:
                raise IntelMisconfigured("intel store not configured")
            _store = IntelStore(UpstashTransport(url, token))
    return _store


def _set_store_for_tests(s: IntelStore | None) -> None:
    global _store
    _store = s
