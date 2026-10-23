import json
import uuid
import time
from openai import OpenAI
from litellm import ModelResponse
from minisweagent.models.litellm_model import LitellmModel
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from mendpilot.core.context import ContextWindow
from mendpilot.core.budget import time_exceeded
from mendpilot.models.common import fallback_command, SafeAPIError, error_category
from minisweagent.exceptions import LimitsExceeded, TimeExceeded

class WindowedModel(LitellmModel):
    def __init__(self, *, window: ContextWindow, **kwargs):
        super().__init__(**kwargs)
        self.window = window
        self.adapter_events = []
        self.http_attempts = 0
        self.deadline = None

    def _query(self, messages, **kwargs):
        if self.deadline is not None:
            import time
            from mendpilot.core.budget import time_exceeded
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise time_exceeded()
            kwargs['timeout'] = min(self.config.model_kwargs.get('timeout', 180), remaining)
        self.http_attempts += 1
        return super()._query(messages, **kwargs)

    def _parse_actions(self, response):
        message = response.choices[0].message
        if not message.tool_calls and response.choices[0].finish_reason != "length":
            command = fallback_command(message.content)
            if command:
                from litellm.types.utils import ChatCompletionMessageToolCall
                message.tool_calls = [ChatCompletionMessageToolCall(
                    id="mendpilot_" + uuid.uuid4().hex, type="function",
                    function={"name": "bash", "arguments": json.dumps({"command": command})})]
                self.adapter_events.append({"event": "text_tool_call_adapted"})
        return super()._parse_actions(response)

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        return self.window.select(super()._prepare_messages_for_api(messages))

class ChatModel(WindowedModel):
    """Chat SDK transport with bounded retries; key is absent from model config."""
    abort_exceptions = WindowedModel.abort_exceptions + [SafeAPIError, LimitsExceeded, TimeExceeded]

    def __init__(self, *, runtime_credential, **kwargs):
        super().__init__(**kwargs)
        options = self.config.model_kwargs
        self._client = OpenAI(api_key=runtime_credential, base_url=options['api_base'],
                              timeout=options.get('timeout', 180), max_retries=0)
        self.transport_events = []
        self.retry_limit = 2
        if any("key" in k.lower() or "authorization" in k.lower() for k in options):
            raise ValueError("Credentials must be supplied at runtime")

    def _query(self, messages, **kwargs):
        options = {k: v for k, v in self.config.model_kwargs.items()
                   if k not in {'api_base', 'timeout'}}
        for attempt in range(self.retry_limit + 1):
            remaining = self.deadline - time.monotonic() if self.deadline else 180
            if remaining <= 0:
                raise time_exceeded()
            self.http_attempts += 1
            try:
                response = self._client.chat.completions.create(
                    model=self.config.model_name.removeprefix('openai/'), messages=messages,
                    tools=[BASH_TOOL], timeout=min(remaining, self.config.model_kwargs['timeout']),
                    **(options | kwargs))
                return ModelResponse(**response.model_dump())
            except Exception as error:
                category = error_category(error)
                status = getattr(error, 'status_code', None)
                delay = 2 ** (attempt + 1)
                retrying = (attempt < self.retry_limit and category in {
                    'connection_error', 'connect_timeout', 'read_timeout', 'timeout',
                    'gateway_502', 'unavailable_503', 'gateway_timeout', 'rate_limit'}
                    and (not self.deadline or time.monotonic() + delay < self.deadline))
                self.transport_events.append({'category': category, 'status': status,
                                              'attempt': self.http_attempts, 'will_retry': retrying})
                if not retrying:
                    raise SafeAPIError(f'Chat request failed: {category}; status={status}',
                                       category=category, status=status) from None
                time.sleep(delay)
