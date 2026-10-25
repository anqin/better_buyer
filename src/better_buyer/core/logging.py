"""结构化日志与脱敏。"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid

_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|authorization|cookie|password|secret|token|session|手机号|身份证)",
    re.IGNORECASE,
)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def redact(value):
    if isinstance(value, dict):
        return {
            key: ("***" if _SENSITIVE_KEYS.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = getattr(record, "extra_fields", None)
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if getattr(root, "_bb_configured", False):
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level.upper())
    root._bb_configured = True  # type: ignore[attr-defined]


def get_logger(name: str = "better_buyer") -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    logger.info(event, extra={"extra_fields": redact(fields)})
