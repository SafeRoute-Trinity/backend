"""
Shared test fixtures for Auth0 JWT testing.

This module provides reusable fixtures for:
- Generating RSA key pairs for test JWT signing
- Creating mock JWKS endpoints
- Creating valid/expired/invalid test JWTs
- Mocking verify_token dependency
- Creating authenticated TestClient instances
"""

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from common.constants import ALGORITHMS, API_AUDIENCE, ISSUER


@pytest.fixture(scope="session")
def rsa_key_pair():
    """
    Generate RSA key pair for signing test JWTs.
    
    Returns:
        Dict with 'private_key' and 'public_key' in PEM format
    """
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    
    # Get private key in PEM format
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    # Get public key in PEM format
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    
    return {
        "private_key": private_pem.decode("utf-8"),
        "public_key": public_pem.decode("utf-8"),
    }


@pytest.fixture(scope="session")
def test_kid():
    """Return a test key ID for JWKS."""
    return "test-key-id-123"


@pytest.fixture(scope="session")
def mock_jwks(rsa_key_pair, test_kid):
    """
    Create a mock JWKS response matching Auth0 format.
    
    Args:
        rsa_key_pair: RSA key pair fixture
        test_kid: Test key ID
        
    Returns:
        Dict representing JWKS response
    """
    from jose.backends import RSAKey
    
    # Convert PEM to JWK format
    key = RSAKey(rsa_key_pair["public_key"], ALGORITHMS[0])
    jwk_dict = key.to_dict()
    
    # Add kid to match Auth0 format
    jwk_dict["kid"] = test_kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    
    return {
        "keys": [jwk_dict]
    }


@pytest.fixture
def create_valid_jwt(rsa_key_pair, test_kid):
    """
    Factory fixture to create valid test JWTs.
    
    Returns:
        Function that creates JWTs with custom claims
    """
    def _create_jwt(user_id: str = "test-user-123", **extra_claims) -> str:
        """
        Create a valid JWT token.
        
        Args:
            user_id: User identifier for 'sub' claim
            **extra_claims: Additional claims to include
            
        Returns:
            Encoded JWT string
        """
        now = int(time.time())
        payload = {
            "sub": user_id,
            "aud": API_AUDIENCE,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,  # Valid for 1 hour
            **extra_claims
        }
        
        headers = {"kid": test_kid}
        
        token = jwt.encode(
            payload,
            rsa_key_pair["private_key"],
            algorithm=ALGORITHMS[0],
            headers=headers
        )
        return token
    
    return _create_jwt


@pytest.fixture
def create_expired_jwt(rsa_key_pair, test_kid):
    """
    Factory fixture to create expired test JWTs.
    
    Returns:
        Function that creates expired JWTs
    """
    def _create_expired_jwt(user_id: str = "test-user-123") -> str:
        """
        Create an expired JWT token.
        
        Args:
            user_id: User identifier for 'sub' claim
            
        Returns:
            Encoded JWT string (expired)
        """
        now = int(time.time())
        payload = {
            "sub": user_id,
            "aud": API_AUDIENCE,
            "iss": ISSUER,
            "iat": now - 7200,  # Issued 2 hours ago
            "exp": now - 3600,  # Expired 1 hour ago
        }
        
        headers = {"kid": test_kid}
        
        token = jwt.encode(
            payload,
            rsa_key_pair["private_key"],
            algorithm=ALGORITHMS[0],
            headers=headers
        )
        return token
    
    return _create_expired_jwt


