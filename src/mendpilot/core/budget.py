"""Wall-clock budget shared by model and tool execution."""
import time
from minisweagent.exceptions import TimeExceeded


def time_exceeded():
    return TimeExceeded({"role": "exit", "content": "TimeExceeded",
                         "extra": {"exit_status": "TimeExceeded", "submission": ""}})


class BudgetEnvironment:
    def __init__(self, wrapped, deadline):
        self.wrapped = wrapped
        self.deadline = deadline
        self.tool_seconds = 0.0
        self.tool_calls = 0

    def execute(self, action, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise time_exceeded()
        kwargs["timeout"] = min(kwargs.get("timeout") or self.wrapped.config.timeout, remaining)
        started = time.monotonic()
        self.tool_calls += 1
        try:
            return self.wrapped.execute(action, **kwargs)
        finally:
            self.tool_seconds += time.monotonic() - started

    def get_template_vars(self, **kwargs):
        return self.wrapped.get_template_vars(**kwargs)

    def serialize(self):
        return self.wrapped.serialize()

    def cleanup(self):
        return self.wrapped.cleanup()
