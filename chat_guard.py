"""Cost and abuse controls for the public /chat endpoint.

/chat spends an Anthropic key and, because the frontend is a static page,
it has to be reachable without a login. The endpoint's URL is in the page
source, CORS was "*", and the caller chose both the model and max_tokens —
so anyone who viewed source could run Opus at a 64k ceiling in a loop on
someone else's card.

There is no honest way to *authenticate* a caller here: a static site can
hold no secret a determined reader cannot extract. So the controls are
layered by how much they actually accomplish, and the ordering matters
because the weakest one is the one that looks the most like security:

  1. Per-request caps (model allowlist, token ceiling, body size). These
     genuinely bound what a single call can cost, and cannot be bypassed.
  2. A daily token budget. Bounds total loss no matter who is calling.
  3. Per-IP rate limiting. Bounds how fast one abuser can work.
  4. Origin allowlist. Stops other websites spending the key from a browser.
  5. A shared secret. A speed bump — it stops scanners that probe for open
     LLM proxies, and nothing more. It is not what makes this safe; 1-3 are.

Counters live in process memory. On Render's free tier that is one
instance, so they work — but they reset when the service restarts or spins
down, which is what the keepalive ping is for. This is a spend cap, not an
audit log.
"""

import os
import threading
import time

# ── Configuration ───────────────────────────────────────────────────────────
# Every limit is env-overridable so it can be tightened without a deploy.

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Frontend asks for claude-sonnet-4-5. Anything outside this list is refused
# rather than silently downgraded — a caller asking for a model we won't run
# should be told, not quietly served something else.
ALLOWED_MODELS = {
    m.strip()
    for m in (
        os.environ.get("GM_CHAT_ALLOWED_MODELS")
        or "claude-sonnet-4-5,claude-haiku-4-5-20251001"
    ).split(",")
    if m.strip()
}

MAX_TOKENS_CEILING = _int_env("GM_CHAT_MAX_TOKENS", 4000)
MAX_MESSAGES = _int_env("GM_CHAT_MAX_MESSAGES", 60)
MAX_BODY_CHARS = _int_env("GM_CHAT_MAX_BODY_CHARS", 120_000)

RATE_LIMIT_REQUESTS = _int_env("GM_CHAT_RATE_REQUESTS", 20)
RATE_LIMIT_WINDOW_SECONDS = _int_env("GM_CHAT_RATE_WINDOW", 300)

# Rough guide: a tool-calling turn runs 15-40k input tokens across its
# rounds. 2M/day leaves a normal user far more headroom than they will use
# while capping a runaway at something survivable.
DAILY_TOKEN_BUDGET = _int_env("GM_CHAT_DAILY_TOKENS", 2_000_000)

SHARED_SECRET = os.environ.get("GM_CHAT_SECRET") or ""
SECRET_HEADER = "x-gm-key"

_DEFAULT_ORIGINS = "https://gm-command.netlify.app,http://localhost:8888,http://localhost:8811"
ALLOWED_ORIGINS = [
    o.strip()
    for o in (os.environ.get("GM_CHAT_ALLOWED_ORIGINS") or _DEFAULT_ORIGINS).split(",")
    if o.strip()
]
# Netlify deploy previews: deploy-preview-12--gm-command.netlify.app etc.
ALLOWED_ORIGIN_REGEX = r"https://[a-z0-9-]+--gm-command\.netlify\.app"


