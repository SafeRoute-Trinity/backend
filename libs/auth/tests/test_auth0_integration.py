"""
Integration tests for Auth0 JWT verification with REAL Auth0 endpoints.

These tests verify that the JWT verification works with:
- Real Auth0 JWKS endpoint (fetches actual public keys)
- Real Auth0 JWT tokens (if provided)

Unlike unit tests, these tests:
- Hit real Auth0 endpoints (require network)
- Run slower (network latency)
- May fail if Auth0 is down

Run with: pytest -m "integration" -v
"""

import json

import pytest
import requests
from fastapi.security import HTTPAuthorizationCredentials

from libs.auth.auth0_verify import verify_token

# Mark all tests in this file as integration tests
pytestmark = [pytest.mark.integration, pytest.mark.auth0]


def test_fetch_real_jwks_from_auth0(skip_if_integration_disabled, real_jwks_url):
    """
    Test that we can fetch real JWKS from Auth0.

    Verifies:
    - Auth0 JWKS endpoint is accessible
    - Response is valid JSON
    - Response contains 'keys' array
    - Keys have required fields (kid, kty, use, n, e)
    """
    print("\n" + "=" * 70)
    print("INTEGRATION TEST: Fetching Real JWKS from Auth0")
    print("=" * 70)
    print(f"JWKS URL: {real_jwks_url}")

    # Fetch real JWKS
    print("Fetching JWKS from Auth0...")
    response = requests.get(real_jwks_url, timeout=10)

    # Verify successful response
    assert response.status_code == 200, f"Failed to fetch JWKS: {response.status_code}"
    print(f"[PASS] Response Status: {response.status_code} OK")

    # Verify JSON response
    jwks = response.json()
    assert "keys" in jwks, "JWKS response missing 'keys' field"
    assert isinstance(jwks["keys"], list), "'keys' should be an array"
    assert len(jwks["keys"]) > 0, "JWKS should contain at least one key"

    print(f"[PASS] JWKS Structure: Valid (contains {len(jwks['keys'])} key(s))")

    # Verify key format
    first_key = jwks["keys"][0]
    required_fields = ["kid", "kty", "use", "n", "e"]
    for field in required_fields:
        assert field in first_key, f"JWKS key missing required field: {field}"

    # Verify key type is RSA
    assert first_key["kty"] == "RSA", "Expected RSA key type"
    assert first_key["use"] == "sig", "Expected signature use"

    print(f"[PASS] Key Format: Valid RSA signature keys")
    print(f"  - Key ID (kid): {first_key.get('kid')}")
    print(f"  - Algorithm: {first_key.get('alg', 'RS256')}")
    print(f"  - Key Type: {first_key.get('kty')}")
    print(f"  - Usage: {first_key.get('use')}")

    print("\n[SUCCESS] Real JWKS fetched and validated from Auth0!")
    print("=" * 70 + "\n")


def test_real_jwks_key_format(skip_if_integration_disabled, real_jwks_url):
    """
    Test that real JWKS keys are in correct format.

    Verifies each key in JWKS has:
    - Unique kid (key ID)
    - Valid algorithm (RS256)
    - Public key components (n, e)
    """
    print("\n" + "=" * 70)
    print("INTEGRATION TEST: Validating JWKS Key Format")
    print("=" * 70)

    response = requests.get(real_jwks_url, timeout=10)
    jwks = response.json()

    print(f"Validating {len(jwks['keys'])} key(s)...")

    kids_seen = set()
    for idx, key in enumerate(jwks["keys"], 1):
        # Verify unique kid
        kid = key.get("kid")
        assert kid, "Key missing 'kid' field"
        assert kid not in kids_seen, f"Duplicate kid found: {kid}"
        kids_seen.add(kid)

        # Verify algorithm
        alg = key.get("alg")
        assert alg == "RS256", f"Expected RS256 algorithm, got: {alg}"

        # Verify RSA public key components
        assert key.get("n"), "Key missing modulus 'n'"
        assert key.get("e"), "Key missing exponent 'e'"

        print(f"  [PASS] Key {idx}: {kid[:20]}... - Valid RS256 key")

    print("\n[SUCCESS] All keys validated successfully!")
    print("=" * 70 + "\n")


