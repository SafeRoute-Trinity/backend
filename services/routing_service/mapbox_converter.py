"""
Convert OpenRouteService responses to Mapbox-compatible GeoJSON format.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def convert_ors_route_to_mapbox(
    ors_response: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Convert OpenRouteService directions response to Mapbox-compatible GeoJSON.

    Args:
        ors_response: OpenRouteService API response

    Returns:
        Mapbox-compatible GeoJSON FeatureCollection with LineString features
    """
    try:
        if "features" not in ors_response:
            logger.error("Invalid OpenRouteService response: missing 'features'")
            return None

        features = []
        for idx, feature in enumerate(ors_response["features"]):
            if feature.get("geometry", {}).get("type") != "LineString":
                continue

            # Extract route properties
            properties = feature.get("properties", {})
            segments = (
                properties.get("segments", [{}])[0]
                if properties.get("segments")
                else {}
            )
            summary = properties.get("summary", {})

            # Create Mapbox-compatible feature
            mapbox_feature = {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "route_index": idx,
                    "is_primary": idx == 0,
                    "distance_m": int(summary.get("distance", 0)),
                    "duration_s": int(summary.get("duration", 0)),
                    # Additional properties for Mapbox
                    "distance": summary.get("distance", 0) / 1000,  # Convert to km
                    "duration": summary.get("duration", 0) / 60,  # Convert to minutes
                },
            }

            features.append(mapbox_feature)

        if not features:
            logger.warning(
                "No valid LineString features found in OpenRouteService response"
            )
            return None

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    except Exception as e:
        logger.error(
            f"Error converting OpenRouteService route to Mapbox format: {e}",
            exc_info=True,
        )
        return None


def convert_ors_isochrone_to_mapbox(
    ors_response: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    Convert OpenRouteService isochrones response to Mapbox-compatible GeoJSON.

    Args:
        ors_response: OpenRouteService API response

    Returns:
        Mapbox-compatible GeoJSON FeatureCollection with Polygon features
    """
    try:
        if "features" not in ors_response:
            logger.error("Invalid OpenRouteService response: missing 'features'")
            return None

        features = []
        for feature in ors_response["features"]:
            if feature.get("geometry", {}).get("type") not in [
                "Polygon",
                "MultiPolygon",
            ]:
                continue

            # Extract isochrone properties
            properties = feature.get("properties", {})
            value = properties.get("value", 0)  # Time or distance value

            # Create Mapbox-compatible feature
            mapbox_feature = {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "value": value,
                    "range_type": properties.get("range_type", "time"),
                },
            }

            features.append(mapbox_feature)

        if not features:
            logger.warning(
                "No valid Polygon features found in OpenRouteService response"
            )
            return None

        return {
            "type": "FeatureCollection",
            "features": features,
        }

    except Exception as e:
        logger.error(
            f"Error converting OpenRouteService isochrone to Mapbox format: {e}",
            exc_info=True,
        )
        return None
