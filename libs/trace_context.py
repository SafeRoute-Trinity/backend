"""
Distributed trace / correlation-ID context for SafeRoute.

Every cross-service operation gets a unique ``trace_id`` that flows through
HTTP headers (``X-Trace-ID``).  The FastAPI middleware (in the service
factory) extracts or generates this automatically per request, and the
structured JSON logger includes it in every log line written to stdout.

Azure Monitor's DaemonSet scrapes stdout → Log Analytics ingests the JSON →
you query by ``trace_id`` in KQL to see the full lifecycle of a coordinator
atomic request across all services.

Usage in business logic:
    from libs.trace_context import trace_id_var

    current = trace_id_var.get()   # read the active trace id

When calling another service via httpx, propagate the header:
    headers = {"X-Trace-ID": trace_id_var.get()}
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

TRACE_HEADER = "X-Trace-ID"

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return str(uuid.uuid4())


def get_or_create_trace_id(incoming_header: str | None = None) -> str:
    """Return the incoming trace id if present, otherwise generate a new one."""
    if incoming_header and incoming_header.strip():
        return incoming_header.strip()
    return new_trace_id()
