"""History integrity with real token estimation, no network or credentials."""
import json
import time
from types import SimpleNamespace
import pytest
from minisweagent.exceptions import LimitsExceeded, TimeExceeded
from mendpilot.models.responses import ResponseModel, ResponseContextWindow


def test_window_keeps_response_reasoning_phase_and_matching_outputs():
    history = [{"role": "system", "content": "Repair code"}, {"role": "user", "content": "Fix issue"}]
    for i in range(8):
        history += [{"object": "response", "output": [
            {"type": "reasoning", "id": f"r{i}", "encrypted_content": f"opaque{i}"},
            {"type": "message", "role": "assistant", "phase": "commentary", "content": []},
            {"type": "function_call", "call_id": f"c{i}", "name": "bash", "arguments": '{"command":"cat file"}'}]},
            {"type": "function_call_output", "call_id": f"c{i}", "output": "long source " * 1000}]
    original = json.dumps(history)
    window = ResponseContextWindow(13000)
    selected = window.select(history)
    assert window.count(selected) <= 13000
    assert selected[:2] == history[:2] and selected[-2:] == history[-2:]
    assert json.dumps(history) == original
    # Upstream flattening must replay every output item without losing phase or
    # opaque reasoning, and never leave an orphaned function_call_output.
    model = object.__new__(ResponseModel)
    replay = model._prepare_messages_for_api(selected)
    calls = {item['call_id'] for item in replay if item.get('type') == 'function_call'}
    assert all(item['call_id'] in calls for item in replay if item.get('type') == 'function_call_output')
    assert any(item.get('encrypted_content') == 'opaque7' for item in replay)
    assert any(item.get('phase') == 'commentary' for item in replay)


@pytest.mark.parametrize("expired", ["time", "tokens"])
def test_exhausted_budget_stops_before_api_without_retry(expired):
    model = object.__new__(ResponseModel)
    model.config = SimpleNamespace(model_kwargs={"max_output_tokens": 32768})
    model.deadline = time.monotonic() - 1 if expired == "time" else None
    model.total_output_limit = 65536
    model.output_tokens_used = 65536 if expired == "tokens" else 0
    # There is deliberately no SDK client; reaching a request would fail this test.
    started = time.monotonic()
    with pytest.raises(TimeExceeded if expired == "time" else LimitsExceeded):
        model.query([])
    assert time.monotonic() - started < 1


