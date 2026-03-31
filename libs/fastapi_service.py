"""
FastAPI Service Factory - Object-Oriented Factory Pattern
Creates standardized FastAPI applications with common middleware and metrics.
"""

import time
from typing import List, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from libs.cas_enforcer import CASConflictError, cas_enforcer
from libs.cas_logger import cas_log
from libs.cas_sync import cas_subscriber
from libs.rate_limiter import RateLimitConfig, RateLimiter, default_rate_limit_config
from libs.structured_logging import setup_structured_logging
from libs.trace_context import TRACE_HEADER, get_or_create_trace_id, trace_id_var


class ServiceMetrics:
    """Encapsulates Prometheus metrics for a service."""

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.registry = CollectorRegistry()

        # Standard metrics that all services share
        self.request_count = Counter(
            "service_requests_total",
            "Total HTTP requests handled by the service",
            ["service", "method", "path", "http_status"],
            registry=self.registry,
        )

        self.request_latency = Histogram(
            "service_request_duration_seconds",
            "Request latency in seconds",
            ["service", "path"],
            registry=self.registry,
        )

        # Business-specific metrics will be added by services
        self.business_metrics: List[Counter] = []

    def record_request(self, method: str, path: str, status_code: int, duration: float):
        """Record a request metric."""
        self.request_count.labels(
            service=self.service_name,
            method=method,
            path=path,
            http_status=status_code,
        ).inc()

        self.request_latency.labels(
            service=self.service_name,
            path=path,
        ).observe(duration)

    def get_metrics_prometheus(self) -> str:
        """Get Prometheus-formatted metrics."""
        return generate_latest(self.registry).decode("utf-8")


class CORSMiddlewareConfig:
    """Configuration for CORS middleware."""

    def __init__(
        self,
        allow_origins: List[str] = None,
        allow_credentials: bool = True,
        allow_methods: List[str] = None,
        allow_headers: List[str] = None,
    ):
        self.allow_origins = allow_origins or ["*"]
        self.allow_credentials = allow_credentials
        self.allow_methods = allow_methods or ["*"]
        self.allow_headers = allow_headers or ["*"]


class ServiceAppConfig:
    """Configuration for creating a service FastAPI app."""

    def __init__(
        self,
        title: str,
        description: str,
        service_name: str,
        version: str = "1.0.0",
        cors_config: Optional[CORSMiddlewareConfig] = None,
        enable_metrics: bool = True,
        rate_limit_config: Optional[RateLimitConfig] = None,
    ):
        self.title = title
        self.description = description
        self.service_name = service_name
        self.version = version
        self.cors_config = cors_config or CORSMiddlewareConfig()
        self.enable_metrics = enable_metrics
        # None → use sensible security defaults (auth endpoints are stricter).
        self.rate_limit_config: RateLimitConfig = (
            rate_limit_config if rate_limit_config is not None else default_rate_limit_config()
        )


