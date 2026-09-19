"""Budget participation for the ADK report agents.

The agents are the expensive path — /report/roster runs the pipeline for every
skill player on a roster — so they need the same discipline as /chat.

Two callbacks, doing different jobs:

  before_model_callback  a hard PRE-SPEND gate. It runs immediately before
                         each provider call and refuses to let it happen when
                         the day's budget is gone or the store is unreachable.
  after_model_callback   records what the call actually cost, against the same
                         durable daily key /chat reserves from.

What this is NOT, yet: a reservation. Reserving an upper bound requires an
explicit output-token ceiling, and these agents currently declare none — see
AGENT_MAX_OUTPUT_TOKENS below. Until one is chosen, the gate bounds overshoot
to at most one in-flight call per concurrent pipeline instead of a whole
report, which is a real improvement but is not the same guarantee /chat has.
Calling it one would be dishonest.
"""

import logging

log = logging.getLogger("gm.agents.usage")

try:
    import budget
except Exception:  # noqa: BLE001 — agents must stay runnable without the API
    budget = None

# Unset on purpose.
#
# Measured, not assumed: with max_output_tokens unset, ADK 2.9.2 passes nothing
# through and LiteLLM 1.101.0 sends max_tokens=128000 to Anthropic. At
# sonnet-4-6 output pricing that is $1.92 of exposure per agent call, against
# a largest declared output contract of roughly 378 tokens.
#
# A pre-call reservation is meaningless without a ceiling, and picking one
# blind risks truncating an agent's JSON mid-object, which fails json.loads and
# kills the report. So the number is a deliberate decision, not a default.
AGENT_MAX_OUTPUT_TOKENS = None


def _priced_model(llm_request) -> str:
    """Normalise LiteLLM's identifier to the priced key.

    llm_request.model is 'anthropic/claude-sonnet-4-6' (verified against the
    installed ADK). Deriving it here beats a second hand-synced constant that
    can drift away from the agent definitions.
    """
    raw = getattr(llm_request, "model", "") or ""
    return raw.split("/", 1)[-1] if "/" in raw else raw


def check_budget_before_model(callback_context, llm_request):
    """ADK before_model_callback. Returning None lets the call proceed.

    Raising stops it. Failures here are NOT swallowed: an accounting system
    that cannot confirm there is money left must not wave the call through,
    which is the whole point of failing closed.
    """
    if budget is None:
        return None

    model = _priced_model(llm_request)
    try:
        budget.price_of(model)  # unpriced model cannot be costed, so cannot run
        if not budget.has_room():
            raise budget.BudgetExhausted(
                f"Daily spend budget is spent; refusing further {model} calls."
            )
    except budget.BudgetUnavailable:
        log.error("budget store unavailable — refusing agent model call (%s)", model)
        raise
    except budget.UnpricedModel:
        log.error("agent requested unpriced model %r — refusing", model)
        raise
    return None


def record_model_usage(callback_context, llm_response):
    """ADK after_model_callback. Accounting only — never alters the response."""
    if budget is None:
        return None

    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
        return None  # streaming partial, or a provider that reports nothing

    model = getattr(llm_response, "model", "") or ""
    model = model.split("/", 1)[-1] if "/" in model else model
    try:
        budget.record_actual(
            model,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )
    except Exception:  # noqa: BLE001
        # Loud, not silent. This used to be `pass`, which meant a store outage
        # mid-report left every subsequent call unrecorded while the pipeline
        # carried on spending. The spend is real whether or not it was written
        # down, so it is logged at error and the pre-call gate on the NEXT call
        # will fail closed against the unreachable store.
        log.exception("FAILED to record agent spend for %s — budget is now under-counting", model)
    return None
