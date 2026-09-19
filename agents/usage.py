"""Report agent LLM spend into the API's daily token budget.

chat_guard caps what /chat may spend per day, counting every round of its
tool loop. The agent pipeline behind /report/* reaches Anthropic through
LiteLLM instead, so none of that spend was visible to the cap — and the
report endpoints are the expensive ones: /report/roster runs this pipeline
once per skill player on a roster. The budget could refuse a cheap chat turn
while a roster report ran unmetered.

ADK's after_model_callback fires once per model call with the LlmResponse,
which LiteLLM populates with prompt_token_count and candidates_token_count.
That is the whole hook. Two details make it safe to attach blindly:

  - Streaming partials carry no usage_metadata (LiteLLM only attaches it to
    the aggregated response), so guarding on its presence both skips them
    and rules out double-counting the same call.
  - Returning None means "response unmodified". Anything truthy would
    REPLACE the model's response, so this must never return the accounting.

Caveat: the counter lives in process memory. Agents run in the API process
when serving /report/*, which is the case the cap exists for; under
Streamlit they run in a different process and record against its own
counter, which nothing reads.
"""

try:
    import budget
except Exception:  # noqa: BLE001 — agents must stay runnable without the API
    budget = None

# Every agent in this package runs this model. It is priced in
# budget.MODEL_PRICING; if that ever drifts, price_of raises and the spend is
# logged rather than silently costed at zero.
AGENT_MODEL = "claude-sonnet-4-6"


def record_model_usage(callback_context, llm_response):
    """ADK after_model_callback. Accounting only — never alters the response.

    This path cannot reserve. ADK owns the model call and reports usage only
    afterwards, so there is no moment at which this process could reserve an
    upper bound first. What it can do is record the actual spend against the
    same daily key, so /chat reservations that come later see it.
    """
    if budget is None:
        return None
    try:
        usage = getattr(llm_response, "usage_metadata", None)
        if usage is None:
            return None  # streaming partial, or a provider that reports nothing
        budget.record_actual(
            AGENT_MODEL,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )
    except Exception:  # noqa: BLE001 — accounting must never break a run
        pass
    return None
