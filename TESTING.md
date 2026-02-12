# Running Tests - Quick Reference

## Test Types

### Unit Tests (Fast, Mocked)

- Use mocked Auth0 JWKS
- Use in-memory database
- No external network calls
- Run in < 2 seconds

### Integration Tests (Slower, Real Auth0)

- Fetch real JWKS from Auth0
- Optionally use real Auth0 JWTs
- Require network connection
- Verify actual Auth0 integration

## Quick Commands

### Run ALL Unit Tests (Default)

```powershell
pytest -m "unit" -v
```

### Run Specific Unit Test File

```powershell
pytest libs/auth/tests/test_auth0_verify.py -v
```

### Run ALL Integration Tests

Enable integration tests in .env file
RUN_INTEGRATION_TESTS="true"

# Run tests

```powershell
pytest -m "integration" -v
```

### Run Specific Integration Test

```powershell
pytest libs/auth/tests/test_auth0_integration.py::test_fetch_real_jwks_from_auth0 -v
```

### Run Integration Tests with Real JWT

Set your real Auth0 JWT in .env file
AUTH0_TEST_JWT="..."

```powershell
# Run tests
pytest -m "integration" -v
```

### Run ALL Tests (Unit + Integration)

```powershell
pytest -v
```

### Run with Coverage

```powershell
pytest -m "unit" --cov=libs/auth --cov=services/user_management --cov-report=html
```

## Test File Locations

**Unit Tests:**

- `libs/auth/tests/test_auth0_verify.py` - JWT verification (mocked)
- `services/user_management/tests/test_auth_endpoints.py` - Endpoints (mocked)

**Integration Tests:**

- `libs/auth/tests/test_auth0_integration.py` - Real Auth0 JWKS

**Configuration:**

- `.env.test` - Test environment variables
- `pytest.ini` - Pytest configuration
- `conftest.py` - Shared test fixtures

## Getting a Real Auth0 JWT for Testing = DONT FOLLOW THIS (temporarily)

### Option 1: Auth0 Dashboard (Easiest)

1. Go to https://saferouteapp.eu.auth0.com/
2. Navigate to Applications > Your App
3. Use the API Explorer to get a test token

### Option 2: Using curl

```powershell
curl --request POST `
  --url https://saferouteapp.eu.auth0.com/oauth/token `
  --header 'content-type: application/json' `
  --data '{
    "client_id":"YOUR_CLIENT_ID",
    "client_secret":"YOUR_CLIENT_SECRET",
    "audience":"https://saferouteapp.eu.auth0.com/api/v2/",
    "grant_type":"client_credentials"
  }'
```

### Option 3: From Frontend

1. Login to your app
2. Open browser DevTools > Application > Local Storage
3. Copy the access token

## CI/CD Integration

### GitHub Actions Example

```yaml
# Run unit tests on every commit (fast)
- name: Run Unit Tests
  run: pytest -m "unit" --cov=libs/auth

# Run integration tests only on main branch
- name: Run Integration Tests
  if: github.ref == 'refs/heads/main'
  env:
    RUN_INTEGRATION_TESTS: "true"
  run: pytest -m "integration"
```

## Troubleshooting

**Integration tests are skipped:**

- Set `RUN_INTEGRATION_TESTS=true` in environment or `.env.test`

**"AUTH0_TEST_JWT not provided":**

- Integration JWT verification tests need a real token
- Set `AUTH0_TEST_JWT` environment variable
- Or just run JWKS fetching tests (don't need JWT)

**Tests taking too long:**

- Run unit tests only: `pytest -m "unit"`
- Unit tests should complete in < 2 seconds

**Network errors in integration tests:**

- Check internet connection
- Verify Auth0 domain is accessible
- Check firewall/proxy settings
