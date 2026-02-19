"""
Service URL configuration helper.
Works seamlessly in Docker Compose (dev), Kubernetes (prod), and local Python development.
Automatically detects the environment and uses appropriate URLs.
"""

import os

# Detect if running locally (not in Docker/K8s)
# Check if we're in a container or if LOCAL_DEV is explicitly set
IS_LOCAL_DEV = os.getenv("LOCAL_DEV", "false").lower() == "true"
IS_IN_CONTAINER = os.path.exists("/.dockerenv") or os.getenv("KUBERNETES_SERVICE_HOST") is not None

# If not in container and LOCAL_DEV not explicitly false, assume local dev
if not IS_IN_CONTAINER and not os.getenv("LOCAL_DEV") == "false":
    IS_LOCAL_DEV = True

# Service ports for local development (when running with uvicorn directly)
LOCAL_PORTS = {
    "notification": 20001,
    "routing": 20002,
    "safety_scoring": 20003,
    "feedback": 20004,
    "graphhopper_proxy": 20007,
    "sos": 20006,
    "user_management": 20000,
}


# Service base URLs
# In local dev: use localhost with original ports
# In Docker/K8s: use service names with port 80
def _get_service_url(service_name: str, local_port: int) -> str:
    """Get service URL based on environment."""
    # Check if explicitly overridden
    # Convert service name to env var format (uppercase, underscores)
    env_var = f"{service_name.upper().replace('-', '_')}_SERVICE_URL"
    if os.getenv(env_var):
        return os.getenv(env_var)

    # Local development: use localhost
    if IS_LOCAL_DEV:
        return f"http://localhost:{local_port}"

    # Docker/K8s: convert underscores to hyphens for service names
    docker_service_name = service_name.replace("_", "-")
    return f"http://{docker_service_name}-service:80"


# Service URLs - automatically adapts to environment
NOTIFICATION_SERVICE_URL = _get_service_url("notification", LOCAL_PORTS["notification"])
ROUTING_SERVICE_URL = _get_service_url("routing", LOCAL_PORTS["routing"])
SAFETY_SCORING_SERVICE_URL = _get_service_url("safety_scoring", LOCAL_PORTS["safety_scoring"])
FEEDBACK_SERVICE_URL = _get_service_url("feedback", LOCAL_PORTS["feedback"])
SOS_SERVICE_URL = _get_service_url("sos", LOCAL_PORTS["sos"])
USER_MANAGEMENT_SERVICE_URL = _get_service_url("user_management", LOCAL_PORTS["user_management"])
GRAPHHOPPER_PROXY_SERVICE_URL = _get_service_url(
    "graphhopper_proxy", LOCAL_PORTS["graphhopper_proxy"]
)
# Backward compatibility for older references.
CH_ROUTING_SERVICE_URL = GRAPHHOPPER_PROXY_SERVICE_URL
