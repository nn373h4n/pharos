"""JSON logging with secret masking.

Masking is attached to the handler, so every record is scrubbed no matter which logger emitted it.
Uvicorn's own loggers are routed through the same handler (see `configure_logging`).
"""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from re import Match
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
org_id_var: ContextVar[str | None] = ContextVar("org_id", default=None)

MASK = "***"

# A key is secret when it ENDS with one of these words: access_token, client_secret, x-api-key,
# db_password. Counters such as max_tokens or tokens_in, and flags such as password_reset_sent, are
# not secrets and stay readable.
_KEY_WORDS = (
    r"(?:api[_-]?key|apikey|access[_-]?key|private[_-]?key|secret|token|password|passwd|"
    r"credential|session[_-]?id)"
)
_SECRET_KEY_RE = re.compile(rf"{_KEY_WORDS}$", re.IGNORECASE)
_LINE_HEADER_RE = re.compile(r"(?:proxy[_-])?authorization$|(?:set[_-])?cookie$", re.IGNORECASE)

_UNQUOTED = r"[^\s\"',;&]+"
_QUOTED = r"\"[^\"]*\"|'[^']*'"


def _mask_keyed_value(match: Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if value[0] in "\"'" else ""
    return f"{match.group('head')}{quote}{MASK}{quote}"


_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Header-style values run to the end of the line, because the credential has several parts
    # (scheme + token) or contains separators (cookies).
    (
        re.compile(
            r"(?i)(?P<head>\b(?:proxy[_-])?authorization[\"']?\s*[:=]\s*)(?P<value>"
            rf"{_QUOTED}|[^\r\n]+)"
        ),
        _mask_keyed_value,
    ),
    (
        re.compile(
            rf"(?i)(?P<head>\b(?:set[_-])?cookie[\"']?\s*[:=]\s*)(?P<value>{_QUOTED}|[^\r\n]+)"
        ),
        _mask_keyed_value,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {MASK}"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        MASK,
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghs)_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    # The user part may be empty, as in redis://:password@host
    (re.compile(r"(://[^:/\s@]*:)[^@\s]+(@)"), rf"\1{MASK}\2"),
    # Query strings: ?api_key=..., &code=..., &state=... (OIDC callbacks carry code and state)
    (
        re.compile(
            r"(?i)(?P<head>[?&][\w.-]*(?:key|token|secret|password|passwd|signature|code|state)"
            r"[\w.-]*=)(?P<value>[^&\s\"']+)"
        ),
        lambda m: f"{m.group('head')}{MASK}",
    ),
    # key=value, key: value and "key": "value", including prefixed names like client_secret
    (
        re.compile(
            rf"(?i)(?P<head>\b[\w-]*{_KEY_WORDS}[\"']?\s*[:=]\s*)(?P<value>{_QUOTED}|{_UNQUOTED})"
        ),
        _mask_keyed_value,
    ),
]

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def mask_secrets(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return bool(_SECRET_KEY_RE.search(normalized) or _LINE_HEADER_RE.search(normalized))


def _mask_value(key: str, value: Any) -> Any:
    if _is_secret_key(key):
        return MASK
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {str(k): _mask_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask_value(key, v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return mask_secrets(str(value))


class MaskingFilter(logging.Filter):
    """Scrubs the message and its arguments before any formatter sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_secrets(record.getMessage()),
            "request_id": request_id_var.get(),
            "org_id": org_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = _mask_value(key, value)
        if record.exc_info:
            payload["exception"] = mask_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(MaskingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn installs its own unfiltered handlers with propagate=False. That would print tracebacks
    # and access lines (with query strings) unmasked, so hand its loggers over to the root handler.
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
