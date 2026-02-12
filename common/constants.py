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

# ========= Redis Configuration =========
# Redis connection settings
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
# Note: REDIS_PASSWORD should be read from env in redis_client, not here (security)

# ========= Token Blacklist Configuration =========
# Blacklist key prefix for revoked tokens
BLACKLIST_KEY_PREFIX = "auth:revoked:"
# Additional TTL buffer to account for clock skew (5 minutes)
BLACKLIST_TTL_BUFFER = 300

# ========= Session Management Configuration =========
# Session key prefixes for Redis
SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"
DEVICE_SESSION_KEY_PREFIX = "device_session:"

# Session TTL (30 days in seconds)
SESSION_TTL = 30 * 24 * 3600  # 2592000 seconds

# Session TTL strategy
# - "sliding": TTL is extended on activity (common for mobile)
# - "absolute": Session expires after fixed time no matter what
SESSION_TTL_STRATEGY = os.getenv("SESSION_TTL_STRATEGY", "sliding")  # sliding or absolute

# Sliding TTL refresh interval (update last_seen_at every N seconds)
# Only used when SESSION_TTL_STRATEGY is "sliding"
SESSION_SLIDING_REFRESH_INTERVAL = 600  # 10 minutes

# Absolute max session lifetime (90 days) - stored in session payload
# Used to enforce maximum session age even with sliding TTL
SESSION_ABSOLUTE_MAX_TTL = 90 * 24 * 3600  # 7776000 seconds
