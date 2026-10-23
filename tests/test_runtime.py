"""Regression cases from transport, context and finalization failures (no API charges)."""
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, InternalServerError, AuthenticationError
from litellm import ModelResponse
from minisweagent.exceptions import Submitted
from minisweagent.environments.local import LocalEnvironment
from mendpilot.core.agent import RepairAgent
from mendpilot.models.chat import WindowedModel
from mendpilot.models.common import fallback_command
from mendpilot.models.responses import WindowedResponseModel, ResponseContextWindow, SafeAPIError


@pytest.mark.parametrize('payload', [
    '<tool_call>{"name":"bash","arguments":{"command":"pwd"}}</tool_call>',
    'I will inspect the current directory.\n```bash\npwd\n```',
    '{"command":"pwd"}',
])
def test_adapted_calls_have_replayable_chat_history(payload):
    model = WindowedModel(window=None, model_name='openai/local', cost_tracking='ignore_errors')
    response = ModelResponse(choices=[{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': payload}}])
    actions = model._parse_actions(response)
    assert actions[0]['command'] == 'pwd'
    assert response.choices[0].message.tool_calls[0].id == actions[0]['tool_call_id']
    assert json.loads(response.choices[0].message.tool_calls[0].function.arguments) == {'command': 'pwd'}


@pytest.mark.parametrize('payload', [
    'Run pwd to inspect the directory', '```bash\npwd\n```\n```bash\nls\n```',
    '<tool_call>{"name":"bash","arguments":{"command":"pwd"}}',
    '{"name":"python","arguments":{"command":"pwd"}}',
    '{"command":"pwd","other":"ls"}',
])
def test_ambiguous_or_truncated_commands_are_never_inferred(payload):
    assert fallback_command(payload) is None


def test_responses_shortens_large_parallel_outputs_without_orphans_or_mutation():
    history = [{'role':'system','content':'Repair code'}, {'role':'user','content':'Fix the bug'}]
    history += [{'object':'response','output': [
        {'type':'function_call','name':'bash','call_id':f'c{i}','arguments':'{"command":"cat file"}'}
        for i in range(3)]}]
    history += [{'type':'function_call_output','call_id':f'c{i}','output':'source\n'*20000} for i in range(3)]
    original = json.dumps(history)
    window = ResponseContextWindow(14000)
    selected = window.select(history)
    assert window.count(selected) <= 14000
    assert selected[:3] == history[:3]
    assert [m['call_id'] for m in selected[3:]] == ['c0','c1','c2']
    assert all('shortened' in m['output'] for m in selected[3:])
    assert json.dumps(history) == original


def test_responses_adaptation_preserves_reasoning_and_records_function_call():
    from openai.types.responses import Response
    from mendpilot.models.responses import ResponseModel
    response = Response.model_validate({'id':'r1','object':'response','created_at':0,'model':'test',
        'status':'completed','parallel_tool_calls':False,'tools':[],'tool_choice':'auto',
        'output':[{'id':'think1','type':'reasoning','summary':[],'encrypted_content':'opaque'},
            {'id':'m1','type':'message','role':'assistant','status':'completed','content':[
                {'type':'output_text','text':'```bash\npwd\n```','annotations':[]}]}]})
    model=object.__new__(WindowedResponseModel)
    model.config=SimpleNamespace(format_error_template='{{error}}')
    model.adapter_events=[]
    actions=model._parse_actions(response)
    assert actions[0]['command']=='pwd'
    replay=ResponseModel._prepare_messages_for_api(model,[response.model_dump()])
    assert replay[0]['encrypted_content']=='opaque'
    assert replay[-1]['call_id']==actions[0]['tool_call_id']


def response_model(client):
    model = object.__new__(WindowedResponseModel)
    model.config = SimpleNamespace(model_name='openai/test', model_kwargs={'max_output_tokens':8192, 'timeout':30})
    model.deadline = time.monotonic()+60
    model.total_output_limit = 65536
    model.output_tokens_used = 0
    model.retry_limit = 2
    model.transport_events = []
    model.http_attempts = 0
    model._client = SimpleNamespace(responses=client)
    return model


def test_gateway_retry_counts_http_attempts_without_leaking_body(monkeypatch):
    class Client:
        def __init__(self): self.n = 0
        def create(self, **kwargs):
            self.n += 1
            if self.n <= 2:
                raise InternalServerError('private upstream detail', response=httpx.Response(502,
                    request=httpx.Request('POST','https://example.invalid')), body=None)
            return SimpleNamespace(usage=SimpleNamespace(output_tokens=4))
    monkeypatch.setattr('mendpilot.models.responses.time.sleep', lambda seconds: None)
    model = response_model(Client())
    assert model._query([]).usage.output_tokens == 4
    assert model.http_attempts == 3 and model.output_tokens_used == 4
    assert [e['category'] for e in model.transport_events] == ['gateway_502']*2
    assert 'private' not in json.dumps(model.transport_events)


def test_authentication_is_not_retried_and_error_is_sanitized():
    class Client:
        def create(self, **kwargs):
            raise AuthenticationError('private credential detail', response=httpx.Response(401,
                request=httpx.Request('POST','https://example.invalid')), body=None)
    model=response_model(Client())
    with pytest.raises(SafeAPIError) as caught:
        model._query([])
    assert model.http_attempts == 1 and not model.transport_events[0]['will_retry']
    assert 'private' not in str(caught.value)


def test_budget_submit_uses_environment_gate_without_another_model_call(tmp_path):
    from minisweagent.config import builtin_config_dir
    from mendpilot.core.verification import VerificationEnvironment
    import yaml
    class Model:
        def format_message(self, **kwargs): return kwargs
    class Gate:
        def execute(self, action):
            assert action['command'] == 'python /tmp/mendpilot_tools.py submit'
            raise Submitted({'role':'exit','content':'Submitted','extra':{'submission':'checked patch'}})
    config=yaml.safe_load((builtin_config_dir/'benchmarks/swebench.yaml').read_text())['agent']
    config.update(step_limit=30, cost_limit=0)
    agent=RepairAgent(Model(), Gate(), **config)
    agent.n_calls=28
    with pytest.raises(Submitted): agent.query()
    assert agent.n_calls == 28
    assert agent.control_events[-1]['event']=='automatic_budget_submit'
