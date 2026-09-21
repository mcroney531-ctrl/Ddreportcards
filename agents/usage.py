"""Budget participation for the ADK report agents.

The agents are the expensive path — /report/roster runs the pipeline for every
skill player on a roster, four concurrently — so they hold the same invariant
as /chat: reserve an upper bound before the provider is called, reconcile to
actual afterwards.

  before_model_callback   reserve, atomically, for this specific round
  after_model_callback    settle that round's reservation to actual usage
  on_model_error_callback drop the association, leave the money charged

There is no optional-import escape hatch and no "log it and carry on" path.
budget.py is a required runtime dependency now: a process that cannot account
for spend does not get to spend.
"""

import asyncio
import logging
import threading

import budget  # required — see module docstring; do NOT wrap in try/except

log = logging.getLogger("gm.agents.usage")

# Measured, not assumed.
#
# With max_output_tokens unset, ADK 2.9.2 passes nothing through and LiteLLM
# 1.101.0 sends max_tokens=128000 to Anthropic — $1.92 of exposure per round
# at sonnet-4-6 output pricing. 4096 is more than ten times the largest
# declared output contract (~378 tokens) and leaves room for a trade response
# listing several candidates, while cutting that exposure to about $0.061.
#
# This only became a safe number once the roster/trade agents stopped echoing
# the whole player_cards payload back as a tool argument: that echo ran to
# ~22,600 output tokens on a 24-player roster, so any ceiling worth having
# would have truncated the tool call rather than the answer.
AGENT_MAX_OUTPUT_TOKENS = 4096


# Reservations are held per invocation, never in a module global: the roster
# endpoint runs four synthesis pipelines concurrently, and a shared slot would
# let one round settle another's reservation.
_reservations: dict[str, budget.Reservation] = {}
_lock = threading.Lock()


def _invocation_key(callback_context) -> str:
    for attr in ("invocation_id", "invocation_context", "agent_name"):
        v = getattr(callback_context, attr, None)
        if isinstance(v, str) and v:
            return v
    return f"ctx-{id(callback_context)}"


def _priced_model(obj) -> str:
    """Normalise LiteLLM's identifier to the priced key.

    llm_request.model is 'anthropic/claude-sonnet-4-6' — verified against the
    installed ADK — so the key is derived from the request rather than kept in
    a second constant that can drift from the agent definitions.
    """
    raw = getattr(obj, "model", "") or ""
    return raw.split("/", 1)[-1] if "/" in raw else raw


def _request_upper_bound_tokens(llm_request) -> int:
    """A deliberately conservative input bound for the assembled request.

    No chars/4 estimate: that is a guess, and a guess is not a spend control.
    This serialises the whole LlmRequest — system instruction, contents, tool
    declarations, config — and reserves at least one token per UTF-8 byte,
    plus a fixed allowance for the structural overhead the provider adds
    around each message and tool definition. One token per byte cannot
    under-count any real tokenizer on text and JSON.
    """
    try:
        blob = llm_request.model_dump_json(exclude_none=True)
    except Exception:  # noqa: BLE001
        blob = repr(getattr(llm_request, "contents", "")) + repr(getattr(llm_request, "config", ""))
    return len(blob.encode("utf-8")) + 2_000


async def check_budget_before_model(callback_context, llm_request):
    """ADK before_model_callback. Returning None lets the call proceed.

    Raising stops it before the provider is reached — verified against the
    installed ADK: the exception propagates out through Runner. Nothing here
    is swallowed; an accounting system that cannot confirm the money is
    available must not wave the call through.

    budget.reserve() makes a synchronous HTTP call to Upstash. Calling it
    directly here would block the whole event loop -- including /health --
    for that round-trip's duration on every single model round: the same
    defect class as the original 502 (a plain `def` tool blocking the loop
    via a synchronous wait), just on the budget path instead of the tool
    path. asyncio.to_thread keeps it off the loop; ADK awaits this callback
    cooperatively either way (google.adk.utils._callback_pipeline
    ._run_callbacks awaits the result whenever it's awaitable).
    """
    model = _priced_model(llm_request)
    budget.price_of(model)  # unpriced cannot be costed, so cannot run

    reservation = await asyncio.to_thread(
        budget.reserve,
        model,
        _request_upper_bound_tokens(llm_request),
        AGENT_MAX_OUTPUT_TOKENS,
    )
    with _lock:
        _reservations[_invocation_key(callback_context)] = reservation
    return None


async def record_model_usage(callback_context, llm_response):
    """ADK after_model_callback. Accounting only — never alters the response.

    budget.settle() is likewise a synchronous Upstash call, offloaded the
    same way and for the same reason as check_budget_before_model's reserve.
    """
    key = _invocation_key(callback_context)
    with _lock:
        reservation = _reservations.pop(key, None)

    usage = getattr(llm_response, "usage_metadata", None)
    if reservation is None:
        # No reservation to settle — the call should not have happened.
        log.error("no reservation found for invocation %s; spend may be unrecorded", key)
        return None
    if usage is None:
        # Streaming partial, or a provider reporting nothing. The reservation
        # stays charged: something was called, and an unrecorded charge is the
        # one outcome worse than over-counting.
        log.warning("no usage reported for invocation %s; leaving reservation charged", key)
        return None

    try:
        await asyncio.to_thread(
            budget.settle,
            reservation,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )
    except Exception:
        # Abort, do not continue. Logging and carrying on is not fail-closed:
        # a write that fails here and a store that recovers before the next
        # pre-call check would leave this round missing from the ledger while
        # the pipeline kept spending against a total it no longer matches.
        log.exception("FAILED to settle agent spend for %s — aborting the run", reservation.model)
        raise
    return None


def clear_invocation_reservation(callback_context, llm_request, error):
    """ADK on_model_error_callback.

    The signature is the contract, not a convenience: ADK invokes this with
    callback_context, llm_request and error all as KEYWORD arguments
    (_SingleOnModelErrorCallback is Callable[[CallbackContext, LlmRequest,
    Exception], ...]). The previous (callback_context, error=None) form would
    have raised TypeError: unexpected keyword argument 'llm_request' on the
    first real provider failure — masking the provider error behind a
    callback error and leaving the association uncleared.

    It drops the association only. The reservation stays charged, because from
    here a call that never reached the provider is indistinguishable from one
    that was generated and billed before the error surfaced.

    Returns None so ADK re-raises the original error rather than treating this
    as a handled response.
    """
    key = _invocation_key(callback_context)
    with _lock:
        reservation = _reservations.pop(key, None)
    if reservation is not None:
        log.warning(
            "model error on invocation %s — leaving %d micro charged (ambiguous failure)",
            key, reservation.micros,
        )
    return None
