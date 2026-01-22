"""
Service Discovery / Documentation Service
Provides a single entry point to discover all SafeRoute microservices.
"""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)

# Create service configuration
service_config = ServiceAppConfig(
    title="SafeRoute Services Discovery",
    description="Service discovery and documentation endpoint for all SafeRoute microservices.",
    service_name="service_discovery",
    cors_config=CORSMiddlewareConfig(),
    enable_metrics=False,  # This is just a discovery endpoint
)

# Create factory and build app
factory = FastAPIServiceFactory(service_config)
app = factory.create_app()


@app.get("/")
async def index():
    """Service discovery endpoint - lists all available services."""
    return {
        "services": {
            "user_management": "http://127.0.0.1:20000/docs",
            "notification": "http://127.0.0.1:20001/docs",
            "routing_service": "http://127.0.0.1:20002/docs",
            "safety_scoring": "http://127.0.0.1:20003/docs",
            "feedback": "http://127.0.0.1:20004/docs",
            "data_cleaner": "http://127.0.0.1:20005/docs",
            "sos": "http://127.0.0.1:20006/docs",
        },
        "description": "SafeRoute Microservices - Click on any service to view its API documentation",
    }
