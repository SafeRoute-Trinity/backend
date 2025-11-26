"""
Unit tests for OpenRouteService integration endpoints.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.routing_service.main import app

client = TestClient(app)


@pytest.fixture
def mock_ors_response_route():
    """Mock OpenRouteService directions response."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-6.256, 53.342], [-6.262, 53.345]],
                },
                "properties": {
                    "summary": {"distance": 1000.0, "duration": 120.0},
                    "segments": [{"distance": 1000.0, "duration": 120.0}],
                },
            }
        ],
    }


@pytest.fixture
def mock_ors_response_isochrone():
    """Mock OpenRouteService isochrones response."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-6.26, 53.34],
                            [-6.27, 53.34],
                            [-6.27, 53.35],
                            [-6.26, 53.35],
                            [-6.26, 53.34],
                        ]
                    ],
                },
                "properties": {"value": 600, "range_type": "time"},
            }
        ],
    }


class TestRouteEndpoint:
    """Tests for /route endpoint."""

    def test_route_missing_api_key(self):
        """Test route endpoint when ORS_API_KEY is not set."""
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "services.routing_service.openrouteservice_client.OpenRouteServiceClient._is_enabled",
                return_value=False,
            ):
                response = client.get("/route?start=53.342,-6.256&end=53.345,-6.262")
                assert response.status_code == 503
                assert "ORS_API_KEY" in response.json()["detail"]

    def test_route_invalid_coordinates_format(self):
        """Test route endpoint with invalid coordinate format."""
        response = client.get("/route?start=invalid&end=53.345,-6.262")
        assert response.status_code == 400
        assert "Invalid coordinate format" in response.json()["detail"]

    def test_route_invalid_latitude(self):
        """Test route endpoint with invalid latitude."""
        response = client.get("/route?start=100,0&end=53.345,-6.262")
        assert response.status_code == 400
        assert "Invalid start coordinates" in response.json()["detail"]

    def test_route_invalid_longitude(self):
        """Test route endpoint with invalid longitude."""
        response = client.get("/route?start=53.342,200&end=53.345,-6.262")
        assert response.status_code == 400
        assert "Invalid start coordinates" in response.json()["detail"]

    @patch("services.routing_service.main.get_ors_client")
    def test_route_success(self, mock_get_client, mock_ors_response_route):
        """Test successful route request."""
        # Setup mock client
        mock_client = MagicMock()
        mock_client._is_enabled.return_value = True
        mock_client.get_directions = AsyncMock(return_value=mock_ors_response_route)
        mock_get_client.return_value = mock_client

        # Set API key
        with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
            response = client.get(
                "/route?start=53.342,-6.256&end=53.345,-6.262&profile=driving-car"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) > 0
        assert data["features"][0]["type"] == "Feature"
        assert data["features"][0]["geometry"]["type"] == "LineString"

    @patch("services.routing_service.main.get_ors_client")
    def test_route_ors_api_failure(self, mock_get_client):
        """Test route endpoint when OpenRouteService API fails."""
        mock_client = MagicMock()
        mock_client._is_enabled.return_value = True
        mock_client.get_directions = AsyncMock(return_value=None)
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
            response = client.get("/route?start=53.342,-6.256&end=53.345,-6.262")

        assert response.status_code == 502
        assert "Failed to get route" in response.json()["detail"]

    def test_route_custom_profile(self, mock_ors_response_route):
        """Test route endpoint with custom profile."""
        with patch("services.routing_service.main.get_ors_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client._is_enabled.return_value = True
            mock_client.get_directions = AsyncMock(return_value=mock_ors_response_route)
            mock_get_client.return_value = mock_client

            with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
                response = client.get(
                    "/route?start=53.342,-6.256&end=53.345,-6.262&profile=foot-walking"
                )

            assert response.status_code == 200
            mock_client.get_directions.assert_called_once()
            call_args = mock_client.get_directions.call_args
            assert call_args[1]["profile"] == "foot-walking"


class TestIsochroneEndpoint:
    """Tests for /isochrone endpoint."""

    def test_isochrone_missing_api_key(self):
        """Test isochrone endpoint when ORS_API_KEY is not set."""
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "services.routing_service.openrouteservice_client.OpenRouteServiceClient._is_enabled",
                return_value=False,
            ):
                response = client.get("/isochrone?location=53.342,-6.256")
                assert response.status_code == 503
                assert "ORS_API_KEY" in response.json()["detail"]

    def test_isochrone_invalid_coordinates_format(self):
        """Test isochrone endpoint with invalid coordinate format."""
        response = client.get("/isochrone?location=invalid")
        assert response.status_code == 400
        assert "Invalid coordinate format" in response.json()["detail"]

    def test_isochrone_invalid_range_format(self):
        """Test isochrone endpoint with invalid range format."""
        response = client.get("/isochrone?location=53.342,-6.256&range=invalid")
        assert response.status_code == 400
        assert "Invalid range format" in response.json()["detail"]

    def test_isochrone_invalid_range_type(self):
        """Test isochrone endpoint with invalid range_type."""
        response = client.get("/isochrone?location=53.342,-6.256&range_type=invalid")
        assert response.status_code == 400
        assert "Invalid range_type" in response.json()["detail"]

    @patch("services.routing_service.main.get_ors_client")
    def test_isochrone_success(self, mock_get_client, mock_ors_response_isochrone):
        """Test successful isochrone request."""
        mock_client = MagicMock()
        mock_client._is_enabled.return_value = True
        mock_client.get_isochrones = AsyncMock(return_value=mock_ors_response_isochrone)
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
            response = client.get(
                "/isochrone?location=53.342,-6.256&range=600,1200&range_type=time"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) > 0
        assert data["features"][0]["type"] == "Feature"
        assert data["features"][0]["geometry"]["type"] == "Polygon"

    @patch("services.routing_service.main.get_ors_client")
    def test_isochrone_ors_api_failure(self, mock_get_client):
        """Test isochrone endpoint when OpenRouteService API fails."""
        mock_client = MagicMock()
        mock_client._is_enabled.return_value = True
        mock_client.get_isochrones = AsyncMock(return_value=None)
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
            response = client.get("/isochrone?location=53.342,-6.256")

        assert response.status_code == 502
        assert "Failed to get isochrone" in response.json()["detail"]

    @patch("services.routing_service.main.get_ors_client")
    def test_isochrone_custom_parameters(
        self, mock_get_client, mock_ors_response_isochrone
    ):
        """Test isochrone endpoint with custom parameters."""
        mock_client = MagicMock()
        mock_client._is_enabled.return_value = True
        mock_client.get_isochrones = AsyncMock(return_value=mock_ors_response_isochrone)
        mock_get_client.return_value = mock_client

        with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
            response = client.get(
                "/isochrone?location=53.342,-6.256&profile=cycling-regular&range=300,600,900&range_type=distance"
            )

        assert response.status_code == 200
        mock_client.get_isochrones.assert_called_once()
        call_args = mock_client.get_isochrones.call_args
        assert call_args[1]["profile"] == "cycling-regular"
        assert call_args[1]["range"] == [300, 600, 900]
        assert call_args[1]["range_type"] == "distance"


class TestHealthEndpoint:
    """Tests for /health endpoint with OpenRouteService status."""

    def test_health_with_ors_enabled(self):
        """Test health endpoint when OpenRouteService is enabled."""
        with patch.dict(os.environ, {"ORS_API_KEY": "test_key"}):
            with patch(
                "services.routing_service.main.get_ors_client"
            ) as mock_get_client:
                mock_client = MagicMock()
                mock_client._is_enabled.return_value = True
                mock_get_client.return_value = mock_client

                response = client.get("/health")
                assert response.status_code == 200
                data = response.json()
                assert data["openrouteservice"] == "enabled"

    def test_health_with_ors_disabled(self):
        """Test health endpoint when OpenRouteService is disabled."""
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "services.routing_service.main.get_ors_client"
            ) as mock_get_client:
                mock_client = MagicMock()
                mock_client._is_enabled.return_value = False
                mock_get_client.return_value = mock_client

                response = client.get("/health")
                assert response.status_code == 200
                data = response.json()
                assert data["openrouteservice"] == "disabled"
