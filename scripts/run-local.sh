#!/bin/bash
# Script to run a service locally with Python/uvicorn
# Usage: ./scripts/run-local.sh <service-name> [port]
# Example: ./scripts/run-local.sh feedback 20004

SERVICE=$1
PORT=${2:-20004}

if [ -z "$SERVICE" ]; then
    echo "Usage: $0 <service-name> [port]"
    echo "Example: $0 feedback 20004"
    echo ""
    echo "Available services:"
    echo "  - feedback (port 20004)"
    echo "  - notification (port 20001)"
    echo "  - routing_service (port 20002)"
    echo "  - safety_scoring (port 20003)"
    echo "  - graphhopper_proxy (port 20007)"
    echo "  - sos (port 20006)"
    echo "  - user_management (port 20000)"
    exit 1
fi

# Convert hyphens to underscores for Python module names
# This allows both "user-management" and "user_management" to work
SERVICE_MODULE=$(echo "$SERVICE" | tr '-' '_')

# Set LOCAL_DEV environment variable
export LOCAL_DEV=true
export PYTHONPATH="${PYTHONPATH}:$(pwd)"

echo "Starting $SERVICE service locally on port $PORT..."
echo "Service URLs will use localhost (automatic detection)"
echo ""

# Run the service using the normalized module name
python3 -m uvicorn services.${SERVICE_MODULE}.main:app --host 0.0.0.0 --port $PORT --reload