class FastAPIServiceFactory:
    """
    Factory class for creating standardized FastAPI service applications.

    This factory encapsulates the common setup logic (CORS, metrics, middleware)
    so each service can focus on business logic.
    """

    def __init__(self, config: ServiceAppConfig):
        """
        Initialize the factory with service configuration.

        Args:
            config: ServiceAppConfig containing all service-specific settings
        """
        self.config = config
        self.metrics = ServiceMetrics(config.service_name) if config.enable_metrics else None
        self.rate_limiter = RateLimiter(config.rate_limit_config, config.service_name)

    def create_app(self) -> FastAPI:
        """
        Factory method: Creates and configures a FastAPI application.

        Returns:
            Fully configured FastAPI app ready for route registration
        """
        # Create the FastAPI app
        app = FastAPI(
            title=self.config.title,
            description=self.config.description,
            version=self.config.version,
        )

        # Add CORS middleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=self.config.cors_config.allow_origins,
            allow_credentials=self.config.cors_config.allow_credentials,
            allow_methods=self.config.cors_config.allow_methods,
            allow_headers=self.config.cors_config.allow_headers,
        )

        # Structured JSON logging to stdout (Azure Monitor scrapes this)
        self._setup_structured_logging()

        # Trace-ID middleware — must be the innermost middleware so that
        # trace_id_var is available to all subsequent handlers / loggers.
        self._add_trace_middleware(app)

        # CAS conflict middleware — catches CASConflictError from any
        # endpoint and returns a clean 409 Conflict response.
        self._add_cas_conflict_middleware(app)

        # Add health check endpoint (always enabled)
        self._add_health_endpoint(app)

        # Add rate limiting middleware.  Must be registered BEFORE the
        # Prometheus middleware so that Prometheus (added after = outer) wraps
        # the rate limiter and therefore records accurate 429 status codes.
        self._add_rate_limit_middleware(app)

        # Add Prometheus metrics middleware if enabled
        if self.config.enable_metrics and self.metrics:
            self._add_metrics_middleware(app)
            self._add_metrics_endpoint(app)

        # Add readiness endpoint for K8s (checks DB + Redis + CAS sync)
        self._add_readiness_endpoint(app)

        # Lifecycle: initialize CAS enforcer + sync subscriber on startup,
        # and tear down on shutdown.
        svc_name = self.config.service_name
        rate_limiter = self.rate_limiter

        @app.on_event("startup")
        async def _startup_cas() -> None:
            await cas_enforcer.initialize(svc_name)
            cas_log.attach_enforcer(cas_enforcer)
            await cas_subscriber.start()

        @app.on_event("shutdown")
        async def _shutdown() -> None:
            await cas_subscriber.stop()
            await cas_enforcer.close()
            await rate_limiter.close()

        # Store metrics in app state for access in routes
        app.state.metrics = self.metrics
        app.state.service_name = self.config.service_name

        return app

    def _setup_structured_logging(self):
        """Switch the root logger to structured JSON output for Azure Monitor."""
        setup_structured_logging(self.config.service_name)

    def _add_trace_middleware(self, app: FastAPI):
        """Inject/propagate X-Trace-ID on every request."""

        @app.middleware("http")
        async def trace_middleware(request: Request, call_next):
            incoming = request.headers.get(TRACE_HEADER)
            tid = get_or_create_trace_id(incoming)
            trace_id_var.set(tid)

            response = await call_next(request)
            response.headers[TRACE_HEADER] = tid
            return response

    def _add_cas_conflict_middleware(self, app: FastAPI):
        """Return HTTP 409 when a CAS transition conflicts."""

        @app.middleware("http")
        async def cas_conflict_middleware(request: Request, call_next):
            try:
                return await call_next(request)
            except CASConflictError as exc:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "State conflict — another replica modified this operation",
                        "operation": exc.operation,
                        "expected": exc.expected,
                        "trace_id": exc.trace_id,
                    },
                )

    def _add_readiness_endpoint(self, app: FastAPI):
        """K8s readiness probe: DB reachable, Redis connected, CAS enforcer ready."""

        @app.get("/ready")
        async def readiness_check():
            checks = {
                "cas_enforcer": cas_enforcer.ready,
                "cas_sync": cas_subscriber.is_connected,
            }
            last = cas_subscriber.last_event_at
            if last:
                checks["last_cas_event"] = last.isoformat()

            all_ok = checks["cas_enforcer"]
            return {
                "ready": all_ok,
                "checks": checks,
            }

    def _add_rate_limit_middleware(self, app: FastAPI):
        """Add Redis-backed rate limiting middleware to the app.

        The middleware runs *before* the business handler.  When a request
        exceeds the configured threshold it is rejected immediately with
        HTTP 429 and ``Retry-After`` / ``X-RateLimit-*`` headers.

        Exempt paths (``/health``, ``/metrics``, docs, CORS preflights) are
        always passed through without counting against any quota.
        """
        limiter = self.rate_limiter  # Capture for closure

        @app.middleware("http")
        async def rate_limit_middleware(request: Request, call_next):
            """Enforce rate limits; attach X-RateLimit-* headers to responses."""
            rejection = await limiter.check(request)
            if rejection is not None:
                return rejection

            response = await call_next(request)

            for header_name, header_value in getattr(
                request.state, "rate_limit_headers", {}
            ).items():
                response.headers[header_name] = header_value

            return response

    def _add_metrics_middleware(self, app: FastAPI):
        """Add Prometheus metrics middleware to the app."""
        metrics = self.metrics  # Capture for closure

        @app.middleware("http")
        async def prometheus_middleware(request: Request, call_next):
            """Middleware to capture request metrics."""
            start = time.time()
            response = await call_next(request)
            duration = time.time() - start

            metrics.record_request(
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration=duration,
            )

            return response

    def _add_metrics_endpoint(self, app: FastAPI):
        """Add /metrics endpoint for Prometheus scraping."""
        metrics = self.metrics  # Capture for closure

        @app.get("/metrics")
        async def metrics_endpoint():
            """Prometheus metrics endpoint."""
            return Response(
                content=metrics.get_metrics_prometheus(),
                media_type=CONTENT_TYPE_LATEST,
            )

    def _add_health_endpoint(self, app: FastAPI):
        """Add /health endpoint for health checks."""
        service_name = self.config.service_name  # Capture for closure

        @app.get("/health")
        async def health_check():
            """
            Health check endpoint.

            Returns:
                Dict with status and service name
            """
            return {"status": "ok", "service": service_name}

    def add_business_metric(self, name: str, description: str, labels: List[str] = None) -> Counter:
        """
        Add a business-specific metric counter.

        Args:
            name: Metric name
            description: Metric description
            labels: Optional list of label names

        Returns:
            Counter object that can be used to increment metrics
        """
        if not self.metrics:
            raise ValueError("Metrics not enabled for this service")

        counter = Counter(
            name,
            description,
            labels or [],
            registry=self.metrics.registry,
        )
        self.metrics.business_metrics.append(counter)
        return counter
