"""Record exactly what /chat sends to the model and the tools.

Used twice: once against the ORIGINAL inline /chat implementation (d154a90)
to write tests/fixtures/chat_loop_golden.json, and then by
test_chat_refactor_equivalence.py against the refactored code, which must
reproduce that recording byte for byte. A fake Anthropic client returns
scripted responses; tools are stubbed; the spend budget uses a memory store.
Nothing here touches the network.
"""
import hashlib
import json
from unittest import mock

from anthropic.types import Message
from fastapi.testclient import TestClient

import api
import budget
import chat_guard

FROZEN_TODAY_TOKEN = "<TODAY>"


def _msg(content, stop_reason, in_tok=100, out_tok=20):
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    })


def _text(t):
    return {"type": "text", "text": t}


def _tool(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


SCENARIOS = {
    "first_turn_text": {
        "body": {"model": "claude-sonnet-5", "max_tokens": 16000, "system": "FRONTEND SYSTEM",
                 "messages": [{"role": "user", "content": "Is Bijan a buy?"}]},
        "script": [_msg([_text("Yes.")], "end_turn")],
    },
    "follow_up_with_tools": {
        "body": {"model": "claude-sonnet-5", "max_tokens": 16000, "system": "FRONTEND SYSTEM",
                 "messages": [{"role": "user", "content": "Trade A for B?"},
                              {"role": "assistant", "content": "Lean accept."},
                              {"role": "user", "content": "Jones is better than you think"}]},
        "script": [
            _msg([_text("Checking."), _tool(1, "get_player_value", {"player_name": "Aaron Jones"})], "tool_use"),
            _msg([_tool(2, "get_player_stats", {"sleeper_id": "1234", "games": 3}),
                  _tool(3, "get_player_stats", {"sleeper_id": "5678", "games": 3})], "tool_use"),
            _msg([_text("Holding.")], "end_turn"),
        ],
    },
    "no_frontend_system": {
        "body": {"model": "claude-sonnet-5", "max_tokens": 900,
                 "messages": [{"role": "user", "content": "hi"}]},
        "script": [_msg([_text("Hello.")], "end_turn")],
    },
    "empty_text_max_tokens": {
        "body": {"model": "claude-sonnet-5", "max_tokens": 16000, "system": "S",
                 "messages": [{"role": "user", "content": "long question"}]},
        "script": [_msg([{"type": "thinking", "thinking": "", "signature": "sig"}], "max_tokens")],
    },
    "five_tool_rounds_exhausted": {
        "body": {"model": "claude-sonnet-5", "max_tokens": 16000, "system": "S",
                 "messages": [{"role": "user", "content": "loop"}]},
        "script": [_msg([_tool(i, "get_trending_players", {"limit": 5})], "tool_use") for i in range(5)],
    },
}


class _FakeMessages:
    def __init__(self, script, log):
        self._script = list(script)
        self._log = log

    def count_tokens(self, **kw):
        self._log.append({"call": "count_tokens", **_norm(kw)})
        return mock.Mock(input_tokens=1000)

    def create(self, **kw):
        self._log.append({"call": "create", **_norm(kw)})
        return self._script.pop(0)


def _norm(kw):
    out = {}
    for k, v in sorted(kw.items()):
        if k == "tools":
            out["tools_sha256"] = hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()
        elif k == "system":
            out["system"] = v
        else:
            out[k] = json.loads(json.dumps(v, default=str))
    return out


def record(scenario_name: str, today: str) -> dict:
    sc = SCENARIOS[scenario_name]
    log, tools_run = [], []

    def fake_tool(name, tool_input, latest_user_text=""):
        tools_run.append({"name": name, "input": tool_input, "latest_user_text": latest_user_text})
        return {"stub": name}

    fake_client = mock.Mock()
    fake_client.messages = _FakeMessages(sc["script"], log)
    budget._set_store_for_tests(budget.MemoryBudgetStore())
    chat_guard._hits.clear()
    with mock.patch.object(api, "_anthropic_client", fake_client), \
         mock.patch.object(api, "_execute_chat_tool", fake_tool), \
         mock.patch.object(chat_guard, "SHARED_SECRET", ""):
        resp = TestClient(api.app).post("/chat", json=sc["body"])
    text = json.dumps({"status": resp.status_code, "response": resp.json(), "calls": log, "tools_run": tools_run},
                      sort_keys=True)
    return json.loads(text.replace(today, FROZEN_TODAY_TOKEN))
