"""
Structured JSON logging for Azure Monitor / Log Analytics.

Azure Monitor's DaemonSet agent (Container Insights) scrapes container
stdout/stderr automatically.  When the output is structured JSON, Azure
Log Analytics can parse each field natively in KQL via ``parse_json()``.

This module replaces Python's default plain-text log formatter with a
JSON formatter so that every ``logger.info(...)`` call in any service
produces a single JSON line like:

    {"timestamp":"2026-03-19T14:30:00.123456+00:00","service":"sos",
     "level":"ERROR","trace_id":"a1b2c3d4-...","message":"...",
     "module":"main","function":"call","line":214}

Call ``setup_structured_logging("sos")`` once at service startup.
The ``FastAPIServiceFactory`` does this automatically.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

from libs.trace_context import trace_id_var


class AzureJsonFormatter(logging.Formatter):
    """JSON formatter whose output Azure Log Analytics can parse with KQL."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": self.service_name,
            "level": record.levelname,
            "trace_id": trace_id_var.get(""),
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if record.exc_info and record.exc_info[1]:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(payload, default=str)


def setup_structured_logging(
    service_name: str,
    *,
    level: int = logging.INFO,
) -> None:
    """
    Replace the root logger's handlers with a single structured-JSON
    ``StreamHandler`` writing to stdout.

    Call once at startup — the ``FastAPIServiceFactory`` does this for you.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(AzureJsonFormatter(service_name))
    console.setLevel(level)
    root.addHandler(console)
