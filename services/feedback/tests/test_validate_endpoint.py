"""
Integration tests for /v1/feedback/validate endpoint.

Tests verify:
- Endpoint accepts valid requests
- Returns correct response format
- Detects various spam patterns
- Handles edge cases gracefully
"""

import pytest
from fastapi.testclient import TestClient

from services.feedback.main import app

# Mark all tests as unit tests (using TestClient, no real network calls)
pytestmark = pytest.mark.unit

client = TestClient(app)


class TestValidateEndpoint:
    """Tests for /v1/feedback/validate endpoint."""

    def test_validate_clean_content(self):
        """Test validation endpoint with clean content."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "This is legitimate feedback about a safety issue.",
                "recent_submissions_count": 2,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "is_spam" in data
        assert "confidence" in data
        assert "flags" in data
        assert "allow_submission" in data
        assert "reason" in data
        assert isinstance(data["flags"], list)

    def test_validate_invalid_user_id(self):
        """Test validation endpoint rejects invalid user_id."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "invalid-uuid",
                "content": "Test content",
                "recent_submissions_count": 0,
            },
        )
        assert response.status_code == 400
        assert "user_id must be a valid UUID" in response.json()["detail"]

    def test_validate_missing_fields(self):
        """Test validation endpoint requires all fields."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                # Missing content and recent_submissions_count
            },
        )
        assert response.status_code == 422  # Validation error

    def test_validate_high_frequency(self):
        """Test validation endpoint detects high frequency submissions."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "Test feedback",
                "recent_submissions_count": 15,  # Above threshold
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_spam"] is True
        assert data["allow_submission"] is False
        assert "high_frequency" in data["flags"]

    def test_validate_excessive_urls(self):
        """Test validation endpoint detects excessive URLs."""
        urls = " ".join(["https://example.com"] * 5)
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": f"Check these links: {urls}",
                "recent_submissions_count": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        # Should flag excessive URLs
        assert data["is_spam"] is True or "excessive_urls" in data["flags"]

    def test_validate_suspicious_urls(self):
        """Test validation endpoint detects suspicious URLs."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "Click here: https://bit.ly/suspicious",
                "recent_submissions_count": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        # Should flag suspicious URLs
        assert data["is_spam"] is True or "suspicious_urls" in data["flags"]

    def test_validate_repeated_words(self):
        """Test validation endpoint detects repeated words."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "spam spam spam spam spam spam spam spam",
                "recent_submissions_count": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        # Should flag repeated words
        assert data["is_spam"] is True or "repeated_words" in data["flags"]

    def test_validate_excessive_caps(self):
        """Test validation endpoint detects excessive caps."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "URGENT!!! CLICK NOW!!! LIMITED TIME!!!",
                "recent_submissions_count": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        # Should flag excessive caps
        assert data["is_spam"] is True or "excessive_caps" in data["flags"]

    def test_validate_response_structure(self):
        """Test that response has correct structure."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "Test content",
                "recent_submissions_count": 0,
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Verify all required fields
        assert "is_spam" in data
        assert isinstance(data["is_spam"], bool)

        assert "confidence" in data
        assert isinstance(data["confidence"], float)
        assert 0.0 <= data["confidence"] <= 1.0

        assert "flags" in data
        assert isinstance(data["flags"], list)

        assert "allow_submission" in data
        assert isinstance(data["allow_submission"], bool)

        assert "reason" in data
        assert isinstance(data["reason"], str)

        # Verify logical consistency
        assert data["allow_submission"] == (not data["is_spam"])


class TestValidateEdgeCases:
    """Tests for edge cases in validation endpoint."""

    def test_validate_empty_content(self):
        """Test validation with empty content."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "",
                "recent_submissions_count": 0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        # Empty content should be handled gracefully
        assert isinstance(data["is_spam"], bool)

    def test_validate_very_long_content(self):
        """Test validation with very long content."""
        long_content = "This is a test. " * 1000
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": long_content,
                "recent_submissions_count": 0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["is_spam"], bool)

    def test_validate_special_characters(self):
        """Test validation with special characters."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "Test with émojis 🚀 and spéciál chàracters",
                "recent_submissions_count": 0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["is_spam"], bool)

    def test_validate_zero_submissions(self):
        """Test validation with zero recent submissions."""
        response = client.post(
            "/v1/feedback/validate",
            json={
                "user_id": "550e8400-e29b-41d4-a716-446655440000",
                "content": "Normal feedback",
                "recent_submissions_count": 0,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["allow_submission"] is True
