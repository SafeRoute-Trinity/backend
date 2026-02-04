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
    ):
        self.title = title
        self.description = description
        self.service_name = service_name
        self.version = version
        self.cors_config = cors_config or CORSMiddlewareConfig()
        self.enable_metrics = enable_metrics


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

        # Add health check endpoint (always enabled)
        self._add_health_endpoint(app)

        # Add Prometheus metrics middleware if enabled
        if self.config.enable_metrics and self.metrics:
            self._add_metrics_middleware(app)
            self._add_metrics_endpoint(app)

        # Store metrics in app state for access in routes
        app.state.metrics = self.metrics
        app.state.service_name = self.config.service_name

        return app

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