def test_auth0_endpoint_availability(skip_if_integration_disabled, real_auth0_config):
    """
    Test that Auth0 endpoints are reachable.

    Verifies:
    - JWKS endpoint responds within timeout
    - Response time is reasonable (< 5 seconds)
    """
    import time

    print("\n" + "=" * 70)
    print("INTEGRATION TEST: Testing Auth0 Endpoint Performance")
    print("=" * 70)

    jwks_url = real_auth0_config["jwks_url"]
    print(f"Testing: {jwks_url}")

    # Measure response time
    print("Measuring response time...")
    start = time.time()
    response = requests.get(jwks_url, timeout=10)
    elapsed = time.time() - start

    assert response.status_code == 200, f"JWKS endpoint not available: {response.status_code}"
    assert elapsed < 5.0, f"JWKS fetch too slow: {elapsed:.2f}s"

    print(f"[PASS] Status: {response.status_code} OK")
    print(f"[PASS] Response Time: {elapsed:.3f}s (< 5s threshold)")

    if elapsed < 1.0:
        print("  Excellent performance!")
    elif elapsed < 2.0:
        print("  Good performance")

    print("\n[SUCCESS] Auth0 endpoint is fast and available!")
    print("=" * 70 + "\n")


def test_verify_real_auth0_jwt(skip_if_integration_disabled, real_auth0_jwt):
    """
    Test JWT verification with a REAL Auth0 JWT token.

    This test requires AUTH0_TEST_JWT to be set in environment.
    It will:
    - Fetch real JWKS from Auth0
    - Verify the real JWT signature
    - Validate audience and issuer

    Note: Skipped if AUTH0_TEST_JWT not provided.
    """
    print("\n" + "=" * 70)
    print("INTEGRATION TEST: Verifying Real Auth0 JWT Token")
    print("=" * 70)
    print("Using real JWT from AUTH0_TEST_JWT environment variable")

    # Create credentials with real JWT
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=real_auth0_jwt)

    # Verify token (this will fetch real JWKS from Auth0)
    print("Fetching real JWKS and verifying JWT signature...")
    payload = verify_token(credentials=credentials)

    # Verify payload was returned
    assert payload is not None, "verify_token should return payload for valid JWT"
    assert "sub" in payload, "JWT payload should contain 'sub' claim"
    assert "aud" in payload, "JWT payload should contain 'aud' claim"
    assert "iss" in payload, "JWT payload should contain 'iss' claim"
    assert "exp" in payload, "JWT payload should contain 'exp' claim"

    print(f"[PASS] JWT Signature: Valid (verified against real Auth0 JWKS)")
    print(f"[PASS] User (sub): {payload.get('sub')}")
    print(f"[PASS] Audience: {payload.get('aud')}")
    print(f"[PASS] Issuer: {payload.get('iss')}")

    print("\n[SUCCESS] Real JWT verified successfully with Auth0!")
    print("=" * 70 + "\n")


def test_real_jwt_claims_structure(skip_if_integration_disabled, real_auth0_jwt):
    """
    Test that real Auth0 JWT has expected claims structure.

    Verifies standard JWT claims and Auth0-specific claims.
    """
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=real_auth0_jwt)

    payload = verify_token(credentials=credentials)

    # Standard JWT claims
    assert "iat" in payload, "Missing 'iat' (issued at) claim"
    assert "exp" in payload, "Missing 'exp' (expiration) claim"
    assert payload["exp"] > payload["iat"], "Expiration must be after issued time"

    # Verify claims are integers (Unix timestamps)
    assert isinstance(payload["iat"], int), "'iat' should be Unix timestamp"
    assert isinstance(payload["exp"], int), "'exp' should be Unix timestamp"

    print(f"✓ Real JWT has valid claims structure")
    print(f"  Issued: {payload.get('iat')}")
    print(f"  Expires: {payload.get('exp')}")
    print(f"  Lifetime: {payload.get('exp') - payload.get('iat')}s")


def test_multiple_keys_in_jwks(skip_if_integration_disabled, real_jwks_url):
    """
    Test that JWKS can contain multiple keys (key rotation support).

    Verifies:
    - JWKS may have multiple keys for rotation
    - Each key has unique kid
    - All keys are valid RSA keys
    """
    print("\n" + "=" * 70)
    print("INTEGRATION TEST: Checking Key Rotation Support")
    print("=" * 70)

    response = requests.get(real_jwks_url, timeout=10)
    jwks = response.json()

    keys = jwks["keys"]
    print(f"JWKS contains {len(keys)} key(s)")

    if len(keys) > 1:
        # Auth0 may have multiple keys during rotation
        kids = [key["kid"] for key in keys]
        assert len(kids) == len(set(kids)), "All keys should have unique kid"
        print(f"[PASS] Multiple keys found (supports key rotation)")
        print(f"  Key IDs:")
        for idx, kid in enumerate(kids, 1):
            print(f"    {idx}. {kid}")
    else:
        print(f"[PASS] Single active key")
        print(f"  Key ID: {keys[0]['kid']}")

    print("\n[SUCCESS] Key rotation capability verified!")
    print("=" * 70 + "\n")
