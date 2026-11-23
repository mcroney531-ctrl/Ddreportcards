"""Every behavioural instruction the GM Assistant receives, by source and topic.

The assistant's system prompt is assembled from several places: the backend
rules, the app's system text, the app's per-message context block, the
backend's follow-up guidance, and the tool descriptions. A fix in one place
is undone if another keeps saying the opposite, so these are inventoried
together.

directives(segments) splits each source into sentences, keeps the ones that
direct behaviour, and tags them by topic. OWNERS says which sources may
direct each topic. KNOWN_CONFLICTS lists instructions Phase A replaced
because they contradicted the new behaviour, so they can't creep back in from
any source. Neither check proves the absence of every semantic contradiction:
that's what the side-by-side review of the snapshots and the live evaluation
are for.
"""
import re

DIRECTIVE = re.compile(
    r"\b(always|never|must|do not|don't|cannot|should|only when|only if|use only|call |"
    r"look (it )?up|fetch|verify|check|hold|give a|say|treat|keep|leave|engage|respond|"
    r"refer|discuss|ignore|confirm)\b",
    re.I,
)

TOPICS = {
    "lookups": re.compile(r"\b(look(ed)? ?up|lookup|call get_|get_[a-z_]+|fetch|tool result|re-fetch|live lookup)\b", re.I),
    "verdicts": re.compile(r"\bverdict", re.I),
    "conviction": re.compile(r"\b(hold|flip|revers|pushback|push(es|ed)? back|agree|capitulat|echo|change your mind|mood|pressure)", re.I),
    "narration": re.compile(r"\b(narrat|announce|preamble|\"confirmed|label his message|describe how you weighed)", re.I),
    "intent": re.compile(r"\b(evaluation|discussion|action|what he wants|re-run|restate)\b", re.I),
    "news": re.compile(r"\b(news|feed|headline|quoted)\b", re.I),
    "context_trust": re.compile(r"\b(context block|cache|starting point|authority|overrides)\b", re.I),
    "sourcing": re.compile(r"\b(source|cite|from memory|invent|training data|attribute)\b", re.I),
}

# Which sources may direct each topic. The app owns persona and style; the
# backend owns behaviour. The context block may direct how its own contents are
# trusted, and the tool descriptions how their own results are read.
OWNERS = {
    "lookups": {"backend_rules", "stance", "context_block", "tool_descriptions"},
    "verdicts": {"backend_rules", "stance", "app_system", "tool_descriptions"},
    "conviction": {"backend_rules", "stance"},
    "narration": {"backend_rules", "stance"},
    "intent": {"backend_rules", "stance"},
    "news": {"backend_rules", "app_system", "news_block"},
    "context_trust": {"backend_rules", "context_block"},
    "sourcing": {"backend_rules", "app_system", "tool_descriptions", "news_block"},
}

# Instructions that Phase A replaced because they contradict its behaviour.
KNOWN_CONFLICTS = [
    ("blanket lookups", r"Call get_player_value for any player you discuss"),
    ("re-fetch every restated figure", r"If you are restating a figure you gave three turns ago, look it up again"),
    ("every follow-up treated as pushback", r"You already gave him a position earlier in this conversation"),
    ("unconditional verdicts", r"When evaluating trades give a clear verdict"),
    ("forced context-block lookups", r"out of date, so a live lookup overrides it"),
    ("verdict re-announced on every follow-up", r"Open any trade verdict by naming what he sends"),
    ("production always the most decision-relevant", r"recent production is usually the most decision-relevant"),
]


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"(0-9-])", text)
    return [p.strip() for p in parts if len(p.strip()) > 3]


def directives(segments: dict[str, str]) -> list[dict]:
    out = []
    for source, text in segments.items():
        for s in sentences(text):
            if not DIRECTIVE.search(s):
                continue
            topics = sorted(t for t, rx in TOPICS.items() if rx.search(s))
            out.append({"source": source, "topics": topics, "text": s})
    return out


# Reviewed exceptions to OWNERS, each with its reason. Keep this list short.
ALLOWED_EXCEPTIONS = {
    ("app_system", "lookups",
     "Use your tools to look up the user's roster, player values, and current NFL situations before answering "
     "anything that depends on them."):
        "Fallback prompt only, used before Sleeper has loaded: it states an app-only fact (no roster in context "
        "yet) and is conditional on dependence, which agrees with backend rule 1.",
}


def ownership_violations(items: list[dict]) -> list[dict]:
    return [dict(i, topic=t) for i in items for t in i["topics"]
            if i["source"] not in OWNERS[t] and (i["source"], t, i["text"]) not in ALLOWED_EXCEPTIONS]


def conflicts_present(full_text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", full_text)
    return [name for name, rx in KNOWN_CONFLICTS if re.search(rx, flat, re.I)]