class ChatRefused(Exception):
    """Raised with the HTTP status the caller should get and why."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ── Per-IP rate limiting ────────────────────────────────────────────────────

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def client_ip(request) -> str:
    """Render terminates TLS upstream, so the socket peer is a proxy. Trust
    the first X-Forwarded-For hop — it is the only thing standing in for a
    caller identity here, and it is spoofable, which is why the daily budget
    exists underneath it rather than relying on this."""
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", None) or "unknown"


def check_rate_limit(ip: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with _lock:
        recent = [t for t in _hits.get(ip, []) if t > cutoff]
        if len(recent) >= RATE_LIMIT_REQUESTS:
            retry_in = int(recent[0] + RATE_LIMIT_WINDOW_SECONDS - now) + 1
            _hits[ip] = recent
            raise ChatRefused(
                429,
                f"Rate limit: {RATE_LIMIT_REQUESTS} requests per "
                f"{RATE_LIMIT_WINDOW_SECONDS // 60} minutes. Try again in {retry_in}s.",
            )
        recent.append(now)
        _hits[ip] = recent
        # Opportunistic sweep so the dict cannot grow without bound.
        if len(_hits) > 2000:
            for k in [k for k, v in _hits.items() if not any(t > cutoff for t in v)]:
                _hits.pop(k, None)


# ── Daily token budget ──────────────────────────────────────────────────────

_budget_day: str = ""
_budget_used: int = 0


def _today_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def check_budget(now: float | None = None) -> None:
    global _budget_day, _budget_used
    day = _today_key(now)
    with _lock:
        if day != _budget_day:
            _budget_day, _budget_used = day, 0
        if _budget_used >= DAILY_TOKEN_BUDGET:
            raise ChatRefused(
                429,
                "Daily token budget for this service is spent. It resets at "
                "00:00 UTC.",
            )


def record_usage(input_tokens: int, output_tokens: int, now: float | None = None) -> None:
    """Called after every Anthropic response, including intermediate
    tool-loop rounds — those cost real money and a budget that ignored them
    would undercount a tool-heavy turn several times over."""
    global _budget_day, _budget_used
    day = _today_key(now)
    with _lock:
        if day != _budget_day:
            _budget_day, _budget_used = day, 0
        _budget_used += max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))


def budget_status() -> dict:
    with _lock:
        used = _budget_used if _budget_day == _today_key() else 0
    return {
        "used_tokens": used,
        "daily_budget": DAILY_TOKEN_BUDGET,
        "remaining": max(0, DAILY_TOKEN_BUDGET - used),
    }


# ── Request validation ──────────────────────────────────────────────────────

def check_secret(request) -> None:
    if not SHARED_SECRET:
        # Deliberately not silent: an unset secret is a real gap, and /health
        # reports it so it cannot sit unnoticed the way REPORTCARDS_API_URL did.
        return
    if (request.headers.get(SECRET_HEADER) or "") != SHARED_SECRET:
        raise ChatRefused(401, "Missing or invalid API key.")


def check_origin(request) -> None:
    """Only enforced when an Origin header is present. A browser always sends
    one on a cross-origin POST; curl does not, and blocking on its absence
    would break server-side callers without stopping anyone, since curl can
    set any Origin it likes. This closes the browser hole, nothing more."""
    origin = request.headers.get("origin")
    if not origin:
        return
    import re

    if origin in ALLOWED_ORIGINS:
        return
    if re.fullmatch(ALLOWED_ORIGIN_REGEX, origin):
        return
    raise ChatRefused(403, f"Origin {origin} is not allowed to use this endpoint.")


def sanitize_body(body: dict) -> dict:
    """Return the request parameters the loop may actually use.

    The caller used to pick the model and the token ceiling. Those are the
    two dials that set the price of a call, so they are decided here now.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ChatRefused(400, "messages must be a non-empty list.")
    if len(messages) > MAX_MESSAGES:
        raise ChatRefused(
            413, f"Conversation too long: {len(messages)} messages, limit {MAX_MESSAGES}."
        )

    size = len(str(messages)) + len(str(body.get("system") or ""))
    if size > MAX_BODY_CHARS:
        raise ChatRefused(413, f"Request too large: {size} chars, limit {MAX_BODY_CHARS}.")

    model = body.get("model") or "claude-sonnet-4-5"
    if model not in ALLOWED_MODELS:
        raise ChatRefused(
            400,
            f"Model {model!r} is not available on this endpoint. "
            f"Allowed: {', '.join(sorted(ALLOWED_MODELS))}.",
        )

    requested = body.get("max_tokens")
    try:
        requested = int(requested) if requested else MAX_TOKENS_CEILING
    except (TypeError, ValueError):
        requested = MAX_TOKENS_CEILING
    max_tokens = max(1, min(requested, MAX_TOKENS_CEILING))

    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "system": body.get("system"),
    }


def enforce(request, body: dict) -> dict:
    """Every gate, in the order that fails cheapest first."""
    check_secret(request)
    check_origin(request)
    check_rate_limit(client_ip(request))
    check_budget()
    return sanitize_body(body)


def config_warnings() -> list[str]:
    """Surfaced by /health so a missing control is visible rather than assumed."""
    warnings = []
    if not SHARED_SECRET:
        warnings.append(
            "GM_CHAT_SECRET is not set — /chat accepts unauthenticated requests."
        )
    return warnings
