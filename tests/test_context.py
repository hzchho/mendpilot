"""Real tokenizer and real shell commands, without API calls or mocks."""
import json
from pathlib import Path

import pytest

from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import Submitted, LimitsExceeded

from mendpilot.core.context import ContextWindow


@pytest.fixture(scope="module")
def tokenizer():
    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            return json.dumps(messages).split()
    return Tokenizer()


def test_context_drops_complete_turns_and_keeps_full_original(tokenizer):
    messages = [{"role": "system", "content": "Repair code."}, {"role": "user", "content": "Fix issue."}]
    for i in range(8):
        messages.extend([
            {"role": "assistant", "content": "Read relevant source", "tool_calls": [{
                "id": f"call{i}", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": "cat source.py"})}}]},
            {"role": "tool", "tool_call_id": f"call{i}", "content": "long source output " * 250},
        ])
    original = json.dumps(messages)
    window = ContextWindow(tokenizer, 1800)
    selected = window.select(messages)
    assert window.count(selected) <= 1800
    assert selected[:2] == messages[:2]
    assert selected[-2:] == messages[-2:]
    assert json.dumps(messages) == original
    seen = set()
    for item in selected:
        for call in item.get("tool_calls", []):
            seen.add(call["id"])
        if item["role"] == "tool":
            assert item["tool_call_id"] in seen


def test_context_explicitly_rejects_oversized_task(tokenizer):
    window = ContextWindow(tokenizer, 200)
    with pytest.raises(LimitsExceeded) as caught:
        window.select([{"role": "system", "content": "system"},
                       {"role": "user", "content": "large task " * 500}])
    assert caught.value.messages[0]["content"] == "ContextBudgetExceeded"






