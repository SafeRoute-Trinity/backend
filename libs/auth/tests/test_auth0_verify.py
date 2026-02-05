"""
Tests for Auth0 JWT verification module.

Tests verify_token function behavior including:
- Valid JWT verification
- Expired token rejection
- Invalid signature rejection
- Invalid audience/issuer rejection
- Missing claims handling
- JWKS fetching and key selection

These are UNIT tests - they use mocked Auth0 endpoints.
For integration tests with real Auth0, see test_auth0_integration.py
"""

import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from libs.auth.auth0_verify import verify_token

# Mark all tests in this file as unit tests
pytestmark = pytest.mark.unit


def test_verify_valid_jwt_with_correct_signature(
    mock_jwks_request, create_valid_jwt
):
    """
    Test that a valid JWT with correct signature returns the payload.
    
    This verifies the happy path where:
    - JWT is properly signed
    - JWT is not expired
    - Audience and issuer match expected values
    - JWKS can be fetched successfully
    """
    # Create a valid JWT
    user_id = "auth0|test-user-123"
    token = create_valid_jwt(user_id=user_id)
    
    # Mock credentials object
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Call verify_token
    payload = verify_token(credentials=credentials)
    
    # Verify payload is returned correctly
    assert payload is not None
    assert payload["sub"] == user_id
    assert "aud" in payload
    assert "iss" in payload
    assert "exp" in payload


def test_verify_expired_jwt_returns_401(
    mock_jwks_request, create_expired_jwt
):
    """
    Test that an expired JWT is rejected with 401 and 'Token expired' detail.
    
    Verifies that jwt.ExpiredSignatureError is caught and converted
    to HTTPException with appropriate status code and message.
    """
    # Create an expired JWT
    token = create_expired_jwt(user_id="test-user-expired")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify status code and detail message
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc_info.value.detail == "Token expired"


def test_verify_jwt_with_wrong_signature_returns_401(
    mock_jwks_request, create_invalid_signature_jwt
):
    """
    Test that a JWT with wrong signature is rejected with 401.
    
    Verifies that tampered tokens (signed with different key)
    are detected and rejected.
    """
    # Create JWT signed with wrong key
    token = create_invalid_signature_jwt(user_id="test-user-tampered")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify 401 status code
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    # Detail will contain "Token verification failed"
    assert "Token verification failed" in exc_info.value.detail


def test_verify_jwt_with_wrong_audience_returns_401(
    mock_jwks_request, create_invalid_audience_jwt
):
    """
    Test that a JWT with wrong audience claim is rejected with 401.
    
    Verifies that audience validation is performed and tokens
    with incorrect audience are rejected.
    """
    # Create JWT with wrong audience
    token = create_invalid_audience_jwt(user_id="test-user-wrong-aud")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify status code and detail message
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc_info.value.detail == "Invalid audience"


def test_verify_jwt_with_wrong_issuer_returns_401(
    mock_jwks_request, create_invalid_issuer_jwt
):
    """
    Test that a JWT with wrong issuer claim is rejected with 401.
    
    Verifies that issuer validation is performed and tokens
    with incorrect issuer are rejected.
    """
    # Create JWT with wrong issuer
    token = create_invalid_issuer_jwt(user_id="test-user-wrong-iss")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify status code and detail message
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc_info.value.detail == "Invalid issuer"


def test_verify_jwt_missing_kid_header_returns_401(
    mock_jwks_request, create_jwt_without_kid
):
    """
    Test that a JWT without 'kid' in header is rejected with 401.
    
    Verifies that tokens missing the key ID cannot be verified
    because we can't match them to a JWKS key.
    """
    # Create JWT without kid header
    token = create_jwt_without_kid(user_id="test-user-no-kid")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify 401 status code
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    # Detail should mention missing kid
    assert "kid" in exc_info.value.detail.lower()


def test_jwks_fetching_and_key_selection(
    mock_jwks_request, create_valid_jwt, test_kid, mock_jwks
):
    """
    Test that JWKS is fetched and the correct signing key is selected by kid.
    
    Verifies:
    - requests.get is called with JWKS URL
    - The key with matching kid is selected from JWKS
    - JWT verification succeeds with the correct key
    """
    from common.constants import JWKS_URL
    
    # Create valid JWT
    token = create_valid_jwt(user_id="test-jwks-user")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Call verify_token
    payload = verify_token(credentials=credentials)
    
    # Verify that requests.get was called with JWKS URL
    mock_jwks_request.assert_called_once()
    call_args = mock_jwks_request.call_args
    assert JWKS_URL in call_args[0][0]
    
    # Verify payload contains expected data
    assert payload["sub"] == "test-jwks-user"
    
    # Verify the JWKS contains our test kid
    assert any(key.get("kid") == test_kid for key in mock_jwks["keys"])


def test_jwks_fetch_ssl_error_returns_401(mocker, create_valid_jwt):
    """
    Test that SSL errors when fetching JWKS return 401.
    
    Verifies that requests.exceptions.SSLError is caught
    and converted to HTTPException with appropriate message.
    """
    import requests
    
    # Mock requests.get to raise SSLError
    mocker.patch(
        "requests.get",
        side_effect=requests.exceptions.SSLError("SSL certificate verification failed")
    )
    
    token = create_valid_jwt(user_id="test-ssl-error")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify status code and detail message
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "SSL error fetching JWKS" in exc_info.value.detail


def test_jwks_fetch_http_error_returns_401(mocker, create_valid_jwt):
    """
    Test that HTTP errors when fetching JWKS return 401.
    
    Verifies that requests.RequestException is caught
    and converted to HTTPException with appropriate message.
    """
    import requests
    
    # Mock requests.get to raise RequestException
    mocker.patch(
        "requests.get",
        side_effect=requests.RequestException("Connection timeout")
    )
    
    token = create_valid_jwt(user_id="test-http-error")
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Verify that HTTPException is raised
    with pytest.raises(HTTPException) as exc_info:
        verify_token(credentials=credentials)
    
    # Verify status code and detail message
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "HTTP error fetching JWKS" in exc_info.value.detail


def test_verify_jwt_with_custom_claims(
    mock_jwks_request, create_valid_jwt
):
    """
    Test that custom claims in JWT are preserved in the payload.
    
    Verifies that additional claims (beyond standard ones)
    are included in the returned payload.
    """
    # Create JWT with custom claims
    custom_claims = {
        "email": "test@example.com",
        "role": "admin",
        "permissions": ["read", "write"]
    }
    token = create_valid_jwt(
        user_id="test-custom-claims",
        **custom_claims
    )
    
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token
    )
    
    # Call verify_token
    payload = verify_token(credentials=credentials)
    
    # Verify custom claims are in payload
    assert payload["email"] == custom_claims["email"]
    assert payload["role"] == custom_claims["role"]
    assert payload["permissions"] == custom_claims["permissions"]
