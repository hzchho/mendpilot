import json
from copy import deepcopy
import tiktoken
from minisweagent.exceptions import LimitsExceeded
from minisweagent.models.utils.actions_toolcall import BASH_TOOL
from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API

class ContextWindow:
    """Keep the initial instructions and recent complete assistant/tool turns.

    Only the API view is reduced; the agent's saved trajectory remains complete.
    No model-generated summary or evaluation metadata is used.
    """

    def __init__(self, tokenizer, max_input_tokens: int = 7000):
        self.tokenizer = tokenizer
        self.max_input_tokens = max_input_tokens
        self.events: list[dict] = []

    def count(self, messages: list[dict]) -> int:
        return len(self.tokenizer.apply_chat_template(
            messages, tools=[BASH_TOOL], tokenize=True, add_generation_prompt=True))

    def select(self, messages: list[dict]) -> list[dict]:
        if self.count(messages) <= self.max_input_tokens:
            return messages
        # Initial system/user pair is immutable. A turn starts at every assistant
        # message; its tool results and format-error feedback stay in the same group.
        prefix = messages[:2]
        groups: list[list[dict]] = []
        for message in messages[2:]:
            if message.get("role") == "assistant" or message.get("object") == "response" or not groups:
                groups.append([])
            groups[-1].append(message)
        removed = 0
        while len(groups) > 1:
            removed += len(groups.pop(0))
            notice = {"role": "user", "content": (
                f"[Context window: {removed} older messages omitted. The original task and recent "
                f"complete tool turns are retained. Files in {getattr(self, 'workspace_root', '/testbed')} have not been reset. "
                "Re-read a specific file if an older detail is needed.]"
            )}
            selected = prefix + [notice] + [item for group in groups for item in group]
            if self.count(selected) <= self.max_input_tokens:
                self.events.append({"original_messages": len(messages), "removed_messages": removed,
                                    "input_tokens": self.count(selected)})
                return selected
        # Shorten only tool result text in a COPY. Never cut arguments, opaque
        # reasoning or the task; all call/result IDs stay paired in API history.
        selected = deepcopy(prefix + [item for group in groups for item in group])
        for limit in (8000, 4000, 2000, 1000, 400, 100):
            for message in selected[2:]:
                field = "content" if message.get("role") == "tool" else (
                    "output" if message.get("type") == "function_call_output" else None)
                if field and isinstance(message.get(field), str) and len(message[field]) > limit:
                    value = message[field]
                    message[field] = (value[:limit // 2] + "\n[Tool output shortened to fit input budget; "
                                      "re-read a focused section if needed.]\n" + value[-limit // 2:])
            tokens = self.count(selected)
            if tokens <= self.max_input_tokens:
                self.events.append({"event": "tool_output_truncated", "original_messages": len(messages),
                                    "removed_messages": removed, "input_tokens": tokens,
                                    "output_character_limit": limit})
                return selected
        self.events.append({"event": "context_budget_exceeded", "input_tokens": self.count(selected)})
        raise LimitsExceeded({"role": "exit", "content": "ContextBudgetExceeded",
                              "extra": {"exit_status": "ContextBudgetExceeded", "submission": ""}})

class ResponseContextWindow(ContextWindow):
    """Conservative estimate, not a claim of an exact GPT-6.1 tokenizer.

    Count serialized replay items and tools using o200k_base, add 25% and 8192
    tokens for protocol/provider overhead. Preserve complete response/tool turns,
    including reasoning items and original assistant phase fields.
    """
    def __init__(self, max_input_tokens=24000):
        super().__init__(None, max_input_tokens)
        self.encoding = tiktoken.get_encoding("o200k_base")

    def count(self, messages):
        items = []
        for message in messages:
            if message.get("object") == "response":
                items.extend(message.get("output", []))
            else:
                items.append({k: v for k, v in message.items() if k != "extra"})
        serialized = json.dumps({"input": items, "tools": [BASH_TOOL_RESPONSE_API]}, ensure_ascii=False)
        return int(len(self.encoding.encode(serialized, disallowed_special=())) * 1.25) + 8192
