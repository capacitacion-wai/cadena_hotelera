"""WAI logging standard for the autonomous employee app: structured JSON on stdout.

Every WAI artifact of the tenant emits ONE JSON line per log (ts, level, service, logger, message,
tenant, + extras such as event/run_id/session_id, + error{type,stack}); a traceback stays on a
single line. That way promtail promotes `level` to a label and `{namespace} | json | level=~"ERROR|FATAL"`
is the list of the employee's REAL bugs. Hard rule: ERROR only for what needs attention.

No project dependencies (stdlib only) and `tenant` is read from the environment to avoid
circular imports with config.py.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}


def _tenant() -> str | None:
    for key in ("GENAI_API_TENANT", "OPENCODE_TENANT_NAME", "TENANT_NAME"):
        val = os.environ.get(key)
        if val:
            return val
    return None


class JsonFormatter(logging.Formatter):
    """One-line JSON formatter. Tags every record with `service` and `tenant`."""

    converter = time.gmtime  # UTC

    def __init__(self, service: str, tenant: str | None = None) -> None:
        super().__init__()
        self.service = service
        self.tenant = tenant

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.tenant:
            payload["tenant"] = self.tenant
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_") and key not in payload:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = {
                "type": record.exc_info[0].__name__,
                "stack": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with a JSON handler on stdout. Idempotent."""
    global _configured
    if _configured:
        return
    service = os.environ.get("WAI_APP_NAME") or "agent-app"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, _tenant()))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    _configured = True
