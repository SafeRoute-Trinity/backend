"""
OpenRouteService API client for SafeRoute backend.
Handles communication with OpenRouteService API and converts responses to Mapbox-compatible format.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from httpx import AsyncClient, Timeout

logger = logging.getLogger(__name__)

# OpenRouteService API base URL
ORS_BASE_URL = "https://api.openrouteservice.org"


class OpenRouteServiceClient:
    """Client for interacting with OpenRouteService API."""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize OpenRouteService client.

        Args:
            api_key: OpenRouteService API key. If None, reads from ORS_API_KEY env var.
        """
        self.api_key = api_key or os.getenv("ORS_API_KEY")
        if not self.api_key:
            logger.warning(
                "ORS_API_KEY not set. OpenRouteService features will be disabled."
            )

        # Create HTTP client with timeout
        self.client = AsyncClient(
            base_url=ORS_BASE_URL,
            timeout=Timeout(30.0),  # 30 second timeout
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    def _is_enabled(self) -> bool:
        """Check if OpenRouteService is enabled (has API key)."""
        return self.api_key is not None

    async def get_directions(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        profile: str = "driving-car",
        format: str = "geojson",
    ) -> Optional[Dict[str, Any]]:
        """
        Get directions from OpenRouteService.

        Args:
            start: Start coordinates as (lat, lon)
            end: End coordinates as (lat, lon)
            profile: Routing profile (e.g., "driving-car", "foot-walking", "cycling-regular")
            format: Response format ("geojson" or "json")

        Returns:
            OpenRouteService response as dict, or None if error
        """
        if not self._is_enabled():
            logger.error("OpenRouteService is not enabled (missing API key)")
            return None

        try:
            # OpenRouteService expects coordinates as [lon, lat]
            coordinates = [[start[1], start[0]], [end[1], end[0]]]

            url = f"/v2/directions/{profile}"
            # OpenRouteService directions API uses POST with JSON body
            body = {"coordinates": coordinates, "format": format}

            logger.info(
                f"Requesting directions from OpenRouteService: profile={profile}, "
                f"start=({start[0]}, {start[1]}), end=({end[0]}, {end[1]})"
            )

            response = await self.client.post(url, json=body)
            response.raise_for_status()

            data = response.json()
            logger.info(
                f"Successfully received directions from OpenRouteService: "
                f"routes={len(data.get('features', []))}"
            )

            return data

        except httpx.HTTPStatusError as e:
            logger.error(
                f"OpenRouteService API error: {e.response.status_code} - {e.response.text}"
            )
            return None
        except httpx.RequestError as e:
            logger.error(f"OpenRouteService request error: {e}")
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error calling OpenRouteService: {e}", exc_info=True
            )
            return None

    async def get_isochrones(
        self,
        location: tuple[float, float],
        profile: str = "driving-car",
        range: List[int] = [600, 1200, 1800],  # seconds
        range_type: str = "time",
    ) -> Optional[Dict[str, Any]]:
        """
        Get isochrones from OpenRouteService.

        Args:
            location: Location coordinates as (lat, lon)
            profile: Routing profile (e.g., "driving-car", "foot-walking", "cycling-regular")
            range: List of ranges in seconds (for time) or meters (for distance)
            range_type: "time" or "distance"

        Returns:
            OpenRouteService response as dict, or None if error
        """
        if not self._is_enabled():
            logger.error("OpenRouteService is not enabled (missing API key)")
            return None

        try:
            # OpenRouteService expects coordinates as [lon, lat]
            coordinates = [[location[1], location[0]]]

            url = f"/v2/isochrones/{profile}"
            # OpenRouteService isochrones API uses POST with JSON body
            body = {
                "locations": coordinates,
                "range": range,
                "range_type": range_type,
            }

            logger.info(
                f"Requesting isochrones from OpenRouteService: profile={profile}, "
                f"location=({location[0]}, {location[1]}), range={range}, "
                f"range_type={range_type}"
            )

            response = await self.client.post(url, json=body)
            response.raise_for_status()

            data = response.json()
            logger.info(
                f"Successfully received isochrones from OpenRouteService: "
                f"features={len(data.get('features', []))}"
            )

            return data

        except httpx.HTTPStatusError as e:
            logger.error(
                f"OpenRouteService API error: {e.response.status_code} - {e.response.text}"
            )
            return None
        except httpx.RequestError as e:
            logger.error(f"OpenRouteService request error: {e}")
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error calling OpenRouteService: {e}", exc_info=True
            )
            return None


# Global client instance
_ors_client: Optional[OpenRouteServiceClient] = None


def get_ors_client() -> OpenRouteServiceClient:
    """Get OpenRouteService client instance (singleton)."""
    global _ors_client
    if _ors_client is None:
        _ors_client = OpenRouteServiceClient()
    return _ors_client
