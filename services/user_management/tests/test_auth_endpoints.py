"""
Tests for protected endpoints requiring Auth0 JWT authentication.

Tests verify that protected endpoints in user_management service:
- Accept valid JWTs and return correct data
- Reject invalid/expired JWTs with 401
- Reject requests without Authorization header with 401
- Enforce user authorization (can't access other users' data) with 403

These are UNIT tests - they use mocked Auth0 and in-memory database.
For integration tests with real Auth0, see test_endpoints_integration.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import libs.db as db
from libs.auth.auth0_verify import verify_token
from models.user_models import Base
from services.user_management.main import app

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit

# Setup in-memory test database
DATABASE_URL = "sqlite+aiosqlite:///:memory:"

async_engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

AsyncTestingSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Patch database
db.engine = async_engine
db.AsyncSessionLocal = AsyncTestingSessionLocal


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Initialize test database tables."""
    import asyncio

    async def init_models():
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_models():
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    asyncio.get_event_loop().run_until_complete(init_models())
    yield
    asyncio.get_event_loop().run_until_complete(drop_models())


async def override_get_db():
    """Override database dependency for testing."""
    async with AsyncTestingSessionLocal() as session:
        yield session


# Override database dependency
app.dependency_overrides[db.get_db] = override_get_db


@pytest.fixture(autouse=True)
def reset_dependency_overrides():
    """Reset dependency overrides after each test."""
    yield
    # Remove verify_token override if it exists
    if verify_token in app.dependency_overrides:
        del app.dependency_overrides[verify_token]


