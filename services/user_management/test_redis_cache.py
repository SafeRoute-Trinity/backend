#!/usr/bin/env python3
"""Test script for Redis cache functionality."""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Import the app from the current directory
import importlib.util

from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location(
    "main", os.path.join(os.path.dirname(__file__), "main.py")
)
main_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main_module)
app = main_module.app

# Set environment variables for local testing
os.environ["LOCAL_DEV"] = "true"
os.environ["REDIS_HOST"] = "localhost"
os.environ["REDIS_PORT"] = "6379"
os.environ["REDIS_PASSWORD"] = "testpass123"  # For testing with Docker Redis

client = TestClient(app)


def test_health_endpoint():
    """Test health endpoint with Redis status."""
    print("=" * 50)
    print("Testing Health Endpoint")
    print("=" * 50)
    # Try both /health and /health/ endpoints
    response = client.get("/health")
    if response.status_code == 404:
        response = client.get("/")
    print(f"Status Code: {response.status_code}")
    data = response.json()
    print(f"Response: {data}")
    print(f"Redis Status: {data.get('redis', 'unknown')}")
    return response.status_code == 200


def test_user_registration():
    """Test user registration and cache."""
    print("\n" + "=" * 50)
    print("Testing User Registration")
    print("=" * 50)
    register_data = {
        "email": "test@example.com",
        "password_hash": "hash123",
        "device_id": "dev_001",
        "name": "Test User",
        "phone": "+1234567890",
    }
    response = client.post("/v1/users/register", json=register_data)
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"User ID: {data.get('user_id')}")
        print(f"Email: {data.get('email')}")
        print(f"Token: {data.get('auth', {}).get('token')}")
        print(f"Status: {data.get('status')}")
        return data.get("user_id")
    else:
        print(f"Error: {response.text}")
        return None


def test_user_login(user_id=None):
    """Test user login and cache lookup."""
    print("\n" + "=" * 50)
    print("Testing User Login")
    print("=" * 50)
    login_data = {
        "email": "test@example.com",
        "password_hash": "hash123",
        "device_id": "dev_001",
    }
    response = client.post("/v1/auth/login", json=login_data)
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"User ID: {data.get('user_id')}")
        print(f"Status: {data.get('status')}")
        print(f"Token: {data.get('auth', {}).get('token')}")
        print(f"Last Login: {data.get('last_login')}")
        return data.get("user_id")
    else:
        print(f"Error: {response.text}")
        return None


def test_get_user(user_id):
    """Test getting user info from cache."""
    print("\n" + "=" * 50)
    print("Testing Get User (from cache)")
    print("=" * 50)
    response = client.get(f"/v1/users/{user_id}")
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"User ID: {data.get('user_id')}")
        print(f"Email: {data.get('email')}")
        print(f"Name: {data.get('name')}")
        print(f"Created At: {data.get('created_at')}")
        return True
    else:
        print(f"Error: {response.text}")
        return False


def main():
    """Run all tests."""
    print("\n" + "=" * 50)
    print("Redis Cache Functionality Test")
    print("=" * 50)

    # Test 1: Health endpoint
    if not test_health_endpoint():
        print("\n❌ Health endpoint test failed!")
        return

    # Test 2: User registration
    user_id = test_user_registration()
    if not user_id:
        print("\n❌ User registration test failed!")
        return

    # Test 3: User login
    login_user_id = test_user_login(user_id)
    if not login_user_id:
        print("\n❌ User login test failed!")
        return

    # Test 4: Get user (should use cache)
    if not test_get_user(user_id):
        print("\n❌ Get user test failed!")
        return

    print("\n" + "=" * 50)
    print("✅ All Tests Passed!")
    print("=" * 50)
    print("\nNote: If Redis is not running, the service will use in-memory storage.")
    print("To test with Redis, start a Redis server and set REDIS_HOST/REDIS_PORT.")


if __name__ == "__main__":
    main()