@pytest.fixture
def create_invalid_signature_jwt(test_kid):
    """
    Factory fixture to create JWTs with invalid signatures.
    
    Returns:
        Function that creates JWTs with wrong signature
    """
    def _create_invalid_jwt(user_id: str = "test-user-123") -> str:
        """
        Create a JWT with an invalid signature.
        
        Args:
            user_id: User identifier for 'sub' claim
            
        Returns:
            Encoded JWT string with wrong signature
        """
        # Generate a different key pair for signing
        wrong_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        
        wrong_private_pem = wrong_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode("utf-8")
        
        now = int(time.time())
        payload = {
            "sub": user_id,
            "aud": API_AUDIENCE,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        
        headers = {"kid": test_kid}
        
        # Sign with wrong key
        token = jwt.encode(
            payload,
            wrong_private_pem,
            algorithm=ALGORITHMS[0],
            headers=headers
        )
        return token
    
    return _create_invalid_jwt


@pytest.fixture
def create_invalid_audience_jwt(rsa_key_pair, test_kid):
    """
    Factory fixture to create JWTs with wrong audience.
    
    Returns:
        Function that creates JWTs with invalid audience
    """
    def _create_jwt(user_id: str = "test-user-123") -> str:
        """
        Create a JWT with wrong audience claim.
        
        Args:
            user_id: User identifier for 'sub' claim
            
        Returns:
            Encoded JWT string with wrong audience
        """
        now = int(time.time())
        payload = {
            "sub": user_id,
            "aud": "https://wrong-audience.com/api/",  # Wrong audience
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        
        headers = {"kid": test_kid}
        
        token = jwt.encode(
            payload,
            rsa_key_pair["private_key"],
            algorithm=ALGORITHMS[0],
            headers=headers
        )
        return token
    
    return _create_jwt


@pytest.fixture
def create_invalid_issuer_jwt(rsa_key_pair, test_kid):
    """
    Factory fixture to create JWTs with wrong issuer.
    
    Returns:
        Function that creates JWTs with invalid issuer
    """
    def _create_jwt(user_id: str = "test-user-123") -> str:
        """
        Create a JWT with wrong issuer claim.
        
        Args:
            user_id: User identifier for 'sub' claim
            
        Returns:
            Encoded JWT string with wrong issuer
        """
        now = int(time.time())
        payload = {
            "sub": user_id,
            "aud": API_AUDIENCE,
            "iss": "https://wrong-issuer.com/",  # Wrong issuer
            "iat": now,
            "exp": now + 3600,
        }
        
        headers = {"kid": test_kid}
        
        token = jwt.encode(
            payload,
            rsa_key_pair["private_key"],
            algorithm=ALGORITHMS[0],
            headers=headers
        )
        return token
    
    return _create_jwt


@pytest.fixture
def create_jwt_without_kid(rsa_key_pair):
    """
    Factory fixture to create JWTs without kid in header.
    
    Returns:
        Function that creates JWTs missing kid
    """
    def _create_jwt(user_id: str = "test-user-123") -> str:
        """
        Create a JWT without kid in header.
        
        Args:
            user_id: User identifier for 'sub' claim
            
        Returns:
            Encoded JWT string without kid header
        """
        now = int(time.time())
        payload = {
            "sub": user_id,
            "aud": API_AUDIENCE,
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
        }
        
        # No kid in headers
        token = jwt.encode(
            payload,
            rsa_key_pair["private_key"],
            algorithm=ALGORITHMS[0],
        )
        return token
    
    return _create_jwt


@pytest.fixture
def mock_jwks_request(mocker, mock_jwks):
    """
    Mock requests.get to return mock JWKS without hitting Auth0.
    
    Args:
        mocker: pytest-mock mocker fixture
        mock_jwks: Mock JWKS fixture
        
    Returns:
        Mocked requests.get function
    """
    mock_response = mocker.Mock()
    mock_response.json.return_value = mock_jwks
    mock_response.raise_for_status = mocker.Mock()
    
    # Patch where requests.get is used, not where it's defined
    mock_get = mocker.patch("libs.auth.auth0_verify.requests.get", return_value=mock_response)
    return mock_get


@pytest.fixture
def authenticated_client(create_valid_jwt):
    """
    Factory fixture to create TestClient with mocked auth.
    
    Returns:
        Function that creates authenticated TestClient
    """
    from fastapi.testclient import TestClient
    from services.user_management.main import app
    from libs.auth.auth0_verify import verify_token
    
    def _create_client(user_id: str = "test-user-123", **jwt_claims) -> TestClient:
        """
        Create a TestClient with mocked authentication.
        
        Args:
            user_id: User identifier for JWT sub claim
            **jwt_claims: Additional JWT claims
            
        Returns:
            TestClient with auth dependency overridden
        """
        # Create mock token payload
        payload = {
            "sub": user_id,
            "aud": API_AUDIENCE,
            "iss": ISSUER,
            **jwt_claims
        }
        
        # Override verify_token to return mock payload
        def mock_verify_token():
            return payload
        
        app.dependency_overrides[verify_token] = mock_verify_token
        
        return TestClient(app)
    
    return _create_client


# ============================================================================
# Integration Test Fixtures (for testing with real Auth0)
# ============================================================================

import os
from dotenv import load_dotenv
from common.constants import JWKS_URL

# Load test environment variables
load_dotenv('.env')


@pytest.fixture(scope="session")
def integration_enabled():
    """
    Check if integration tests are enabled.
    
    Returns:
        bool: True if RUN_INTEGRATION_TESTS=true in environment
    """
    return os.getenv("RUN_INTEGRATION_TESTS", "false").lower() == "true"


@pytest.fixture(scope="session")
def real_jwks_url():
    """
    Return the real Auth0 JWKS URL from constants.
    
    Returns:
        str: Full JWKS URL for Auth0
    """
    return JWKS_URL


@pytest.fixture(scope="session")
def real_auth0_jwt():
    """
    Return a real Auth0 JWT if provided in environment.
    
    Skips test if no JWT is provided.
    
    Returns:
        str: Real Auth0 JWT token
        
    Raises:
        pytest.skip: If AUTH0_TEST_JWT not provided
    """
    jwt_token = os.getenv("AUTH0_TEST_JWT")
    if not jwt_token:
        pytest.skip("AUTH0_TEST_JWT not provided - skipping real JWT test")
    return jwt_token


@pytest.fixture
def skip_if_integration_disabled(integration_enabled):
    """
    Skip test if integration tests are disabled.
    
    Usage:
        def test_something(skip_if_integration_disabled):
            # This test only runs if RUN_INTEGRATION_TESTS=true
            ...
    """
    if not integration_enabled:
        pytest.skip("Integration tests disabled (set RUN_INTEGRATION_TESTS=true to enable)")


@pytest.fixture(scope="session")
def real_auth0_config():
    """
    Return real Auth0 configuration.
    
    Returns:
        dict: Auth0 configuration with domain, audience, issuer, JWKS URL
    """
    return {
        "jwks_url": JWKS_URL,
        "audience": API_AUDIENCE,
        "issuer": ISSUER,
        "domain": os.getenv("AUTH0_DOMAIN", "saferouteapp.eu.auth0.com"),
    }