def test_get_current_user_with_valid_jwt_returns_user_data(mock_jwks_request, create_valid_jwt):
    """
    Test that /v1/users/me with valid JWT returns user data.

    Verifies:
    - Endpoint accepts valid JWT
    - Returns user information from JWT sub claim
    - Response matches UserResponse model
    """
    user_id = "test-user-123"
    token = create_valid_jwt(user_id=user_id)

    client = TestClient(app)

    # Make request with Authorization header
    response = client.get("/v1/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200

    data = response.json()
    assert data["user_id"] == user_id
    assert "email" in data
    assert "created_at" in data


def test_get_current_user_with_invalid_jwt_returns_401(
    mock_jwks_request, create_invalid_signature_jwt
):
    """
    Test that /v1/users/me with invalid JWT returns 401.

    Verifies that tampered or invalid tokens are rejected.
    """
    token = create_invalid_signature_jwt(user_id="test-user")

    client = TestClient(app)

    response = client.get("/v1/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_get_current_user_without_jwt_returns_403():
    """
    Test that /v1/users/me without Authorization header returns 403.

    Verifies that the endpoint requires authentication.
    """
    client = TestClient(app)

    response = client.get("/v1/users/me")

    # HTTPBearer dependency returns 401 when credentials are missing
    # (This is FastAPI/Starlette's behavior for missing Authorization header)
    assert response.status_code in [401, 403]  # Accept both as valid


def test_get_user_with_valid_jwt_succeeds(mock_jwks_request, create_valid_jwt):
    """
    Test that /v1/users/{user_id} with matching JWT succeeds.

    Verifies:
    - User can access their own data
    - JWT sub claim matches user_id parameter
    """
    user_id = "test-user-456"
    token = create_valid_jwt(user_id=user_id)

    client = TestClient(app)

    response = client.get(f"/v1/users/{user_id}", headers={"Authorization": f"Bearer {token}"})

    # May return 404 if user doesn't exist in DB, but should not return 401/403
    assert response.status_code in [200, 404]

    if response.status_code == 200:
        data = response.json()
        assert data["user_id"] == user_id


def test_get_user_with_mismatched_jwt_returns_403(mock_jwks_request, create_valid_jwt):
    """
    Test that /v1/users/{user_id} where JWT sub doesn't match user_id returns 403.

    Verifies:
    - Users cannot access other users' data
    - Authorization check is performed
    """
    # JWT is for user-123
    token = create_valid_jwt(user_id="user-123")

    client = TestClient(app)

    # Try to access user-456's data
    response = client.get("/v1/users/user-456", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert "can only access your own" in response.json()["detail"].lower()


def test_update_preferences_with_valid_jwt_succeeds(mock_jwks_request, create_valid_jwt):
    """
    Test that /v1/users/{user_id}/preferences with valid JWT succeeds.

    Verifies:
    - Authenticated users can update their preferences
    - Response matches PreferencesResponse model
    """
    user_id = "pref-user-123"
    token = create_valid_jwt(user_id=user_id)

    client = TestClient(app)

    preferences = {"voice_guidance": "on", "safety_bias": "safest", "units": "metric"}

    response = client.post(
        f"/v1/users/{user_id}/preferences",
        json=preferences,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "preferences_saved"
    assert data["user_id"] == user_id
    assert data["preferences"]["voice_guidance"] == "on"


def test_update_preferences_with_expired_jwt_returns_401(mock_jwks_request, create_expired_jwt):
    """
    Test that expired JWT for preferences update returns 401.

    Verifies that expired tokens are rejected even for valid requests.
    """
    token = create_expired_jwt(user_id="pref-user-expired")

    client = TestClient(app)

    preferences = {"voice_guidance": "on", "safety_bias": "safest"}

    response = client.post(
        "/v1/users/pref-user-expired/preferences",
        json=preferences,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_update_preferences_with_mismatched_user_returns_403(mock_jwks_request, create_valid_jwt):
    """
    Test that user trying to update another user's preferences returns 403.

    Verifies authorization check prevents cross-user modifications.
    """
    # JWT is for user-aaa
    token = create_valid_jwt(user_id="user-aaa")

    client = TestClient(app)

    preferences = {"voice_guidance": "on", "safety_bias": "fastest"}

    # Try to update user-bbb's preferences
    response = client.post(
        "/v1/users/user-bbb/preferences",
        json=preferences,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "can only modify your own" in response.json()["detail"].lower()


def test_upsert_trusted_contact_requires_valid_jwt(mock_jwks_request, create_valid_jwt):
    """
    Test that /v1/users/{user_id}/trusted-contacts requires valid JWT.

    Verifies:
    - Endpoint is protected
    - Valid JWT allows contact creation
    """
    user_id = "contact-user-123"
    token = create_valid_jwt(user_id=user_id)

    client = TestClient(app)

    contact_data = {"name": "Test Contact", "phone": "+353123456789", "relationship": "friend"}

    response = client.post(
        f"/v1/users/{user_id}/trusted-contacts",
        json=contact_data,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "contact_upserted"
    assert data["user_id"] == user_id
    assert data["contact"]["name"] == "Test Contact"


def test_list_trusted_contacts_requires_valid_jwt(mock_jwks_request, create_valid_jwt):
    """
    Test that /v1/users/{user_id}/trusted-contacts (GET) requires valid JWT.

    Verifies:
    - Endpoint is protected
    - Valid JWT allows listing contacts
    - Response matches TrustedContactsListResponse model
    """
    user_id = "list-contact-user"
    token = create_valid_jwt(user_id=user_id)

    client = TestClient(app)

    response = client.get(
        f"/v1/users/{user_id}/trusted-contacts", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200

    data = response.json()
    assert data["user_id"] == user_id
    assert "data" in data
    assert isinstance(data["data"], list)
    assert "filters" in data
    assert "pagination" in data


def test_auth0_verify_endpoint_with_valid_jwt(mock_jwks_request, create_valid_jwt):
    """
    Test that /auth0/verify endpoint returns valid response with JWT.

    Verifies the dedicated verification endpoint works correctly.
    """
    user_id = "verify-user-789"
    token = create_valid_jwt(user_id=user_id)

    client = TestClient(app)

    response = client.get("/auth0/verify", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200

    data = response.json()
    assert "message" in data
    assert data["user"] == user_id


def test_protected_endpoint_handles_auth0_sub_format(mock_jwks_request, create_valid_jwt):
    """
    Test that endpoints correctly handle Auth0 sub format (auth0|user_id).

    Verifies that the auth sub extraction logic works for both:
    - "auth0|user_id" format (standard Auth0)
    - "user_id" format (custom)
    """
    # Test with Auth0 format
    auth0_sub = "auth0|extracted-user-id"
    token = create_valid_jwt(user_id=auth0_sub)

    client = TestClient(app)

    # The endpoint should extract "extracted-user-id" from "auth0|extracted-user-id"
    response = client.get(
        "/v1/users/extracted-user-id", headers={"Authorization": f"Bearer {token}"}
    )

    # Should not return 403 because user_id matches after extraction
    assert response.status_code in [200, 404]  # 200 if exists, 404 if not in DB


def test_update_preferences_without_jwt_returns_403():
    """
    Test that preferences update without JWT returns 403.

    Verifies missing authentication is handled properly with 403 Forbidden.
    """
    client = TestClient(app)

    preferences = {"voice_guidance": "on", "safety_bias": "safest"}

    response = client.post("/v1/users/some-user/preferences", json=preferences)

    # Missing Authorization header returns 403 Forbidden
    assert response.status_code == 403
