#!/bin/bash

# Script to rebuild and push Docker images with correct platform for Kubernetes
# Usage: ./scripts/rebuild-image.sh <service_name>
# Example: ./scripts/rebuild-image.sh user_management

set -e

SERVICE=$1

if [ -z "$SERVICE" ]; then
    echo "❌ Error: Service name is required"
    echo "Usage: ./scripts/rebuild-image.sh <service_name>"
    echo "Available services: feedback, notification, routing_service, safety_scoring, sos, user_management"
    exit 1
fi

SERVICE_PATH="./services/$SERVICE"
DOCKERFILE="$SERVICE_PATH/dockerfile"
IMAGE_NAME="saferoute/$SERVICE:latest"

if [ ! -f "$DOCKERFILE" ]; then
    echo "❌ Error: Dockerfile not found at $DOCKERFILE"
    exit 1
fi

echo "🔧 Setting up Docker Buildx..."
docker buildx create --use --name multiarch-builder 2>/dev/null || docker buildx use multiarch-builder

echo "🚀 Building and pushing $IMAGE_NAME for linux/amd64 platform..."
docker buildx build \
    --platform linux/amd64 \
    -f "$DOCKERFILE" \
    -t "$IMAGE_NAME" \
    --push \
    .

echo "✅ Successfully built and pushed $IMAGE_NAME"
echo "📋 Image details:"
docker buildx imagetools inspect "$IMAGE_NAME"

