import json
import time
from openai import OpenAI
from minisweagent.models.litellm_response_model import LitellmResponseModel
from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API
from minisweagent.exceptions import LimitsExceeded, TimeExceeded
from mendpilot.core.budget import time_exceeded
from mendpilot.core.context import ResponseContextWindow
from mendpilot.models.common import SafeAPIError, error_category, fallback_command

class ResponseModel(LitellmResponseModel):
    """Unchanged upstream parsing/history, with an SDK-only transport override.

    No provider credential is accepted in
    model_kwargs, which upstream persists in trajectories and template variables.
    """
    abort_exceptions = LitellmResponseModel.abort_exceptions + [SafeAPIError, LimitsExceeded, TimeExceeded]

    def __init__(self, *, runtime_credential, **kwargs):
        super().__init__(**kwargs)
        self.deadline = None
        self.total_output_limit = 0
        self.output_tokens_used = 0
        self.transport_events = []
        self.http_attempts = 0
        self.retry_limit = 2
        if any("key" in key.lower() or "authorization" in key.lower()
               for key in self.config.model_kwargs):
            raise ValueError("Supply credentials through the runtime argument only")
        credential = runtime_credential
        if not credential:
            raise SafeAPIError("Missing runtime credential")
        options = self.config.model_kwargs
        self._client = OpenAI(api_key=credential, base_url=options["api_base"],
                              timeout=options.get("timeout", 180), max_retries=0)

    def _query(self, messages, **kwargs):
        options = {key: value for key, value in self.config.model_kwargs.items()
                   if key not in {"api_base", "timeout", "num_retries", "drop_params"}}
        if self.deadline is not None:
            remaining_seconds = self.deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise time_exceeded()
            kwargs["timeout"] = min(self.config.model_kwargs.get("timeout", 180), remaining_seconds)
        if self.total_output_limit:
            remaining_tokens = self.total_output_limit - self.output_tokens_used
            if remaining_tokens < 256:
                raise LimitsExceeded({"role": "exit", "content": "OutputTokenBudgetExceeded",
                                      "extra": {"exit_status": "OutputTokenBudgetExceeded", "submission": ""}})
            options["max_output_tokens"] = min(options.get("max_output_tokens", 32768), remaining_tokens)
        for attempt in range(getattr(self, "retry_limit", 0) + 1):
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise time_exceeded()
                kwargs["timeout"] = min(options.get("timeout", 180), self.config.model_kwargs.get("timeout", 180), remaining)
            self.http_attempts = getattr(self, "http_attempts", 0) + 1
            try:
                response = self._client.responses.create(
                    model=self.config.model_name.removeprefix("openai/"),
                    input=messages, tools=[BASH_TOOL_RESPONSE_API], **(options | kwargs))
                if response.usage:
                    self.output_tokens_used += response.usage.output_tokens or 0
                return response
            except Exception as error:
                status = getattr(error, "status_code", None)
                category = error_category(error)
                retryable = category in {"connection_error", "connect_timeout", "read_timeout", "timeout",
                                         "gateway_502", "unavailable_503", "gateway_timeout", "rate_limit"}
                retrying = retryable and attempt < getattr(self, "retry_limit", 0)
                delay = min(2 ** (attempt + 1), 8)
                if self.deadline is not None and time.monotonic() + delay >= self.deadline:
                    retrying = False
                if not hasattr(self, "transport_events"):
                    self.transport_events = []
                self.transport_events.append({"event": "api_error", "category": category,
                    "exception_type": type(error).__name__, "status": status,
                    "attempt": self.http_attempts, "will_retry": retrying})
                if not retrying:
                    raise SafeAPIError(f"Responses request failed: {category}; status={status}",
                                       category=category, status=status) from None
                time.sleep(delay)

class WindowedResponseModel(ResponseModel):
    def __init__(self, *, window, **kwargs):
        super().__init__(**kwargs)
        self.window = window
        self.retry_limit = 2
        self.adapter_events = []

    def _parse_actions(self, response):
        if not any(item.type == "function_call" for item in response.output) and response.status == "completed":
            texts = [part.text for item in response.output if item.type == "message"
                     for part in item.content if part.type == "output_text"]
            command = fallback_command("\n".join(texts))
            if command:
                import uuid
                from openai.types.responses import ResponseFunctionToolCall
                response.output.append(ResponseFunctionToolCall(type="function_call", name="bash",
                    call_id="mendpilot_" + uuid.uuid4().hex, arguments=json.dumps({"command": command})))
                self.adapter_events.append({"event": "text_tool_call_adapted"})
        return super()._parse_actions(response)

    def _prepare_messages_for_api(self, messages):
        return super()._prepare_messages_for_api(self.window.select(messages))
