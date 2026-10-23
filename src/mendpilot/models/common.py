import logging
import re
import json
from openai import APITimeoutError

class SafeAPIError(RuntimeError):
    def __init__(self, message, *, category="upstream_error", status=None):
        super().__init__(message)
        self.category = category
        self.status = status

def error_category(error):
    """Use types/status only: never serialize provider bodies, headers or keys."""
    status = getattr(error, "status_code", None)
    if isinstance(error, APITimeoutError):
        cause = error.__cause__
        return "connect_timeout" if type(cause).__name__ == "ConnectTimeout" else "read_timeout" if type(cause).__name__ == "ReadTimeout" else "timeout"
    if type(error).__name__ == "APIConnectionError":
        return "connection_error"
    return {429: "rate_limit", 502: "gateway_502", 503: "unavailable_503", 504: "gateway_timeout"}.get(
        status, "upstream_http_error" if status else "client_error")

class RuntimeSecretFilter(logging.Filter):
    def __init__(self, credential):
        super().__init__()
        self.credential = credential

    def filter(self, record):
        record.msg = clean_text(record.getMessage(), self.credential)
        record.args = ()
        record.exc_info = record.exc_text = None
        return True

def clean_text(text, credential):
    if credential:
        text = text.replace(credential, '[REDACTED]')
    return re.sub(r'\bsk-[A-Za-z0-9_-]{16,}\b', '[REDACTED]', text)

def fallback_command(text):
    """Accept one complete explicit bash payload, never partial JSON or prose."""
    if not isinstance(text, str):
        return None
    tagged = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.S)
    fenced = re.findall(r"```(\w*)\s*\n(.*?)```", text, re.S)
    candidate = None
    if tagged:
        if len(tagged) != 1 or fenced:
            return None
        candidate = tagged[0]
    elif len(fenced) == 1:
        language, payload = fenced[0]
        if language in ("bash", "sh", "shell"):
            return payload.strip() or None
        if language == "json":
            candidate = payload
    elif not fenced:
        candidate = text.strip()
    if candidate:
        try:
            obj = json.loads(candidate)
            if not isinstance(obj, dict):
                return None
            if set(obj) == {"command"}:
                arguments = obj
            elif obj.get("name") == "bash" and set(obj) <= {"name", "arguments"}:
                arguments = obj.get("arguments")
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
            else:
                return None
            if (isinstance(arguments, dict) and set(arguments) == {"command"}
                    and isinstance(arguments["command"], str)):
                return arguments["command"].strip() or None
        except (ValueError, TypeError):
            pass
    return None
