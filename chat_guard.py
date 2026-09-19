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
  2. A daily SPEND budget, in microdollars, held in a durable store and
     reserved atomically before each model round. Bounds total loss no
     matter who is calling, and survives a restart.
  3. Per-IP rate limiting. Bounds how fast one abuser can work.
  4. Origin allowlist. Stops other websites spending the key from a browser.
  5. A shared secret. A speed bump — it stops scanners that probe for open
     LLM proxies, and nothing more. It is not what makes this safe; 1-3 are.

Counters live in process memory. On Render's free tier that is one
instance, so they work — but they reset when the service restarts or spins
down, which is what the keepalive ping is for. This is a spend cap, not an
audit log.
"""

import logging
import os
import threading
import time

import budget

# ── Configuration ───────────────────────────────────────────────────────────
# Every limit is env-overridable so it can be tightened without a deploy.

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Sonnet 5 only. Every call site in the frontend asks for it and nothing else
# (verified across index.html and the Netlify functions before narrowing this),
# so the previous entries were compatibility for callers that do not exist.
# Sonnet 4.5 in particular is near its retirement window and was only ever
# here for cached PWA clients — and the service worker is network-first, so
# those pick up the current bundle on their next online load.
#
# The narrowing matters because of the rule below it: budget.MODEL_PRICING is
# authoritative. An operator can narrow this set by env var but cannot widen
# it past what the spend system knows how to cost, so a model can never be
# permitted and billed at zero. Adding one is a deliberate paired edit —
# price it, then permit it.
_REQUESTED_MODELS = {
    m.strip()
    for m in (os.environ.get("GM_CHAT_ALLOWED_MODELS") or "claude-sonnet-5").split(",")
    if m.strip()
}
_UNPRICED = _REQUESTED_MODELS - set(budget.MODEL_PRICING)
if _UNPRICED:
    # Loud, and then excluded. A model whose spend cannot be bounded does not
    # get served because someone put it in an env var.
    logging.getLogger("gm.chat_guard").error(
        "Refusing to serve unpriced model(s) %s — add them to "
        "budget.MODEL_PRICING first.", sorted(_UNPRICED),
    )
ALLOWED_MODELS = _REQUESTED_MODELS & set(budget.MODEL_PRICING)

# Raised from 4000 with the move to Sonnet 5. Thinking is on by default
# there and its tokens count against max_tokens, so a ceiling sized for the
# answer alone now has to cover the reasoning too — and a turn that runs out
# mid-thought returns no text block at all. This is a cap, not a reservation:
# raising it costs nothing on a reply that does not need the room.
MAX_TOKENS_CEILING = _int_env("GM_CHAT_MAX_TOKENS", 16000)
# Two knobs, two different jobs, and they should not have moved together.
#
# The failure actually observed was the MESSAGE ceiling: both counts include
# assistant turns, so 60 messages was really 30 exchanges, and a normal trade
# conversation hit it. That one stays raised.
#
# The CHARACTER ceiling was raised in the same edit without evidence that it
# was ever the binding constraint, and it is the half that bounds spend. It
# goes back to 120k. The reasoning for 600k — "Sonnet 5 has a 1M context" —
# answered the wrong question: the context window says what the model can
# read, not what this service should agree to pay for on one public request.
#
# What 600k actually bought, per accepted request: roughly 150k input tokens
# resent on every round of a tool loop that runs up to five of them, plus the
# system prompt, eleven tool definitions and the tool results appended
# mid-turn. That is the better part of a million input tokens from a single
# message, against a daily budget of two million that a process restart
# resets to zero. At 120k the same worst case is roughly a fifth of that.
MAX_MESSAGES = _int_env("GM_CHAT_MAX_MESSAGES", 500)
MAX_BODY_CHARS = _int_env("GM_CHAT_MAX_BODY_CHARS", 120_000)

RATE_LIMIT_REQUESTS = _int_env("GM_CHAT_RATE_REQUESTS", 20)
RATE_LIMIT_WINDOW_SECONDS = _int_env("GM_CHAT_RATE_WINDOW", 300)

# Rough guide: a tool-calling turn runs 15-40k input tokens across its
# rounds. 2M/day leaves a normal user far more headroom than they will use
# while capping a runaway at something survivable.
# Superseded by budget.DAILY_LIMIT_MICRO. Kept only so an operator reading an
# old runbook sees why GM_CHAT_DAILY_TOKENS no longer does anything.
DAILY_TOKEN_BUDGET_RETIRED = _int_env("GM_CHAT_DAILY_TOKENS", 0)

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


def check_rate_limit(
    ip: str,
    now: float | None = None,
    bucket: str = "chat",
    limit: int | None = None,
    window: int | None = None,
) -> None:
    """Sliding-window limit, per IP and per bucket.

    Buckets are separate because the endpoints are not the same size. A chat
    turn is a handful of model calls; a roster report is the whole agent
    pipeline run once per player on a 24-man roster, several minutes of
    billed work for one request. Sharing one allowance would let the
    expensive endpoint hide inside the cheap one's budget.
    """
    now = time.time() if now is None else now
    limit = RATE_LIMIT_REQUESTS if limit is None else limit
    window = RATE_LIMIT_WINDOW_SECONDS if window is None else window
    key = f"{bucket}:{ip}"
    cutoff = now - window
    with _lock:
        recent = [t for t in _hits.get(key, []) if t > cutoff]
        if len(recent) >= limit:
            retry_in = int(recent[0] + window - now) + 1
            _hits[key] = recent
            raise ChatRefused(
                429,
                f"Rate limit: {limit} requests per "
                f"{max(1, window // 60)} minutes. Try again in {retry_in}s.",
            )
        recent.append(now)
        _hits[key] = recent
        # Opportunistic sweep so the dict cannot grow without bound.
        if len(_hits) > 2000:
            for k in [k for k, v in _hits.items() if not any(t > cutoff for t in v)]:
                _hits.pop(k, None)


# ── Daily spend budget ──────────────────────────────────────────────────────
#
# The counter itself now lives in budget.py: durable, atomic, denominated in
# microdollars. What remains here is the request-path gate.


def _today_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def check_budget(now: float | None = None) -> None:
    """Is there anything left today.

    Used by paths that cannot reserve in advance — the report pipeline, and
    the cheap pre-flight on /chat. The real protection on /chat is the
    per-round reservation in the tool loop; this only avoids starting work
    that is already doomed.

    Fails CLOSED: an unreachable store means the spend control is not
    operating, and a public endpoint holding a real API key does not run
    unmetered because a dependency is down.
    """
    try:
        room = budget.has_room()
    except budget.BudgetUnavailable as exc:
        raise ChatRefused(
            503,
            "Spend accounting is unavailable, so this request cannot be "
            "authorised. Try again shortly.",
        ) from exc
    if not room:
        raise ChatRefused(
            429,
            "Daily spend budget for this service is spent. It resets at "
            "00:00 UTC.",
        )


def budget_status() -> dict:
    return budget.status()


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

    model = body.get("model") or "claude-sonnet-5"
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


REPORT_RATE_LIMIT_REQUESTS = _int_env("GM_REPORT_RATE_REQUESTS", 4)
REPORT_RATE_WINDOW_SECONDS = _int_env("GM_REPORT_RATE_WINDOW", 3600)


def enforce_report(request) -> None:
    """Gate for the agent-pipeline endpoints.

    These were left open when /chat was locked down, and they are the more
    expensive pair by a wide margin: /report/roster runs the synthesis
    pipeline once for every skill player on a roster — roughly two dozen
    multi-agent runs, minutes of billed work — from one unauthenticated POST
    on a guessable URL. There is no body to sanitize here, so no model or
    token ceiling applies; the protection is who may call, how often, and
    whether the day's budget is already spent.

    Agent spend counts toward the same budget: each agent registers
    agents.usage.record_model_usage as ADK's after_model_callback, so every
    LiteLLM call reports its tokens here the way the chat loop does.
    """
    check_secret(request)
    check_origin(request)
    check_rate_limit(
        client_ip(request),
        bucket="report",
        limit=REPORT_RATE_LIMIT_REQUESTS,
        window=REPORT_RATE_WINDOW_SECONDS,
    )
    check_budget()


def config_warnings() -> list[str]:
    """Surfaced by /health so a missing control is visible rather than assumed."""
    warnings = []
    if not SHARED_SECRET:
        warnings.append(
            "GM_CHAT_SECRET is not set — /chat accepts unauthenticated requests."
        )
    # /health is how a misconfiguration gets NOTICED, so nothing in here may
    # raise — including store(), which now refuses to build on a bad or
    # missing backend config rather than silently falling back to memory.
    try:
        durable = budget.store().durable
    except budget.BudgetUnavailable:
        warnings.append(
            "Budget store is misconfigured — /chat and /report are refusing "
            "requests (503) rather than spending unmetered. Check "
            "GM_BUDGET_BACKEND and the Upstash settings."
        )
    else:
        if not durable:
            warnings.append(
                "Budget store is not durable (GM_BUDGET_BACKEND=memory) — the "
                "daily spend budget resets on every restart. This is intended "
                "for local development only."
            )
        else:
            try:
                budget.has_room()
            except budget.BudgetUnavailable:
                warnings.append(
                    "Budget store is unreachable — /chat and /report are "
                    "refusing requests (503) rather than spending unmetered."
                )
    if _UNPRICED:
        warnings.append(
            f"Model(s) {sorted(_UNPRICED)} are allowlisted but have no verified "
            f"price, so they are refused. Add them to budget.MODEL_PRICING."
        )
    return warnings
