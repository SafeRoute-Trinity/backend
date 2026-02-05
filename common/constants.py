"""
Application-wide constants for SafeRoute backend.

This module contains all shared constants used across the application.
"""

import os

# ========= Service Configuration =========
# Service configuration: service_name -> (module_path, port)
SERVICES = {
    "user_management": ("services.user_management.main", 20000),
    "notification": ("services.notification.main", 20001),
    "routing_service": ("services.routing_service.main", 20002),
    "safety_scoring": ("services.safety_scoring.main", 20003),
    "feedback": ("services.feedback.main", 20004),
    "data_cleaner": ("services.data_cleaner.main", 20005),
    "sos": ("services.sos.main", 20006),
}

# Docs service (service discovery)
DOCS_SERVICE = ("docs.main", 8080)

# ========= Auth Configuration =========
# Auth0 configuration
AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "saferouteapp.eu.auth0.com")
API_AUDIENCE = os.getenv("API_AUDIENCE", "https://saferouteapp.eu.auth0.com/api/v2/")
ISSUER = f"https://{AUTH0_DOMAIN}/"
JWKS_URL = f"{ISSUER}.well-known/jwks.json"
ALGORITHMS = ["RS256"]

# Auth Token TTL (1 hour in seconds)
AUTH_TOKEN_TTL = 3600
