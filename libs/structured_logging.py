"""
Structured JSON logging for Azure Monitor / Log Analytics.

Stack
-----
- **Ingest**: Container Insights scrapes stdout; each log line is one JSON object.
- **Services**: FastAPI apps call ``setup_structured_logging(service_name)`` from
  ``FastAPIServiceFactory`` or (e.g. safety-scoring) at module startup.
- **Request correlation**: ``trace_id`` comes from ``libs.trace_context`` (HTTP
  ``X-Trace-ID`` / ``TRACE_HEADER``).
- **Replicas**: ``instance_id`` is ``POD_NAME`` or ``HOSTNAME`` (Kubernetes pod
  name) or the machine hostname for local dev.
- **CAS** (``libs.cas_logger``): transition lines add the extra keys listed in
  ``CAS_LOG_EXTRA_KEYS`` when present on the ``LogRecord`` (including
  ``cas_row_version`` after a successful enforcer write to ``saferoute.cas_state``).

Example base line::

    {"timestamp":"...","service":"sos","instance_id":"...","level":"INFO",
     "trace_id":"...","message":"...","module":"...","function":"...","line":42}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Final, Tuple

from libs.trace_context import trace_id_var

# Copied onto JSON output when set on the LogRecord (see libs.cas_logger).
CAS_LOG_EXTRA_KEYS: Final[Tuple[str, ...]] = (
    "cas_operation",
    "cas_sequence",
    "cas_expected_state",
    "cas_new_state",
    "cas_payload_hash",
    "cas_valid",
    "cas_detail",
    "cas_row_version",
    "cas_conflict",
)


def _logging_instance_id() -> str:
    """Stable per-process id: K8s pod name (POD_NAME / HOSTNAME) or machine hostname."""
    return (
        os.getenv("POD_NAME", "").strip()
        or os.getenv("HOSTNAME", "").strip()
        or socket.gethostname()
    )


class AzureJsonFormatter(logging.Formatter):
    """JSON formatter whose output Azure Log Analytics can parse with KQL."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name
        self._instance_id = _logging_instance_id()

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": self.service_name,
            "instance_id": self._instance_id,
            "level": record.levelname,
            "trace_id": trace_id_var.get(""),
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if record.exc_info and record.exc_info[1]:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))

        for key in CAS_LOG_EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val

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
