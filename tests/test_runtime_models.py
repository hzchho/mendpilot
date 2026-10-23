"""General-use credentials are kept out of upstream configuration serialization."""
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import AuthenticationError, InternalServerError

from mendpilot.models.responses import WindowedResponseModel, SafeAPIError
from mendpilot.models.chat import ChatModel
from mendpilot.models.common import clean_text


def test_both_transports_accept_only_runtime_credentials():
    for cls in [ChatModel, WindowedResponseModel]:
        model = cls(window=None, runtime_credential='fake-runtime-test-key',
                    model_name='openai/test', cost_tracking='ignore_errors',
                    model_kwargs={'api_base': 'http://127.0.0.1:1234/v1', 'timeout': 5})
        assert 'fake-runtime-test-key' not in json.dumps(model.config.model_dump())
        assert model._client.api_key == 'fake-runtime-test-key'
        model._client.close()


@pytest.mark.parametrize('status,retries', [(502, 3), (401, 1)])
def test_chat_retries_only_transient_errors_without_provider_body(monkeypatch, status, retries):
    class Client:
        calls = 0
        def create(self, **kwargs):
            self.calls += 1
            error = InternalServerError if status == 502 else AuthenticationError
            raise error('private provider body', response=httpx.Response(status,
                request=httpx.Request('POST', 'https://example.invalid')), body=None)
    client = Client()
    model = object.__new__(ChatModel)
    model.config = SimpleNamespace(model_name='openai/test', model_kwargs={'api_base': 'local', 'timeout': 5})
    model.deadline = time.monotonic() + 100
    model.http_attempts = 0
    model.retry_limit = 2
    model.transport_events = []
    model._client = SimpleNamespace(chat=SimpleNamespace(completions=client))
    monkeypatch.setattr('mendpilot.models.chat.time.sleep', lambda seconds: None)
    with pytest.raises(SafeAPIError) as error:
        model._query([])
    assert client.calls == model.http_attempts == retries
    assert 'private provider body' not in str(error.value)
    assert 'private provider body' not in json.dumps(model.transport_events)


def test_secret_redaction_handles_nonstandard_gateway_key():
    assert clean_text('log: unusual-test-credential', 'unusual-test-credential') == 'log: [REDACTED]'
